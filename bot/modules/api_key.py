"""Модуль 2: получение API-ключа CSFloat.

Заглушка. Контракт с runner'ом уже соблюдён: к моменту вызова браузер поднят,
cookies CSFloat подняты из state/<login>.json, логи и артефакты настроены,
статус пишется в results.csv отдельной строкой (module=api_key).

Когда дойдём до модуля 2, сюда добавится примерно это:
    cs = CsFloatPage(await ctx.session.page("csfloat"), ctx)
    async with ctx.step("csfloat_open"): await cs.open_home()
    async with ctx.step("api_key_generate"): key = await cs.generate_api_key()
    ctx.data["api_key"] = key   # runner положит его в results/выгрузку
"""
from __future__ import annotations

from ..errors import NotImplementedYet
from .base import register


@register("api_key")
class ApiKeyModule:
    name = "api_key"

    async def run(self, ctx) -> None:
        async with ctx.step("api_key_stub", "модуль 2 ещё не реализован"):
            raise NotImplementedYet("модуль api_key будет добавлен отдельно")
