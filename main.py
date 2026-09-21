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
    parser.add_argument(
        "--probe", nargs="?", const="", metavar="URL",
        help="открыть один URL в настроенном браузере и показать, что с ним не так "
             "(по умолчанию csfloat.com); аккаунт берётся из --only или первый в списке",
    )
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


async def run_probe(args) -> int:
    """Диагностика одной страницы: тот же браузер, профиль и прокси, что в прогоне.

    Нужна, когда сайт открывается руками, но не открывается в боте: печатает
    упавшие запросы, JS-ошибки и то, отрисовалось ли вообще хоть что-нибудь.
    """
    from bot.browser import BrowserSession
    from bot.storage import ArtifactStore, StateStore

    cfg, _selectors, log_path = load_everything(args)
    log = logging_setup.get_logger()
    log.info("Лог пишется в %s", log_path)

    url = args.probe or (cfg.get("csfloat.base_url", "https://csfloat.com").rstrip("/") + "/")
    bundles = load_all(cfg)
    if args.only:
        bundles = [b for b in bundles if b.account.login.lower() == args.only.lower()]
        if not bundles:
            print(f"Аккаунт {args.only} не найден в accounts.txt", file=sys.stderr)
            return 2
    bundle = bundles[0]

    account_log = logging_setup.get_logger(bundle.account.login)
    state = StateStore(cfg.path_for("state"), cfg.path_for("profiles"))
    artifacts = ArtifactStore(cfg.path_for("errors"), cfg.path_for("debug_dumps"))
    session = BrowserSession(
        bundle.account.login, bundle.proxy, cfg, state, account_log,
        headful=True if args.headful or args.debug else cfg.get("run.headful", False),
    )

    try:
        await session.start()
        page = await session.page("csfloat")
        account_log.info("Открываю %s", url)
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=cfg.get("timeouts.page_load_ms", 60000)
            )
        except Exception as exc:  # noqa: BLE001 — диагностике трейсбек ни к чему
            print("\n" + "─" * 64)
            print(f"  Навигация не удалась: {type(exc).__name__}")
            print(f"  {str(exc).splitlines()[0][:200]}")
            print(f"  Прокси: {bundle.proxy.safe()}")
            print("  Чаще всего это мёртвый прокси или его сессия. Проверь ту же строку")
            print("  прокси в обычном браузере и возьми другую сессию.")
            print("─" * 64)
            await artifacts.dump(page, bundle.account.login, "probe_failed", debug=True, note=str(exc))
            return 1

        rendered = True
        try:
            await page.wait_for_function(
                "() => document.body && document.body.innerText.trim().length > 0",
                timeout=float(cfg.get("csfloat.render_timeout_s", 25)) * 1000,
            )
        except Exception:  # noqa: BLE001
            rendered = False

        html = await page.content()
        text = await page.evaluate("() => (document.body ? document.body.innerText : '').trim()")
        title = await page.title()
        user_agent = await page.evaluate("() => navigator.userAgent")
        saved = await artifacts.dump(page, bundle.account.login, "probe", debug=True, note=f"проба {url}")

        print("\n" + "─" * 64)
        print(f"  URL после загрузки : {page.url}")
        if response is not None:
            print(f"  HTTP-статус        : {response.status} {response.status_text}")
            try:
                headers = await response.all_headers()
            except Exception:  # noqa: BLE001
                headers = {}
            interesting = (
                "content-type", "content-length", "content-encoding", "server",
                "cf-ray", "cf-mitigated", "cf-cache-status", "x-served-by", "location",
                "retry-after", "set-cookie",
            )
            for name in interesting:
                if name in headers:
                    print(f"  {name:<18} : {headers[name][:120]}")
        else:
            print("  HTTP-статус        : ответа не было (навигация без запроса)")
        print(f"  Заголовок          : {title or '(пусто)'}")
        print(f"  User-Agent         : {user_agent}")
        print(f"  HTML               : {len(html)} символов")
        print(f"  Видимый текст      : {len(text)} символов")
        print(f"  Отрисовалось       : {'да' if rendered else 'НЕТ — страница пустая'}")
        if text:
            print(f"  Начало текста      : {text[:160]!r}")
        if saved:
            print(f"  Скриншот и HTML    : {saved[0].parent}")
        if len(html) < 2000:
            print("\n  HTML целиком (он подозрительно короткий):")
            print("  " + html.replace("\n", "\n  ")[:1800])
        print("─" * 64)
        status = response.status if response is not None else 0
        if status in (407, 429) or 500 <= status < 600:
            print(
                f"\n  Это ответ ПРОКСИ, а не сайта (код {status}).\n"
                f"  Прокси: {bundle.proxy.safe()}\n"
                "  Проверь эту же строку прокси в обычном браузере, возьми другую сессию\n"
                "  или другой выход. Браузер и селекторы тут ни при чём.\n"
            )
        elif not rendered:
            print(
                "\n  Страница пустая. Смотри выше строки 'запрос не прошёл' и 'JS-ошибка'.\n"
                "  Быстрые проверки в config.yaml, по одной за раз:\n"
                "    browser.disable_ublock: true     # мешает встроенный блокировщик\n"
                "    browser.block_images: false      # мешает блокировка картинок\n"
                "    browser.humanize: false\n"
                "    browser.geoip: false             # мешает подмена локали/таймзоны\n"
            )
        if args.debug:
            await asyncio.to_thread(input, "  Enter — закрыть браузер: ")
        return 0
    finally:
        await session.close()


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
        if args.probe is not None:
            return asyncio.run(run_probe(args))
        return asyncio.run(run_cli(args))
    except (ConfigError, LoaderError) as exc:
        print(f"\nОшибка входных данных: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
