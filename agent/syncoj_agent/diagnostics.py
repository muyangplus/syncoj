"""诊断包：定期把现场回传给服务端（机器离线之后就抓不到了）。

为什么要它：机器一旦离线，管理端再也拿不到任何东西。所以要在**健康的时候**就把
现场留下 —— 出事时看到的至少是"最后一份"。

**三条硬约束**（都有测试盯着）：

1. **绝不带密钥与 token**。``config_summary`` 用**白名单**构造（只列已知安全的
   那几个键），**不是**"把配置文件整份塞进去再删几个"—— 后者迟早会漏。
   ``bootstrap.key`` 只留**路径**，``credential.json`` 的内容一个字都不进来。
2. **触发节奏**：健康时每 10 分钟一份；出错立即补一份（并有 60 秒最小间隔，
   别一次故障刷十份）；服务端可以在 tick 里下 ``diagnostics_request=true`` 要一份。
3. **上传失败绝不影响心跳**：它走独立的一次请求，失败只记 DEBUG（连续多次才
   WARNING），而且有自己的短超时（15 秒）。

报文形状（字段名是**冻结**的，服务端按它解析）::

    {
      "version", "generated_at", "reason", "state",
      "machine_id", "machine_uuid", "hostname", "run_user", "os_info",
      "tick": {"count", "last_error", "consecutive_failures", "next_tick_seconds"},
      "process": {"pid", "uptime_seconds", "rss_bytes", "threads"},
      "disk": {"free_bytes", "total_bytes"},
      "policy": {"scan_interval", "max_file_size", "max_files"},
      "config_summary": {"section.key": "值", ...},
      "log_tail": "..."
    }
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

#: 压缩**后**的上限（服务端按这个收）。压缩前不做限制，但日志尾部自己先截。
MAX_BUNDLE_BYTES = 256 * 1024

#: 日志尾部：取"最后 N 行"与"最后 M 字节"里**更小**的那个。
LOG_TAIL_MAX_LINES = 200
LOG_TAIL_MAX_BYTES = 64 * 1024

#: ``config_summary`` 的**白名单**（形如 ``<段>.<键>``）。
#:
#: 只有这些键会被看：全都是"路径、开关、节奏"这类不影响安全的东西。
#: ``agent.bootstrap_key_file`` 只留**路径**（内容绝不读、绝不打包）。
CONFIG_SUMMARY_KEYS = (
    "server.url",
    "agent.state_dir",
    "agent.deploy_root",
    "agent.bootstrap_key_file",
    "agent.run_user",
    "scan.roots",
    "scan.prefix",
    "scan.interval",
    "scan.max_file_size",
    "log.level",
    "log.file",
    "upgrade.mode",
    "upgrade.install_root",
    "upgrade.public_key",
)

#: 进程启动时刻（用来算 uptime）。放在 import 时取一次。
_PROCESS_STARTED = time.monotonic()


def read_log_tail(
    path: Optional[Path],
    max_lines: int = LOG_TAIL_MAX_LINES,
    max_bytes: int = LOG_TAIL_MAX_BYTES,
) -> str:
    """读日志尾部：**最后 max_lines 行**与**最后 max_bytes 字节**里更小的那个。

    只从文件尾部倒着读，不把整个 ``agent.log`` 读进内存（它可能已经几 MB）。
    读不到（文件还没建、权限不够）就返回空串 —— 诊断包能发出去比"日志完整"重要。
    """
    if path is None:
        return ""
    try:
        with open(str(path), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read(max_bytes)
    except OSError:
        return ""

    text = raw.decode("utf-8", "replace")
    if start > 0:
        # 从中间切进去的第一行多半是半截的，丢掉它（诊断包不该出现半行乱码）
        _, sep, rest = text.partition("\n")
        text = rest if sep else ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def _value_of(config, dotted: str) -> str:
    """从配置里取一个白名单键的值，统一成字符串。"""
    section, _, key = dotted.partition(".")
    attribute = {
        ("server", "url"): "server_url",
        ("agent", "state_dir"): "state_dir",
        ("agent", "deploy_root"): "deploy_root",
        ("agent", "bootstrap_key_file"): "bootstrap_key_file",
        ("agent", "run_user"): "run_user",
        ("scan", "roots"): "scan_roots",
        ("scan", "prefix"): "scan_prefix",
        ("scan", "interval"): "scan_interval",
        ("scan", "max_file_size"): "max_file_size",
        ("log", "level"): "log_level",
        ("log", "file"): "log_file",
        ("upgrade", "mode"): "upgrade_mode",
        ("upgrade", "install_root"): "install_root",
        ("upgrade", "public_key"): "release_public_key",
    }.get((section, key))
    value = getattr(config, attribute, "") if attribute else ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def config_summary(config) -> Dict[str, str]:
    """**按白名单**构造配置摘要。

    刻意不用"整份配置转字典再删几个键"：那种写法每加一个配置项都会**默认**被
    带出去，而这里面迟早会出现密钥类的东西（现在已经有 ``bootstrap_key_file``
    这种"路径安全、内容危险"的键）。白名单相反：新键默认**不出门**。
    """
    return {dotted: _value_of(config, dotted) for dotted in CONFIG_SUMMARY_KEYS}


def rss_bytes() -> Optional[int]:
    """常驻内存（字节）。Linux 上读 ``/proc/self/statm``；拿不到返回 ``None``。"""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError, AttributeError):
        return None


def disk_usage(path: Path) -> Tuple[Optional[int], Optional[int]]:
    """``(free_bytes, total_bytes)``；磁盘信息拿不到就 ``(None, None)``。"""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None, None
    return usage.free, usage.total


def process_stats(now: Optional[float] = None) -> Dict[str, object]:
    """进程自身的几个数。**不含命令行**（可能有敏感参数）。"""
    moment = time.monotonic() if now is None else now
    return {
        "pid": os.getpid(),
        "uptime_seconds": round(max(0.0, moment - _PROCESS_STARTED), 1),
        "rss_bytes": rss_bytes(),
        "threads": threading.active_count(),
    }


def build_bundle(
    *,
    version: str,
    reason: str,
    state: str,
    machine_id: str,
    machine_uuid: str,
    hostname: str,
    run_user: str,
    os_info: str,
    tick: Dict[str, object],
    policy: Dict[str, object],
    config,
    log_path: Optional[Path],
    now: Optional[float] = None,
    generated_at: Optional[int] = None,
) -> Dict[str, object]:
    """按冻结的字段名拼出诊断包。**只在这里拼**（别处再拼一份必然漂）。"""
    free, total = disk_usage(Path(getattr(config, "state_dir", ".")))
    policy_subset = {
        "scan_interval": policy.get("scan_interval"),
        "max_file_size": policy.get("max_file_size"),
        "max_files": policy.get("max_files"),
    }
    return {
        "version": version,
        "generated_at": int(time.time()) if generated_at is None else int(generated_at),
        "reason": reason,
        "state": state,
        "machine_id": machine_id,
        "machine_uuid": machine_uuid,
        "hostname": hostname,
        "run_user": run_user,
        "os_info": os_info,
        "tick": {
            "count": int(tick.get("count") or 0),
            "last_error": tick.get("last_error"),
            "consecutive_failures": int(tick.get("consecutive_failures") or 0),
            "next_tick_seconds": tick.get("next_tick_seconds"),
        },
        "process": process_stats(now),
        "disk": {"free_bytes": free, "total_bytes": total},
        "policy": policy_subset,
        "config_summary": config_summary(config),
        "log_tail": read_log_tail(log_path),
    }


def encode_bundle(
    bundle: Dict[str, object], max_bytes: int = MAX_BUNDLE_BYTES
) -> Tuple[bytes, bool]:
    """把诊断包 gzip 成服务端要的字节；返回 ``(blob, 有没有丢掉日志尾部)``。

    压缩后仍然超限时**丢掉日志尾部再压一次**（诊断包的骨架比日志重要）；
    连那样都超限（理论上不会：其余字段只有几百字节）才抛 ``ValueError``，
    由调用方当作"这次没传成"处理 —— 绝不发一个服务端会拒收的超大包。
    """
    blob = _gzip_json(bundle)
    if len(blob) <= max_bytes:
        return blob, False

    trimmed = dict(bundle)
    trimmed["log_tail"] = ""
    blob = _gzip_json(trimmed)
    if len(blob) <= max_bytes:
        return blob, True

    raise ValueError("诊断包压缩后仍然超过 %d 字节（%d）" % (max_bytes, len(blob)))


def _gzip_json(bundle: Dict[str, object]) -> bytes:
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, mtime=0)
