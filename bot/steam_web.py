"""Сессия Steam, построенная из maFile, — как у SDA, без открытого браузера.

Подтверждения не требуют браузера: мобильному приложению хватает токенов, а
они либо лежат в maFile (блок Session), либо получаются мобильным входом по
логину, паролю и коду Steam Guard. Из токена собираются те же cookies, что
ставит приложение, и Steam уже не считает сессию «немобильной».

Запросы идут через прокси аккаунта: Steam не должен видеть, что аккаунт вдруг
сменил страну.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .logging_setup import get_logger
from .proxy_relay import maybe_relay
from .steam_auth import AuthError, login as mobile_login, refresh_access

#: Мобильное приложение Steam — им и представляемся.
UA = "Mozilla/5.0 (Linux; Android 13; SM-S901B) AppleWebKit/537.36 (KHTML, like Gecko) " \
     "Chrome/119.0.0.0 Mobile Safari/537.36 Valve Steam App"

COOKIE_DOMAINS = (".steamcommunity.com", ".steampowered.com")

#: Обновляем access_token заранее: на границе срока Steam уже капризничает.
EXPIRY_SLACK_S = 600


def token_expiry(token: str) -> int:
    """Срок жизни JWT из Steam. 0 — разобрать не вышло."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return 0
    chunk = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(chunk))
    except Exception:  # noqa: BLE001 — чужой формат не наша беда
        return 0
    return int(payload.get("exp") or 0)


def token_alive(token: str, *, slack: int = EXPIRY_SLACK_S) -> bool:
    expiry = token_expiry(token)
    return bool(token) and (expiry == 0 or expiry - slack > time.time())


