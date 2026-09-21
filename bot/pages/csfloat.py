"""CSFloat: вход через Steam, привязка почты, проверка подтверждения."""
from __future__ import annotations

import asyncio
import re

from ..errors import StepTimeout, UnexpectedState
from .base import PageHelper
from .steam_login import SteamLoginPage


class CsFloatPage(PageHelper):
    def __init__(self, page, ctx):
        super().__init__(page, ctx, name="csfloat")
        self.base_url = ctx.cfg.get("csfloat.base_url", "https://csfloat.com").rstrip("/")
        self.settings_url = ctx.cfg.get("csfloat.settings_url") or f"{self.base_url}/profile/settings"

    # ── сессия ───────────────────────────────────────────────
    async def open_home(self) -> None:
        await self.goto(self.base_url + "/")
        await self.settle(1.5)
        await self.click(self.ctx.sel("csfloat.cookie_accept", required=False), "баннер cookies", optional=True)
        await self.check_captcha("csfloat_home")

    async def is_logged_in(self) -> bool:
        for candidate in self.ctx.sel("csfloat.logged_in"):
            if await self.matches(candidate, timeout=1500):
                return True
        return False

    async def login_via_steam(self, account, mafile, steam_time) -> None:
        """Клик по «войти через Steam» + прохождение OpenID (в попапе или в той же вкладке)."""
        sel = self.ctx.sel
        context = self.page.context
        popup: list = []
        context.on("page", lambda page: popup.append(page))

        await self.click(sel("csfloat.sign_in_button"), "кнопку входа через Steam")
        target = await self._resolve_steam_page(popup)

        steam = SteamLoginPage(target, self.ctx, name="steam")
        success_markers = sel("csfloat.logged_in") + [f"url:{re.escape(self._host())}"]
        await steam.perform(account, mafile, steam_time, success_markers)

        # OpenID иногда показывает промежуточную кнопку подтверждения
        await steam.click(sel("steam.openid_signin_button", required=False), "подтверждение OpenID", optional=True)

        if target is not self.page:
            for _ in range(30):
                if target.is_closed():
                    break
                await asyncio.sleep(0.5)
            await self.page.reload(wait_until="domcontentloaded")

        await self.settle(2.0)
        if not await self.is_logged_in():
            await self.open_home()
            if not await self.is_logged_in():
                raise UnexpectedState("после редиректа со Steam CSFloat не считает нас залогиненными")
        self.log.info("CSFloat: вход выполнен")

    async def _resolve_steam_page(self, popup: list):
        """Steam может открыться в новой вкладке или в текущей."""
        sel = self.ctx.sel
        for _ in range(24):
            if popup:
                page = popup[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                self.log.debug("Steam открылся в новой вкладке: %s", page.url)
                self.ctx.session._pages["steam_popup"] = page
                return page
            for candidate in sel("steam.openid_page"):
                if await self.matches(candidate, timeout=200):
                    self.log.debug("Steam открылся в текущей вкладке: %s", self.page.url)
                    return self.page
            await asyncio.sleep(0.5)
        raise StepTimeout("страница входа Steam так и не открылась")

    def _host(self) -> str:
        return self.base_url.split("://", 1)[-1]

    # ── почта в настройках ───────────────────────────────────
    async def open_settings(self) -> None:
        await self.goto(self.settings_url)
        await self.settle(1.5)
        await self.check_captcha("csfloat_settings")

    async def email_state(self) -> str:
        """'verified' | 'pending' | 'none' — чтобы не слать письмо повторно без нужды."""
        for candidate in self.ctx.sel("csfloat.email_verified"):
            if await self.matches(candidate, timeout=800):
                return "verified"
        for candidate in self.ctx.sel("csfloat.email_pending"):
            if await self.matches(candidate, timeout=800):
                return "pending"
        return "none"

    async def set_email(self, email: str) -> None:
        sel = self.ctx.sel
        await self.click(sel("csfloat.settings_email_edit", required=False), "кнопку редактирования почты", optional=True)
        await self.fill(sel("csfloat.settings_email_input"), email, "поле почты в настройках")
        await self.click(sel("csfloat.settings_email_save"), "кнопку отправки письма")
        await self.settle(2.5)
        await self.check_captcha("csfloat_set_email")
        self.log.info("Письмо подтверждения запрошено для %s", email)

    async def open_confirmation_link(self, url: str) -> None:
        self.log.info("Открываю ссылку подтверждения в контексте CSFloat")
        await self.goto(url)
        await self.settle(2.5)
        await self.check_captcha("csfloat_confirm")

    async def wait_email_verified(self, *, attempts: int = 3) -> bool:
        for attempt in range(1, attempts + 1):
            await self.open_settings()
            state = await self.email_state()
            self.log.debug("Состояние почты в настройках: %s (попытка %d)", state, attempt)
            if state == "verified":
                return True
            await asyncio.sleep(3)
        return False
