"""内容寻址的 blob 存储。

同一个 SHA-256 只落一份盘 —— 50 个选手交同一份模板代码时，磁盘占用是 1 份而不是
50 份。目录按哈希前两位分片，避免单目录塞进几十万文件（ext4 的目录项查找会退化）。

写入一律"先写临时文件 → 校验 → ``os.replace``"，保证任何时刻磁盘上都不存在
"半个文件冒充完整文件" 的状态。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

__all__ = ["BlobStore", "BlobTooLarge", "HashMismatch", "materialize"]

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


class BlobTooLarge(ValueError):
    """上传内容超过策略上限。"""


class HashMismatch(ValueError):
    """客户端声明的 sha256 与实际内容不符。"""


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.tmp_root = self.root / ".tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- #
    # 路径
    # ---------------------------------------------------------------- #

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64:
            raise ValueError("sha256 长度不合法")
        return self.root / sha256[:2] / sha256

    def has(self, sha256: str) -> bool:
        try:
            return self.path_for(sha256).is_file()
        except ValueError:
            return False

    def open(self, sha256: str) -> BinaryIO:
        return self.path_for(sha256).open("rb")

    # ---------------------------------------------------------------- #
    # 写入
    # ---------------------------------------------------------------- #

    def put_stream(
        self,
        stream: BinaryIO,
        max_bytes: int,
        expected_sha256: Optional[str] = None,
    ) -> Tuple[str, int]:
        """从 ``stream`` 读取并落盘，返回 ``(sha256, size)``。

        边读边算哈希，因此不需要把文件读进内存，也不需要二次读盘。
        """
        tmp_path = self.tmp_root / ("%s.part" % uuid.uuid4().hex)
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp_path.open("wb") as out:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise BlobTooLarge("内容超过上限 %d 字节" % max_bytes)
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())

            actual = digest.hexdigest()
            if expected_sha256 and actual != expected_sha256:
                raise HashMismatch(
                    "sha256 不符：客户端声明 %s，实际 %s" % (expected_sha256, actual)
                )
            self._commit(tmp_path, actual)
            return actual, size
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def put_bytes(self, data: bytes, expected_sha256: Optional[str] = None) -> Tuple[str, int]:
        import io

        return self.put_stream(io.BytesIO(data), max_bytes=len(data), expected_sha256=expected_sha256)

    def _commit(self, tmp_path: Path, sha256: str) -> None:
        """把临时文件搬到最终位置。已存在则丢弃临时文件（去重）。"""
        final = self.path_for(sha256)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            tmp_path.unlink(missing_ok=True)
            return
        try:
            os.replace(tmp_path, final)
        except OSError:
            # 并发写入同一 sha256 时，先到的会赢，后到的 replace 可能失败
            if final.exists():
                tmp_path.unlink(missing_ok=True)
                return
            raise

    def sweep_tmp(self, max_age_seconds: int = 86400) -> int:
        """清理崩溃残留的临时文件。返回清理数量。"""
        import time

        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in self.tmp_root.glob("*.part"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:  # pragma: no cover
                continue
        return removed


def materialize(blob: Path, dest: Path) -> None:
    """把 blob 呈现到 ``dest``。

    优先硬链接（同分区零拷贝、零额外磁盘占用）。跨分区或文件系统不支持时
    回落到拷贝。两种情况下 ``dest`` 都是普通只读文件，评测器无需感知区别。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            if os.path.samefile(blob, dest):
                return
        except OSError:
            pass
        dest.unlink()

    try:
        os.link(blob, dest)
        return
    except OSError:
        pass

    tmp = dest.with_name(dest.name + ".syncoj-tmp")
    try:
        shutil.copyfile(blob, tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
