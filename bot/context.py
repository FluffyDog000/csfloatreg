"""AccountContext — всё, что нужно модулю для работы с одним аккаунтом.

Модуль 2 (API-ключ) получит ровно такой же контекст: браузер уже поднят,
cookies CSFloat восстановлены из state/, логи и артефакты настроены.
"""
from __future__ import annotations

import contextlib
import dataclasses
import time
from typing import Any

from .errors import BotError, StepTimeout
from .models import Bundle


@dataclasses.dataclass
class AccountContext:
    bundle: Bundle
    cfg: Any
    selectors: dict
    log: Any
    session: Any                # BrowserSession
    results: Any                # ResultsStore
    artifacts: Any              # ArtifactStore
    steam_time: Any             # SteamTime
    solver: Any                 # CaptchaSolver
    debugger: Any = None        # Debugger | None
    module: str = ""
    stage: str = ""
    attempt: int = 1
    data: dict = dataclasses.field(default_factory=dict)  # обмен между модулями

    # ── ярлыки ───────────────────────────────────────────────
    @property
    def account(self):
        return self.bundle.account

    @property
    def proxy(self):
        return self.bundle.proxy

    @property
    def mafile(self):
        return self.bundle.mafile

    @property
    def login(self) -> str:
        return self.bundle.account.login

    def sel(self, dotted: str, *, required: bool = True) -> list[str]:
        from .config import selector

        return selector(self.selectors, dotted, required=required)

    # ── шаг модуля ───────────────────────────────────────────
    @contextlib.asynccontextmanager
    async def step(self, name: str, title: str = ""):
        """Логирует шаг, держит паузу в debug-режиме, дампит артефакты при падении."""
        self.stage = name
        started = time.monotonic()
        self.log.info("→ %s%s", name, f" — {title}" if title else "")
        if self.debugger is not None:
            await self.debugger.before(self, name, title)
        try:
            yield self
        except BotError as exc:
            exc.stage = exc.stage or name
            await self.dump(name, note=f"{type(exc).__name__}: {exc}")
            self.log.error("✗ %s: %s", name, exc)
            raise
        except Exception as exc:  # noqa: BLE001 — неизвестное падение тоже должно оставить следы
            await self.dump(name, note=f"{type(exc).__name__}: {exc}")
            self.log.exception("✗ %s: непредвиденная ошибка", name)
            raise
        else:
            self.log.debug("✓ %s (%.1f c)", name, time.monotonic() - started)
            if self.debugger is not None:
                await self.debugger.after(self, name)

    async def dump(self, tag: str, *, note: str = "", debug: bool = False) -> list:
        """Скриншот + HTML всех открытых страниц аккаунта."""
        saved = []
        pages = getattr(self.session, "_pages", {}) or {}
        for name, page in list(pages.items()):
            saved += await self.artifacts.dump(
                page, self.login, f"{self.module}_{tag}_{name}", debug=debug, note=note
            )
        if not pages:
            self.log.debug("Дамп пропущен: открытых страниц нет")
        elif saved:
            self.log.info("Артефакты: %s", ", ".join(p.name for p in saved))
        return saved

    def deadline(self, seconds: float | None = None) -> float:
        return time.monotonic() + (seconds or self.cfg.get("timeouts.step_ms", 180000) / 1000)

    @staticmethod
    def expired(deadline: float, what: str = "шаг") -> None:
        if time.monotonic() > deadline:
            raise StepTimeout(f"{what}: истёк таймаут")
