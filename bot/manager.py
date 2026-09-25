"""Менеджер профилей: пул прокси, запуск браузеров, данные аккаунта под рукой.

Автоматизации здесь нет: бот поднимает изолированный профиль с нужным прокси
и отпечатком, а всё остальное делает человек. Задача менеджера — чтобы под
рукой были логин, пароль, почта и живой код Steam Guard.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .bindings import BindingStore
from .browser import BrowserSession
from .captcha import build_solver
from .context import AccountContext
from .confirmations import Confirmation, ConfirmationError
from .confirmations import fetch as fetch_confirmations
from .confirmations import respond as respond_confirmations
from .loader import load_accounts, load_mafiles, load_mails, load_proxies
from .logging_setup import get_logger
from .mailbox import attach_mailboxes, mail_source, mail_stats, replace_mailbox
from .models import Account, Bundle, MaFile, Mailbox, Proxy
from .steam_guard import seconds_until_next_code
from .storage import StateStore


def mask_mail(mail: str) -> str:
    if "@" not in mail:
        return mail
    name, domain = mail.split("@", 1)
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}{'*' * max(3, len(name) - len(head))}@{domain}"


class ProfileManager:
    def __init__(self, cfg, steam_time, selectors: dict | None = None):
        self.cfg = cfg
        self.steam_time = steam_time
        self.log = get_logger()
        self._selectors = selectors
        cfg.ensure_dirs()

        self.state = StateStore(cfg.path_for("state"), cfg.path_for("profiles"))
        self.bindings = BindingStore(cfg.path_for("data") / "bindings.json")

        self.accounts: dict[str, Account] = {}
        self.mafiles: dict[str, MaFile] = {}
        self.pool: list[Proxy] = []
        self.mails: list[Mailbox] = []
        self.load_error: str | None = None

        self.sessions: dict[str, BrowserSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._confs: dict[str, dict[str, Confirmation]] = {}   # последний список подтверждений
        self.reload_inputs()

    # ── входные данные ───────────────────────────────────────
    def reload_inputs(self) -> None:
        try:
            self.accounts = {a.login: a for a in load_accounts(self.cfg.path_for("accounts"))}
            self.pool = load_proxies(
                self.cfg.path_for("proxies"), default_scheme=self.cfg.get("proxy.default_scheme", "http")
            )
            self.mafiles = load_mafiles(self.cfg.path_for("mafiles"))
            self.mails = (
                load_mails(self.cfg.path_for("mails")) if mail_source(self.cfg) != "accounts" else []
            )
            self.load_error = None
        except Exception as exc:  # noqa: BLE001 — покажем текст в интерфейсе
            self.load_error = str(exc)
            return
        self.ensure_bindings()
        attach_mailboxes(
            self.cfg, list(self.accounts.values()),
            bindings=self.bindings, pool=self.mails, log=self.log,
        )

    def ensure_bindings(self) -> int:
        """Каждому аккаунту — свой прокси из пула. Уже выданные не трогаем."""
        pool = [p.raw for p in self.pool]
        assigned = 0
        for login in self.accounts:
            current = self.bindings.proxy_of(login)
            if current and not self.bindings.is_bad(current):
                continue
            free = self.bindings.free_proxy(pool)
            if free is None:
                break
            if current:
                self.bindings.remember_history(login, current, "прокси помечен плохим")
            self.bindings.bind(login, free)
            assigned += 1
        if assigned:
            self.log.info("Выдано прокси: %d", assigned)
        return assigned

    # ── прокси ───────────────────────────────────────────────
    def proxy_for(self, login: str) -> Proxy | None:
        raw = self.bindings.proxy_of(login)
        if not raw:
            return None
        for proxy in self.pool:
            if proxy.raw == raw:
                return proxy
        return None    # строку удалили из файла

    def replace_proxy(self, login: str, *, mark_bad: bool = True) -> dict:
        """Заменить прокси аккаунта. Старый уходит в плохие и больше не выдаётся."""
        if login in self.sessions:
            raise RuntimeError("сначала закрой профиль этого аккаунта")

        # сначала ищем замену: если её нет, текущий прокси должен остаться рабочим
        free = self.bindings.free_proxy([p.raw for p in self.pool])
        if free is None:
            raise RuntimeError("в пуле не осталось свободных прокси")

        current = self.bindings.proxy_of(login)
        if current and mark_bad:
            self.bindings.mark_bad(current)
            self.bindings.remember_history(login, current, "заменён вручную")
        self.bindings.bind(login, free)
        proxy = self.proxy_for(login)
        self.log.info("[%s] прокси заменён на %s", login, proxy.safe() if proxy else free)
        return {"proxy": proxy.safe() if proxy else free}

    def replace_proxies(self, logins: list[str], *, mark_bad: bool = True) -> dict:
        """Смена прокси пачкой. Один сломавшийся аккаунт не отменяет остальные."""
        done, failed = [], []
        for login in logins:
            try:
                result = self.replace_proxy(login, mark_bad=mark_bad)
            except Exception as exc:  # noqa: BLE001 — причина нужна по каждому аккаунту
                failed.append({"login": login, "error": str(exc)})
            else:
                done.append({"login": login, "proxy": result["proxy"]})
        self.log.info("Прокси заменены: %d, не получилось: %d", len(done), len(failed))
        return {"done": done, "failed": failed}

    def pool_stats(self) -> dict:
        used = self.bindings.used_proxies()
        bad = set(self.bindings.data["bad_proxies"])
        total = len(self.pool)
        raws = {p.raw for p in self.pool}
        return {
            "total": total,
            "used": len(used & raws),
            "bad": len(bad & raws),
            "free": len([r for r in raws if r not in used and r not in bad]),
        }

    # ── почты ────────────────────────────────────────────────
    def mail_stats(self) -> dict:
        if not self.mails:
            return {"total": 0, "used": 0, "bad": 0, "free": 0}
        return mail_stats(self.bindings, self.mails)

    def replace_mail(self, login: str, *, mark_bad: bool = True) -> dict:
        """Заменить почту аккаунта. Старая уходит в плохие и больше не выдаётся."""
        if login not in self.accounts:
            raise KeyError(login)
        if mail_source(self.cfg) == "accounts":
            raise RuntimeError("почта берётся из accounts.txt (mail.source: accounts) — заменять нечего")
        box = replace_mailbox(login, self.bindings, self.mails, mark_bad=mark_bad)
        account = self.accounts[login]
        account.mail, account.mail_password = box.address, box.password
        self.log.info("[%s] почта заменена на %s", login, box.address)
        return {"mail": box.address}

    # ── Steam Guard ──────────────────────────────────────────
    def guard(self, login: str) -> dict:
        mafile = self.mafiles.get(login.lower())
        if mafile is None or not mafile.shared_secret:
            return {"code": None, "seconds_left": 0, "reason": "нет maFile"}
        now = self.steam_time.now()
        return {
            "code": self.steam_time.code(mafile.shared_secret),
            "seconds_left": round(seconds_until_next_code(now), 1),
            "reason": None,
        }

    # ── данные аккаунта ──────────────────────────────────────
    def rows(self) -> list[dict]:
        rows = []
        for login, account in self.accounts.items():
            entry = self.bindings.entry(login)
            proxy = self.proxy_for(login)
            raw = self.bindings.proxy_of(login)
            rows.append(
                {
                    "login": login,
                    "mail": mask_mail(account.mail),
                    "proxy": proxy.safe() if proxy else None,
                    "proxy_missing": bool(raw) and proxy is None,
                    "mail_missing": not account.mail,
                    "has_mafile": bool(self.mafiles.get(login.lower(), MaFile("", "")).shared_secret),
                    "status": entry.get("status", "new"),
                    "note": entry.get("note", ""),
                    "trade_url": entry.get("trade_url", ""),
                    "opened": login in self.sessions,
                    "profile_exists": (self.cfg.path_for("profiles") / login).exists(),
                }
            )
        return rows

    def card(self, login: str) -> dict:
        """Полные данные для панели справа — вместе с паролями, это и есть смысл."""
        account = self.accounts.get(login)
        if account is None:
            raise KeyError(login)
        proxy = self.proxy_for(login)
        entry = self.bindings.entry(login)
        mafile = self.mafiles.get(login.lower())
        return {
            "login": account.login,
            "password": account.password,
            "mail": account.mail,
            "mail_password": account.mail_password,
            "proxy": proxy.safe() if proxy else None,
            "proxy_raw": self.bindings.proxy_of(login),
            "mail_from_pool": mail_source(self.cfg) != "accounts",
            "status": entry.get("status", "new"),
            "note": entry.get("note", ""),
            "trade_url": entry.get("trade_url", ""),
            "steam_id": mafile.steam_id if mafile else "",
            "has_mafile": bool(mafile and mafile.shared_secret),
            "can_confirm": bool(mafile and mafile.identity_secret and mafile.steam_id),
            "opened": login in self.sessions,
            "guard": self.guard(login),
        }

    def set_status(self, login: str, status: str, note: str = "") -> None:
        self.bindings.set_status(login, status, note)

    def set_trade_url(self, login: str, url: str) -> str:
        """Трейд-ссылку вбивает человек, наше дело — проверить и запомнить."""
        if login not in self.accounts:
            raise KeyError(login)
        url = (url or "").strip()
        if url and "tradeoffer/new" not in url:
            raise ValueError("это не похоже на трейд-ссылку: в ней должно быть tradeoffer/new")
        self.bindings.set_field(login, "trade_url", url)
        self.log.info("[%s] трейд-ссылка %s", login, "сохранена" if url else "очищена")
        return url

    # ── подтверждения Steam (как в SDA) ──────────────────────
    async def _mobile(self, login: str):
        """Запросы к mobileconf идут через cookies открытого профиля."""
        session = self.sessions.get(login)
        if session is None:
            raise ConfirmationError("открой профиль: подтверждения берутся из его сессии Steam")
        mafile = self.mafiles.get(login.lower())
        if mafile is None:
            raise ConfirmationError("нет maFile для этого аккаунта")
        context = await session.context("main")
        return context.request, mafile

    async def request_for(self, login: str, *, headful: bool | None = None):
        """Запросы от имени аккаунта: поднимет профиль, если он ещё не открыт."""
        if login not in self.sessions:
            await self.open_profile(login, headful=headful)
        session = self.sessions[login]
        context = await session.context("main")
        return context

    # ── вход в Steam ─────────────────────────────────────────
    @property
    def selectors(self) -> dict:
        """Те же selectors.yaml, что и у очереди: правки действуют на оба режима."""
        if self._selectors is None:
            from .config import load_selectors

            self._selectors = load_selectors(self.cfg.get("paths.selectors", "selectors.yaml"))
        return self._selectors

    def page_context(self, login: str, session, log=None) -> AccountContext:
        """Контекст страницы для кода, написанного под очередь: тот же набор данных."""
        bundle = Bundle(
            account=self.accounts[login],
            proxy=self.proxy_for(login),
            mafile=self.mafiles.get(login.lower()),
        )
        return AccountContext(
            bundle=bundle, cfg=self.cfg, selectors=self.selectors, log=log or get_logger(login),
            session=session, results=None, artifacts=None, steam_time=self.steam_time,
            solver=build_solver(self.cfg), bindings=self.bindings, module="manager",
        )

    async def ensure_steam_login(self, login: str, *, headful: bool | None = None,
                                 force: bool = False) -> dict:
        """Steam должен помнить аккаунт. Не помнит — бот входит сам."""
        from .steam_session import ensure_login

        async with self._lock(f"steam:{login}"):
            return await ensure_login(self, login, headful=headful, force=force)

    async def confirmations(self, login: str) -> list[dict]:
        request, mafile = await self._mobile(login)
        timeout = int(self.cfg.get("timeouts.action_ms", 20000))
        items = await fetch_confirmations(request, mafile, self.steam_time, timeout_ms=timeout)
        self._confs[login] = {item.id: item for item in items}
        self.log.info("[%s] подтверждений: %d", login, len(items))
        return [item.as_dict() for item in items]

    async def respond_confirmation(self, login: str, ids: list[str], *, accept: bool) -> dict:
        request, mafile = await self._mobile(login)
        known = self._confs.get(login) or {}
        if any(cid not in known for cid in ids):
            await self.confirmations(login)          # список устарел — перечитаем
            known = self._confs.get(login) or {}
        items = [known[cid] for cid in ids if cid in known]
        if not items:
            raise ConfirmationError("этих подтверждений больше нет — обнови список")

        timeout = int(self.cfg.get("timeouts.action_ms", 20000))
        result = await respond_confirmations(
            request, mafile, self.steam_time, items, accept=accept, timeout_ms=timeout
        )
        for item in items:
            known.pop(item.id, None)
        self.log.info("[%s] %s подтверждений: %d", login, "принято" if accept else "отклонено", len(items))
        return result

    # ── профили ──────────────────────────────────────────────
    def _lock(self, login: str) -> asyncio.Lock:
        return self._locks.setdefault(login, asyncio.Lock())

    async def open_profile(self, login: str, *, headful: bool | None = None) -> dict:
        """headful=None — как в конфиге; рассылка поднимает профили в фоне."""
        async with self._lock(login):
            if login in self.sessions:
                return {"opened": True, "already": True}
            if login not in self.accounts:
                raise KeyError(login)
            proxy = self.proxy_for(login)
            if proxy is None:
                raise RuntimeError("аккаунту не выдан прокси (пул пуст или строка удалена)")

            log = get_logger(login)
            session = BrowserSession(
                login, proxy, self.cfg, self.state, log,
                headful=True if headful is None else headful,
            )
            await session.start()
            page = await session.page("main")

            start_url = self.cfg.get("browser.start_url")
            if start_url:
                try:
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:  # noqa: BLE001 — стартовая страница не критична
                    log.warning("Стартовая страница не открылась: %s", exc)

            self.sessions[login] = session
            self._watch(login, session)
            self.bindings.entry(login)["opened_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.bindings.save()
            log.info("Профиль открыт")
            return {"opened": True, "proxy": proxy.safe()}

    def _watch(self, login: str, session: BrowserSession) -> None:
        """Пользователь закрывает окно сам — менеджер должен это заметить."""
        target = session._persistent or session.browser
        if target is None:
            return

        def on_close(*_):
            self.sessions.pop(login, None)
            self.log.info("[%s] профиль закрыт пользователем", login)

        try:
            target.on("close" if session._persistent else "disconnected", on_close)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("Не удалось отследить закрытие профиля: %s", exc)

    async def close_profile(self, login: str) -> dict:
        async with self._lock(login):
            session = self.sessions.pop(login, None)
            self._confs.pop(login, None)
            if session is None:
                return {"closed": False}
            await session.close()
            get_logger(login).info("Профиль закрыт")
            return {"closed": True}

    async def close_all(self) -> None:
        for login in list(self.sessions):
            try:
                await self.close_profile(login)
            except Exception:  # noqa: BLE001
                pass
