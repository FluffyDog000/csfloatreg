"""Outlook через веб: login.live.com -> outlook.live.com."""
from __future__ import annotations

import asyncio
import time

from ...errors import (
    MailBadCredentials,
    MailBlocked,
    MailNotReceived,
    MailVerifyRequired,
    StepTimeout,
)
from ..base import PageHelper
from .base import extract_link, register

LOGIN_URL = "https://login.live.com/"


@register("outlook_web")
class OutlookWebProvider:
    """Живёт в отдельном контексте того же браузера: тот же прокси, свои cookies."""

    name = "outlook_web"

    def __init__(self, ctx):
        self.ctx = ctx
        self.log = ctx.log
        self.cfg = ctx.cfg
        self.account = ctx.account
        self.page = None
        self.helper: PageHelper | None = None

    # ── жизненный цикл ───────────────────────────────────────
    async def _ensure_page(self) -> PageHelper:
        if self.helper is None:
            self.page = await self.ctx.session.page("mail")
            self.helper = PageHelper(self.page, self.ctx, name="mail")
        return self.helper

    async def close(self) -> None:
        try:
            await self.ctx.session.save_state("mail")
        except Exception:  # noqa: BLE001
            pass

    # ── вход ─────────────────────────────────────────────────
    async def login(self) -> None:
        helper = await self._ensure_page()
        sel = self.ctx.sel

        await helper.goto(self.cfg.get("mail.base_url", "https://outlook.live.com/mail/0/"))
        await helper.settle(2.0)
        if await self._mailbox_ready(helper):
            self.log.info("Почта: сессия восстановлена из cookies")
            return

        await helper.goto(LOGIN_URL)
        await helper.settle(1.5)
        await helper.check_captcha("mail_login")

        self.log.info("Почта: ввожу адрес")
        await helper.fill(sel("outlook.email_input"), self.account.mail, "поле адреса почты")
        await helper.click(sel("outlook.email_next"), "кнопку «Далее»")
        await helper.settle(2.0)

        state = await helper.wait_any(
            {
                "password": sel("outlook.password_input"),
                "wrong_password": sel("outlook.wrong_password"),
                # порядок важен: экран с кодом отличается от настоящей верификации
                # именно наличием перехода на пароль, поэтому проверяем его раньше
                "passwordless": sel("outlook.use_password", required=False)
                + sel("outlook.other_sign_in_ways", required=False),
                "blocked": sel("outlook.blocked"),
                "verify_required": sel("outlook.verify_required"),
                "captcha": sel("captcha.markers", required=False),
            },
            timeout=45,
        )
        if state == "passwordless":
            await self._switch_to_password(helper)
            state = "password"
        await self._raise_on_bad_state(helper, state)

        self.log.info("Почта: ввожу пароль")
        await helper.fill(sel("outlook.password_input"), self.account.mail_password, "поле пароля почты")
        await helper.click(sel("outlook.password_submit"), "кнопку входа в почту")
        await helper.settle(2.5)

        await self._pass_interstitials(helper)

        if not await self._mailbox_ready(helper, timeout=45):
            raise StepTimeout("почтовый ящик так и не открылся после входа")
        self.log.info("Почта: вход выполнен")

    async def _switch_to_password(self, helper: PageHelper) -> None:
        """«Отправим код на почту» -> «ввести пароль».

        Microsoft всё чаще делает вход по коду вариантом по умолчанию. Экран с
        вводом кода внешне совпадает с настоящей верификацией личности, но
        отличается наличием перехода на пароль — если перехода нет, это
        действительно верификация, и аккаунт честно помечается ошибкой.
        """
        sel = self.ctx.sel
        self.log.info("Почта: Microsoft предлагает вход по коду — переключаюсь на пароль")

        clicked = await helper.click(
            sel("outlook.use_password", required=False), "переход «ввести пароль»", optional=True
        )
        if clicked is None:
            clicked = await helper.click(
                sel("outlook.other_sign_in_ways", required=False),
                "переход «другие способы входа»",
                optional=True,
            )
            if clicked is None:
                raise MailVerifyRequired(
                    "Microsoft требует вход по коду, перехода на ввод пароля на странице нет"
                )
            await helper.settle(1.5)
            await helper.click(
                sel("outlook.cred_picker_password", required=False),
                "вариант «пароль» в списке способов входа",
                optional=True,
            )
        await helper.settle(1.5)

        if await helper.first_visible(sel("outlook.password_input"), timeout=10) is None:
            raise MailVerifyRequired("переключился на ввод пароля, но поле пароля не появилось")

    async def _pass_interstitials(self, helper: PageHelper, *, rounds: int = 8) -> None:
        """«Оставаться в системе?», «Добавьте телефон», «Сведения безопасности» и прочее."""
        sel = self.ctx.sel
        for _ in range(rounds):
            state = await helper.wait_any(
                {
                    "mailbox": sel("outlook.mailbox_ready") + ["url:outlook\\.live\\.com/mail"],
                    "stay_signed_in": ["text=Stay signed in?", "text=Не выходить из системы", "#KmsiCheckboxField"],
                    "skip": sel("outlook.skip_buttons"),
                    "blocked": sel("outlook.blocked"),
                    "verify_required": sel("outlook.verify_required"),
                    "wrong_password": sel("outlook.wrong_password"),
                    "captcha": sel("captcha.markers", required=False),
                },
                timeout=30,
            )
            await self._raise_on_bad_state(helper, state)
            if state == "mailbox":
                return
            if state == "stay_signed_in":
                self.log.debug("Экран «Оставаться в системе?» — отвечаю «Да»")
                await helper.click(sel("outlook.stay_signed_in_yes"), "кнопку «Да»", optional=True)
            elif state == "skip":
                self.log.debug("Пропускаю предложение о сведениях безопасности")
                await helper.click(sel("outlook.skip_buttons"), "кнопку пропуска", optional=True)
            await helper.settle(2.0)

    async def _raise_on_bad_state(self, helper: PageHelper, state: str) -> None:
        if state == "wrong_password":
            raise MailBadCredentials("Outlook: неверный пароль почты")
        if state == "blocked":
            raise MailBlocked("Outlook: аккаунт заблокирован / требует восстановления")
        if state == "verify_required":
            raise MailVerifyRequired("Outlook: требуется верификация личности (код/телефон)")
        if state == "captcha":
            await helper.check_captcha("mail_login")

    async def _mailbox_ready(self, helper: PageHelper, *, timeout: float = 8) -> bool:
        markers = [m for m in self.ctx.sel("outlook.mailbox_ready") if not m.startswith("url:")]
        if await helper.first_visible(markers, timeout=timeout) is not None:
            return True

        # URL сам по себе не доказательство: на /mail/0/ может висеть редирект на логин
        if not await helper.matches(r"url:outlook\.live\.com/mail/\d"):
            return False
        for candidate in self.ctx.sel("outlook.email_input") + self.ctx.sel("outlook.password_input"):
            if await helper.matches(candidate):
                return False
        return True

    # ── поиск письма ─────────────────────────────────────────
    async def wait_for_link(self, pattern: str, *, timeout_s: float, poll_s: float) -> str:
        helper = await self._ensure_page()
        folders = [("Входящие", self.cfg.get("mail.base_url", "https://outlook.live.com/mail/0/"))]
        if self.cfg.get("mail.check_junk", True):
            folders.append(("Нежелательные", self.cfg.get("mail.junk_url", "https://outlook.live.com/mail/0/junkemail/")))

        deadline = time.monotonic() + timeout_s
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            for title, url in folders:
                self.log.debug("Проверяю папку «%s» (попытка %d)", title, attempt)
                link = await self._scan_folder(helper, url, pattern)
                if link:
                    self.log.info("Ссылка подтверждения найдена в папке «%s»", title)
                    return link
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_s, max(1.0, remaining)))

        raise MailNotReceived(f"письмо от CSFloat не пришло за {timeout_s:.0f} c")

    async def wait_for_code(self, pattern: str, *, timeout_s: float, poll_s: float) -> str:
        """Задел: те же поиски, но вытаскиваем код, а не ссылку."""
        link = await self.wait_for_link(pattern, timeout_s=timeout_s, poll_s=poll_s)
        return link

    async def _scan_folder(self, helper: PageHelper, url: str, pattern: str) -> str | None:
        sel = self.ctx.sel
        needle = (self.cfg.get("csfloat.mail_search") or "csfloat").lower()
        try:
            await helper.goto(url)
            await helper.settle(2.0)
        except Exception as exc:  # noqa: BLE001 — папка может не открыться, попробуем на следующем круге
            self.log.debug("Не удалось открыть папку: %s", exc)
            return None

        # 1) письмо может уже быть открыто в области чтения
        link = extract_link(pattern, await helper.html())
        if link:
            return link

        # 2) ищем письмо в списке
        for row_selector in sel("outlook.message_rows"):
            rows = self.page.locator(f"{row_selector}:has-text('{needle}')")
            try:
                count = await rows.count()
            except Exception:  # noqa: BLE001
                continue
            for index in range(min(count, 5)):
                try:
                    await rows.nth(index).click()
                    await helper.settle(2.0)
                except Exception:  # noqa: BLE001
                    continue
                body = await helper.text_of(sel("outlook.message_body"), timeout=5)
                link = extract_link(pattern, await helper.html(), body, await self._anchor_hrefs())
                if link:
                    return link
        return None

    async def _anchor_hrefs(self) -> str:
        """Ссылка может жить только в href, но не в видимом тексте."""
        try:
            hrefs = await self.page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href).join('\\n')"
            )
            return hrefs or ""
        except Exception:  # noqa: BLE001
            return ""
