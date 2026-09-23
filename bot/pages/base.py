"""Базовый слой над Playwright: списки кандидатов вместо жёстких селекторов.

Любой элемент задаётся списком в selectors.yaml и ищется по порядку — это
даёт запас прочности на мелкие редизайны CSFloat/Outlook.

Спец-синтаксис: селектор вида "url:<regex>" проверяет текущий URL, а не DOM.
"""
from __future__ import annotations

import asyncio
import random
import re
import time

from ..captcha import detect as detect_captcha
from ..errors import NetworkError, ProxyAuthFailed, StepTimeout, UnexpectedState

#: Коды, которые отдаёт шлюз/прокси, а не сам сайт: ретраить, а не искать селекторы.
_GATEWAY_STATUSES = {502, 503, 504, 520, 521, 522, 523, 524}

_NET_MARKERS = (
    "net::err", "ns_error", "econnreset", "timeout", "socket hang up",
    "proxy", "connection closed", "tunnel", "dns",
)


class PageHelper:
    def __init__(self, page, ctx, *, name: str = "page"):
        self.page = page
        self.ctx = ctx
        self.log = ctx.log
        self.cfg = ctx.cfg
        self.name = name

    # ── навигация ────────────────────────────────────────────
    async def goto(self, url: str, *, wait: str = "domcontentloaded") -> None:
        self.log.debug("goto %s", url)
        try:
            response = await self.page.goto(
                url, wait_until=wait, timeout=self.cfg.get("timeouts.page_load_ms", 60000)
            )
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc, f"переход на {url}") from exc
        if response is not None:
            self.check_status(response.status, url)

    def check_status(self, status: int, url: str) -> None:
        """Пустая страница с кодом 504 — это мёртвый прокси, а не сломанный сайт."""
        proxy = getattr(self.ctx.bundle, "proxy", None)
        where = f" (прокси {proxy.safe()})" if proxy is not None else ""
        if status == 407:
            raise ProxyAuthFailed(f"прокси не принял логин/пароль (407){where}")
        if status in _GATEWAY_STATUSES:
            raise NetworkError(
                f"шлюз вернул {status} при переходе на {url}{where}: "
                f"прокси не смог достучаться до сайта"
            )
        if status == 429:
            raise NetworkError(f"слишком много запросов (429) при переходе на {url}{where}")

    @staticmethod
    def classify(exc: Exception, what: str) -> Exception:
        """Сетевые/таймаутные падения — повторяемые, остальное наверх как есть."""
        text = f"{type(exc).__name__}: {exc}".lower()
        if any(marker in text for marker in _NET_MARKERS):
            return NetworkError(f"{what}: {exc}")
        return exc

    # ── поиск элементов ──────────────────────────────────────
    def locator(self, selector: str):
        return self.page.locator(selector).first

    async def matches(self, selector: str, *, timeout: int = 250) -> bool:
        if selector.startswith("url:"):
            try:
                return re.search(selector[4:], self.page.url) is not None
            except re.error:
                return False
        try:
            return await self.page.locator(selector).first.is_visible(timeout=timeout)
        except Exception:  # noqa: BLE001 — невалидный/отсутствующий селектор не должен ронять шаг
            return False

    async def first_visible(self, candidates: list[str], *, timeout: float | None = None):
        """Первый видимый кандидат. None, если не дождались."""
        timeout_s = (timeout if timeout is not None else self.cfg.get("timeouts.action_ms", 20000) / 1000)
        deadline = time.monotonic() + timeout_s
        while True:
            for candidate in candidates:
                if candidate.startswith("url:"):
                    continue
                if await self.matches(candidate):
                    return self.locator(candidate)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)

    async def require(self, candidates: list[str], what: str, *, timeout: float | None = None):
        locator = await self.first_visible(candidates, timeout=timeout)
        if locator is None:
            raise StepTimeout(f"не нашёл {what}; пробовал: {candidates}")
        return locator

    async def wait_gone(self, candidates: list[str], *, timeout: float = 25) -> bool:
        """Ждёт, пока экран уйдёт.

        Нужно после кликов по промежуточным экранам: пока Microsoft крутит
        спиннер, разметка ещё на месте, и без этого бот жмёт кнопку повторно.
        """
        deadline = time.monotonic() + timeout
        while True:
            present = False
            for candidate in candidates:
                if await self.matches(candidate, timeout=150):
                    present = True
                    break
            if not present:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.4)

    async def wait_any(self, groups: dict[str, list[str]], *, timeout: float, poll: float = 0.35) -> str:
        """Ждёт, какое из состояний наступит первым. Возвращает ключ группы."""
        deadline = time.monotonic() + timeout
        while True:
            for key, candidates in groups.items():
                for candidate in candidates:
                    if await self.matches(candidate, timeout=120):
                        self.log.debug("wait_any -> %s (%s)", key, candidate)
                        return key
            if time.monotonic() >= deadline:
                raise StepTimeout(
                    f"ни одно из состояний не наступило за {timeout:.0f} c: {list(groups)}"
                )
            await asyncio.sleep(poll)

    # ── действия ─────────────────────────────────────────────
    async def fill(self, candidates: list[str], value: str, what: str, *, timeout: float | None = None):
        locator = await self.require(candidates, what, timeout=timeout)
        try:
            await locator.click()
            await locator.fill("")
            await self.type_text(locator, value)
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc, f"ввод в {what}") from exc
        return locator

    async def type_text(self, locator, value: str) -> None:
        """Печать с человеческими задержками — сплошной fill() палится чаще."""
        for char in value:
            await locator.press_sequentially(char, delay=random.uniform(30, 110))

    async def click(self, candidates: list[str], what: str, *, timeout: float | None = None, optional: bool = False):
        locator = await self.first_visible(candidates, timeout=timeout if timeout is not None else 5)
        if locator is None:
            if optional:
                self.log.debug("необязательный элемент не найден: %s", what)
                return None
            raise StepTimeout(f"не нашёл {what}; пробовал: {candidates}")
        try:
            await locator.click()
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc, f"клик по {what}") from exc
        return locator

    async def text_of(self, candidates: list[str], *, timeout: float = 3) -> str:
        locator = await self.first_visible(candidates, timeout=timeout)
        if locator is None:
            return ""
        try:
            return (await locator.inner_text()).strip()
        except Exception:  # noqa: BLE001
            return ""

    async def wait_rendered(self, timeout: float = 20) -> bool:
        """Ждёт, пока на странице появится хоть какой-то текст.

        CSFloat — SPA: domcontentloaded наступает задолго до отрисовки, а если
        бандл не загрузился, страница так и остаётся белой. Без этой проверки
        белая страница выглядит как «не нашёл селектор», что уводит в сторону.
        """
        try:
            await self.page.wait_for_function(
                "() => document.body && document.body.innerText.trim().length > 0",
                timeout=timeout * 1000,
            )
            return True
        except Exception:  # noqa: BLE001
            return False

    async def ensure_rendered(self, what: str, *, timeout: float = 20) -> None:
        """Пустая страница после клика или редиректа — тоже повод назвать причину.

        goto проверяет код ответа сам, но переход бывает и по клику: форма
        логина, редирект провайдера. Здесь смотрим код последнего перехода в
        главном фрейме и по нему решаем, прокси это или сам сайт.
        """
        if await self.wait_rendered(timeout=timeout):
            return

        last = None
        session = getattr(self.ctx, "session", None)
        if session is not None and hasattr(session, "last_status"):
            last = session.last_status(self.name)

        if last:
            status, url = last
            self.log.error("Пустая страница, последний переход: %s -> %s", status, url)
            self.check_status(status, url)          # 5xx/407/429 поднимут свою ошибку
        await self.ctx.dump(f"blank_{self.name}", note=f"{what}: пустая страница, {last}")
        raise UnexpectedState(f"{what}: страница осталась пустой ({self.page.url})")

    async def settle(self, seconds: float = 1.0) -> None:
        await asyncio.sleep(seconds + random.uniform(0, 0.4))

    # ── капча ────────────────────────────────────────────────
    async def check_captcha(self, stage: str = "") -> None:
        marker = await detect_captcha(self.page, self.ctx.selectors)
        if marker is None:
            return
        self.log.warning("Обнаружена капча (%s) на этапе %s", marker, stage or self.ctx.stage)
        await self.ctx.dump(f"captcha_{stage or self.ctx.stage}", note=f"captcha marker: {marker}")
        # Решалка не подключена -> NullSolver поднимет CaptchaDetected
        await self.ctx.solver.solve(self.page, marker, {"url": self.page.url})

    async def html(self) -> str:
        try:
            return await self.page.content()
        except Exception:  # noqa: BLE001
            return ""
