"""Генерация кодов Steam Guard (TOTP) из shared_secret + синхронизация времени.

Алгоритм Steam: обычный TOTP/HMAC-SHA1 с шагом 30 секунд, но вместо цифр —
алфавит из 26 символов и 5 знаков на выходе. Внешних зависимостей не нужно.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import struct
import time
import urllib.error
import urllib.request

STEAM_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"
_INTERVAL = 30


def generate_code(shared_secret: str, timestamp: float | None = None) -> str:
    """5-символьный код Steam Guard."""
    if not shared_secret:
        raise ValueError("пустой shared_secret")
    try:
        key = base64.b64decode(shared_secret)
    except Exception as exc:  # noqa: BLE001 — хотим понятное сообщение выше по стеку
        raise ValueError(f"shared_secret не является base64: {exc}") from exc

    ts = int(timestamp if timestamp is not None else time.time())
    counter = struct.pack(">Q", ts // _INTERVAL)
    digest = hmac.new(key, counter, hashlib.sha1).digest()

    offset = digest[19] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF

    code = []
    for _ in range(5):
        code.append(STEAM_ALPHABET[code_int % len(STEAM_ALPHABET)])
        code_int //= len(STEAM_ALPHABET)
    return "".join(code)


def seconds_until_next_code(timestamp: float | None = None) -> float:
    ts = timestamp if timestamp is not None else time.time()
    return _INTERVAL - (ts % _INTERVAL)


def confirmation_key(identity_secret: str, tag: str, timestamp: float) -> str:
    """Ключ для мобильных подтверждений: тот же HMAC, но с identity_secret и тегом."""
    if not identity_secret:
        raise ValueError("пустой identity_secret")
    buf = struct.pack(">Q", int(timestamp)) + tag.encode()
    digest = hmac.new(base64.b64decode(identity_secret), buf, hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def device_id(steam_id: str) -> str:
    """Идентификатор устройства, как его считает SDA, если его нет в maFile."""
    digest = hashlib.sha1(str(steam_id).encode()).hexdigest()
    return "android:" + "-".join([digest[:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32]])


class SteamTime:
    """Хранит смещение локальных часов относительно серверов Steam.

    Steam отклоняет коды при расхождении больше ~20 секунд, а на VPS часы
    уезжают регулярно, поэтому синхронизируемся один раз за прогон.
    """

    def __init__(self, url: str, *, enabled: bool = True):
        self.url = url
        self.enabled = enabled
        self.offset: float = 0.0
        self.synced = False

    def _fetch_offset(self) -> float:
        request = urllib.request.Request(
            self.url,
            data=b"steamid=0",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        server_time = int(payload["response"]["server_time"])
        return server_time - time.time()

    async def sync(self, logger=None) -> float:
        if not self.enabled:
            return 0.0
        try:
            self.offset = await asyncio.to_thread(self._fetch_offset)
            self.synced = True
            if logger:
                logger.info("Время Steam синхронизировано, смещение %.1f с", self.offset)
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError, OSError) as exc:
            self.offset = 0.0
            if logger:
                logger.warning("Не удалось синхронизировать время Steam (%s), работаем по локальным часам", exc)
        return self.offset

    def now(self) -> float:
        return time.time() + self.offset

    def code(self, shared_secret: str) -> str:
        return generate_code(shared_secret, self.now())

    def confirmation_key(self, identity_secret: str, tag: str) -> tuple[str, int]:
        moment = int(self.now())
        return confirmation_key(identity_secret, tag, moment), moment

    def fresh_code(self, shared_secret: str, *, min_lifetime: float = 5.0) -> tuple[str, float]:
        """Код и сколько секунд он ещё проживёт.

        Если код вот-вот протухнет, зовущий может подождать следующее окно —
        иначе Steam покажет 'Неверный код' на ровном месте.
        """
        now = self.now()
        left = seconds_until_next_code(now)
        if left < min_lifetime:
            now += left + 0.5
            left = _INTERVAL
        return generate_code(shared_secret, now), left
