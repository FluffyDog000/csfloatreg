"""Вход в Steam внутри уже открытого профиля.

Cookies живут не вечно: аккаунт, который вчера торговал, сегодня для Steam
чужой. Раньше это упиралось в «сессия профиля истекла» и требовало человека —
теперь бот входит заново сам: логином и паролем из accounts.txt и кодом
Steam Guard из maFile.

Проверка нарочно не гадает по разметке: открываем /my/ и смотрим, куда Steam
нас увёл. Залогинен — в профиль, нет — на страницу входа. Третьего нет.
"""
from __future__ import annotations

from .errors import UnexpectedState
from .logging_setup import get_logger
from .pages.steam_login import SteamLoginPage

STEAM = "https://steamcommunity.com"
WHOAMI = f"{STEAM}/my/"
LOGIN_URL = f"{STEAM}/login/home/?goto=my%2Fprofile"

#: Мы на странице своего профиля — значит вход состоялся.
SUCCESS_MARKERS = [r"url:^https?://steamcommunity\.com/(id|profiles)/"]

#: Отдельная вкладка в том же профиле: рабочую страницу csfloat не трогаем.
PAGE = "steam"


def on_login_page(url: str) -> bool:
    return "/login" in (url or "").lower()


async def whoami(page, *, timeout_ms: int) -> str:
    await page.goto(WHOAMI, wait_until="domcontentloaded", timeout=timeout_ms)
    return page.url


async def ensure_login(manager, login: str, *, headful: bool | None = None, force: bool = False) -> dict:
    """Гарантирует живую сессию Steam в профиле аккаунта.

    force=True — входить, не спрашивая: так вызывают после отказа Steam, когда
    проверять состояние уже поздно.
    """
    account = manager.accounts.get(login)
    if account is None:
        raise UnexpectedState(f"{login} нет в accounts.txt — нечем входить в Steam")

    log = get_logger(login)
    if login not in manager.sessions:
        await manager.open_profile(login, headful=headful)
    session = manager.sessions[login]
    page = await session.page(PAGE)
    timeout = int(manager.cfg.get("timeouts.page_load_ms", 60000))

    if not force:
        url = await whoami(page, timeout_ms=timeout)
        if not on_login_page(url):
            log.info("Steam помнит аккаунт: %s", url[:90])
            return {"logged_in": True, "relogin": False, "url": url}
        log.warning("Сессия Steam истекла — вхожу заново")
    else:
        log.info("Вхожу в Steam заново")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
    helper = SteamLoginPage(page, manager.page_context(login, session, log), name="steam")
    await helper.perform(account, manager.mafiles.get(login.lower()), manager.steam_time, SUCCESS_MARKERS)

    url = await whoami(page, timeout_ms=timeout)
    if on_login_page(url):
        raise UnexpectedState("после ввода логина Steam снова показывает страницу входа")
    log.info("Вход в Steam выполнен: %s", url[:90])
    return {"logged_in": True, "relogin": True, "url": url}
