"""Детект капчи + интерфейс решалки (сама решалка не встроена)."""
from __future__ import annotations

from typing import Protocol

from .config import selector
from .errors import CaptchaDetected


class CaptchaSolver(Protocol):
    """Точка расширения: реализуй solve() и укажи captcha.solver в config.yaml."""

    name: str

    async def solve(self, page, kind: str, payload: dict) -> str:  # pragma: no cover - интерфейс
        ...


class NullSolver:
    """По умолчанию: не решаем, помечаем аккаунт статусом captcha."""

    name = "none"

    async def solve(self, page, kind: str, payload: dict) -> str:
        raise CaptchaDetected(f"капча ({kind}), решалка не подключена")


def build_solver(cfg) -> CaptchaSolver:
    name = (cfg.get("captcha.solver") or "none").lower()
    if name in ("none", "", "null"):
        return NullSolver()
    raise NotImplementedError(
        f"captcha.solver='{name}' не реализован. Добавь класс с методом solve() "
        f"и зарегистрируй его в bot/captcha.py:build_solver"
    )


async def detect(page, selectors: dict) -> str | None:
    """Возвращает сработавший маркер капчи или None."""
    for marker in selector(selectors, "captcha.markers", required=False):
        try:
            if await page.locator(marker).first.is_visible(timeout=200):
                return marker
        except Exception:  # noqa: BLE001 — маркер может быть невалидным селектором
            continue
    return None
