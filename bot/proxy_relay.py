"""Локальный SOCKS5-релей.

Playwright (и Firefox под ним) не умеет авторизацию в SOCKS5-прокси.
Релей поднимает на 127.0.0.1 сокет без авторизации, а в апстрим ходит уже
с логином/паролем. Браузеру отдаётся адрес релея.

Для http-прокси это не нужно — там авторизация работает штатно.
"""
from __future__ import annotations

import asyncio
import contextlib

from .models import Proxy

_NO_AUTH = 0x00
_USER_PASS = 0x02


async def _read_addr(reader: asyncio.StreamReader, atyp: int) -> bytes:
    """Читает адрес+порт SOCKS5 и возвращает сырые байты (для проброса as-is)."""
    if atyp == 0x01:  # IPv4
        return await reader.readexactly(4 + 2)
    if atyp == 0x03:  # domain
        length = await reader.readexactly(1)
        return length + await reader.readexactly(length[0] + 2)
    if atyp == 0x04:  # IPv6
        return await reader.readexactly(16 + 2)
    raise ValueError(f"неизвестный ATYP: {atyp}")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


class SocksRelay:
    def __init__(self, upstream: Proxy, host: str = "127.0.0.1", logger=None):
        self.upstream = upstream
        self.host = host
        self.port: int | None = None
        self.logger = logger
        self._server: asyncio.AbstractServer | None = None

    @property
    def url(self) -> str:
        return f"socks5://{self.host}:{self.port}"

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.logger:
            self.logger.debug("SOCKS-релей %s -> %s", self.url, self.upstream.safe())
        return self.url

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    # ── обработка одного соединения ──────────────────────────
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_writer: asyncio.StreamWriter | None = None
        try:
            # 1. Приветствие от браузера — отвечаем "без авторизации"
            ver, nmethods = await reader.readexactly(2)
            await reader.readexactly(nmethods)
            if ver != 0x05:
                raise ValueError(f"не SOCKS5: ver={ver}")
            writer.write(bytes([0x05, _NO_AUTH]))
            await writer.drain()

            # 2. Запрос CONNECT — читаем целиком, чтобы переслать без изменений
            head = await reader.readexactly(4)
            request = head + await _read_addr(reader, head[3])

            # 3. Апстрим с авторизацией
            up_reader, up_writer = await asyncio.open_connection(self.upstream.host, self.upstream.port)
            up_writer.write(bytes([0x05, 0x01, _USER_PASS]))
            await up_writer.drain()
            _, method = await up_reader.readexactly(2)
            if method == _USER_PASS:
                user = (self.upstream.username or "").encode()
                password = (self.upstream.password or "").encode()
                up_writer.write(bytes([0x01, len(user)]) + user + bytes([len(password)]) + password)
                await up_writer.drain()
                _, status = await up_reader.readexactly(2)
                if status != 0x00:
                    raise ValueError("апстрим отклонил логин/пароль прокси")
            elif method != _NO_AUTH:
                raise ValueError(f"апстрим не поддерживает user/pass (method={method})")

            # 4. Проброс запроса и ответа
            up_writer.write(request)
            await up_writer.drain()
            reply_head = await up_reader.readexactly(4)
            reply = reply_head + await _read_addr(up_reader, reply_head[3])
            writer.write(reply)
            await writer.drain()

            # 5. Двусторонний поток
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError) as exc:
            # молчаливый отказ здесь выглядит как «General SOCKS server failure»
            # на стороне браузера, поэтому причину пишем явно
            if self.logger:
                self.logger.warning(
                    "SOCKS-релей не смог проксировать соединение через %s: %s: %s",
                    self.upstream.safe(), type(exc).__name__, exc,
                )
            with contextlib.suppress(Exception):
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
        finally:
            for w in (writer, up_writer):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()


async def maybe_relay(proxy: Proxy, cfg, logger=None) -> tuple[dict, SocksRelay | None]:
    """Возвращает (proxy-конфиг для Playwright, релей или None)."""
    if proxy.needs_socks_relay and cfg.get("proxy.relay_socks_auth", True):
        relay = SocksRelay(proxy, cfg.get("proxy.relay_host", "127.0.0.1"), logger)
        await relay.start()
        return {"server": relay.url}, relay
    return proxy.playwright(), None
