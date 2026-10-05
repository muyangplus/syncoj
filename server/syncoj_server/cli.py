"""命令行入口。

    syncoj-server init          建库 + 建管理员（口令未给则随机生成并打印）
    syncoj-server serve         启动服务
    syncoj-server info          打印当前配置与统计
"""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys
from typing import List, Optional

from . import __version__
from .config import Settings
from .context import AppContext
from .models import Admin, Contest, ContestStatus, Player
from .paths import slugify
from .security import hash_password

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
    print("下一步:")
    print("  1. syncoj-server serve --host 0.0.0.0 --port 8000")
    print("  2. 用 /api/v1/admin/login 登录，创建场次并导入选手")
    print("  3. 为每位选手签发注册码 /api/v1/admin/players/{id}/enroll-code")
    return 0


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

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
