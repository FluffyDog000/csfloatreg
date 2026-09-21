"""Steam OpenID: логин, пароль, код Steam Guard из maFile."""
from __future__ import annotations

import asyncio

from ..errors import (
    BadCredentials,
    SteamEmailCodeRequired,
    SteamLocked,
    SteamMobileConfirmRequired,
    SteamRateLimited,
    StepTimeout,
    UnexpectedState,
)
from .base import PageHelper


class SteamLoginPage(PageHelper):
    """Работает на любой странице входа Steam: и в попапе OpenID, и в той же вкладке."""

    async def perform(self, account, mafile, steam_time, success_markers: list[str]) -> None:
        sel = self.ctx.sel
        await self.check_captcha("steam_login")

        self.log.info("Ввожу учётные данные Steam")
        await self.fill(sel("steam.username"), account.login, "поле логина Steam")
        await self.fill(sel("steam.password"), account.password, "поле пароля Steam")
        await self.click(sel("steam.submit"), "кнопку входа Steam")
        await self.settle(1.5)

        state = await self.wait_any(
            {
                "guard_boxes": sel("steam.guard_boxes"),
                "guard_single": sel("steam.guard_single"),
                "bad_credentials": sel("steam.bad_credentials"),
                "rate_limited": sel("steam.rate_limited"),
                "locked": sel("steam.locked"),
                "email_code": sel("steam.email_code"),
                "mobile_confirm": sel("steam.mobile_confirm"),
                "captcha": sel("captcha.markers", required=False),
                "success": success_markers,
            },
            timeout=60,
        )
        await self._raise_on_bad_state(state)

        if state == "success":
            self.log.info("Steam пустил без запроса кода (сессия жива)")
            return

        await self._enter_guard_code(mafile, steam_time, success_markers)

    # ── Steam Guard ──────────────────────────────────────────
    async def _enter_guard_code(self, mafile, steam_time, success_markers: list[str]) -> None:
        sel = self.ctx.sel
        if mafile is None or not mafile.shared_secret:
            raise UnexpectedState("Steam запросил код, но shared_secret недоступен")

        for attempt in (1, 2):
            code, lifetime = steam_time.fresh_code(mafile.shared_secret, min_lifetime=7)
            self.log.info("Код Steam Guard сгенерирован (живёт ещё %.0f c), попытка %d", lifetime, attempt)

            boxes = self.page.locator(sel("steam.guard_boxes")[0])
            count = 0
            try:
                count = await boxes.count()
            except Exception:  # noqa: BLE001
                count = 0

            if count >= len(code):
                for index, char in enumerate(code):
                    await boxes.nth(index).fill(char)
                    await asyncio.sleep(0.12)
            else:
                field = await self.require(sel("steam.guard_single"), "поле кода Steam Guard", timeout=10)
                await field.click()
                await self.type_text(field, code)
                await self.click(sel("steam.guard_submit"), "кнопку подтверждения кода", optional=True)

            await self.settle(2.0)
            state = await self.wait_any(
                {
                    "success": success_markers,
                    "bad_code": sel("steam.guard_boxes") + sel("steam.guard_single"),
                    "locked": sel("steam.locked"),
                    "rate_limited": sel("steam.rate_limited"),
                    "captcha": sel("captcha.markers", required=False),
                },
                timeout=40,
            )
            await self._raise_on_bad_state(state)
            if state == "success":
                self.log.info("Steam Guard пройден")
                return

            # поле кода всё ещё на экране: скорее всего код протух на границе окна
            if attempt == 1:
                self.log.warning("Код не принят, жду следующее 30-секундное окно")
                await asyncio.sleep(max(2.0, lifetime))
                continue
            raise UnexpectedState("Steam не принял код Steam Guard дважды подряд")

        raise StepTimeout("не удалось пройти Steam Guard")

    async def _raise_on_bad_state(self, state: str) -> None:
        if state == "bad_credentials":
            raise BadCredentials("Steam: неверный логин или пароль")
        if state == "locked":
            raise SteamLocked("Steam: аккаунт заблокирован или отключён")
        if state == "rate_limited":
            raise SteamRateLimited("Steam: слишком много попыток входа с этого IP")
        if state == "email_code":
            raise SteamEmailCodeRequired("Steam требует код с почты (нет мобильного аутентификатора)")
        if state == "mobile_confirm":
            raise SteamMobileConfirmRequired("Steam требует подтверждение в мобильном приложении")
        if state == "captcha":
            await self.check_captcha("steam_login")
