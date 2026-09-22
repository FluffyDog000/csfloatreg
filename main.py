#!/usr/bin/env python3
"""Менеджер профилей: изолированный браузер на аккаунт + данные под рукой.

    python main.py             # веб-интерфейс на 127.0.0.1:8000
    python main.py --check     # проверить входные файлы и показать пул
    python main.py --reset LOGIN   # стереть профиль и cookies аккаунта
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from bot import logging_setup
from bot.config import Config
from bot.errors import ConfigError, LoaderError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description="Менеджер профилей браузера")
    parser.add_argument("--config", default="config.yaml", help="путь к конфигу")
    parser.add_argument("--host", help="хост веб-интерфейса")
    parser.add_argument("--port", type=int, help="порт веб-интерфейса")
    parser.add_argument("--check", action="store_true", help="проверить входные файлы и выйти")
    parser.add_argument("--reset", metavar="LOGIN", help="стереть профиль и cookies аккаунта")
    parser.add_argument("--log-level", default="INFO", help="уровень логов в консоли")
    return parser


def code_version(root) -> str:
    try:
        run = lambda args: subprocess.run(  # noqa: E731
            args, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, check=True,
        ).stdout.strip()
        dirty = run(["git", "status", "--porcelain", "config.yaml"])
        return (
            f"{run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'])} @ "
            f"{run(['git', 'log', '-1', '--format=%h %ad', '--date=short'])}"
            + (f" | ЛОКАЛЬНО ИЗМЕНЁН: {dirty}" if dirty else "")
        )
    except Exception:  # noqa: BLE001 — без git тоже должно работать
        return "версия неизвестна"


def load_config(args) -> Config:
    cfg = Config.load(args.config)
    for attr, dotted in (("host", "web.host"), ("port", "web.port")):
        value = getattr(args, attr, None)
        if value is not None:
            cfg.set(dotted, value)
    cfg.ensure_dirs()
    log_path = logging_setup.setup(cfg.path_for("logs"), level=args.log_level)
    log = logging_setup.get_logger()
    log.info("Код: %s", code_version(cfg.root))
    log.info("Лог пишется в %s", log_path)
    return cfg


def run_check(cfg) -> int:
    from bot.manager import ProfileManager
    from bot.steam_guard import SteamTime

    manager = ProfileManager(cfg, SteamTime("", enabled=False))
    if manager.load_error:
        print(f"\nОшибка входных данных: {manager.load_error}\n", file=sys.stderr)
        return 2

    pool = manager.pool_stats()
    rows = manager.rows()
    print(f"\nАккаунтов: {len(rows)}   maFile есть: {sum(1 for r in rows if r['has_mafile'])}")
    print(f"Прокси в пуле: {pool['total']}   занято: {pool['used']}   свободно: {pool['free']}   плохих: {pool['bad']}")
    print(f"\n{'логин':<24} {'прокси':<42} maFile  статус")
    for row in rows[:200]:
        print(
            f"  {row['login']:<22} {(row['proxy'] or '—'):<42} "
            f"{'✓' if row['has_mafile'] else '—':<7} {row['status']}"
        )
    if len(rows) > 200:
        print(f"  … ещё {len(rows) - 200}")
    if pool["free"] == 0 and any(r["proxy"] is None for r in rows):
        print("\nВНИМАНИЕ: свободных прокси в пуле не осталось, часть аккаунтов без привязки.")
    return 0


def run_reset(cfg, login: str) -> int:
    from bot.storage import StateStore

    StateStore(cfg.path_for("state"), cfg.path_for("profiles")).forget(login)
    print(f"Профиль и cookies аккаунта {login} стёрты (отпечаток сохранён).")
    return 0


def run_web(cfg) -> int:
    try:
        import uvicorn
    except ImportError:
        print("Нужны fastapi и uvicorn: pip install -r requirements.txt", file=sys.stderr)
        return 2

    from web.app import create_app

    host, port = cfg.get("web.host", "127.0.0.1"), int(cfg.get("web.port", 8000))
    token = cfg.get("web.token")
    print(f"\n  Интерфейс: http://{host}:{port}{'/?token=' + str(token) if token else ''}")
    print("  Остановить: Ctrl+C\n")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        cfg = load_config(args)
        if args.check:
            return run_check(cfg)
        if args.reset:
            return run_reset(cfg, args.reset)
        return run_web(cfg)
    except (ConfigError, LoaderError) as exc:
        print(f"\nОшибка: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nОстановлено", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
