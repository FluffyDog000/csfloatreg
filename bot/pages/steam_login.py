"""Steam OpenID: логин, пароль, код Steam Guard из maFile."""
from __future__ import annotations

import asyncio
import time

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

    async def authorize(self, account, mafile, steam_time, success_markers: list[str]) -> None:
        """Вход, учитывающий уже живую сессию Steam.

        Возможны три состояния: нас уже вернули авторизованными, Steam помнит
        аккаунт и показывает только кнопку подтверждения, либо нужна полная
        форма логина. Раньше кнопка искалась вслепую три секунды, и при живой
        сессии бот уходил искать несуществующее поле логина.
        """
        sel = self.ctx.sel
        # порядок важен: если на странице есть кнопка подтверждения, жать надо её,
        # даже когда какой-то маркер успеха тоже сработал
        state = await self.wait_any(
            {
                "confirm": sel("steam.openid_signin_button", required=False),
                "success": success_markers,
                "credentials": sel("steam.username"),
                "qr": sel("steam.use_password_login", required=False),
                "bad_credentials": sel("steam.bad_credentials"),
                "locked": sel("steam.locked"),
                "rate_limited": sel("steam.rate_limited"),
            },
            timeout=30,
        )
        await self._raise_on_bad_state(state)
        self.log.info("Страница Steam: состояние '%s', URL %s", state, self.page.url[:120])

        if state == "success" and "/openid" in self.page.url:
            # ложное срабатывание: страница OpenID — это ещё не вход
            self.log.warning("Маркер успеха сработал на странице OpenID — ищу кнопку подтверждения")
            if await self.first_visible(sel("steam.openid_signin_button", required=False), timeout=5):
                state = "confirm"
            else:
                raise UnexpectedState(
                    "на странице OpenID нет ни кнопки подтверждения, ни формы логина"
                )

        if state == "success":
            self.log.info("Steam уже авторизован, подтверждение не требуется")
            return

        if state == "confirm" and await self.first_visible(sel("steam.username"), timeout=2):
            # на странице есть поле логина — значит это обычная форма входа,
            # а не подтверждение уже живой сессии
            self.log.info("Рядом с кнопкой есть поле логина — это форма входа, а не подтверждение")
            state = "credentials"

        if state == "confirm":
            self.log.info("Steam помнит аккаунт — подтверждаю вход")
            await self._confirm_openid(sel("steam.openid_signin_button"))
            return

        await self.perform(account, mafile, steam_time, success_markers)
        await self.after_login(success_markers)

    async def after_login(self, success_markers: list[str], *, seconds: float = 25) -> None:
        """После ввода логина Steam ещё раз показывает подтверждение входа.

        Раньше здесь был один необязательный клик сразу после формы: страница в
        этот момент ещё редиректила на /openid/login, кнопки не было, и бот молча
        уходил дальше — со стороны это выглядело как «не нажимает Sign In».
        Поэтому ждём, чем всё кончится: вернулись на сайт или показали кнопку.
        """
        sel = self.ctx.sel
        candidates = sel("steam.openid_signin_button", required=False)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for marker in success_markers:
                if await self.matches(marker, timeout=120):
                    self.log.info("После входа Steam сразу вернул нас обратно")
                    return
            if candidates and await self.first_visible(candidates, timeout=1) is not None:
                self.log.info("После входа Steam показал подтверждение — жму Sign In")
                await self._confirm_openid(candidates)
                return
            await asyncio.sleep(0.5)

        self.log.info(
            "После входа ни подтверждения, ни возврата за %.0f c. Страница: %s",
            seconds, self.page.url[:110],
        )

    async def _confirm_openid(self, candidates: list[str]) -> None:
        """Жмём Sign In и проверяем, что страница действительно ушла.

        Кнопка — это input[type=submit] внутри формы: обычный клик по ней иногда
        не доходит (перекрытие, фокус, ранний клик до готовности формы), и тогда
        бот уходил дальше с ощущением, что всё сделано. Поэтому проверяем URL и,
        если остались на месте, дожимаем форму её же средствами.
        """
        before = self.page.url
        if "/openid" not in before:
            self.log.info("Страница уже ушла с OpenID — подтверждать нечего")
            return

        await self._describe_button(candidates)
        if await self._navigating_away():
            self.log.info("Steam уже перекинул нас дальше, кнопку жать не нужно")
            return

        ways = (
            ("обычный клик", self._plain_click),
            ("клик с force", self._force_click),
            ("событие click", self._dispatch_click),
            ("Enter на кнопке", self._press_enter),
            ("отправка формы", self._submit_form),
        )
        for title, attempt in ways:
            if await self._navigating_away():
                self.log.info("Вход подтверждён: Steam ушёл со страницы сам")
                return
            try:
                await attempt(candidates)
            except Exception as exc:  # noqa: BLE001 — на то они и запасные пути
                self.log.warning("Способ «%s» не сработал: %s", title, str(exc).splitlines()[0][:120])
                continue
            if await self._left_openid(before, seconds=8):
                self.log.info("Вход подтверждён (%s)", title)
                return
            self.log.warning("Способ «%s» ничего не изменил, URL прежний", title)

        raise UnexpectedState(
            "Steam не реагирует на кнопку Sign In ни одним из способов. "
            f"URL: {self.page.url[:120]}"
        )

    async def _describe_button(self, candidates: list[str]) -> None:
        """Пишет в лог, что именно бот считает кнопкой. Без этого «не нажимает»
        невозможно отличить от «нажимает не туда».

        Все запросы к странице — с коротким таймаутом: диагностика не имеет права
        стоить дороже самого действия, а на уходящей странице evaluate висит до
        последнего.
        """
        self.log.info("Страница перед подтверждением: %s | %s", self.page.url[:110], await self._title())
        for candidate in candidates:
            try:
                locator = self.page.locator(candidate)
                count = await locator.count()
            except Exception as exc:  # noqa: BLE001
                self.log.info("  %-46s ошибка селектора: %s", candidate, str(exc)[:60])
                continue
            if not count:
                self.log.info("  %-46s не найдено", candidate)
                continue
            try:
                info = await locator.first.evaluate(
                    """el => {
                        const r = el.getBoundingClientRect();
                        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                        const top = document.elementFromPoint(cx, cy);
                        return {
                            tag: el.tagName, id: el.id, cls: el.className, value: el.value || '',
                            box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                            disabled: !!el.disabled,
                            covered: top !== el && !el.contains(top),
                            coveredBy: top ? (top.tagName + '.' + (top.className || '')).slice(0, 40) : '',
                        };
                    }""",
                    timeout=2500,
                )
            except Exception as exc:  # noqa: BLE001
                self.log.info("  %-46s найдено (%d), детали недоступны: %s", candidate, count, str(exc)[:60])
                continue
            self.log.info(
                "  %-46s найдено %d: <%s id=%s value=%r> бокс %s%s%s",
                candidate, count, info["tag"].lower(), info["id"] or "—", info["value"], info["box"],
                ", ВЫКЛЮЧЕНА" if info["disabled"] else "",
                f", ПЕРЕКРЫТА {info['coveredBy']}" if info["covered"] else "",
            )

    async def _navigating_away(self) -> bool:
        """Steam сам ушёл со страницы подтверждения (в заголовке уже Loading …)."""
        if "/openid" not in self.page.url:
            return True
        title = await self._title()
        return title.lower().startswith("loading")

    async def _title(self) -> str:
        try:
            return (await self.page.title())[:60]
        except Exception:  # noqa: BLE001
            return "?"

    async def _plain_click(self, candidates: list[str]) -> None:
        await self.click(candidates, "кнопку подтверждения входа")

    async def _force_click(self, candidates: list[str]) -> None:
        locator = await self._button(candidates)
        await locator.click(force=True, timeout=5000)

    async def _dispatch_click(self, candidates: list[str]) -> None:
        locator = await self._button(candidates)
        await locator.dispatch_event("click")

    async def _button(self, candidates: list[str]):
        locator = await self.first_visible(candidates, timeout=3)
        if locator is None:
            raise StepTimeout("кнопка подтверждения пропала со страницы")
        return locator

    async def _left_openid(self, before: str, *, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            url = self.page.url
            if "/openid" not in url or url != before:
                await self.settle(1.5)
                return "/openid" not in self.page.url
            await asyncio.sleep(0.4)
        return False

    async def _press_enter(self, candidates: list[str]) -> None:
        locator = await self._button(candidates)
        await locator.press("Enter")

    async def _submit_form(self, candidates: list[str]) -> None:
        await self.page.evaluate(
            """() => {
                const button = document.querySelector('#imageLogin, input[type=submit], button[type=submit]');
                if (button) { button.click(); return; }
                const form = document.querySelector('#openidForm, form[action*=openid], form');
                if (form) form.submit();
            }"""
        )

    async def perform(self, account, mafile, steam_time, success_markers: list[str]) -> None:
        sel = self.ctx.sel
        await self.check_captcha("steam_login")

        await self._ensure_credentials_form()

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
        if state == "mobile_confirm":
            state = await self._switch_to_code_entry()
        await self._raise_on_bad_state(state)

        if state == "success":
            self.log.info("Steam пустил без запроса кода (сессия жива)")
            return

        await self._enter_guard_code(mafile, steam_time, success_markers)

    async def _ensure_credentials_form(self) -> None:
        """Новый логин Steam открывается на вкладке QR-кода: формы с логином там нет."""
        sel = self.ctx.sel
        if await self.first_visible(sel("steam.username"), timeout=5) is not None:
            return
        self.log.info("Форма логина не видна — переключаюсь с QR-кода на ввод логина")
        await self.click(
            sel("steam.use_password_login", required=False),
            "переключатель на вход по логину",
            optional=True,
        )
        await self.settle(1.2)

    async def _switch_to_code_entry(self) -> str:
        """«Подтвердите вход в приложении» -> «ввести код вместо этого».

        У аккаунта с maFile это штатный экран, а не тупик: Steam прячет ввод кода
        за ссылкой. Фатальный статус оставляем только если ссылки действительно нет.
        """
        sel = self.ctx.sel
        self.log.info("Steam предлагает подтверждение в приложении — ищу переход на ввод кода")
        link = await self.first_visible(sel("steam.use_code_instead", required=False), timeout=6)
        if link is None:
            raise SteamMobileConfirmRequired(
                "Steam требует подтверждение в мобильном приложении, перехода на ввод кода нет"
            )
        try:
            await link.click()
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc, "клик по переходу на ввод кода") from exc
        await self.settle(1.5)

        if await self.first_visible(sel("steam.guard_boxes"), timeout=8) is not None:
            return "guard_boxes"
        if await self.first_visible(sel("steam.guard_single"), timeout=3) is not None:
            return "guard_single"
        raise SteamMobileConfirmRequired(
            "переключился на ввод кода, но поле кода так и не появилось"
        )

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
                    # после кода Steam часто показывает ещё и подтверждение OpenID —
                    # это успех, а не «ничего не произошло»
                    "confirm": sel("steam.openid_signin_button", required=False),
                    "bad_code": sel("steam.guard_boxes") + sel("steam.guard_single"),
                    "locked": sel("steam.locked"),
                    "rate_limited": sel("steam.rate_limited"),
                    "captcha": sel("captcha.markers", required=False),
                },
                timeout=40,
            )
            await self._raise_on_bad_state(state)
            if state in ("success", "confirm"):
                self.log.info("Steam Guard пройден%s", " (показано подтверждение входа)" if state == "confirm" else "")
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
