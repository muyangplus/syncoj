"""安全地读导入包：所有格式都经过这里，不直接碰 ``zipfile``。

限制（打开时一次查完，任何一条不过就整包拒绝，抛 :class:`ProblemIoError`）：

* 成员名：``testdata.safe_member``，另拒 NUL 与盘符（``C:``）
* 成员数 ≤ :data:`MAX_MEMBERS`
* 按 ``file_size`` 累计的解压总量 ≤ :data:`MAX_UNCOMPRESSED_BYTES`
* 单成员压缩比 > 1000 且解压后 > 64 MiB 视为炸弹
* ``read(name, limit)`` 按实际读到的字节截断，不信任 ``file_size``；描述文件（json /
  yaml / cdf / xml）用 :data:`MAX_DESCRIPTOR_BYTES`

``__MACOSX/``、``.DS_Store`` 与以 ``.`` 开头的文件从 :attr:`SafeArchive.names` 里隐去
（仍要过安全检查）。:class:`ArchiveView` 是"以某个目录为根"的视图，用来剥外壳目录。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import IO, Dict, List, Optional, Sequence, Union

from ...judging.testdata import MAX_UNCOMPRESSED_BYTES, safe_member
from .notices import ProblemIoError

__all__ = [
    "SafeArchive",
    "SingleFileArchive",
    "ArchiveView",
    "MAX_MEMBERS",
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_DESCRIPTOR_BYTES",
    "BOMB_RATIO",
    "BOMB_MIN_BYTES",
    "is_ignored",
    "open_upload",
    "read_head",
    "human_size",
]

MAX_MEMBERS = 20000
MAX_DESCRIPTOR_BYTES = 16 * 1024 * 1024
BOMB_RATIO = 1000
BOMB_MIN_BYTES = 64 * 1024 * 1024


def human_size(size: int) -> str:
    if size >= 1 << 30:
        return "%d GB" % (size >> 30)
    return "%d MB" % (size >> 20)


def is_ignored(name: str) -> bool:
    """``__MACOSX/`` 下的、``.DS_Store``、以 ``.`` 开头的文件或目录 —— 当成不存在。"""
    parts = name.split("/")
    return any(part == "__MACOSX" or part.startswith(".") for part in parts if part)


def _check_name(name: str) -> None:
    if (
        not name
        or "\x00" in name
        or not safe_member(name)
        or (len(name) >= 2 and name[1] == ":")
    ):
        raise ProblemIoError("unsafe_path", path=name.replace("\x00", "\\0"))


class SafeArchive:
    """包一层 ``zipfile.ZipFile``。``names`` 是可见文件（完整路径，已排序）。"""

    def __init__(
        self,
        source: Union[str, Path, IO[bytes]],
        max_members: int = MAX_MEMBERS,
        max_total: int = MAX_UNCOMPRESSED_BYTES,
    ) -> None:
        try:
            self._zip = zipfile.ZipFile(source if not isinstance(source, Path) else str(source))
        except (zipfile.BadZipFile, OSError, ValueError):
            raise ProblemIoError("bad_zip")
        try:
            self._infos = self._check(max_members, max_total)
        except Exception:
            self._zip.close()
            raise
        self.names: List[str] = sorted(n for n in self._infos if not is_ignored(n))
        self._dirs = _dir_set(self.names)

    def _check(self, max_members: int, max_total: int) -> Dict[str, zipfile.ZipInfo]:
        infos = self._zip.infolist()
        if len(infos) > max_members:
            raise ProblemIoError("too_many_members", limit=max_members)
        total = 0
        files: Dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            name = info.filename
            _check_name(name.rstrip("/") if info.is_dir() else name)
            if info.is_dir():
                continue
            total += info.file_size
            if total > max_total:
                raise ProblemIoError("archive_too_large", limit=human_size(max_total))
            if (
                info.file_size > BOMB_MIN_BYTES
                and info.file_size > BOMB_RATIO * max(info.compress_size, 1)
            ):
                raise ProblemIoError("zip_bomb", path=name)
            files[name] = info
        return files

    # ---- 读 ----

    def exists(self, name: str) -> bool:
        return name in self._infos

    def is_dir(self, name: str) -> bool:
        return name.rstrip("/") in self._dirs

    def size(self, name: str) -> int:
        return int(self._infos[name].file_size)

    def open(self, name: str) -> IO[bytes]:
        """流式读一个成员（大文件用这个）。不存在 → ``KeyError``。"""
        return self._zip.open(self._infos[name])

    def read(self, name: str, limit: int = MAX_DESCRIPTOR_BYTES) -> bytes:
        """读一个成员，超过 ``limit`` 字节 → ``member_too_large``。"""
        with self.open(name) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ProblemIoError("member_too_large", path=name, limit=human_size(limit))
        return data

    def view(self, root: str = "") -> "ArchiveView":
        return ArchiveView(self, root)

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "SafeArchive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SingleFileArchive:
    """非 zip 的上传（FPS 的 .xml）：当成只有一个文件的归档，接口与 :class:`SafeArchive` 相同。"""

    def __init__(self, path: Union[str, Path], name: str) -> None:
        self._path = Path(path)
        self._name = name
        self.names: List[str] = [name]
        self._dirs: set = set()

    def exists(self, name: str) -> bool:
        return name == self._name

    def is_dir(self, name: str) -> bool:
        return False

    def size(self, name: str) -> int:
        self._require(name)
        return self._path.stat().st_size

    def _require(self, name: str) -> None:
        if name != self._name:
            raise KeyError(name)

    def open(self, name: str) -> IO[bytes]:
        self._require(name)
        return open(str(self._path), "rb")

    def read(self, name: str, limit: int = MAX_DESCRIPTOR_BYTES) -> bytes:
        with self.open(name) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ProblemIoError("member_too_large", path=name, limit=human_size(limit))
        return data

    def view(self, root: str = "") -> "ArchiveView":
        return ArchiveView(self, root)

    def close(self) -> None:
        pass

    def __enter__(self) -> "SingleFileArchive":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _dir_set(names: Sequence[str]) -> set:
    dirs = set()
    for name in names:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


class ArchiveView:
    """以 ``root``（"" 或 "外壳目录/"）为根的视图。方法收**视图相对路径**；
    :meth:`full` 把它换成归档完整路径（写进 ``FileRef.member`` 的是完整路径）。"""

    def __init__(self, archive: Union[SafeArchive, SingleFileArchive], root: str = "") -> None:
        if root and not root.endswith("/"):
            root += "/"
        self.archive = archive
        self.root = root
        self.names: List[str] = [n[len(root):] for n in archive.names if n.startswith(root)]

    def full(self, name: str) -> str:
        return self.root + name

    def exists(self, name: str) -> bool:
        return self.archive.exists(self.full(name))

    def is_dir(self, name: str) -> bool:
        return self.archive.is_dir(self.full(name))

    def read(self, name: str, limit: int = MAX_DESCRIPTOR_BYTES) -> bytes:
        return self.archive.read(self.full(name), limit)

    def open(self, name: str) -> IO[bytes]:
        return self.archive.open(self.full(name))

    def root_files(self) -> List[str]:
        """根目录下的文件名。"""
        return [n for n in self.names if "/" not in n]

    def root_dirs(self) -> List[str]:
        """根目录下的子目录名（不带斜杠）。"""
        return sorted({n.split("/", 1)[0] for n in self.names if "/" in n})

    def listdir(self, folder: str) -> List[str]:
        """``folder`` 下（不递归）的文件名，相对 ``folder``。"""
        prefix = folder.rstrip("/") + "/" if folder else ""
        return [n[len(prefix):] for n in self.names if n.startswith(prefix) and "/" not in n[len(prefix):]]

    def walk(self, folder: str) -> List[str]:
        """``folder`` 下（递归）的文件，相对 ``folder``。"""
        prefix = folder.rstrip("/") + "/" if folder else ""
        return [n[len(prefix):] for n in self.names if n.startswith(prefix)]

    def shell_stripped(self) -> Optional["ArchiveView"]:
        """根目录只有一个目录、没有文件时，剥掉这层外壳的视图；否则 None。"""
        if self.root_files():
            return None
        dirs = self.root_dirs()
        if len(dirs) != 1:
            return None
        return ArchiveView(self.archive, self.root + dirs[0] + "/")


def open_upload(path: Union[str, Path], filename: str = "upload") -> Union[SafeArchive, SingleFileArchive]:
    """按首字节分流：zip → :class:`SafeArchive`；其余 → :class:`SingleFileArchive`（由 detect 再判）。"""
    with open(str(path), "rb") as handle:
        head = handle.read(4)
    if head.startswith(b"PK"):
        return SafeArchive(path)
    name = Path(filename or "upload").name or "upload"
    return SingleFileArchive(path, name)


def read_head(archive: Union[SafeArchive, SingleFileArchive], name: str, size: int = 4096) -> bytes:
    with archive.open(name) as handle:
        return handle.read(size)

