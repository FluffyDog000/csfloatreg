"""Модуль 1: регистрация на CSFloat через Steam + подтверждение почты."""
from __future__ import annotations

from ..errors import MaFileEncrypted, MaFileMissing, UnexpectedState
from ..pages.csfloat import CsFloatPage
from ..pages.steam_trade import SteamTradePage
from ..pages.mail.base import build_mail_provider
from .base import register


@register("registration")
class RegistrationModule:
    name = "registration"

    async def run(self, ctx) -> None:
        self._require_mafile(ctx)
        cs = CsFloatPage(await ctx.session.page("csfloat"), ctx)
        mail = None

        try:
            # 1-3. Вход на CSFloat через Steam
            async with ctx.step("csfloat_open", "открываю csfloat.com"):
                await cs.open_home()

            async with ctx.step("csfloat_login", "вход через Steam"):
                if await cs.is_logged_in():
                    ctx.log.info("Уже залогинены (cookies из state/)")
                else:
                    await cs.login_via_steam(ctx.account, ctx.mafile, ctx.steam_time)
                    await ctx.session.save_state("csfloat")

            async with ctx.step("csfloat_session_check", "проверяю, что аккаунт создан"):
                if not await cs.wait_logged_in():
                    raise UnexpectedState("сессия CSFloat не подтверждается после входа")

            # 4. Почта: либо мастер Onboard, либо поле в настройках
            async with ctx.step("csfloat_email_state", "смотрю текущее состояние почты"):
                if await cs.find_onboarding():
                    state = "onboarding"
                else:
                    await cs.open_account_page()
                    state = await cs.email_state()
                ctx.log.info("Состояние почты на CSFloat: %s", state)

            if state == "verified":
                ctx.log.info("Почта уже подтверждена — модуль завершён")
                return

            if state == "onboarding":
                # Почту открываем ДО запроса письма: так провайдер знает, какие письма
                # были в ящике раньше, и не примет старый токен за новый.
                async with ctx.step("mail_login", "открываю почту"):
                    mail = build_mail_provider(ctx)
                    await mail.login()

                # CSFloat присылает токен, а не ссылку: запрашиваем его и вводим здесь же
                async with ctx.step("csfloat_onboarding", "принимаю условия и запрашиваю токен"):
                    result = await cs.complete_onboarding(ctx.account.mail)
                    if result != "token_sent":
                        raise UnexpectedState(f"онбординг остановился: {result}")

                async with ctx.step("mail_wait_token", "жду письмо с токеном"):
                    token = await mail.wait_for_code(
                        ctx.cfg.get("csfloat.token_pattern"),
                        timeout_s=ctx.cfg.get("timeouts.mail_wait_s", 180),
                        poll_s=ctx.cfg.get("timeouts.mail_poll_s", 10),
                    )
                    ctx.data["token"] = token
                    ctx.log.info("Токен получен: %s… (%d символов)", token[:3], len(token))

                async with ctx.step("csfloat_submit_token", "ввожу токен на CSFloat"):
                    # вкладка CSFloat всё это время стояла на мастере: лишний переход
                    # сбросил бы его на шаг Verify Email и потребовал новый токен
                    if not await cs.onboarding_visible(timeout=4):
                        ctx.log.info("Окно Onboard закрылось — открываю профиль заново")
                        await cs.open_account_page()
                        if not await cs.find_onboarding():
                            raise UnexpectedState("окно Onboard закрылось, токен вводить некуда")
                    if not await cs.submit_token(token):
                        raise UnexpectedState("CSFloat не принял токен")
                ctx.log.info("Почта подтверждена")

                if ctx.cfg.get("csfloat.fill_trade_link", True):
                    trade_url = await self._trade_url(ctx)

                    async with ctx.step("csfloat_trade_link", "вставляю трейд-ссылку"):
                        if not await cs.onboarding_visible(timeout=4):
                            await cs.open_account_page()
                            if not await cs.find_onboarding():
                                raise UnexpectedState("окно Onboard закрылось, ссылку вставлять некуда")
                        if not await cs.submit_trade_link(trade_url):
                            raise UnexpectedState("CSFloat не принял трейд-ссылку")

                await ctx.session.save_state()
                return

            # 5. Почта открывается до запроса письма — см. комментарий выше
            async with ctx.step("mail_login", "открываю почту"):
                mail = build_mail_provider(ctx)
                await mail.login()

            if state != "pending":
                async with ctx.step("csfloat_set_email", "указываю почту и запрашиваю письмо"):
                    await cs.set_email(ctx.account.mail)

            # 6. Поиск письма. В состоянии 'pending' CSFloat отправил его раньше нас,
            #    поэтому там смотрим и на письма, которые уже лежали в ящике.
            async with ctx.step("mail_wait_link", "жду письмо от CSFloat"):
                link = await mail.wait_for_link(
                    ctx.cfg.get("csfloat.confirm_link_pattern"),
                    timeout_s=ctx.cfg.get("timeouts.mail_wait_s", 180),
                    poll_s=ctx.cfg.get("timeouts.mail_poll_s", 10),
                    include_existing=(state == "pending"),
                )
                ctx.data["confirm_link"] = link

            async with ctx.step("confirm_link", "открываю ссылку подтверждения"):
                await cs.open_confirmation_link(link)

            # 7. Проверка
            async with ctx.step("csfloat_verify_email", "проверяю, что почта подтверждена"):
                if not await cs.wait_email_verified():
                    raise UnexpectedState("CSFloat не показывает почту как подтверждённую")
                ctx.log.info("Почта подтверждена")

            if ctx.cfg.get("csfloat.fill_trade_link", True):
                # вставлять её здесь некуда (мастера нет), но в карточке аккаунта она нужна
                await self._trade_url(ctx)

            await ctx.session.save_state()
        finally:
            if mail is not None:
                await mail.close()

    @staticmethod
    async def _trade_url(ctx) -> str:
        """Трейд-ссылка из Steam. Сохраняется в bindings.json — её показывает менеджер."""
        saved = ""
        if ctx.bindings is not None:
            saved = ctx.bindings.entry(ctx.login).get("trade_url", "")
        if saved:
            ctx.log.info("Трейд-ссылка уже сохранена, Steam не трогаем")
            ctx.data["trade_url"] = saved
            return saved

        async with ctx.step("steam_trade_link", "беру трейд-ссылку из Steam"):
            trade = SteamTradePage(await ctx.session.page("steam_trade"), ctx)
            trade_url = await trade.fetch_trade_url()
            ctx.remember("trade_url", trade_url)
            return trade_url

    @staticmethod
    def _require_mafile(ctx) -> None:
        if ctx.mafile is None:
            raise MaFileMissing(f"maFile с account_name='{ctx.login}' не найден")
        if not ctx.mafile.shared_secret:
            raise MaFileEncrypted(
                f"maFile {ctx.mafile.path.name if ctx.mafile.path else '?'} зашифрован — "
                f"расшифруй его в SDA и положи в mafiles/"
            )
