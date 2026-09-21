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
        self.profile_url = ctx.cfg.get("csfloat.profile_url") or f"{self.base_url}/profile"

    # ── сессия ───────────────────────────────────────────────
    async def open_home(self) -> None:
        await self.goto(self.base_url + "/")
        if not await self.wait_rendered(timeout=self.cfg.get("csfloat.render_timeout_s", 25)):
            self.log.error(
                "CSFloat отдал пустую страницу: приложение не отрисовалось. "
                "Смотри выше строки 'запрос не прошёл' и 'JS-ошибка' — "
                "чаще всего это блокировка ресурсов прокси или блокировщиком."
            )
            await self.ctx.dump("csfloat_blank", note="пустая страница csfloat.com")
        await self.settle(1.5)
        await self.click(self.ctx.sel("csfloat.cookie_accept", required=False), "баннер cookies", optional=True)
        await self.check_captcha("csfloat_home")

    async def is_logged_in(self, *, timeout: int = 1500) -> bool:
        """Мгновенный срез. В циклах ожидания зови с маленьким timeout —
        иначе каждая итерация стоит timeout × число кандидатов."""
        for candidate in self.ctx.sel("csfloat.logged_in"):
            if await self.matches(candidate, timeout=timeout):
                return True
        return False

    async def wait_logged_in(self, timeout: float = 12) -> bool:
        """CSFloat — SPA: аватар после редиректа появляется не мгновенно."""
        markers = self.ctx.sel("csfloat.logged_in")
        dom = [m for m in markers if not m.startswith("url:")]
        if dom and await self.first_visible(dom, timeout=timeout) is not None:
            return True
        for candidate in markers:
            if candidate.startswith("url:") and await self.matches(candidate):
                return True
        return False

    async def login_via_steam(self, account, mafile, steam_time) -> None:
        """Вход через Steam OpenID с повторами.

        CSFloat регулярно не подхватывает сессию с первого редиректа — лечится
        повторным заходом. Повторяем внутри той же сессии: Steam уже авторизован,
        так что второй проход идёт без пароля и кода, в отличие от ретрая всего
        модуля с перезапуском браузера.
        """
        attempts = max(1, int(self.ctx.cfg.get("csfloat.login_attempts", 3)))
        popup: list = []
        self.page.context.on("page", lambda page: popup.append(page))

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self.log.warning(
                    "CSFloat не подхватил сессию Steam — повторяю вход (попытка %d из %d)",
                    attempt, attempts,
                )
                await self.open_home()
                if await self.wait_logged_in(timeout=5):
                    self.log.info("CSFloat: вход выполнен")
                    return

            popup.clear()
            await self._login_pass(account, mafile, steam_time, popup)

            if await self.wait_logged_in():
                self.log.info("CSFloat: вход выполнен")
                return
            await self.open_home()
            if await self.wait_logged_in(timeout=6):
                self.log.info("CSFloat: вход выполнен (сессия подхватилась после перезагрузки)")
                return

        raise UnexpectedState(
            f"CSFloat не считает нас залогиненными после {attempts} попыток входа через Steam"
        )

    async def _login_pass(self, account, mafile, steam_time, popup: list) -> None:
        """Один заход: кнопка входа -> Steam -> возврат на CSFloat."""
        sel = self.ctx.sel
        await self.click(sel("csfloat.sign_in_button"), "кнопку входа через Steam")
        target = await self._resolve_steam_page(popup)
        if target is None:
            return  # Steam вернул нас сразу, форма логина не понадобилась

        steam = SteamLoginPage(target, self.ctx, name="steam")
        success_markers = sel("csfloat.logged_in") + [f"url:{re.escape(self._host())}"]
        await steam.authorize(account, mafile, steam_time, success_markers)

        if target is not self.page:
            for _ in range(30):
                if target.is_closed():
                    break
                await asyncio.sleep(0.5)
            self.ctx.session._pages.pop("steam_popup", None)
            await self.page.reload(wait_until="domcontentloaded")
        await self.settle(2.0)

    async def _resolve_steam_page(self, popup: list):
        """Steam может открыться в новой вкладке, в текущей — или не открыться вовсе."""
        sel = self.ctx.sel
        for _ in range(24):
            if await self.is_logged_in(timeout=250):
                self.log.info("Steam авторизовал сразу, без формы логина")
                return None
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

    # ── онбординг ────────────────────────────────────────────
    async def open_profile(self) -> None:
        """Прямой переход на /profile. Меню аватарки — только если нас увели."""
        await self.goto(self.profile_url)
        await self.wait_rendered(timeout=self.cfg.get("csfloat.render_timeout_s", 25))
        await self.settle(1.5)

        if "/profile" in self.page.url:
            self.log.info("Профиль открыт: %s", self.page.url)
        else:
            self.log.warning(
                "CSFloat увёл с %s на %s — пробую через меню аватарки", self.profile_url, self.page.url
            )
            await self._open_profile_via_menu()
        await self.check_captcha("csfloat_profile")

    async def _open_profile_via_menu(self) -> bool:
        sel = self.ctx.sel
        avatar = await self.first_visible(sel("csfloat.avatar_menu_button"), timeout=8)
        if avatar is None:
            self.log.debug("Аватарка не найдена")
            return False
        try:
            await avatar.click()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("Не удалось кликнуть по аватарке: %s", exc)
            return False
        await self.settle(1.0)

        item = await self.first_visible(sel("csfloat.menu_profile"), timeout=5)
        if item is None:
            self.log.debug("В меню аватарки нет пункта Profile")
            return False
        await item.click()
        await self.wait_rendered(timeout=self.cfg.get("csfloat.render_timeout_s", 25))
        await self.settle(1.5)
        self.log.info("Профиль открыт через меню аватарки, адрес: %s", self.page.url)
        await self.check_captcha("csfloat_profile")
        return True

    async def find_onboarding(self) -> bool:
        """Окно Onboard может висеть где угодно: на главной, в профиле, в настройках.

        Проверяем текущую страницу, потом обходим оба адреса и в каждом случае
        пишем, куда нас реально привело — CSFloat умеет редиректить.
        """
        if await self.onboarding_visible(timeout=2):
            self.log.info("Окно Onboard уже открыто на текущей странице")
            return True
        for title, opener in (("профиль", self.open_profile), ("настройки", self.open_settings)):
            await opener()
            if await self.onboarding_visible(timeout=4):
                self.log.info("Окно Onboard найдено: %s", title)
                return True
            self.log.info("Окно Onboard не найдено: %s (%s)", title, self.page.url)
        return False

    async def onboarding_visible(self, timeout: float = 4) -> bool:
        """Окно Onboard: Terms -> Verify Email -> Trade Link -> Done."""
        return await self.first_visible(self.ctx.sel("csfloat.onboard_dialog"), timeout=timeout) is not None

    async def complete_onboarding(self, email: str) -> str:
        """Проходит онбординг до отправки письма.

        Возвращает 'email_sent', если письмо запрошено, или 'email_step_missing',
        если до шага с почтой дойти не удалось.
        """
        sel = self.ctx.sel
        if not await self.first_visible(sel("csfloat.onboard_email_input"), timeout=2):
            ticked = await self._tick_checkboxes()
            self.log.info("Онбординг: отмечено согласий — %d", ticked)
            next_button = await self.first_visible(sel("csfloat.onboard_next"), timeout=5)
            if next_button is not None:
                try:
                    if not await next_button.is_enabled():
                        self.log.warning("Кнопка Next осталась неактивной — отмечены не все согласия")
                        await self.ctx.dump("onboard_terms_blocked", note="Next неактивна")
                except Exception:  # noqa: BLE001
                    pass
                await next_button.click()
                await self.settle(2.0)

        field = await self.first_visible(sel("csfloat.onboard_email_input"), timeout=8)
        if field is None:
            self.log.warning("Онбординг: шаг с почтой не открылся")
            await self.ctx.dump("onboard_no_email_step", note="шаг Verify Email не найден")
            return "email_step_missing"

        await field.click()
        await field.fill("")
        await self.type_text(field, email)
        await self.click(sel("csfloat.onboard_email_submit"), "кнопку отправки письма")
        await self.settle(2.5)
        await self.check_captcha("csfloat_onboard_email")
        self.log.info("Онбординг: письмо подтверждения запрошено для %s", email)
        return "email_sent"

    async def _tick_checkboxes(self) -> int:
        """Отмечает все согласия. Чекбоксы Angular Material — это не input,
        поэтому кликаем по самому элементу и проверяем состояние по атрибутам."""
        for candidate in self.ctx.sel("csfloat.onboard_checkboxes"):
            boxes = self.page.locator(candidate)
            try:
                count = await boxes.count()
            except Exception:  # noqa: BLE001
                continue
            if not count:
                continue

            ticked = 0
            for index in range(count):
                box = boxes.nth(index)
                if await self._is_checked(box):
                    continue
                try:
                    await box.click(force=True, timeout=4000)
                    ticked += 1
                    await asyncio.sleep(0.25)
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("Не удалось отметить чекбокс %d (%s): %s", index, candidate, exc)
            if ticked:
                return ticked
        return 0

    @staticmethod
    async def _is_checked(locator) -> bool:
        try:
            return await locator.is_checked()
        except Exception:  # noqa: BLE001 — не input, смотрим атрибуты
            pass
        try:
            if (await locator.get_attribute("aria-checked")) == "true":
                return True
            classes = (await locator.get_attribute("class")) or ""
            return "checkbox-checked" in classes or "mat-mdc-checkbox-checked" in classes
        except Exception:  # noqa: BLE001
            return False

    # ── почта в настройках ───────────────────────────────────
    async def open_settings(self) -> None:
        await self.goto(self.settings_url)
        await self.wait_rendered(timeout=self.cfg.get("csfloat.render_timeout_s", 25))
        await self.settle(1.5)
        self.log.info("Настройки открыты, адрес: %s", self.page.url)
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