class SteamWeb:
    """Один Playwright на процесс, по контексту запросов на аккаунт."""

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or get_logger()
        self._pw = None
        self.domains = COOKIE_DOMAINS        # подменяется в тестах на локальный Steam
        self._contexts: dict[str, object] = {}
        self._relays: dict[str, object] = {}

    # ── токены ───────────────────────────────────────────────
    def tokens_path(self, login: str) -> Path:
        return Path(self.cfg.path_for("state")) / f"{login}.tokens.json"

    def saved_tokens(self, login: str) -> dict:
        path = self.tokens_path(login)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_tokens(self, login: str, data: dict) -> None:
        path = self.tokens_path(login)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def possible(self, mafile, account=None) -> bool:
        """Можно ли вообще обойтись без браузера для этого аккаунта."""
        if mafile is None or not mafile.steam_id:
            return False
        if mafile.refresh_token or mafile.access_token:
            return True
        return bool(account and account.password and mafile.shared_secret)

    def why_not(self, mafile, account=None) -> str:
        if mafile is None:
            return "нет maFile"
        if not mafile.steam_id:
            return "в maFile нет steamid"
        return (
            "в maFile нет Session.RefreshToken, а войти самому нечем: "
            "нужен пароль в accounts.txt и shared_secret в maFile"
        )

    # ── контексты ────────────────────────────────────────────
    async def _engine(self):
        if self._pw is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
        return self._pw

    async def _bare_context(self, login: str, proxy):
        """Контекст без cookies — на нём добываем токены."""
        engine = await self._engine()
        options = {"extra_http_headers": {"User-Agent": UA}, "ignore_https_errors": False}
        if proxy is not None:
            proxy_cfg, relay = await maybe_relay(proxy, self.cfg, self.log)
            options["proxy"] = proxy_cfg
            if relay is not None:
                old = self._relays.pop(f"{login}:auth", None)
                if old is not None:
                    await old.stop()
                self._relays[f"{login}:auth"] = relay
        return await engine.request.new_context(**options)

    async def tokens_for(self, login: str, mafile, proxy, account=None, *, force: bool = False) -> dict:
        """Живые токены сессии: из файла, из maFile или новым входом."""
        saved = {} if force else self.saved_tokens(login)
        access = saved.get("access_token") or (mafile.access_token if not force else "")
        refresh = saved.get("refresh_token") or mafile.refresh_token
        steam_id = str(saved.get("steam_id") or mafile.steam_id or "")

        if access and token_alive(access) and not force:
            return {"access_token": access, "refresh_token": refresh, "steam_id": steam_id}

        timeout = int(self.cfg.get("timeouts.action_ms", 20000))
        context = await self._bare_context(login, proxy)
        try:
            if refresh and token_alive(refresh, slack=0):
                access = await refresh_access(
                    context, refresh_token=refresh, steam_id=steam_id, timeout_ms=timeout
                )
                self.log.info("[%s] токен Steam обновлён по refresh_token", login)
            else:
                if account is None or not account.password:
                    raise AuthError(self.why_not(mafile, account))
                from .steam_guard import SteamTime

                steam_time = getattr(self, "steam_time", None) or SteamTime("", enabled=False)
                code, _ = steam_time.fresh_code(mafile.shared_secret, min_lifetime=7)
                self.log.info("[%s] вхожу в Steam как мобильное приложение", login)
                got = await mobile_login(
                    context, account_name=mafile.account_name or login,
                    password=account.password, code=code, timeout_ms=timeout,
                )
                access, refresh = got["access_token"], got["refresh_token"]
                steam_id = got["steam_id"] or steam_id
        finally:
            await context.dispose()

        tokens = {"access_token": access, "refresh_token": refresh, "steam_id": steam_id,
                  "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.save_tokens(login, tokens)
        return tokens

    def _cookies(self, tokens: dict) -> list[dict]:
        """Те же cookies, что ставит мобильное приложение Steam."""
        value = f"{tokens['steam_id']}%7C%7C{tokens['access_token']}"
        session_id = base64.b16encode(tokens["steam_id"].encode()[:12]).decode().lower()[:24]
        pairs = (
            ("steamLoginSecure", value),
            ("sessionid", session_id),
            ("mobileClientVersion", "777777 3.6.4"),
            ("mobileClient", "android"),
            ("Steam_Language", "english"),
            ("dob", ""),
        )
        # Playwright требует полный набор полей, иначе отказывается принимать cookie
        return [
            {"name": name, "value": value, "domain": domain, "path": "/",
             "expires": -1, "httpOnly": False, "secure": domain.endswith("com"),
             "sameSite": "Lax"}
            for domain in self.domains
            for name, value in pairs
        ]

    async def context_for(self, login: str, mafile, proxy, account=None, *, force: bool = False):
        """Запросы от имени аккаунта: cookies мобильного приложения, прокси аккаунта."""
        if not force and login in self._contexts:
            return self._contexts[login]
        await self.close_one(login)

        tokens = await self.tokens_for(login, mafile, proxy, account, force=force)
        if not tokens.get("access_token"):
            raise AuthError("Steam не дал access_token")

        engine = await self._engine()
        options = {
            "extra_http_headers": {"User-Agent": UA},
            "storage_state": {"cookies": self._cookies(tokens), "origins": []},
        }
        if proxy is not None:
            proxy_cfg, relay = await maybe_relay(proxy, self.cfg, self.log)
            options["proxy"] = proxy_cfg
            if relay is not None:
                self._relays[login] = relay
        context = await engine.request.new_context(**options)
        self._contexts[login] = context
        return context

    async def close_one(self, login: str) -> None:
        context = self._contexts.pop(login, None)
        if context is not None:
            try:
                await context.dispose()
            except Exception:  # noqa: BLE001
                pass
        for key in (login, f"{login}:auth"):
            relay = self._relays.pop(key, None)
            if relay is not None:
                try:
                    await relay.stop()
                except Exception:  # noqa: BLE001
                    pass

    async def close(self) -> None:
        for login in list(self._contexts):
            await self.close_one(login)
        for key in list(self._relays):
            relay = self._relays.pop(key)
            try:
                await relay.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
