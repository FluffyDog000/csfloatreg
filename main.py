#!/usr/bin/env python3
"""CSFloat bot — точка входа.

    python main.py                          # прогон по config.yaml
    python main.py --only user1 --debug     # отладка одного аккаунта с паузами
    python main.py --threads 5 --headful
    python main.py --check                  # только проверить входные файлы
    python main.py --web                    # веб-интерфейс на 127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from bot import logging_setup
from bot.config import Config, load_selectors
from bot.errors import ConfigError, LoaderError
from bot.loader import load_all
from bot.runner import Runner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Массовая регистрация на CSFloat через Steam + подтверждение почты",
    )
    parser.add_argument("--config", default="config.yaml", help="путь к конфигу")
    parser.add_argument("--selectors", default="selectors.yaml", help="путь к файлу селекторов")
    parser.add_argument("--only", metavar="LOGIN", help="обработать только один аккаунт")
    parser.add_argument("--threads", type=int, help="число параллельных аккаунтов")
    parser.add_argument("--limit", type=int, help="взять не больше N аккаунтов из очереди")
    parser.add_argument("--modules", help="список модулей через запятую (registration,api_key)")
    parser.add_argument("--headful", action="store_true", help="видимый браузер")
    parser.add_argument("--debug", action="store_true", help="пошаговый режим (включает headful, 1 поток)")
    parser.add_argument("--force", action="store_true", help="не пропускать аккаунты со статусом done")
    parser.add_argument("--check", action="store_true", help="проверить входные файлы и выйти")
    parser.add_argument("--log-level", default="INFO", help="уровень логов в консоли")
    parser.add_argument("--web", action="store_true", help="запустить веб-интерфейс вместо прогона")
    parser.add_argument("--host", help="хост веб-интерфейса")
    parser.add_argument("--port", type=int, help="порт веб-интерфейса")
    return parser


def load_everything(args):
    cfg = Config.load(args.config).apply_cli(args)
    cfg.ensure_dirs()
    log_path = logging_setup.setup(cfg.path_for("logs"), level=args.log_level)
    selectors = load_selectors(args.selectors)
    return cfg, selectors, log_path


def print_check(bundles) -> None:
    ok = [b for b in bundles if not b.error]
    bad = [b for b in bundles if b.error]
    print(f"\nАккаунтов: {len(bundles)}   готовы: {len(ok)}   с проблемами: {len(bad)}")
    for bundle in bundles[:200]:
        mark = "OK " if not bundle.error else "!! "
        print(f"  {mark}{bundle.account.login:<24} {bundle.proxy.safe():<38} {bundle.error or ''}")
    if len(bundles) > 200:
        print(f"  … ещё {len(bundles) - 200}")


async def run_cli(args) -> int:
    cfg, selectors, log_path = load_everything(args)
    log = logging_setup.get_logger()
    log.info("Лог пишется в %s", log_path)

    if args.force:
        cfg.set("run.skip_statuses", [])
        log.info("--force: аккаунты со статусом done тоже будут обработаны")

    bundles = load_all(cfg)
    if args.check:
        print_check(bundles)
        return 0

    runner = Runner(cfg, selectors, bundles)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.stop)
        except NotImplementedError:  # Windows
            pass

    summary = await runner.run(only=args.only, limit=args.limit)
    if summary.get("total"):
        print("\nИтог:")
        for status, count in sorted(summary.get("stats", {}).items(), key=lambda x: -x[1]):
            print(f"  {status:<28} {count}")
        print(f"  подробности: {cfg.path_for('results')}")
    return 0


def run_web(args) -> int:
    cfg, selectors, log_path = load_everything(args)
    try:
        import uvicorn
    except ImportError:
        print("Для веб-интерфейса нужны fastapi и uvicorn: pip install -r requirements.txt", file=sys.stderr)
        return 2

    from web.app import create_app

    app = create_app(cfg, selectors, selectors_path=args.selectors)
    host = cfg.get("web.host", "127.0.0.1")
    port = int(cfg.get("web.port", 8000))
    print(f"\n  Веб-интерфейс:  http://{host}:{port}\n  Лог:            {log_path}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.web:
            return run_web(args)
        return asyncio.run(run_cli(args))
    except (ConfigError, LoaderError) as exc:
        print(f"\nОшибка входных данных: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
