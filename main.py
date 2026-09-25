#!/usr/bin/env python3
"""CSFloat bot — точка входа.

    python main.py                          # веб-интерфейс: «Запуск» + «Профили»
    python main.py --run                    # прогон очереди прямо в консоли
    python main.py --run --only user1 --debug   # отладка одного аккаунта с паузами
    python main.py --run --threads 5 --headful
    python main.py --check                  # проверить входные файлы и выйти
    python main.py --reset --only user1     # стереть cookies, профиль и статусы
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import urllib.parse

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
    parser.add_argument(
        "--reset", action="store_true",
        help="стереть cookies и профиль браузера аккаунта (--only) или всех, и сбросить статусы",
    )
    parser.add_argument(
        "--click", metavar="SELECTOR",
        help="в режиме --probe: кликнуть по селектору и снять второй срез кандидатов "
             "(так достаются пункты меню, которых нет в DOM до клика)",
    )
    parser.add_argument("--check", action="store_true", help="проверить входные файлы и выйти")
    parser.add_argument(
        "--mail-probe", nargs="?", const="", metavar="MAIL",
        help="разведка API firstmail: скачать спецификацию, перебрать адреса и показать, "
             "какой отвечает (ящик берётся из --only или первый в списке)",
    )
    parser.add_argument("--run", action="store_true", help="прогон очереди в консоли (без веб-интерфейса)")
    parser.add_argument("--log-level", default="INFO", help="уровень логов в консоли")
    parser.add_argument("--web", action="store_true", help="веб-интерфейс (режим по умолчанию)")
    parser.add_argument("--host", help="хост веб-интерфейса")
    parser.add_argument("--port", type=int, help="порт веб-интерфейса")
    return parser


def code_version(root) -> str:
    """Короткий идентификатор запущенного кода: ветка и последний коммит."""
    import subprocess

    try:
        # только хеш и дата: заголовок коммита ломается в консоли Windows
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h %ad", "--date=short"], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "config.yaml", "selectors.yaml"], cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=True,
        ).stdout.strip()
        suffix = f" | ЛОКАЛЬНО ИЗМЕНЕНЫ: {dirty.replace(chr(10), ', ')}" if dirty else ""
        return f"{branch} @ {out}{suffix}"
    except Exception:  # noqa: BLE001 — без git тоже должно работать
        return "версия неизвестна (git недоступен)"


def load_everything(args):
    cfg = Config.load(args.config).apply_cli(args)
    cfg.ensure_dirs()
    log_path = logging_setup.setup(cfg.path_for("logs"), level=args.log_level)
    selectors = load_selectors(args.selectors)
    log = logging_setup.get_logger()
    log.info("Код: %s", code_version(cfg.root))
    legacy = Config.stale_local(cfg.path) if cfg.path else None
    if legacy is not None:
        log.warning(
            "%s больше не читается: настройки живут в одном config.yaml. "
            "Перенеси нужное туда и удали файл", legacy.name,
        )
    log.info(
        "Адреса CSFloat: профиль=%s настройки=%s",
        cfg.get("csfloat.profile_url"), cfg.get("csfloat.settings_url"),
    )
    return cfg, selectors, log_path


def print_effective(cfg) -> None:
    """Что бот реально прочитал из конфигов — чтобы не гадать, чей это файл."""
    print("\nНастройки, которые применились:")
    print(f"  config.yaml        : {cfg.path}")
    for key in ("csfloat.profile_url", "csfloat.settings_url", "browser.engine",
                "browser.persistent_profile", "browser.firefox_prefs", "run.threads",
                "mail.source", "mail.provider", "mail.firstmail.base_url"):
        print(f"  {key:<26} = {cfg.get(key)!r}")
    key_set = bool(cfg.get("mail.firstmail.api_key") or os.getenv("FIRSTMAIL_API_KEY"))
    print(f"  {'mail.firstmail.api_key':<26} = {'задан' if key_set else 'НЕ ЗАДАН'}")


def print_pool(cfg) -> None:
    """Вторая половина картины: что видит менеджер профилей."""
    from bot.bindings import BindingStore

    bindings = BindingStore(cfg.path_for("data") / "bindings.json")
    accounts = bindings.data.get("accounts") or {}
    if not accounts:
        print("\nМенеджер профилей: привязок ещё нет (страница «Профили» их создаст)")
        return
    marked = sum(1 for e in accounts.values() if e.get("status") not in (None, "new"))
    trade = sum(1 for e in accounts.values() if e.get("trade_url"))
    mails = sum(1 for e in accounts.values() if e.get("mail"))
    print(
        f"\nМенеджер профилей: аккаунтов в памяти {len(accounts)}, "
        f"с почтой {mails}, "
        f"с пометкой {marked}, с трейд-ссылкой {trade}, "
        f"плохих прокси {len(bindings.data.get('bad_proxies') or [])}"
    )


def print_check(bundles) -> None:
    ok = [b for b in bundles if not b.error and b.account.mail]
    bad = [b for b in bundles if b.error]
    no_mail = [b for b in bundles if not b.account.mail]
    print(f"\nАккаунтов: {len(bundles)}   готовы: {len(ok)}   с проблемами: {len(bad)}")
    if no_mail:
        print(f"Без почты: {len(no_mail)} — добавь строк в mails.txt")
    print(f"\n     {'логин':<20} {'почта':<32} {'прокси':<34} проблема")
    for bundle in bundles[:200]:
        mark = "OK " if not bundle.error and bundle.account.mail else "!! "
        mail = bundle.account.mail or "ПОЧТЫ НЕТ"
        print(
            f"  {mark}{bundle.account.login:<20} {mail:<32} "
            f"{bundle.proxy.safe():<34} {bundle.error or ''}"
        )
    if len(bundles) > 200:
        print(f"  … ещё {len(bundles) - 200}")


async def run_reset(args) -> int:
    """Стирает сессию аккаунта: cookies, профиль браузера, статусы в results.csv.

    Закреплённый отпечаток сохраняется — он должен пережить сброс, иначе
    аккаунт снова станет для Steam и Microsoft новым устройством.
    """
    from bot.storage import ResultsStore, StateStore

    cfg, _selectors, _log_path = load_everything(args)
    bundles = load_all(cfg)
    if args.only:
        bundles = [b for b in bundles if b.account.login.lower() == args.only.lower()]
        if not bundles:
            print(f"Аккаунт {args.only} не найден в accounts.txt", file=sys.stderr)
            return 2

    state = StateStore(cfg.path_for("state"), cfg.path_for("profiles"))
    results = ResultsStore(cfg.path_for("results"))
    modules = cfg.get("run.modules") or ["registration"]
    for bundle in bundles:
        login = bundle.account.login
        state.forget(login)
        for module in modules:
            await results.update(login, module, status="new", stage="", error="", attempts=0)
        print(f"  сброшен: {login}")
    print(f"\nГотово: {len(bundles)} аккаунт(ов). Отпечатки в state/*.fp.json сохранены.")
    return 0


def run_mail_probe(args) -> int:
    """Разведка API firstmail: где живут эндпоинты и что они отвечают.

    Документация у сервиса меняется, а гадать по одной строке лога — худший
    способ её читать. Команда делает это с машины, у которой есть доступ.
    """
    from bot.pages.mail.firstmail_api import (
        CANDIDATE_BASES, CANDIDATE_PATHS, DEFAULTS, KNOWN_OK_PATH, SPEC_URLS,
        looks_like_antibot, raw_get, spec_summary,
    )

    def kind_of(body: str) -> str:
        if looks_like_antibot(body):
            return "АНТИБОТ"
        return "JSON" if body.lstrip().startswith(("{", "[")) else "HTML/текст"

    cfg, _selectors, _log_path = load_everything(args)
    settings = {**DEFAULTS, **(cfg.get("mail.firstmail") or {})}
    key = str(settings.get("api_key") or os.getenv("FIRSTMAIL_API_KEY") or "").strip()
    header = str(settings.get("auth_header") or "Authorization")
    prefix = str(settings.get("auth_prefix") or "")

    def auth(scheme: str | None = None) -> dict:
        """Заголовок с ключом. scheme=None — как настроено в конфиге."""
        if not key:
            return {}
        if scheme is None:
            return {header: f"{prefix}{key}"}
        return {"Authorization": f"Bearer {key}"} if scheme == "bearer" else {"X-API-KEY": key}

    mail = args.mail_probe or ""
    password = ""
    if not mail:
        bundles = load_all(cfg)
        if args.only:
            bundles = [b for b in bundles if b.account.login.lower() == args.only.lower()]
        boxes = [b.account for b in bundles if b.account.mail]
        if not boxes:
            print("Не из чего брать ящик: заполни mails.txt или передай почту в --mail-probe", file=sys.stderr)
            return 2
        mail, password = boxes[0].mail, boxes[0].mail_password

    # версию печатаем прямо в отчёт: по присланному тексту сразу видно,
    # какая сборка его сделала, и не приходится гадать, дошёл ли git pull
    print(f"\nКод                : {code_version(cfg.root)}")
    print(f"Ключ API           : {'задан (' + key[:4] + '…, ' + str(len(key)) + ' символов)' if key else 'НЕ ЗАДАН'}")
    print(f"Заголовок ключа    : {header}: {prefix}<ключ>")
    print(f"Ящик для проверки  : {mail}")
    # запрос — обычный GET с заголовком, как в примере из панели: показываем его целиком,
    # чтобы можно было сравнить руками
    print(
        f"\nЭквивалент curl:\n  curl -H \"{header}: {prefix}<ключ>\" "
        f"\"{str(settings['base_url']).rstrip('/')}{settings.get('message_path')}"
        f"?{settings['username_param']}=<почта>&{settings['password_param']}=<пароль>\""
    )

    # ── 1. спецификация ──────────────────────────────────────
    print("\n1) Ищу спецификацию API:")
    found_spec = False
    for url in SPEC_URLS:
        status, body = raw_get(url, timeout=15)
        mark = "OK " if status == 200 and body.lstrip().startswith("{") else f"{status or '—'}  "
        print(f"  {mark} {url}  {kind_of(body)}")
        if status == 200 and body.lstrip().startswith("{"):
            lines = spec_summary(body)
            if lines:
                found_spec = True
                print(f"\n  Эндпоинты из спецификации ({len(lines)}):")
                print(f"    {'МЕТОД':<5} {'ПУТЬ':<40} {'ПАРАМЕТРЫ':<40} ОПИСАНИЕ")
                for line in lines:
                    print("    " + line)
                target = cfg.root / "firstmail-openapi.json"
                target.write_text(body, encoding="utf-8")
                print(f"\n  Полная спецификация сохранена: {target}")
            break
    if not found_spec:
        print("  спецификацию скачать не удалось — иду перебором адресов")

    # ── 1.5 корень API: DRF обычно сам перечисляет эндпоинты ──
    print("\n1.5) Спрашиваю корень API и адрес из подсказки панели:")
    for base in CANDIDATE_BASES:
        for suffix, label in (("/", "корень"), (KNOWN_OK_PATH, "домены (проверка ключа)")):
            status, body = raw_get(f"{base}{suffix}", auth(), timeout=15)
            flat = " ".join(body.split())
            print(f"  {status or '—':<4} {kind_of(body):<10} {label:<24} {base}{suffix}")
            print(f"       {flat[:260]}")

    # ── 2. перебор адресов ───────────────────────────────────
    print("\n2) Пробую адреса с настоящим ключом и ящиком:")
    query = urllib.parse.urlencode({
        str(settings["username_param"]): mail,
        str(settings["password_param"]): password,
    })
    configured = (
        str(settings["base_url"]).rstrip("/"),
        str(settings.get("messages_path") or settings.get("message_path") or "/market/get/message"),
    )
    combos = [configured] + [
        (base, path)
        for base in CANDIDATE_BASES
        for path in CANDIDATE_PATHS
        if (base, path) != configured
    ]

    working = []
    for base, path in combos[:16]:
        url = f"{base}{path}?{query}"
        status, body = raw_get(url, auth(), timeout=20)
        flat = " ".join(body.split())
        kind = kind_of(body)
        if status == 200 and kind == "JSON":
            working.append((base, path))
        print(f"  {status or '—':<4} {kind:<10} {base}{path}")
        print(f"       {flat[:150]}")

    # ── 3. проверка ключа на адресе, который отвечает JSON ───
    alive = working[0] if working else None
    if alive is None:
        # адрес, ответивший JSON хоть с какой-то ошибкой, тоже годится для проверки ключа
        for base, path in combos[:16]:
            status, body = raw_get(f"{base}{path}?{query}", auth(), timeout=15)
            if body.lstrip().startswith(("{", "[")):
                alive = (base, path)
                break

    if alive is not None:
        base, path = alive
        print(f"\n3) Проверяю ключ на {base}{path} (длина ключа {len(key)} символов):")
        variants = {
            f"{header}: {prefix}<ключ>": auth(),
            "Authorization: Bearer <ключ>": auth("bearer"),
            "X-API-KEY: <ключ>": auth("x-api-key"),
            "без ключа": {},
        }
        for label, headers in variants.items():
            status, body = raw_get(f"{base}{path}?{query}", headers, timeout=15)
            print(f"  {status or '—':<4} {kind_of(body):<10} {label:<30} {' '.join(body.split())[:100]}")
        print("\n  АНТИБОТ = домен отдаёт JS-заглушку вместо API, скриптам он недоступен."
              "\n  Тот заголовок, где ответ не «Token is not valid», и есть верный.")

    print()
    if working:
        base, path = working[0]
        print("Рабочий адрес найден. Впиши в config.yaml:\n")
        print("mail:\n  firstmail:")
        print(f"    base_url: {base}")
        print(f"    message_path: {path}")
        print("    messages_path: null")
        print(f"    auth_header: {header}")
        print(f"    auth_prefix: '{prefix}'")
    else:
        print("Ни один адрес не отдал письма. Пришли вывод этой команды — по нему видно,"
              " что именно отвечает сервис.")
    return 0


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

        prefs = cfg.get("browser.firefox_prefs") or {}
        print("\n" + "─" * 64)
        print(f"  Движок             : {cfg.get('browser.engine')}"
              f", профиль: {'да' if cfg.get('browser.persistent_profile') else 'нет'}"
              f", geoip: {'да' if cfg.get('browser.geoip') else 'нет'}"
              f", картинки: {'блок' if cfg.get('browser.block_images') else 'ок'}")
        print(f"  Настройки Firefox  : {', '.join(f'{k}={v}' for k, v in prefs.items()) or '(нет)'}")
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
            if 200 <= response.status < 300:
                for name in interesting:
                    if name in headers:
                        print(f"  {name:<18} : {headers[name][:120]}")
            else:
                # на ошибке важен каждый заголовок: по ним видно, кто ответил
                print(f"  Заголовки ответа   : {len(headers)} шт.")
                for name, value in sorted(headers.items()):
                    print(f"    {name:<22} {value[:110]}")
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
        # кандидаты в селекторы — главное, ради чего гоняют пробу на живом сайте
        from bot.debug import collect_page, print_candidates

        folder = artifacts.dir_for(bundle.account.login, debug=True)
        report = await collect_page(page)
        if report:
            print_candidates(report)
            dump = folder / "probe_candidates.json"
            dump.write_text(
                json.dumps({"url": page.url, **report}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"  Полный список: {dump}")

        if args.click:
            print(f"\n  Кликаю по '{args.click}' и снимаю второй срез…")
            try:
                await page.locator(args.click).first.click(timeout=10000)
                await asyncio.sleep(1.5)
            except Exception as exc:  # noqa: BLE001
                print(f"  Клик не удался: {str(exc).splitlines()[0][:160]}")
            else:
                text_after = await page.evaluate("() => (document.body ? document.body.innerText : '').trim()")
                print(f"  После клика: URL {page.url[:90]}")
                print(f"  Видимого текста: {len(text_after)} символов (было {len(text)})")
                for marker in ("Select an item to read", "Nothing is selected", "Выберите элемент"):
                    if marker in text_after:
                        print(f"  Область чтения всё ещё пуста: «{marker}»")
                after = await collect_page(page)
                if after:
                    print_candidates(after)
                    dump = folder / "probe_candidates_after_click.json"
                    dump.write_text(
                        json.dumps({"url": page.url, "clicked": args.click, **after},
                                   ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(f"  Полный список: {dump}")
                await artifacts.dump(page, bundle.account.login, "probe_after_click", debug=True)

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
        print_effective(cfg)
        print_check(bundles)
        print_pool(cfg)
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
    if args.host:
        cfg.set("web.host", args.host)
    if args.port:
        cfg.set("web.port", args.port)
    try:
        import uvicorn
    except ImportError:
        print("Для веб-интерфейса нужны fastapi и uvicorn: pip install -r requirements.txt", file=sys.stderr)
        return 2

    from web.app import create_app

    app = create_app(cfg, selectors, selectors_path=args.selectors)
    host = cfg.get("web.host", "127.0.0.1")
    port = int(cfg.get("web.port", 8000))
    token = cfg.get("web.token")
    suffix = f"/?token={token}" if token else ""
    print(
        f"\n  Запуск:    http://{host}:{port}{suffix}"
        f"\n  Профили:   http://{host}:{port}/profiles{suffix}"
        f"\n  Лог:       {log_path}\n  Остановить: Ctrl+C\n"
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.reset:
            return asyncio.run(run_reset(args))
        if args.mail_probe is not None:
            return run_mail_probe(args)
        if args.probe is not None:
            return asyncio.run(run_probe(args))
        if args.run or args.check:
            return asyncio.run(run_cli(args))
        return run_web(args)          # по умолчанию — интерфейс с обеими вкладками
    except (ConfigError, LoaderError) as exc:
        print(f"\nОшибка входных данных: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
