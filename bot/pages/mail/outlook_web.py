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
    UnexpectedState,
)
from urllib.parse import parse_qs, urlparse

from ..base import PageHelper
from .base import extract_link, extract_token, register

LOGIN_URL = "https://login.live.com/"


def _return_url(url: str) -> str | None:
    """Адрес из параметра ru: куда Microsoft вернула бы нас после уведомления."""
    try:
        target = parse_qs(urlparse(url).query).get("ru", [None])[0]
    except ValueError:
        return None
    if target and target.startswith("http"):
        return target
    return None


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
        for marker in sel("outlook.signed_out", required=False):
            if await helper.matches(marker, timeout=300):
                self.log.info("Почта: сессии нет, Microsoft увёл на %s — иду на форму входа", helper.page.url[:80])
                break

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
        await self._pass_privacy_notice(helper)
        await helper.ensure_rendered("вход в Outlook после пароля")

        await self._pass_interstitials(helper)

        if not await self._mailbox_ready(helper, timeout=45):
            # последняя попытка: просто открыть ящик по адресу
            self.log.info("Почта: ящик не открылся сам — перехожу по адресу")
            await helper.goto(self.cfg.get("mail.base_url", "https://outlook.live.com/mail/0/"))
            await helper.settle(3.0)

        if not await self._mailbox_ready(helper, timeout=30):
            await helper.ensure_rendered("открытие почтового ящика", timeout=10)
            for marker in sel("outlook.signed_out", required=False):
                if await helper.matches(marker, timeout=300):
                    raise UnexpectedState(
                        f"после входа Microsoft снова показывает страницу для незалогиненных "
                        f"({helper.page.url[:90]}) — сессия не сохранилась"
                    )
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

    async def _pass_privacy_notice(self, helper: PageHelper, *, rounds: int = 4) -> bool:
        """Уведомление о приватности при первом входе.

        Отдельная страница privacynotice.account.microsoft.com, из которой login
        продолжается только после нажатия кнопки. Часто она вообще не
        отрисовывается — тогда возвращаемся по адресу из параметра ru, это и есть
        тот адрес, куда Microsoft вернула бы нас сама.
        """
        sel = self.ctx.sel
        marker = sel("outlook.privacy_notice")[0]
        if not await helper.matches(marker, timeout=400):
            return False

        self.log.info("Почта: уведомление о приватности — прохожу")
        for attempt in range(1, rounds + 1):
            clicked = await helper.click(sel("outlook.privacy_next"), "кнопку уведомления", optional=True)
            if clicked is None:
                target = _return_url(helper.page.url)
                if not target:
                    break
                self.log.info("Кнопок на уведомлении нет — возвращаюсь по адресу из параметра ru")
                await helper.goto(target)
            await helper.settle(2.5)

            if not await helper.matches(marker, timeout=400):
                self.log.info("Почта: уведомление пройдено (попытка %d)", attempt)
                return True

        await self.ctx.dump("privacy_notice", note=f"не пройдено: {helper.page.url}")
        await helper.ensure_rendered("уведомление о приватности", timeout=5)
        raise UnexpectedState(
            f"не удалось пройти уведомление о приватности Microsoft ({helper.page.url[:100]})"
        )

    async def _pass_interstitials(self, helper: PageHelper, *, rounds: int = 8) -> None:
        """«Оставаться в системе?», «Добавьте телефон», «Сведения безопасности» и прочее."""
        sel = self.ctx.sel
        budget = float(self.cfg.get("timeouts.mail_login_s", 240))
        deadline = time.monotonic() + budget
        for _ in range(rounds):
            left = deadline - time.monotonic()
            if left <= 0:
                raise StepTimeout(f"вход в почту не завершился за {budget:.0f} c")
            state = await helper.wait_any(
                {
                    "mailbox": sel("outlook.mailbox_ready") + ["url:outlook\\.live\\.com/mail"],
                    "privacy": sel("outlook.privacy_notice", required=False),
                    "account_home": sel("outlook.account_home", required=False),
                    "stay_signed_in": ["text=Stay signed in?", "text=Не выходить из системы", "#KmsiCheckboxField"],
                    "skip": sel("outlook.skip_buttons"),
                    "blocked": sel("outlook.blocked"),
                    "verify_required": sel("outlook.verify_required"),
                    "wrong_password": sel("outlook.wrong_password"),
                    "captcha": sel("captcha.markers", required=False),
                },
                timeout=min(45, max(10, left)),
            )
            await self._raise_on_bad_state(helper, state)
            if state == "mailbox":
                return
            if state == "privacy":
                await self._pass_privacy_notice(helper)
                await helper.settle(2.0)
                continue
            if state == "account_home":
                # вход прошёл, но нас увело на страницу аккаунта — идём в ящик сами
                self.log.info("Почта: Microsoft увёл на страницу аккаунта — открываю ящик")
                await helper.goto(self.cfg.get("mail.base_url", "https://outlook.live.com/mail/0/"))
                await helper.settle(2.5)
                continue
            if state == "stay_signed_in":
                markers = ["text=Stay signed in?", "text=Не выходить из системы", "#KmsiCheckboxField"]
                self.log.info("Почта: экран «Оставаться в системе?» — отвечаю «Да»")
                await helper.click(sel("outlook.stay_signed_in_yes"), "кнопку «Да»", optional=True)
                if not await helper.wait_gone(markers, timeout=40):
                    self.log.info("Microsoft всё ещё обрабатывает ответ — жду дальше")
            elif state == "skip":
                markers = sel("outlook.skip_buttons")
                self.log.info("Почта: пропускаю предложение о сведениях безопасности")
                await helper.click(markers, "кнопку пропуска", optional=True)
                await helper.wait_gone(markers, timeout=25)
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
        return await self._wait_in_mail(
            lambda sources: extract_link(pattern, *sources),
            timeout_s=timeout_s, poll_s=poll_s, what="ссылка подтверждения",
        )

    async def wait_for_code(self, pattern: str, *, timeout_s: float, poll_s: float) -> str:
        return await self._wait_in_mail(
            lambda sources: extract_token(pattern, *sources),
            timeout_s=timeout_s, poll_s=poll_s, what="токен подтверждения",
        )

    async def _wait_in_mail(self, extractor, *, timeout_s: float, poll_s: float, what: str) -> str:
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
                found = await self._scan_folder(helper, url, extractor)
                if found:
                    self.log.info("%s найден в папке «%s»", what.capitalize(), title)
                    return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_s, max(1.0, remaining)))

        raise MailNotReceived(f"письмо от CSFloat не пришло за {timeout_s:.0f} c ({what})")

    async def _scan_folder(self, helper: PageHelper, url: str, extractor) -> str | None:
        sel = self.ctx.sel
        try:
            await helper.goto(url)
            await helper.settle(2.5)
        except Exception as exc:  # noqa: BLE001 — папка может не открыться, попробуем на следующем круге
            self.log.debug("Не удалось открыть папку: %s", exc)
            return None

        # письмо могло уже открыться в области чтения
        found = extractor(await self._sources(helper))
        if found:
            return found

        for tab in ("Focused", "Other"):
            if tab == "Other" and not await self._switch_to_other(helper):
                continue
            found = await self._scan_rows(helper, extractor)
            if found:
                self.log.debug("Письмо найдено во вкладке «%s»", tab)
                return found
        return None

    async def _switch_to_other(self, helper: PageHelper) -> bool:
        """Вкладка Other: Outlook раскладывает письма по двум спискам."""
        tab = await helper.first_visible(self.ctx.sel("outlook.tab_other", required=False), timeout=2)
        if tab is None:
            return False
        try:
            await tab.click()
            await helper.settle(2.0)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _scan_rows(self, helper: PageHelper, extractor) -> str | None:
        """Открывает письма от CSFloat и вытаскивает из них токен или ссылку."""
        sel = self.ctx.sel
        needle = (self.cfg.get("csfloat.mail_search") or "csfloat").lower()
        subject = self.cfg.get("csfloat.mail_subject") or "CSFloat"

        for opener in (
            self._rows_by_selector(needle),
            self._rows_by_text(subject),
            self._rows_by_text(needle),
        ):
            async for label, row in opener:
                try:
                    await row.click(timeout=8000)
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("Не открылось письмо (%s): %s", label, exc)
                    continue

                if not await self._reading_pane_ready(helper):
                    self.log.debug("Клик по «%s» не открыл письмо", label)
                    continue

                sources = await self._sources(helper)
                found = extractor(sources)
                if found:
                    return found
                self.log.info(
                    "Письмо открыто (%s), но нужного в нём нет: источников %d, текста %d символов",
                    label, len(sources), sum(len(x) for x in sources),
                )
        return None

    async def _sources(self, helper: PageHelper) -> list[str]:
        """Текст письма живёт в iframe, поэтому собираем и страницу, и все фреймы.

        Порядок важен: сначала видимый текст письма, потом текст фреймов, и только
        потом HTML — в разметке слишком много мусора, из которого легко достать
        случайную строку вместо токена.
        """
        sources: list[str] = []
        body = await helper.text_of(self.ctx.sel("outlook.message_body"), timeout=3)
        if body:
            sources.append(body)

        for frame in self.page.frames:
            try:
                text = await frame.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:  # noqa: BLE001 — кросс-доменный фрейм читать нельзя
                continue
            if text and text.strip():
                sources.append(text)

        try:
            sources.append(await helper.html())
        except Exception:  # noqa: BLE001
            pass
        for frame in self.page.frames[1:]:
            try:
                sources.append(await frame.content())
            except Exception:  # noqa: BLE001
                continue
        hrefs = await self._anchor_hrefs()
        if hrefs:
            sources.append(hrefs)
        return sources

    async def _rows_by_selector(self, needle: str):
        """Строки списка по селекторам из конфига."""
        for row_selector in self.ctx.sel("outlook.message_rows"):
            rows = self.page.locator(f"{row_selector}:has-text('{needle}')")
            try:
                count = await rows.count()
            except Exception:  # noqa: BLE001
                continue
            if not count:
                continue
            self.log.info("Писем от CSFloat в списке: %d (%s)", count, row_selector)
            for index in range(min(count, 5)):
                yield f"{row_selector}#{index}", rows.nth(index)

    async def _rows_by_text(self, text: str):
        """Запасной путь: клик по видимому тексту письма, без опоры на разметку."""
        try:
            rows = self.page.get_by_text(text, exact=False)
            count = await rows.count()
        except Exception:  # noqa: BLE001
            return
        if not count:
            return
        self.log.info("Нашёл по тексту «%s»: %d совпадений", text, count)
        for index in range(min(count, 5)):
            yield f"текст «{text}»#{index}", rows.nth(index)

    async def _reading_pane_ready(self, helper: PageHelper, *, timeout: float = 10) -> bool:
        """Письмо считается открытым, когда ушла заглушка области чтения."""
        empty = self.ctx.sel("outlook.reading_pane_empty", required=False)
        if empty and not await helper.wait_gone(empty, timeout=timeout):
            return False
        await helper.settle(1.5)
        return True

    async def _anchor_hrefs(self) -> str:
        """Ссылка может жить только в href, но не в видимом тексте."""
        try:
            hrefs = await self.page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href).join('\\n')"
            )
            return hrefs or ""
        except Exception:  # noqa: BLE001
            return ""
