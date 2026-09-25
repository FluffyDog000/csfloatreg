"""Чтение почты по IMAP — без API, ключей и антиботов.

У firstmail HTTP-API закрыт то антиботом на панели, то блокировкой по IP на
api-домене. IMAP таких сюрпризов не устраивает: логин и пароль от ящика у нас
уже есть, а протокол не зависит от того, что сервис думает о нашем IP.

Провайдер видит все письма, а не только последнее, и умеет заглядывать в папку
со спамом — письмо от CSFloat нередко лежит именно там.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message

from ...errors import MailBadCredentials, MailNotReceived, NetworkError
from .base import extract_link, extract_token, register

DEFAULTS = {
    "host": "",                 # пусто = подобрать по домену ящика
    "port": 993,
    "ssl": True,
    "folders": ["INBOX", "Junk", "Spam", "Junk E-mail"],
    "timeout_s": 30,
    "hosts": {},                # домен ящика -> сервер, если нужен свой
}

#: Как обычно называется IMAP-сервер, если явного адреса не задали.
HOST_TEMPLATES = ("imap.{domain}", "mail.{domain}", "imap.firstmail.ltd")


def host_candidates(address: str, settings: dict) -> list[str]:
    """Кандидаты в IMAP-серверы для конкретного ящика."""
    domain = address.rsplit("@", 1)[-1].lower()
    explicit = (settings.get("hosts") or {}).get(domain) or settings.get("host")
    if explicit:
        return [str(explicit)]
    return [template.format(domain=domain) for template in HOST_TEMPLATES]


def _decode(value) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001 — кривая кодировка не повод падать
        return str(value or "")


def message_texts(message: Message) -> list[str]:
    """Все текстовые куски письма: тема, text/plain, text/html."""
    parts = [_decode(message.get("Subject")), _decode(message.get("From"))]
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            parts.append(payload.decode(charset, errors="replace"))
        except LookupError:
            parts.append(payload.decode("utf-8", errors="replace"))
    return [p for p in parts if p]


class _Box:
    """Тонкая обёртка над imaplib: всё блокирующее уходит в поток."""

    def __init__(self, host: str, port: int, use_ssl: bool, timeout: float):
        self.host, self.port, self.ssl, self.timeout = host, port, use_ssl, timeout
        self.conn: imaplib.IMAP4 | None = None

    def connect(self, address: str, password: str) -> None:
        factory = imaplib.IMAP4_SSL if self.ssl else imaplib.IMAP4
        self.conn = factory(self.host, self.port, timeout=self.timeout)
        self.conn.login(address, password)

    def folders(self) -> list[str]:
        assert self.conn is not None
        ok, rows = self.conn.list()
        if ok != "OK":
            return []
        names = []
        for row in rows or []:
            text = row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row)
            match = re.search(r'"([^"]+)"$', text.strip()) or re.search(r"(\S+)$", text.strip())
            if match:
                names.append(match.group(1))
        return names

    def uids(self, folder: str) -> list[str]:
        assert self.conn is not None
        ok, _ = self.conn.select(f'"{folder}"', readonly=True)
        if ok != "OK":
            return []
        ok, data = self.conn.uid("search", None, "ALL")
        if ok != "OK" or not data or not data[0]:
            return []
        return data[0].decode().split()

    def fetch(self, folder: str, uid: str) -> Message | None:
        assert self.conn is not None
        self.conn.select(f'"{folder}"', readonly=True)
        ok, data = self.conn.uid("fetch", uid, "(RFC822)")
        if ok != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001 — разрыв при выходе ничего не значит
            pass
        self.conn = None


@register("imap")
class ImapProvider:
    """Ящик по IMAP. Работает с любым почтовиком, где есть логин и пароль."""

    name = "imap"

    def __init__(self, ctx):
        self.ctx = ctx
        self.log = ctx.log
        self.cfg = {**DEFAULTS, **(ctx.cfg.get("mail.imap") or {})}
        self.mail = ctx.account.mail
        self.password = ctx.account.mail_password
        self.box: _Box | None = None
        self._seen: set[tuple[str, str]] = set()
        self._folders: list[str] = []

    # ── подключение ──────────────────────────────────────────
    async def login(self) -> None:
        if not self.mail or not self.password:
            raise MailBadCredentials("в mails.txt нет почты или пароля от неё")

        errors = []
        for host in host_candidates(self.mail, self.cfg):
            box = _Box(host, int(self.cfg["port"]), bool(self.cfg["ssl"]), float(self.cfg["timeout_s"]))
            try:
                await asyncio.to_thread(box.connect, self.mail, self.password)
            except imaplib.IMAP4.error as exc:
                # сервер ответил, но логин не принял — перебирать хосты бессмысленно
                box.close()
                raise MailBadCredentials(f"IMAP {host}: {exc}") from None
            except (OSError, TimeoutError) as exc:
                errors.append(f"{host}: {exc}")
                box.close()
                continue
            self.box = box
            break

        if self.box is None:
            raise NetworkError("IMAP: ни один сервер не ответил — " + "; ".join(errors[:3]))

        available = await asyncio.to_thread(self.box.folders)
        wanted = [f for f in self.cfg["folders"] if f in available] or ["INBOX"]
        self._folders = wanted
        self._seen = await self._snapshot()
        self.log.info(
            "IMAP %s: вход выполнен, папки %s, писем сейчас %d",
            self.box.host, ", ".join(wanted), len(self._seen),
        )

    async def close(self) -> None:
        if self.box is not None:
            await asyncio.to_thread(self.box.close)
            self.box = None

    # ── ожидание письма ──────────────────────────────────────
    async def wait_for_link(
        self, pattern: str, *, timeout_s: float, poll_s: float, include_existing: bool = False
    ) -> str:
        return await self._wait(
            lambda message: extract_link(pattern, *message_texts(message)),
            timeout_s=timeout_s, poll_s=poll_s, what="ссылку", include_existing=include_existing,
        )

    async def wait_for_code(
        self, pattern: str, *, timeout_s: float, poll_s: float, include_existing: bool = False
    ) -> str:
        return await self._wait(
            lambda message: extract_token(pattern, *message_texts(message)),
            timeout_s=timeout_s, poll_s=poll_s, what="токен", include_existing=include_existing,
        )

    async def _snapshot(self) -> set[tuple[str, str]]:
        assert self.box is not None
        found: set[tuple[str, str]] = set()
        for folder in self._folders:
            for uid in await asyncio.to_thread(self.box.uids, folder):
                found.add((folder, uid))
        return found

    async def _wait(self, extractor, *, timeout_s: float, poll_s: float, what: str, include_existing: bool) -> str:
        if self.box is None:
            raise NetworkError("IMAP: соединение не открыто")
        if include_existing:
            self._seen = set()          # письмо могло прийти до начала ожидания

        import time as _time

        deadline = _time.monotonic() + timeout_s
        checked = 0
        while True:
            for folder, uid in sorted(await self._snapshot() - self._seen, key=lambda x: int(x[1])):
                self._seen.add((folder, uid))
                message = await asyncio.to_thread(self.box.fetch, folder, uid)
                if message is None:
                    continue
                checked += 1
                value = extractor(message)
                if value:
                    self.log.info("Письмо найдено в папке %s (uid %s)", folder, uid)
                    return value
            if _time.monotonic() >= deadline:
                raise MailNotReceived(
                    f"IMAP: за {timeout_s:.0f} c не пришло письмо, из которого можно взять {what} "
                    f"(новых писем просмотрено: {checked}, папки: {', '.join(self._folders)})"
                )
            await asyncio.sleep(poll_s)
