"""Запуск браузера на аккаунт.

Camoufox инжектит фингерпринт на уровне ЗАПУСКА, поэтому изоляция строится так:
    1 аккаунт = 1 прокси = 1 инстанс браузера,
а внутри него — именованные контексты (csfloat, mail) со своими cookies.

Движок переключается в config.yaml (browser.engine), API наружу одинаковый —
чтобы при желании вернуться на чистый Playwright не трогая остальной код.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import sys
import time
from functools import partial
from pathlib import Path

from .errors import NetworkError
from .models import Proxy
from .proxy_relay import SocksRelay, maybe_relay

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


#: Ключи, которые НЕ закрепляем: их должен пересчитывать geoip под IP прокси.
_GEO_PREFIXES = (
    "webrtc:", "geolocation:", "timezone", "locale:",
    "navigator.language", "headers.Accept-Language", "proxy",
)


def _config_from_launch_options(options: dict) -> dict:
    """Camoufox отдаёт итоговый отпечаток чанками в env CAMOU_CONFIG_<n>."""
    env = options.get("env") or {}
    chunks = [
        (int(key.rsplit("_", 1)[1]), value)
        for key, value in env.items()
        if key.startswith("CAMOU_CONFIG_")
    ]
    if not chunks:
        return {}
    return json.loads("".join(value for _, value in sorted(chunks)))


def _strip_geo(config: dict) -> dict:
    return {key: value for key, value in config.items() if not key.startswith(_GEO_PREFIXES)}


def _stable_random(seed: str) -> random.Random:
    """Один и тот же аккаунт должен получать один и тот же профиль между запусками."""
    return random.Random(f"csfloatreg::{seed}")


class BrowserSession:
    def __init__(self, login: str, proxy: Proxy, cfg, state_store, logger, *, headful: bool | None = None):
        self.login = login
        self.proxy = proxy
        self.cfg = cfg
        self.state = state_store
        self.log = logger
        self.headful = cfg.get("run.headful", False) if headful is None else headful

        self.browser = None
        self._persistent = None     # BrowserContext, если включён профиль на аккаунт
        self._camoufox = None
        self._playwright = None
        self._relay: SocksRelay | None = None
        self._contexts: dict[str, object] = {}
        self._pages: dict[str, object] = {}

        rng = _stable_random(login)
        viewports = cfg.get("browser.viewports") or [[1366, 768]]
        self.viewport = tuple(rng.choice(viewports))
        user_agents = cfg.get("browser.user_agents") or []
        self.user_agent = rng.choice(user_agents) if user_agents else None
        self.os_choice = rng.choice(cfg.get("browser.os") or ["windows"])
        self.persistent = bool(cfg.get("browser.persistent_profile", True)) and (
            (cfg.get("browser.engine") or "camoufox").lower() == "camoufox"
        )

    # ── запуск ───────────────────────────────────────────────
    async def start(self) -> "BrowserSession":
        proxy_cfg, self._relay = await maybe_relay(self.proxy, self.cfg, self.log)
        engine = (self.cfg.get("browser.engine") or "camoufox").lower()
        self.log.info(
            "Запуск браузера (%s%s, %s, %dx%d) через %s",
            engine, ", профиль аккаунта" if self.persistent else "",
            "headful" if self.headful else "headless",
            self.viewport[0], self.viewport[1], self.proxy.safe(),
        )
        try:
            if engine == "camoufox":
                await self._start_camoufox(proxy_cfg)
            else:
                await self._start_playwright(engine, proxy_cfg)
        except ImportError:
            raise
        except Exception as exc:  # noqa: BLE001 — падение запуска лечится повтором
            await self.close()
            raise NetworkError(f"не удалось запустить браузер: {exc}") from exc
        return self

    def _headless_mode(self):
        """На Linux без DISPLAY headful возможен только через Xvfb."""
        if not self.headful:
            return True
        virtual = str(self.cfg.get("browser.virtual_display", "auto")).lower()
        if virtual == "true":
            return "virtual"
        if virtual == "false":
            return False
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            return "virtual"
        return False

    async def _fingerprint(self) -> dict | None:
        """Отпечаток, закреплённый за аккаунтом.

        Без этого Camoufox генерирует новые seed'ы canvas/audio/fonts при каждом
        запуске, и аккаунт с живыми cookies выглядит как то же самое устройство
        только до перезапуска бота. Гео-ключи намеренно не закрепляем — их
        пересчитывает geoip под текущий IP прокси.
        """
        if not self.cfg.get("browser.pin_fingerprint", True):
            return None

        path = self.state.path(self.login, "fp")
        try:
            from importlib.metadata import version as _pkg_version

            camoufox_version = _pkg_version("camoufox")
        except Exception:  # noqa: BLE001
            camoufox_version = "unknown"

        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("camoufox") == camoufox_version and saved.get("config"):
                    self.log.debug("Отпечаток поднят из %s", path.name)
                    return saved["config"]
                self.log.info("Версия Camoufox изменилась — перегенерирую отпечаток аккаунта")
            except (OSError, ValueError) as exc:
                self.log.warning("Не читается %s (%s) — перегенерирую отпечаток", path.name, exc)

        try:
            from camoufox.utils import launch_options as camoufox_launch_options

            generated = await asyncio.to_thread(
                partial(
                    camoufox_launch_options,
                    os=self.os_choice,
                    window=self.viewport,
                    i_know_what_im_doing=True,
                )
            )
            config = _strip_geo(_config_from_launch_options(generated))
            if not config:
                raise ValueError("Camoufox не отдал CAMOU_CONFIG")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "camoufox": camoufox_version,
                        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "os": self.os_choice,
                        "window": list(self.viewport),
                        "config": config,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.log.info("Отпечаток сгенерирован и закреплён за аккаунтом (%d свойств)", len(config))
            return config
        except Exception as exc:  # noqa: BLE001 — не повод не запускать браузер
            self.log.warning("Не удалось закрепить отпечаток (%s) — Camoufox сгенерирует свой", exc)
            return None

    async def _start_camoufox(self, proxy_cfg: dict) -> None:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "camoufox не установлен. `pip install -r requirements.txt` и `python -m camoufox fetch`, "
                "либо переключи browser.engine на firefox/chromium в config.yaml"
            ) from exc

        options = {
            "headless": self._headless_mode(),
            "proxy": proxy_cfg,
            "os": self.os_choice,
            "humanize": bool(self.cfg.get("browser.humanize", True)),
            "geoip": bool(self.cfg.get("browser.geoip", True)),
            "block_images": bool(self.cfg.get("browser.block_images", True)),
            "enable_cache": bool(self.cfg.get("browser.enable_cache", True)),
            "window": self.viewport,
            "i_know_what_im_doing": True,
        }
        locale = self.cfg.get("browser.locale")
        if locale and not options["geoip"]:
            options["locale"] = locale

        fingerprint = await self._fingerprint()
        if fingerprint:
            options["config"] = fingerprint

        if self.persistent:
            profile = self.state.profile(self.login)
            options["persistent_context"] = True
            options["user_data_dir"] = str(profile)
            self.log.debug("Профиль аккаунта: %s", profile)

        self._camoufox = AsyncCamoufox(**options)
        target = await self._camoufox.__aenter__()
        if self.persistent:
            # launch_persistent_context отдаёт сразу контекст, а не браузер
            self._persistent = target
            target.set_default_timeout(self.cfg.get("timeouts.action_ms", 20000))
            target.set_default_navigation_timeout(self.cfg.get("timeouts.page_load_ms", 60000))
        else:
            self.browser = target

    async def _start_playwright(self, engine: str, proxy_cfg: dict) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, "chromium" if engine == "chromium" else "firefox")
        self.browser = await launcher.launch(headless=not self.headful, proxy=proxy_cfg)

    # ── контексты и страницы ─────────────────────────────────
    async def context(self, name: str = "csfloat"):
        if name in self._contexts:
            return self._contexts[name]
        if self._persistent is not None:
            # один профиль на аккаунт: csfloat и почта живут во вкладках одного окна,
            # как у живого человека, а не в изолированных контекстах
            self._contexts[name] = self._persistent
            return self._persistent
        if self.browser is None:
            raise RuntimeError("браузер не запущен")

        options: dict = {}
        state_path: Path = self.state.path(self.login, name)
        if state_path.exists():
            options["storage_state"] = str(state_path)
            self.log.debug("Контекст '%s': поднимаю cookies из %s", name, state_path.name)

        engine = (self.cfg.get("browser.engine") or "camoufox").lower()
        if engine != "camoufox":
            # у camoufox эти параметры уже зашиты в фингерпринт запуска
            options["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
            if self.user_agent:
                options["user_agent"] = self.user_agent
            if self.cfg.get("browser.locale"):
                options["locale"] = self.cfg.get("browser.locale")
            if self.cfg.get("browser.timezone"):
                options["timezone_id"] = self.cfg.get("browser.timezone")

        context = await self.browser.new_context(**options)
        context.set_default_timeout(self.cfg.get("timeouts.action_ms", 20000))
        context.set_default_navigation_timeout(self.cfg.get("timeouts.page_load_ms", 60000))
        if engine != "camoufox":
            await context.add_init_script(_STEALTH_JS)

        self._contexts[name] = context
        return context

    async def page(self, name: str = "csfloat"):
        if name in self._pages:
            return self._pages[name]
        context = await self.context(name)
        if self._persistent is not None:
            # первая запрошенная страница занимает стартовую вкладку, остальные — новые
            page = context.pages[0] if (not self._pages and context.pages) else await context.new_page()
        else:
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
        self._pages[name] = page
        return page

    async def save_state(self, name: str | None = None) -> None:
        if self._persistent is not None:
            # источник правды — сам профиль; cookies выгружаем рядом для отладки
            try:
                path = self.state.path(self.login, "csfloat")
                path.parent.mkdir(parents=True, exist_ok=True)
                await self._persistent.storage_state(path=str(path))
            except Exception as exc:  # noqa: BLE001
                self.log.debug("Не удалось выгрузить cookies из профиля: %s", exc)
            return

        names = [name] if name else list(self._contexts)
        for item in names:
            context = self._contexts.get(item)
            if context is None:
                continue
            try:
                path = self.state.path(self.login, item)
                path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(path))
                self.log.debug("Контекст '%s': cookies сохранены", item)
            except Exception as exc:  # noqa: BLE001 — не повод ронять аккаунт
                self.log.warning("Не удалось сохранить cookies контекста '%s': %s", item, exc)

    # ── завершение ───────────────────────────────────────────
    async def close(self) -> None:
        try:
            await self.save_state()
        except Exception:  # noqa: BLE001
            pass
        for context in list(self._contexts.values()):
            if context is self._persistent:
                continue  # закроется вместе с браузером в __aexit__
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        self._contexts.clear()
        self._persistent = None
        self._pages.clear()

        if self._camoufox is not None:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._camoufox = None
        elif self.browser is not None:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None
        self.browser = None

        if self._relay is not None:
            await self._relay.stop()
            self._relay = None


def describe_engine(cfg) -> str:
    engine = (cfg.get("browser.engine") or "camoufox").lower()
    return f"{engine} ({platform.system()})"
