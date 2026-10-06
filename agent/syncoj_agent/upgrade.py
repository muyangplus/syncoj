"""Agent 自更新：下载 → 验签 → 解包 → 激活。

这份代码的风险等级是全项目最高的 —— 它会以 root/专用账号身份，在 50 台考试机
上替换正在运行的程序。因此设计了四道闸：

1. **默认关闭**（``upgrade.mode = off``）。即使签名完全合法也不动 —— 静默地
   在考试机上升级是高风险动作，必须由教师显式开启。
2. **签名 + 摘要双重校验**。先验 SHA-256 再验签名，任一不过立即丢弃。
3. **版本号管降级、sha 管内容**。低于当前版本的一律拒绝 —— 否则攻击者可以重放
   一个历史版本的真实签名包，把 Agent 退回已知有漏洞的状态。**同版本号**则比
   内容指纹（``releases/<版本>/.syncoj-bundle-sha256``，与安装器 ``install.py``
   用同一套记录）：指纹不同 → 当成"同版本重建"照常 stage/apply（同名版本重打的
   包也能到达机器）；指纹相同 → 真无操作（幂等，不反复下载/解包）。

   接受的一面：**同一版本号内的内容回滚从此理论可行** —— 有人重放一份旧的、
   签过名的同版本 manifest，会被判定为"内容不同"而照单接收。跨版本号的降级闸
   仍然在（``parse_version`` 比较），而"同版本号内的内容回退"事前无法与"同版本
   重建"区分，所以只能接受这个代价（好在这一步仍要过签名）。
   另一面：同版本内容被换掉之后**没有"上一份"可退** —— 所以 apply 时不会把
   同一个版本号记成回滚点（``previous`` 留空），新内容起不来时启动守卫只能清掉
   状态、不做假回滚。想留退路，正解是换版本号（版本单调本来就是这套设计的假设）。
4. **安全解包**。``tarfile.extractall`` 在 Python 3.12 之前对路径穿越与符号
   链接逃逸**没有任何防护**（3.8 上更没有 ``filter="data"``）。这里逐条检查
   每个成员，拒绝绝对路径、``..``、符号链接、设备节点，并限制解压后总大小。

激活采用"版本目录 + 软链原子切换"，保留上一版本以便回滚。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .rsa import RSAPublicKey, SignatureError, verify_pkcs1v15_sha256

__all__ = [
    "UpgradeError",
    "ReleaseManifest",
    "UpgradeMode",
    "parse_version",
    "verify_bundle",
    "safe_extract_tar",
    "stage_release",
    "activate_release",
    "rollback_release",
    "current_release",
    "read_release_fingerprint",
    "list_releases",
    "prune_releases",
    "symlinks_supported",
    "UpgradeState",
    "load_state",
    "save_state",
    "clear_state",
    "note_boot",
    "mark_healthy",
    "BUNDLE_SHA_MARKER_FILENAME",
    "MAX_BOOT_ATTEMPTS",
    "MAX_BUNDLE_BYTES",
    "MAX_EXTRACTED_BYTES",
]

log = logging.getLogger(__name__)

#: 发布包大小上限
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
#: 解压后总大小上限（防 zip bomb）
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
#: 单个成员数量上限
MAX_MEMBERS = 20000

CURRENT_LINK = "current"
RELEASES_DIR = "releases"

#: 版本目录里记录"这一份内容指纹"的标记文件。
#:
#: **必须与安装器 ``install.py`` 用同一个名字与格式**：装机（``--from-server`` /
#: ``--bundle``）与自更新（stage）都会往 ``releases/<版本>/`` 里放这个记录、
#: 也都会读它来判断"这一份内容换没换"。名字或格式不一致，两边就会把对方的内容
#: 当成"变了"，同一份包被反复覆盖，幂等性当场失效。
#:
#: 放在版本目录**内部**：删版本目录 / 卸载 / 回滚时它自然一起消失。
BUNDLE_SHA_MARKER_FILENAME = ".syncoj-bundle-sha256"


class UpgradeError(Exception):
    """升级过程中的任何失败。调用方一律降级为"记日志 + 上报"，不重启、不中断服务。"""


class UpgradeMode:
    #: 只报告有新版本，不下载（默认）
    OFF = "off"
    #: 下载、验签、解包到独立目录，但不激活
    STAGE = "stage"
    #: 完整执行：解包后切换软链并重启
    APPLY = "apply"

    ALL = (OFF, STAGE, APPLY)


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #


def parse_version(text: str) -> Tuple[int, ...]:
    """把 ``"1.2.3"`` 解析成 ``(1, 2, 3)``。

    非数字部分（``1.2.3-rc1``）截断到最后一个纯数字段之前。解析不出来就抛出，
    让调用方拒绝这次升级 —— 不认识版本号时**宁可不动**。
    """
    if not isinstance(text, str):
        raise UpgradeError("版本号必须是字符串")
    cleaned = text.strip()
    if not cleaned:
        raise UpgradeError("版本号为空")

    parts: List[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))

    if not parts:
        raise UpgradeError("无法解析版本号: %r" % text)
    return tuple(parts)


# --------------------------------------------------------------------------- #
# 发布清单
# --------------------------------------------------------------------------- #


@dataclass
class ReleaseManifest:
    version: str
    url: str
    sha256: str
    size: int = 0
    signature: str = ""  # base64url
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "ReleaseManifest":
        if not isinstance(data, dict):
            raise UpgradeError("发布清必须是 JSON 对象")
        try:
            version = str(data["version"])
            url = str(data["url"])
            sha256 = str(data["sha256"])
            signature = str(data.get("signature", ""))
        except KeyError as exc:
            raise UpgradeError("发布清单缺少字段: %s" % exc)

        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
            raise UpgradeError("sha256 格式不合法")

        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size < 0:
            raise UpgradeError("size 不能为负")
        if size > MAX_BUNDLE_BYTES:
            raise UpgradeError("发布包声明大小 %d 超过上限" % size)

        return cls(
            version=version,
            url=url,
            sha256=sha256.lower(),
            size=size,
            signature=signature,
            notes=str(data.get("notes") or "")[:500],
        )

    def signature_bytes(self) -> bytes:
        import base64
        import binascii

        cleaned = self.signature.strip().replace("+", "-").replace("/", "_")
        padding = "=" * (-len(cleaned) % 4)
        try:
            return base64.urlsafe_b64decode(cleaned + padding)
        except (binascii.Error, ValueError) as exc:
            raise UpgradeError("签名不是合法的 base64: %s" % exc)


def _sha256_file(path: Path) -> str:
    """流式计算文件 sha256（256MB 的包也只占 1MB 缓冲区）。"""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(public_key: RSAPublicKey, bundle_path: Path, manifest: ReleaseManifest) -> None:
    """校验摘要与签名。不通过就抛 :class:`UpgradeError`。

    **签名对象是 sha256 的十六进制字符串，而不是文件的原始字节。**
    这样验证分两步且都不需要把包读进内存：

    1. 流式计算 sha256 并与清单比对（256MB 的包也只占 1MB 缓冲区）
    2. 对那 64 个 ASCII 字符验签

    安全性与"对整个文件签名"等价：攻击者要伪造仍是找 sha256 碰撞，而
    PKCS#1 内部本来也是"先哈希再签名"。换来的是内存占用与文件大小解耦 ——
    否则 Agent 的 200MB 内存预算会被一个 256MB 的包直接撑爆。

    先摘要后签名：摘要便宜且能挡住绝大多数传输损坏，签名昂贵。
    """
    if not public_key:
        raise UpgradeError("未配置发布公钥，拒绝任何升级")

    try:
        size = bundle_path.stat().st_size
    except OSError as exc:
        raise UpgradeError("无法读取发布包: %s" % exc)

    if size == 0:
        raise UpgradeError("发布包为空")
    if size > MAX_BUNDLE_BYTES:
        raise UpgradeError("发布包 %d 字节超过上限" % size)
    if manifest.size and size != manifest.size:
        raise UpgradeError("发布包大小不符：清单 %d，实际 %d" % (manifest.size, size))

    try:
        actual = _sha256_file(bundle_path)
    except OSError as exc:
        raise UpgradeError("读取发布包失败: %s" % exc)

    if actual != manifest.sha256:
        raise UpgradeError("发布包 sha256 不符：期望 %s，实际 %s"
                           % (manifest.sha256[:12], actual[:12]))

    if not manifest.signature:
        raise UpgradeError("发布清单没有签名")

    if not verify_pkcs1v15_sha256(
        public_key, manifest.sha256.encode("ascii"), manifest.signature_bytes()
    ):
        raise UpgradeError("发布包签名验证失败")


# --------------------------------------------------------------------------- #
# 安全解包
# --------------------------------------------------------------------------- #


def _check_member(member: tarfile.TarInfo, dest: Path) -> Path:
    """校验单个 tar 成员，返回其目标路径。"""
    name = member.name
    if not name:
        raise UpgradeError("tar 中存在无名成员")

    # Windows 风格的反斜杠在 Linux 上会被当成普通字符，但换个平台就成路径分隔符
    if "\\" in name:
        raise UpgradeError("tar 成员名含反斜杠: %r" % name)

    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        raise UpgradeError("tar 成员使用绝对路径: %r" % name)

    if member.isdev() or member.isfifo() or member.ischr() or member.isblk():
        raise UpgradeError("tar 成员是设备/管道节点，已拒绝: %r" % name)

    if member.issym() or member.islnk():
        # 发布包不需要符号链接或硬链接。一律拒绝而不是去校验链接目标 ——
        # "校验链接目标"正是历史上反复出错的写法规避点。
        raise UpgradeError("tar 成员是链接，已拒绝: %r" % name)

    target = (dest / name).resolve()
    dest_resolved = dest.resolve()
    try:
        common = os.path.commonpath([str(target), str(dest_resolved)])
    except ValueError:
        raise UpgradeError("tar 成员路径逃逸: %r" % name)
    if common != str(dest_resolved):
        raise UpgradeError("tar 成员路径逃逸出解压目录: %r" % name)

    return target


def safe_extract_tar(bundle_path: Path, dest: Path) -> int:
    """把 tar 包安全地解压到 ``dest``，返回解压出的文件数。

    不调用 ``extractall`` —— 它在 Python 3.12 之前（以及 3.8 上的所有版本）
    对路径穿越与链接逃逸没有任何防护。
    """
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    count = 0

    try:
        archive = tarfile.open(bundle_path, mode="r:*")
    except (tarfile.TarError, OSError) as exc:
        raise UpgradeError("无法打开发布包（不是有效的 tar）: %s" % exc)

    with archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise UpgradeError("tar 成员数 %d 超过上限 %d" % (len(members), MAX_MEMBERS))

        for member in members:
            target = _check_member(member, dest)

            total += max(0, member.size)
            if total > MAX_EXTRACTED_BYTES:
                raise UpgradeError("解压后总大小超过上限 %d 字节" % MAX_EXTRACTED_BYTES)

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                raise UpgradeError("不支持的 tar 成员类型: %r" % member.name)

            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover - 理论上不可达
                raise UpgradeError("无法读取 tar 成员: %r" % member.name)
            with source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)

            # 保留可执行位。tarfile 的 mode 里高 3 位是 SUID/SGID/Sticky，
            # 一律剥掉 —— 升级包没有理由携带 SUID 程序
            try:
                os.chmod(target, (member.mode & 0o777) or 0o644)
            except OSError:
                pass
            count += 1

    return count


# --------------------------------------------------------------------------- #
# 安装布局
# --------------------------------------------------------------------------- #
#
#   <install_root>/
#   ├── releases/<version>/...    每个版本一份，互不覆盖
#   └── current -> releases/<版本>  原子切换的软链，systemd 跑的就是它


def current_release(install_root: Path) -> Optional[str]:
    """返回当前激活的版本号，没有则 None。"""
    link = Path(install_root) / CURRENT_LINK
    try:
        if not link.is_symlink():
            return None
        return os.readlink(str(link)).rstrip("/").split("/")[-1]
    except OSError:
        return None


def _release_dir(install_root: Path, version: str) -> Path:
    return Path(install_root) / RELEASES_DIR / version


def _read_fingerprint(release_dir: Path) -> Optional[str]:
    """读版本目录里的内容指纹；没有记录 / 读不出来返回 ``None``。

    ``None`` 表示"这份内容的来源未知"（历史安装，那时还没有这个记录），调用方
    按"可能与清单不同"处理 —— 也就是允许覆盖一次，之后就有记录了。
    """
    marker = release_dir / BUNDLE_SHA_MARKER_FILENAME
    try:
        text = marker.read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _write_fingerprint(staging: Path, fingerprint: str) -> None:
    """把内容指纹写进**待上位的 staging**，随目录一起换上去。"""
    marker = staging / BUNDLE_SHA_MARKER_FILENAME
    # 3.8 上 write_text 没有 newline 参数，用 open
    with marker.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(fingerprint + "\n")


def read_release_fingerprint(install_root: Path, version: str) -> Optional[str]:
    """本机 ``releases/<version>/`` 里记着的那一份内容指纹；没有记录返回 ``None``。

    与安装器 ``install.py`` 读的是**同一个文件、同一种格式**（发布包的 sha256），
    所以"装机装上的那份"与"自更新暂存的那份"能互相认出来。
    """
    return _read_fingerprint(_release_dir(install_root, version))


def _replace_version_dir(staging: Path, final: Path) -> None:
    """把 ``staging`` 原子换到**已存在**的 ``final``（同版本号重建）。

    老目录先 rename 成 ``.old-<pid>``（同在 releases/ 下，rename 是原子的），
    再把 staging rename 成版本目录，成功后才删老目录；第二步失败就把老目录搬
    回去。**这里不停服务**：正在跑的就是 Agent 自己，停自己不在它的权限面里；
    真正的切换由后面的 ``activate_release`` + systemd ``Restart=always`` 完成。
    替换本身是原子的，不存在"半新半旧"的目录被读到。

    注意：老内容在这里就被删掉了，所以同版本内容替换**没有**可回滚的上一份
    （``UpgradeState.previous`` 会是同一个版本号）—— 见模块 docstring 第 3 条。
    """
    parent = final.parent
    old = parent / (".old-" + str(os.getpid()))
    # 上一次崩在中间留下的 .old-* 先清掉：rename 到已存在的非空目录会失败
    shutil.rmtree(str(old), ignore_errors=True)

    os.replace(str(final), str(old))
    swapped = False
    try:
        os.replace(str(staging), str(final))
        swapped = True
    finally:
        if swapped:
            shutil.rmtree(str(old), ignore_errors=True)
        else:
            # 回滚：把老目录搬回原位，别让 current 指到一个不存在的路径
            try:
                os.replace(str(old), str(final))
            except OSError as exc:  # pragma: no cover - 罕见
                log.error(
                    "同版本替换失败且回滚也失败：老版本在 %s（%s）", old, exc
                )


def stage_release(install_root: Path, version: str, bundle_path: Path) -> Path:
    """把发布包解压到 ``releases/<version>/``。

    先解压到临时目录再整体改名，避免解压到一半失败留下一个"看起来存在但残缺"
    的版本目录 —— 那会让后续的激活判断出错。

    **同版本号也要按内容判断**（与安装器 ``install.py`` 同一套记录，见
    :data:`BUNDLE_SHA_MARKER_FILENAME`）：

    * 版本目录里记的指纹与这一份包相同 → 直接复用，不重解（幂等）；
    * 不同、或历史安装没有记录 → 换上新内容，别让"同版本重复暂存"把新内容吃掉。
    """
    install_root = Path(install_root)
    releases = install_root / RELEASES_DIR
    releases.mkdir(parents=True, exist_ok=True)

    try:
        parse_version(version)
    except UpgradeError as exc:
        raise UpgradeError("版本号不合法，拒绝落盘: %s" % exc)

    try:
        fingerprint = _sha256_file(Path(bundle_path))
    except OSError as exc:
        raise UpgradeError("读取发布包失败: %s" % exc)

    final = releases / version
    if final.is_dir() and _read_fingerprint(final) == fingerprint:
        # 同一份内容：直接复用，不重解（幂等）
        return final

    staging = releases / (".staging-" + hashlib.sha256(version.encode()).hexdigest()[:12])
    if staging.exists():
        shutil.rmtree(str(staging), ignore_errors=True)

    try:
        safe_extract_tar(bundle_path, staging)
        _write_fingerprint(staging, fingerprint)
        if final.is_dir():
            _replace_version_dir(staging, final)
        else:
            os.replace(str(staging), str(final))
    except BaseException:
        # 解包失败 / 替换失败都别把 .staging-* 留在 releases/ 里
        shutil.rmtree(str(staging), ignore_errors=True)
        raise

    return final


def symlinks_supported(directory: Optional[Path] = None) -> bool:
    """探测当前环境是否允许创建符号链接。

    Linux 上恒为 True。Windows 上需要开发者模式或管理员权限，否则
    ``os.symlink`` 抛 ``WinError 1314``（客户端没有所需的特权）。

    目标平台是 NOI Linux，符号链接当然可用；这个探测存在的意义是让
    **在 Windows 上开发时**能立刻得到明确提示，而不是等到激活阶段才撞墙。
    """
    import tempfile

    probe_dir = Path(directory) if directory else None
    created_dir = None
    try:
        if probe_dir is None:
            created_dir = Path(tempfile.mkdtemp(prefix="syncoj-symlink-probe-"))
            probe_dir = created_dir
        probe_dir.mkdir(parents=True, exist_ok=True)
        target = probe_dir / ".link-target"
        link = probe_dir / ".link-probe"
        target.write_text("x", encoding="utf-8")
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(str(target), str(link))
            return True
        finally:
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
            except OSError:
                pass
            try:
                target.unlink()
            except OSError:
                pass
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        if created_dir is not None:
            shutil.rmtree(created_dir, ignore_errors=True)


def activate_release(install_root: Path, version: str) -> Optional[str]:
    """把 ``current`` 指向指定版本，返回被替换掉的旧版本号（供回滚）。

    用"写临时软链再 rename"实现原子切换 —— ``os.symlink`` 到已存在的路径会失败，
    而先删后建之间有一个窗口期，此时 systemd 重启会找不到可执行文件。
    """
    install_root = Path(install_root)
    install_root.mkdir(parents=True, exist_ok=True)

    target = install_root / RELEASES_DIR / version
    if not target.is_dir():
        raise UpgradeError("版本目录不存在: %s" % target)

    if not symlinks_supported(install_root):
        raise UpgradeError(
            "当前环境不允许创建符号链接，无法激活版本。"
            "Linux 上请检查目录权限与文件系统；"
            "Windows 上需要开启开发者模式或以管理员身份运行。"
        )

    previous = current_release(install_root)

    link = install_root / CURRENT_LINK
    tmp_link = install_root / (CURRENT_LINK + ".tmp")

    try:
        if tmp_link.is_symlink() or tmp_link.exists():
            tmp_link.unlink()
        os.symlink(str(target), str(tmp_link))
        os.replace(str(tmp_link), str(link))
    except OSError as exc:
        try:
            tmp_link.unlink()
        except OSError:
            pass
        raise UpgradeError("切换软链失败: %s" % exc)

    return previous


def rollback_release(install_root: Path, version: str) -> None:
    """把 ``current`` 指回指定版本。"""
    activate_release(install_root, version)
    log.warning("已回滚到版本 %s", version)


def list_releases(install_root: Path) -> List[str]:
    releases = Path(install_root) / RELEASES_DIR
    if not releases.is_dir():
        return []
    result = []
    for entry in sorted(releases.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            result.append(entry.name)
    return result


def prune_releases(install_root: Path, keep: int = 3) -> List[str]:
    """清理旧版本：保留最近 ``keep`` 个**之外**，还有两个必须保住的。

    ``keep`` 约束的是"除必须保住的之外，再留几个最新的"，**不是"总数不超过
    keep"** —— 下面保护的版本哪怕比所有保留项都老，也一个都不能删。

    必须保住：

    * **正在运行的版本**（``current`` 指向的那个）—— 删了它下次重启就起不来；
    * **``UpgradeState.previous``**（真的存在时）—— 那是启动守卫的回滚点。
      回滚点被删 = **回滚能力被删**：新版本一崩，守卫切回去的目标已经没了，
      机器就卡死在起不来的版本上（50 台一起）。而整套回滚设计正是为这个场景
      存在的 —— 所以这里保护的不是"某个版本号"，而是**回滚能力本身**。

    返回被删掉的版本号列表。
    """
    releases = Path(install_root) / RELEASES_DIR
    items: List[Tuple[Tuple[int, ...], str]] = []
    for name in list_releases(install_root):
        try:
            items.append((parse_version(name), name))
        except UpgradeError:
            continue
    items.sort()

    protected = set()
    active = current_release(install_root)
    if active:
        protected.add(active)
    state = load_state(install_root)
    if state is not None and state.previous:
        # 回滚点：删它等于删掉"新版本起不来还能退回去"的能力
        protected.add(state.previous)

    candidates = [(key, name) for key, name in items if name not in protected]
    doomed = candidates[:-keep] if keep > 0 else candidates

    removed: List[str] = []
    for _key, name in doomed:
        shutil.rmtree(releases / name, ignore_errors=True)
        removed.append(name)
    return removed


def read_manifest_file(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise UpgradeError("发布清单读取失败: %s" % exc)
    if not isinstance(data, dict):
        raise UpgradeError("发布清单必须是 JSON 对象")
    return data


# --------------------------------------------------------------------------- #
# 启动守卫与自动回滚
# --------------------------------------------------------------------------- #
#
# 自更新最可怕的失败模式不是"升级包是恶意的"（签名挡住了），而是
# **新版本有 bug 起不来** —— 一次操作把 50 台考试机同时变成砖。签名对这种情况
# 毫无帮助，必须有回滚。
#
# 机制：激活新版本时写一个状态文件记录"这次换了什么、换之前是什么"。
# 每次启动给计数器 +1；跑够若干个成功周期后清掉状态文件。
# 若计数超过阈值说明新版本反复起不来，就自动切回上一版本。


@dataclass
class UpgradeState:
    version: str
    previous: Optional[str]
    boots: int = 0
    applied_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "previous": self.previous,
            "boots": self.boots,
            "applied_at": self.applied_at,
        }


STATE_FILE = "upgrade-state.json"
#: 同一版本连续启动失败多少次就回滚
MAX_BOOT_ATTEMPTS = 2


def _state_path(install_root: Path) -> Path:
    return Path(install_root) / STATE_FILE


def load_state(install_root: Path) -> Optional[UpgradeState]:
    path = _state_path(install_root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "version" not in data:
        return None
    try:
        return UpgradeState(
            version=str(data["version"]),
            previous=(str(data["previous"]) if data.get("previous") else None),
            boots=int(data.get("boots") or 0),
            applied_at=float(data.get("applied_at") or 0.0),
        )
    except (TypeError, ValueError):
        return None


def save_state(install_root: Path, state: UpgradeState) -> None:
    path = _state_path(install_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp), str(path))


def clear_state(install_root: Path) -> None:
    try:
        _state_path(install_root).unlink()
    except OSError:
        pass


def note_boot(install_root: Path, running_version: str) -> Optional[str]:
    """每次启动调用一次。

    返回**应当回滚到的版本号**；返回 ``None`` 表示正常（无需回滚，
    或状态文件不属于当前版本）。
    """
    state = load_state(install_root)
    if state is None:
        return None

    if state.version != running_version:
        # 状态文件记的不是当前运行的版本 —— 可能是手工换过版本，
        # 状态已失效，直接清掉而不是拿它做判断
        log.info(
            "升级状态记录的是 %s，当前运行 %s，状态已失效，清除",
            state.version, running_version,
        )
        clear_state(install_root)
        return None

    state.boots += 1
    if state.boots > MAX_BOOT_ATTEMPTS:
        if not state.previous:
            log.error("版本 %s 反复启动失败，但没有可回滚的上一版本", running_version)
            clear_state(install_root)
            return None
        log.error(
            "版本 %s 已连续启动 %d 次仍未标记成功，自动回滚到 %s",
            running_version, state.boots, state.previous,
        )
        return state.previous

    save_state(install_root, state)
    return None


def mark_healthy(install_root: Path) -> None:
    """Agent 跑够若干周期后调用：确认新版本可用，清掉回滚状态。"""
    if load_state(install_root) is not None:
        clear_state(install_root)
        log.info("新版本已验证可用，清除回滚状态")
