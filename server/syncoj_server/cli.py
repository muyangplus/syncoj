"""命令行入口。

    syncoj-server init          建库 + 建管理员（口令未给则随机生成并打印）
    syncoj-server serve         启动服务
    syncoj-server info          打印当前配置与统计
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import func, select

from . import __version__
from . import keys as keypaths
from .config import Settings
from .context import AppContext
from .models import (
    Admin,
    Agent,
    BootstrapKey,
    Contest,
    ContestStatus,
    Player,
    utcnow,
)
from .paths import slugify
from .security import (
    hash_bootstrap_key,
    hash_password,
    new_bootstrap_key,
)

__all__ = ["main"]

log = logging.getLogger("syncoj")


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    if getattr(args, "data_root", None):
        from pathlib import Path

        settings.data_root = Path(args.data_root).expanduser().resolve()
    return settings


def _cmd_init(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    ctx = AppContext.create(settings)
    ctx.db.create_all()

    with ctx.db.session() as session:
        existing = session.query(Admin).filter(Admin.username == args.admin_user).one_or_none()
        if existing is not None:
            print("[=] 管理员 %s 已存在，未做改动" % args.admin_user)
        else:
            password = args.admin_password
            generated = False
            if not password:
                password = secrets.token_urlsafe(12)
                generated = True
            session.add(Admin(username=args.admin_user, password_hash=hash_password(password)))
            print("[+] 已创建管理员: %s" % args.admin_user)
            if generated:
                print()
                print("    初始口令: %s" % password)
                print("    （只显示这一次，请立即记录并自行修改）")
            print()

    print("[+] 数据目录: %s" % settings.data_root)
    print("[+] 数据库:   %s" % settings.db_path)
    print()
    release_state, bootstrap_state = _ensure_local_keys(ctx)
    print()
    print("下一步:")
    print("  1. syncoj-server serve --host 0.0.0.0 --port 8000")
    print("  2. 用 /api/v1/admin/login 登录，创建场次并导入名单（名单可复用）")
    if bootstrap_state == "no-dir":
        print("  3. 签发统一密钥: syncoj-server bootstrap-key issue --out /etc/syncoj/bootstrap.key")
    else:
        print("  3. 装机时让 Agent 读统一密钥（默认 /etc/syncoj/bootstrap.key，属主 root 0600）")
        print("     它就在 %s，装镜像时把这个文件放进去" % _bootstrap_key_display())
    print("  4. 机器装完 Agent 后会自动出现「配对码」，在管理界面「机器配对」页绑定选手")
    if release_state in ("created", "exists"):
        print("  5. 服务端已自动加载发布私钥，打包时公钥会随包带走:")
        print("     python agent/packaging/build_bundle.py")
    # 命令到这里就结束了，但连接池还攥着 syncoj.db。进程退出时操作系统会收拾，
    # 所以从前没人注意 —— 直到同一个进程里连着跑 init 和 db reset：Windows 上
    # 删不掉一个被打开的文件，于是 db reset 报"另一个程序正在使用此文件"。
    # 短命令显式归还句柄，读起来也更清楚。
    ctx.db.dispose()
    return 0


def _bootstrap_key_display() -> str:
    where = keypaths.key_dir_for_write()
    if where is None:  # pragma: no cover - 上面已经判过
        return "（未知）"
    return str(where / keypaths.BOOTSTRAP_KEY_NAME)


def _ensure_local_keys(ctx) -> "Tuple[str, str]":
    """补齐 ``.key/`` 下的两把密钥，返回 ``(发布密钥状态, 统一密钥状态)``。

    状态取值：``created`` / ``exists`` / ``no-dir`` / ``failed`` / ``skipped``，
    上层据此决定打印哪些"下一步" —— 已经有的东西不该再叫用户去创建一次。

    **只补缺，从不覆盖。** 轮换签名私钥会让所有已发布的签名失效，换统一密钥会
    让已经装好的镜像整批作废 —— 这两件事都必须是有人明确按键的，不能由一个
    初始化命令顺手做掉。
    """
    where = keypaths.key_dir_for_write()
    if where is None:
        print("[ ] 没有可用的密钥目录，跳过密钥生成")
        print("    原因：既没有设 %s，当前布局也不是源码仓库。" % keypaths.KEY_DIR_ENV)
        print("    影响：服务端不提供任何升级；装机需要手工指定统一密钥。")
        print("    要启用：set %s=<目录> 后重跑 init" % keypaths.KEY_DIR_ENV)
        return "no-dir", "no-dir"

    keypaths.ensure_key_dir(where)
    print("[+] 密钥目录: %s （0700）" % where)
    return _ensure_release_key(where), _ensure_bootstrap_key(ctx, where)


def _ensure_release_key(where: Path) -> str:
    """发布签名密钥对：缺私钥就生成，缺公钥就从私钥重导（不重签任何东西）。"""
    from .services.signing import DerError, generate_keypair, load_signing_key, openssl_available

    private = where / keypaths.RELEASE_SIGNING_KEY_NAME
    public = where / keypaths.RELEASE_PUBLIC_KEY_NAME

    if private.is_file():
        print("[=] 发布签名私钥已存在，未做改动: %s" % private)
        if not public.is_file():
            # 公钥丢了可以从私钥重新导出来 —— 它是派生品，不需要重签任何东西，
            # 所以这一步是安全的；私钥本身一个字节都不动。
            try:
                _write_public_key(public, load_signing_key(private))
            except Exception as exc:
                print("[!] 私钥在，但从它导出公钥失败：%s" % exc, file=sys.stderr)
                print("    自更新会保持关闭，直到公钥可读。", file=sys.stderr)
                return "failed"
            print("[+] 公钥缺失，已从私钥重新导出: %s" % public)
        return "exists"

    if not openssl_available():
        print("[!] 跳过发布签名密钥：找不到 openssl")
        print("    Debian/Ubuntu: apt install openssl，然后重跑 init")
        print("    影响：服务端不提供升级（这是安全的默认值，不是故障）。")
        return "skipped"

    try:
        key = generate_keypair(private)
    except (DerError, RuntimeError, OSError, FileExistsError) as exc:
        print("[!] 生成发布签名密钥失败：%s" % exc, file=sys.stderr)
        print("    自更新会保持关闭。", file=sys.stderr)
        return "failed"

    _write_public_key(public, key)
    print("[+] 已生成发布签名密钥: %s （0600）" % private)
    print("    公钥: %s" % public)
    return "created"


def _write_public_key(public: Path, key) -> None:
    """公钥不是秘密，0644 就好；但行尾必须是 LF。

    同一条理由写在这里而不是只在 genkey 里：公钥要在开发机和服务器之间搬运、
    被 sha256 比对，字节不同会让人怀疑"是不是换了密钥"。
    """
    public.parent.mkdir(parents=True, exist_ok=True)
    public.write_text(
        json.dumps(key.public_key_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        os.chmod(str(public), 0o644)
    except OSError:
        pass


def _ensure_bootstrap_key(ctx, where: Path) -> str:
    """统一注册密钥：文件与库里的哈希**两边都要在**才算好。

    这里有个不显眼但会真出事的交叉点：密钥的明文在 ``.key/`` 里，而它的哈希在
    数据库里。``db reset`` 只删库、不碰 ``.key/``（这是对的，密钥不是数据），
    于是"文件还在、哈希没了"是个必然会出现的状态 —— 那时这把密钥在界面上
    看起来好好的，实际注册会被判成"密钥无效"。所以判据不是"文件在不在"，
    而是"文件里的这一把，库里登记了没有"。
    """
    from .models import BootstrapKey

    path = where / keypaths.BOOTSTRAP_KEY_NAME

    if not path.is_file():
        raw = new_bootstrap_key()
        keypaths.write_secret_text(path, raw + "\n")
        with ctx.db.session() as session:
            session.add(
                BootstrapKey(
                    key_hash=hash_bootstrap_key(raw),
                    label="init 自动签发",
                    note="由 syncoj-server init 生成在 %s" % where,
                )
            )
        print("[+] 已生成统一注册密钥: %s （0600）" % path)
        print("    明文只在这个文件里，库里只存哈希 —— 装镜像时带上它")
        return "created"

    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        print("[!] %s 是空的，未做改动" % path)
        print("    删掉它再跑一次 init 会重新生成一把。")
        return "failed"

    digest = hash_bootstrap_key(raw)
    with ctx.db.session() as session:
        rows = list(
            session.execute(select(BootstrapKey).where(BootstrapKey.key_hash == digest)).scalars()
        )

    if rows:
        state = "已吊销" if rows[0].revoked_at else "有效"
        print("[=] 统一注册密钥已登记（%s），未做改动: %s" % (state, path))
        if rows[0].revoked_at:
            # 明说一句：revoke 是有意的动作，init 不该把它悄悄撤销回来
            print("    这把密钥已被吊销，init 不会替你恢复它。要换一把就删掉文件再跑 init。")
            return "exists"
        return "exists"

    # 文件在、库里完全没有这个哈希：最可能是 db reset 之后。补登记，
    # 但要让人看见 —— 如果那把密钥是刚刚被有意吊销的，这条日志就是线索。
    with ctx.db.session() as session:
        session.add(
            BootstrapKey(
                key_hash=digest,
                label="init 补登记",
                note="文件已存在但库里没有这个哈希（多半是 db reset 之后）",
            )
        )
    print("[+] 统一注册密钥的哈希不在库里，已补登记: %s" % path)
    print("    若这把密钥是你刚有意吊销的，请到管理界面重新吊销一次。")
    return "created"


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _build_settings(args)
    # 用工厂模式，避免 import 时产生磁盘副作用
    from . import main as app_module

    app = app_module.create_app(settings)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=not args.quiet_access,
    )
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    ctx = AppContext.create(settings)
    ctx.db.create_all()
    agents = ctx.registry.all()
    with ctx.db.session() as session:
        contests = session.query(Contest).count()
        players = session.query(Player).count()
        admins = session.query(Admin).count()
    print("版本:       %s" % __version__)
    print("数据目录:   %s" % settings.data_root)
    print("数据库:     %s" % settings.db_path)
    print("场次/选手:  %d / %d" % (contests, players))
    print("管理员:     %d" % admins)
    print("在线 Agent: %d / %d" % (sum(1 for a in agents if a.online), len(agents)))
    return 0


def _cmd_genkey(args: argparse.Namespace) -> int:
    """生成发布签名密钥对。

    私钥留在服务端（用于签发升级包），公钥要分发到每台考试机的
    ``/etc/syncoj/release-key.pub.json`` —— 它就是 Agent 的信任锚。
    """
    from .services.signing import DerError, generate_keypair, openssl_available

    if not openssl_available():
        print(
            "错误：找不到 openssl。密钥生成依赖它（Miller-Rabin 与随机素数搜索是\n"
            "      极易写错的密码学代码，不适合自己实现）。\n"
            "      Debian/Ubuntu: apt install openssl",
            file=sys.stderr,
        )
        return 2

    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        print("错误：私钥已存在，拒绝覆盖: %s" % out, file=sys.stderr)
        print("      覆盖它会让所有已发布的签名失效。要强制覆盖请加 --force。", file=sys.stderr)
        return 2
    if out.exists() and args.force:
        out.unlink()

    try:
        key = generate_keypair(out, bits=args.bits)
    except (DerError, RuntimeError, OSError) as exc:
        print("生成失败：%s" % exc, file=sys.stderr)
        return 1

    pub_path = Path(args.public_out) if args.public_out else out.with_suffix(".pub.json")
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    # 显式 LF：Windows 上 write_text 默认翻成 CRLF，于是"公钥文件"在两个平台上
    # 字节不同。JSON 本身不在乎，但一台机器上的文件跟另一台上的不一样，
    # 会让人怀疑内容真的不同 —— 排查时间就是这么烧掉的。
    pub_path.write_text(
        json.dumps(key.public_key_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print("[+] 私钥: %s  (权限 0600，绝不外传)" % out)
    print("[+] 公钥: %s" % pub_path)
    print("    密钥长度: %d 位    key_id: %s" % (key.bits, key.key_id))
    print()
    print("下一步:")
    print("  1. 服务端启动时带上私钥: SYNCOJ_RELEASE_KEY=%s syncoj-server serve" % out)
    print("  2. 把公钥分发到每台考试机: /etc/syncoj/release-key.pub.json")
    print("     并在 agent.ini 的 [upgrade] 段写 public_key = <该路径>")
    print("  3. 未配置这两项时自更新整体关闭 —— 这是安全的默认状态")
    return 0


def _cmd_contest(args: argparse.Namespace) -> int:
    """场次与选手的命令行管理。

    界面已经能做这些事，但脚本化部署（比如按名单批量建场次、从教务系统导出的
    CSV 直接灌进来）在命令行里更顺手，也不需要先起浏览器。
    """
    from .models import Contest, ContestStatus, Player
    from .paths import slugify

    settings = _build_settings(args)
    ctx = AppContext.create(settings)
    ctx.db.create_all()
    action = args.contest_action

    if action == "list":
        with ctx.db.session() as session:
            contests = list(session.execute(select(Contest).order_by(Contest.id)).scalars())
            counts = dict(
                session.execute(
                    select(Player.contest_id, func.count(Player.id)).group_by(Player.contest_id)
                ).all()
            )
        if not contests:
            print("还没有任何场次。用 syncoj-server contest create --name <名称> 创建。")
            return 0
        print("%-4s %-20s %-24s %-10s %s" % ("ID", "标识", "名称", "状态", "选手数"))
        for contest in contests:
            print(
                "%-4d %-20s %-24s %-10s %d"
                % (
                    contest.id,
                    contest.slug,
                    contest.name[:24],
                    contest.status,
                    counts.get(contest.id, 0),
                )
            )
        return 0

    if action == "create":
        slug = slugify(args.slug or args.name, fallback="contest")
        status_value = args.status if args.status in ContestStatus.ALL else ContestStatus.DRAFT
        with ctx.db.session() as session:
            if session.execute(select(Contest).where(Contest.slug == slug)).scalar_one_or_none():
                print("错误：场次标识 %s 已存在" % slug, file=sys.stderr)
                return 1
            contest = Contest(slug=slug, name=args.name, status=status_value, note=args.note)
            session.add(contest)
            session.flush()
            print("[+] 已创建场次 #%d %s（标识 %s，状态 %s）"
                  % (contest.id, contest.name, contest.slug, contest.status))
        print()
        print("下一步:")
        print("  syncoj-server contest import-players --contest %s --file roster.csv" % slug)
        return 0

    if action == "import-players":
        with ctx.db.session() as session:
            contest = session.execute(
                select(Contest).where(Contest.slug == args.contest)
            ).scalar_one_or_none()
            if contest is None:
                print("错误：找不到场次 %s" % args.contest, file=sys.stderr)
                return 1

            rows = _read_roster(args.file)
            if not rows:
                print("错误：名单里没有可导入的选手（文件：%s）" % args.file, file=sys.stderr)
                return 1

            existing = {
                row.player_no: row
                for row in session.execute(
                    select(Player).where(Player.contest_id == contest.id)
                ).scalars()
            }
            created = updated = 0
            for player_no, name, seat, group in rows:
                row = existing.get(player_no)
                if row is None:
                    row = Player(contest_id=contest.id, player_no=player_no)
                    session.add(row)
                    created += 1
                else:
                    updated += 1
                row.name = name or row.name
                row.seat = seat or row.seat
                row.group_name = group or row.group_name
            session.flush()
            print("[+] 场次 %s：新增 %d 名，更新 %d 名（共 %d）"
                  % (contest.slug, created, updated, len(rows)))
        return 0

    print("未知子命令: %s" % action, file=sys.stderr)
    return 2


def _read_roster(path: str) -> List[tuple]:
    """读选手名单 CSV。

    列顺序：``选手编号,姓名,座位,分组``，只有编号必填。
    允许 ``#`` 开头的注释行与空行 —— 现场手写的名单经常带这些。
    """
    import csv

    rows: List[tuple] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        first_line = _first_data_line(handle)
        # 显式判定分隔符，不用 csv.Sniffer —— 它是启发式的，在两三行的小样本上
        # 会猜错（实测会把制表符分隔的名单猜成逗号，整行被当成一个编号）。
        # 名单通常只有几十行，猜错一次就得人工排查，不值得冒这个险。
        delimiter = "\t" if "\t" in first_line else ("," if "," in first_line else ";")

        handle.seek(0)
        for raw in csv.reader(handle, delimiter=delimiter):
            if not raw:
                continue
            first = (raw[0] or "").strip()
            if not first or first.startswith("#"):
                continue
            # 跳过表头（第一行写"选手编号/player_no"之类）
            if first.lower() in ("选手编号", "编号", "player_no", "playerno", "id"):
                continue
            cells = [(cell or "").strip() for cell in raw] + [""] * 4
            rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _first_data_line(handle) -> str:
    """取第一行非空、非注释的内容，用于判定分隔符。"""
    for line in handle:
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncoj-server",
        description="SyncOJ 服务端",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-root", default=None, help="数据目录（默认 runtime/server）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化数据库与管理员")
    p_init.add_argument("--admin-user", default="admin")
    p_init.add_argument("--admin-password", default=None, help="留空则随机生成并打印")
    p_init.set_defaults(func=_cmd_init)

    p_serve = sub.add_parser("serve", help="启动服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    p_serve.add_argument("--quiet-access", action="store_true", help="关闭访问日志")
    p_serve.set_defaults(func=_cmd_serve)

    p_info = sub.add_parser("info", help="打印配置与统计")
    p_info.set_defaults(func=_cmd_info)

    p_db = sub.add_parser("db", help="数据库维护")
    db_sub = p_db.add_subparsers(dest="db_action", required=True)

    d_reset = db_sub.add_parser(
        "reset", help="删库重建（开发期用；会丢掉全部数据）"
    )
    d_reset.add_argument(
        "--yes", action="store_true", help="确认执行（不给就只打印会做什么）"
    )
    d_reset.add_argument(
        "--keep-admin", action="store_true", help="重建后保留原有管理员账号与口令"
    )
    d_reset.set_defaults(func=_cmd_db)

    p_key = sub.add_parser("genkey", help="生成发布签名密钥对（自更新用）")
    p_key.add_argument(
        "--out",
        default="/etc/syncoj/release-key.pem",
        help="私钥输出路径（默认 /etc/syncoj/release-key.pem）",
    )
    p_key.add_argument("--public-out", default=None, help="公钥输出路径（默认同目录 .pub.json）")
    p_key.add_argument("--bits", type=int, default=2048, help="密钥长度，不得低于 2048")
    p_key.add_argument("--force", action="store_true", help="覆盖已存在的私钥")
    p_key.set_defaults(func=_cmd_genkey)

    p_contest = sub.add_parser("contest", help="场次与选手管理（脚本化部署用）")
    contest_sub = p_contest.add_subparsers(dest="contest_action", required=True)

    c_list = contest_sub.add_parser("list", help="列出全部场次")
    c_list.set_defaults(func=_cmd_contest)

    c_create = contest_sub.add_parser("create", help="创建场次")
    c_create.add_argument("--name", required=True, help="场次名称")
    c_create.add_argument("--slug", default=None, help="场次标识（默认从名称生成）")
    c_create.add_argument(
        "--status", default="running", choices=["draft", "running", "frozen", "closed"]
    )
    c_create.add_argument("--note", default=None)
    c_create.set_defaults(func=_cmd_contest)

    c_import = contest_sub.add_parser("import-players", help="从 CSV 导入/更新选手")
    c_import.add_argument("--contest", required=True, help="场次标识（slug）")
    c_import.add_argument(
        "--file", required=True, help="CSV 路径，列顺序：编号,姓名,座位,分组"
    )
    c_import.set_defaults(func=_cmd_contest)


    p_boot = sub.add_parser(
        "bootstrap-key",
        help="镜像内置的统一注册密钥（整间机房一份，配合短码配对使用）",
    )
    boot_sub = p_boot.add_subparsers(dest="bootstrap_action", required=True)

    b_issue = boot_sub.add_parser("issue", help="签发一把新密钥并打印（只显示一次）")
    b_issue.add_argument("--label", default=None, help="用途备注，例如「2025 机房镜像」")
    b_issue.add_argument("--note", default=None)
    b_issue.add_argument(
        "--expires-days", type=int, default=None, help="多少天后过期（默认不过期）"
    )
    b_issue.add_argument(
        "--out",
        default=None,
        help="同时写入这个文件（root 只读 0600），供装机脚本直接拷进镜像",
    )
    b_issue.set_defaults(func=_cmd_bootstrap_key)

    b_list = boot_sub.add_parser("list", help="列出全部密钥（只显示指纹，不显示明文）")
    b_list.set_defaults(func=_cmd_bootstrap_key)

    b_revoke = boot_sub.add_parser("revoke", help="吊销一把密钥（已注册的机器不受影响）")
    b_revoke.add_argument("--id", type=int, required=True, dest="key_id")
    b_revoke.set_defaults(func=_cmd_bootstrap_key)

    b_pending = boot_sub.add_parser("pending", help="列出还没配对的机器")
    b_pending.set_defaults(func=_cmd_bootstrap_key)

    return parser


def _cmd_bootstrap_key(args: argparse.Namespace) -> int:
    """统一注册密钥的签发、查看与吊销。

    **明文只在签发时打印一次**，之后库里只有哈希 —— 和注册码同一个规矩。
    丢了就再签一把，吊销旧的即可；已经注册好的机器不受影响（它们手里是
    各自的 token，不是这把密钥）。
    """
    from datetime import timedelta

    from .services.enrollment import fingerprint_duplicates

    settings = _build_settings(args)
    ctx = AppContext.create(settings)
    ctx.db.create_all()
    action = args.bootstrap_action

    if action == "issue":
        raw = new_bootstrap_key()
        expires_at = (
            utcnow() + timedelta(days=args.expires_days) if args.expires_days else None
        )
        with ctx.db.session() as session:
            key = BootstrapKey(
                key_hash=hash_bootstrap_key(raw),
                label=args.label or "",
                note=args.note,
                expires_at=expires_at,
            )
            session.add(key)
            session.flush()
            key_id = key.id

        if args.out:
            target = Path(args.out).expanduser()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # 只给 root 读：这把钥匙能注册整间机房，不该躺在选手读得到的地方。
                # 先建成 0600 再写内容，避免有一瞬间是宽权限。
                target.touch(mode=0o600, exist_ok=True)
                target.chmod(0o600)
                target.write_text(raw + "\n", encoding="utf-8", newline="\n")
            except OSError as exc:
                print("错误：写入 %s 失败：%s" % (target, exc), file=sys.stderr)
                return 1
            print("[+] 已签发统一密钥 #%d，并写入 %s（0600）" % (key_id, target))
        else:
            print("[+] 已签发统一密钥 #%d" % key_id)
        print()
        print("  %s" % raw)
        print()
        print("  明文只显示这一次，库里只存哈希。装机时把它放进镜像：")
        print("    /etc/syncoj/bootstrap.key   （0600，属主 root）")
        print("  然后装 Agent 时带上 --bootstrap-key-file /etc/syncoj/bootstrap.key")
        return 0

    if action == "list":
        with ctx.db.session() as session:
            keys = list(
                session.execute(select(BootstrapKey).order_by(BootstrapKey.id)).scalars()
            )
        if not keys:
            print("还没有统一密钥。用 syncoj-server bootstrap-key issue 签发一把。")
            return 0
        print("%-4s %-24s %-8s %-20s %s" % ("ID", "用途", "用过", "最后使用", "状态"))
        for key in keys:
            state = "已吊销" if key.revoked_at else ("已过期" if _expired(key) else "有效")
            print(
                "%-4d %-24s %-8d %-20s %s"
                % (
                    key.id,
                    (key.label or "—")[:24],
                    key.use_count or 0,
                    _fmt(key.last_used_at),
                    state,
                )
            )
        return 0

    if action == "revoke":
        with ctx.db.session() as session:
            key = session.get(BootstrapKey, args.key_id)
            if key is None:
                print("错误：没有 #%d 这把密钥" % args.key_id, file=sys.stderr)
                return 1
            if key.revoked_at is not None:
                print("这把密钥已经是吊销状态。")
                return 0
            key.revoked_at = utcnow()
        print("[+] 已吊销统一密钥 #%d。已注册的机器不受影响。" % args.key_id)
        return 0

    if action == "pending":
        now = utcnow()
        with ctx.db.session() as session:
            agents = list(
                session.execute(
                    select(Agent)
                    .where(Agent.roster_entry_id.is_(None), Agent.revoked_at.is_(None))
                    .order_by(Agent.last_seen_at.desc().nullslast(), Agent.id)
                ).scalars()
            )
            peers = fingerprint_duplicates(session)

        if peers:
            print("⚠ 克隆镜像告警：有机器共用同一个硬件指纹")
            for fingerprint, count in peers:
                print("    %s… 被 %d 台机器共用" % (fingerprint[:16], count))
            print()

        if not agents:
            print("没有待配对的机器。")
            return 0
        print("%-4s %-18s %-12s %-8s %s" % ("ID", "主机名", "机器编号", "配对码", "最后心跳"))
        for agent in agents:
            if agent.pair_code_expires_at is None:
                code_state = "—"
            else:
                left = int((agent.pair_code_expires_at - now).total_seconds())
                # 负数是"码过期了但机器还没被配对走"，直接显示过期，别印出 -320 秒。
                code_state = "已过期" if left <= 0 else "%d 秒" % left
            print(
                "%-4d %-18s %-12s %-8s %s"
                % (
                    agent.id,
                    (agent.hostname or "—")[:18],
                    (agent.machine_id or "—")[:12],
                    code_state,
                    _fmt(agent.last_seen_at),
                )
            )
        print()
        print("配对请到管理界面「机器配对」页：读机器桌面上的配对码，选中选手即可。")
        return 0

    print("错误：未知操作 %s" % action, file=sys.stderr)  # pragma: no cover
    return 2


def _expired(key) -> bool:
    return key.expires_at is not None and key.expires_at < utcnow()


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def _cmd_db(args: argparse.Namespace) -> int:
    """数据库维护。

    ``db reset`` 是**开发期**的逃生口：结构改动做一次性迁移太麻烦时，
    直接删库重建。它明确会丢数据 —— 所以默认只打印计划，要真跑得加 ``--yes``。

    为什么不做成"自动检测结构不一致就重建"：那会在生产环境里因为一次手误
    把整场比赛的数据抹掉，而**删库这件事必须是有人明确按下去的**。
    """
    settings = _build_settings(args)
    db_path = settings.db_path

    if args.db_action != "reset":  # pragma: no cover - argparse 已限定
        print("错误：未知的数据库操作 %s" % args.db_action, file=sys.stderr)
        return 2

    stash: List[Dict[str, object]] = []
    if args.keep_admin and db_path.is_file():
        # 趁库还在，把管理员账号捞出来 —— 重建之后不用再跑 init 设口令
        try:
            ctx = AppContext.create(settings)
            with ctx.db.session() as session:
                for admin in session.execute(select(Admin)).scalars():
                    stash.append(
                        {"username": admin.username, "password_hash": admin.password_hash}
                    )
            ctx.db.dispose()
        except Exception as exc:
            print("读取原管理员账号失败（将不会保留）：%s" % exc, file=sys.stderr)

    print("将要删除：%s" % db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            print("           %s" % sidecar)
    if stash:
        print("将保留管理员账号：%s" % "、".join(str(item["username"]) for item in stash))

    if not args.yes:
        print()
        print("这是**只打印计划**。确认要丢数据就加 --yes：")
        print("    syncoj-server db reset --yes")
        return 0

    removed = 0
    for target in [db_path] + [Path(str(db_path) + s) for s in ("-wal", "-shm")]:
        if target.exists():
            try:
                target.unlink()
                removed += 1
            except OSError as exc:
                print("删除 %s 失败：%s" % (target, exc), file=sys.stderr)
                return 1

    ctx = AppContext.create(settings)
    ctx.db.create_all()
    if stash:
        with ctx.db.session() as session:
            for item in stash:
                session.add(
                    Admin(username=item["username"], password_hash=item["password_hash"])
                )
    ctx.db.dispose()

    print("[+] 已重建数据库（删除 %d 个文件）" % removed)
    if not stash:
        print("下一步：syncoj-server init --admin-user admin")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        # 默认 WARNING：CLI 的输出是给人看的（表格、注册码），不该被
        # "未配置签名私钥"这类例行 INFO 淹掉。要看细节加 -v。
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
