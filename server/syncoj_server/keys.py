"""密钥放在哪、以及怎么把它安全地写下来。

**这里是"私钥在哪"的唯一定义。** 在它之前，同一件事散在三个地方：``Settings``
里的环境变量、``genkey --out`` 的默认值、以及文档里让人手工往
``/etc/syncoj/release-key.pub.json`` 拷公钥的那句话。三处各自演进的结果是没人
说得清当前这台机器到底信任哪把公钥 —— 而自更新失败时，最先要回答的就是这个
问题，偏偏那时最没有线索。

## 读：解析顺序

1. ``SYNCOJ_KEY_DIR`` —— 设了就听它的
2. ``<仓库根>/.key`` —— **只有它已经存在**时才用
3. 都没有 → ``None``，也就是"没有隐式密钥"：服务端不提供升级、Agent 拒绝未签名包，
   与加入 ``.key/`` 之前完全一样

第 2 步刻意要求"已经存在"，而不是"路径算得出来"。pip 把包装进 site-packages 时
``<仓库根>`` 是没有意义的，硬凑一个路径只会在某个意想不到的地方**创建**出一个
密钥目录 —— 而一个"看起来有密钥"的目录比没有密钥更危险。所以这里宁可退化成
``None``。

## 写：``init`` 往哪放

``key_dir_for_write()`` 与上面的区别只有一处：``<仓库根>/.key`` **不存在也算数**
（``init`` 的职责就是把它建出来）。仍然要求能确认"这是一个源码仓库"，否则返回
``None``，``init`` 会明说"没有可用密钥目录"并跳过 —— 绝不在生产布局里瞎猜一个
位置。

## 权限

私钥 0600、目录 0700。先按 0600 创建再写内容，而不是写完再 chmod：后者有一瞬间
是宽权限，而那一瞬间足够一个正在跑的选手进程把"能注册整间机房"的密钥读走。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

__all__ = [
    "BOOTSTRAP_KEY_NAME",
    "KEY_DIR_ENV",
    "RELEASE_PUBLIC_KEY_NAME",
    "RELEASE_SIGNING_KEY_NAME",
    "ensure_key_dir",
    "key_dir",
    "key_dir_for_write",
    "release_public_key_path",
    "release_signing_key_path",
    "repo_root",
    "write_secret_text",
]

#: 覆盖密钥目录的环境变量。设了它就完全按它走，不再猜仓库根。
KEY_DIR_ENV = "SYNCOJ_KEY_DIR"

#: ``.key/`` 下的文件名。三个都要一致地引用，不要再在别处写字面量。
RELEASE_SIGNING_KEY_NAME = "release-key.pem"
RELEASE_PUBLIC_KEY_NAME = "release-key.pub.json"
BOOTSTRAP_KEY_NAME = "bootstrap.key"


def repo_root() -> Optional[Path]:
    """源码仓库根目录；不在仓库布局里就返回 ``None``。

    判据不是"路径算不算得出来"（``parents[2]`` 永远算得出来），而是
    **这个位置真的长得像一个仓库**：``<root>/server/syncoj_server`` 存在。
    editable 安装成立，site-packages 不成立。
    """
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "server" / "syncoj_server").is_dir():
        return candidate
    return None


def key_dir() -> Optional[Path]:
    """**读**密钥时用的目录；没有隐式密钥目录就是 ``None``。

    环境变量优先且不检查存在性 —— 操作员明确说了密钥在哪，就不该由我们来
    否决他；文件是否真的在，交给下面按文件名去判断。
    """
    raw = os.environ.get(KEY_DIR_ENV)
    if raw:
        return Path(raw).expanduser()
    root = repo_root()
    if root is None:
        return None
    candidate = root / ".key"
    return candidate if candidate.is_dir() else None


def key_dir_for_write() -> Optional[Path]:
    """``init`` 该往哪写密钥；``None`` 表示不要写（生产布局，没有线索）。

    与 :func:`key_dir` 的唯一差别：``<仓库根>/.key`` 还**没建出来**时也算数。
    """
    raw = os.environ.get(KEY_DIR_ENV)
    if raw:
        return Path(raw).expanduser()
    root = repo_root()
    return (root / ".key") if root is not None else None


def release_signing_key_path() -> Optional[Path]:
    """服务端该加载的发布签名私钥；没有就是 ``None``。

    ``SYNCOJ_RELEASE_KEY`` 仍然最优先 —— 生产上密钥常在
    ``/etc/syncoj/release-key.pem``，那是仓库布局之外的路径。

    退回到 ``<key_dir>/release-key.pem`` 时**要求文件真的存在**：存在才加载，
    不存在就当没有密钥（服务端因此不提供任何升级）。这条是安全默认值的核心 ——
    "配了个路径但文件不在"如果也算启用，那启用就成了猜测。
    """
    raw = os.environ.get("SYNCOJ_RELEASE_KEY")
    if raw:
        return Path(raw).expanduser()
    where = key_dir()
    if where is None:
        return None
    candidate = where / RELEASE_SIGNING_KEY_NAME
    return candidate if candidate.is_file() else None


def release_public_key_path() -> Optional[Path]:
    """随包带走的发布**公钥**（``<密钥目录>/release-key.pub.json``）；没有就是 ``None``。

    与 :func:`release_signing_key_path` 有一处刻意的不对称：**没有单独的环境变量
    能指向它**。私钥在生产上常在仓库布局之外（``/etc/syncoj/release-key.pem``），
    所以它需要 ``SYNCOJ_RELEASE_KEY`` 这条逃生通道；公钥则是要被内嵌进升级包、
    跟着包走到每一台目标机上的信任锚，它只能是"密钥目录里那一个文件"。给它再加
    一个覆盖入口，只会多出"私钥是 A、公钥是 B"这种谁也没法从现场看出来的错配。

    只在文件真的存在时才返回路径 —— "没有公钥"是**正常**状态（自更新整体关闭），
    不是错误；而给一个不存在的路径会让构建脚本当场失败。
    """
    where = key_dir()
    if where is None:
        return None
    candidate = where / RELEASE_PUBLIC_KEY_NAME
    return candidate if candidate.is_file() else None


def ensure_key_dir(where: Path) -> Path:
    """建出密钥目录并尽力收紧到 0700。

    ``chmod`` 失败不算错误：Windows 上没有 POSIX 权限位，而"目录建好了"才是
    调用方真正需要的那件事。硬失败会让这个工具在开发机上完全不能用。
    """
    where = Path(where)
    where.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(where), 0o700)
    except OSError:
        pass
    return where


def write_secret_text(path: Path, text: str) -> None:
    """写一个只有属主可读的文件（0600），并且**显式 LF**。

    ``newline="\\n"`` 不是可选的：Windows 上 ``write_text`` 会把 ``\\n`` 翻成
    ``\\r\\n``，于是同一个密钥文件在开发机和服务器上字节不同 —— 而密钥是要被
    人抄、被脚本比对的，字节不同会让人怀疑"是不是换了密钥"。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先以 0600 创建再写：反过来的话，从创建到 chmod 之间有一瞬间是宽权限
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(handle, text.encode("utf-8"))
    finally:
        os.close(handle)
    # 文件**已存在**时 O_CREAT 不会改权限，所以再钉一次
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
