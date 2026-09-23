"""Страница приватности обменов Steam: оттуда берётся трейд-ссылка.

Отдельная вкладка: вкладку CSFloat трогать нельзя, там открыт мастер Onboard,
и любой переход сбросил бы его на предыдущий шаг.
"""
from __future__ import annotations

from ..errors import UnexpectedState
from .base import PageHelper


class SteamTradePage(PageHelper):
    def __init__(self, page, ctx):
        super().__init__(page, ctx, name="steam_trade")
        self.url = ctx.cfg.get(
            "steam.trade_url_page", "https://steamcommunity.com/id/me/tradeoffers/privacy"
        )

    async def fetch_trade_url(self) -> str:
        sel = self.ctx.sel
        await self.goto(self.url)
        await self.wait_rendered(timeout=20)
        await self.settle(1.5)
        self.log.info("Страница трейд-ссылки открыта: %s", self.page.url)

        if "login" in self.page.url or "steamcommunity.com/id" not in self.page.url:
            await self.ctx.dump("steam_trade_page", note=f"неожиданный адрес: {self.page.url}")
            raise UnexpectedState(f"Steam не пустил на страницу обменов ({self.page.url[:90]})")

        field = await self.first_visible(sel("steam.trade_url_input"), timeout=15)
        if field is None:
            await self.ctx.dump("steam_trade_page", note="поле с трейд-ссылкой не найдено")
            raise UnexpectedState("не нашёл поле с трейд-ссылкой на странице Steam")

        value = (await self._value(field)) or ""
        if "tradeoffer" not in value:
            # ссылка ещё не создана — у Steam для этого отдельная кнопка
            self.log.info("Трейд-ссылки ещё нет — создаю")
            await self.click(sel("steam.trade_url_create"), "кнопку создания трейд-ссылки", optional=True)
            await self.settle(2.5)
            field = await self.require(sel("steam.trade_url_input"), "поле с трейд-ссылкой", timeout=10)
            value = (await self._value(field)) or ""

        value = value.strip()
        if "tradeoffer" not in value or "token=" not in value:
            await self.ctx.dump("steam_trade_page", note=f"непохоже на трейд-ссылку: {value[:120]}")
            raise UnexpectedState("на странице Steam нет готовой трейд-ссылки")

        self.log.info("Трейд-ссылка получена: %s…", value[:48])
        return value

    @staticmethod
    async def _value(locator) -> str:
        try:
            return await locator.input_value()
        except Exception:  # noqa: BLE001 — не input, читаем атрибут
            try:
                return (await locator.get_attribute("value")) or ""
            except Exception:  # noqa: BLE001
                return ""
