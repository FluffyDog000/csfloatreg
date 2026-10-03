"""Мобильный вход в Steam — тот же путь, которым идёт SDA.

Браузер для подтверждений не нужен: SDA логинится сам и держит свои токены.
Порядок у Steam такой:

    1. взять RSA-ключ для имени аккаунта,
    2. зашифровать им пароль и начать сессию,
    3. подтвердить её кодом Steam Guard из shared_secret,
    4. забрать refresh_token и access_token.

Дальше access_token живёт сутки, а refresh_token — месяцы, и новый access
получается одним запросом. Из него собираются cookies мобильного приложения.
"""
from __future__ import annotations

import base64
import json
import secrets

API = "https://api.steampowered.com/IAuthenticationService"
RSA_URL = f"{API}/GetPasswordRSAPublicKey/v1/"
BEGIN_URL = f"{API}/BeginAuthSessionViaCredentials/v1/"
GUARD_URL = f"{API}/UpdateAuthSessionWithSteamGuardCode/v1/"
POLL_URL = f"{API}/PollAuthSessionStatus/v1/"
TOKEN_URL = f"{API}/GenerateAccessTokenForApp/v1/"

#: Так представляется мобильное приложение Steam.
DEVICE_NAME = "Steam App"
WEBSITE_ID = "Mobile"
PLATFORM_MOBILE = 3          # k_EAuthTokenPlatformType_MobileApp
CODE_TOTP = 3                # k_EAuthSessionGuardType_DeviceCode


class AuthError(RuntimeError):
    """Steam не дал войти — причина в тексте."""


def encrypt_password(password: str, modulus_hex: str, exponent_hex: str) -> str:
    """RSA PKCS#1 v1.5, как это делает клиент Steam. Без сторонних библиотек."""
    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    size = (modulus.bit_length() + 7) // 8
    message = password.encode()
    if len(message) > size - 11:
        raise AuthError("пароль слишком длинный для ключа Steam")

    # заполнитель PKCS#1: нули в нём запрещены, иначе Steam не разберёт пароль
    padding = bytearray()
    while len(padding) < size - len(message) - 3:
        byte = secrets.token_bytes(1)
        if byte != b"\x00":
            padding += byte
    block = b"\x00\x02" + bytes(padding) + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), exponent, modulus)
    return base64.b64encode(cipher.to_bytes(size, "big")).decode()


def _answer(body: str, status: int, headers: dict, what: str) -> dict:
    """Тело ответа Steam. Пустое — смотрим x-eresult, там код отказа."""
    eresult = str((headers or {}).get("x-eresult") or "")
    try:
        payload = json.loads(body) if body.strip() else {}
    except ValueError:
        raise AuthError(f"{what}: Steam ответил не JSON (HTTP {status}, {len(body)} символов)") from None
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        raise AuthError(f"{what}: в ответе нет данных (HTTP {status}, eresult {eresult or '—'})")
    if not response and eresult not in ("", "1"):
        raise AuthError(f"{what}: Steam отказал, eresult {eresult}")
    return response


async def _post(request, url: str, fields: dict, what: str, timeout_ms: int) -> dict:
    response = await request.post(url, form={k: str(v) for k, v in fields.items()}, timeout=timeout_ms)
    return _answer(await response.text(), response.status, dict(response.headers), what)


async def login(request, *, account_name: str, password: str, code: str, timeout_ms: int = 30000) -> dict:
    """Полный мобильный вход. Возвращает токены сессии."""
    if not password:
        raise AuthError(f"для {account_name} нет пароля — входить нечем")

    response = await request.get(RSA_URL, params={"account_name": account_name}, timeout=timeout_ms)
    keys = _answer(await response.text(), response.status, dict(response.headers), "ключ шифрования")
    if not keys.get("publickey_mod"):
        raise AuthError("Steam не дал ключ шифрования — проверь имя аккаунта")

    begin = await _post(
        request, BEGIN_URL,
        {
            "account_name": account_name,
            "encrypted_password": encrypt_password(password, keys["publickey_mod"], keys["publickey_exp"]),
            "encryption_timestamp": keys.get("timestamp", ""),
            "remember_login": "true",
            "persistence": 1,
            "website_id": WEBSITE_ID,
            "platform_type": PLATFORM_MOBILE,
            "device_friendly_name": DEVICE_NAME,
        },
        "начало входа", timeout_ms,
    )
    client_id = str(begin.get("client_id") or "")
    request_id = str(begin.get("request_id") or "")
    steam_id = str(begin.get("steamid") or "")
    if not client_id or not request_id:
        raise AuthError("Steam не принял логин и пароль")

    await _post(
        request, GUARD_URL,
        {"client_id": client_id, "steamid": steam_id, "code": code, "code_type": CODE_TOTP},
        "код Steam Guard", timeout_ms,
    )

    poll = await _post(
        request, POLL_URL,
        {"client_id": client_id, "request_id": request_id},
        "получение токенов", timeout_ms,
    )
    refresh = str(poll.get("refresh_token") or "")
    access = str(poll.get("access_token") or "")
    if not refresh:
        raise AuthError("Steam не вернул refresh_token — вход не завершён")
    return {"refresh_token": refresh, "access_token": access, "steam_id": str(poll.get("steamid") or steam_id)}


async def refresh_access(request, *, refresh_token: str, steam_id: str, timeout_ms: int = 30000) -> str:
    """Новый access_token по refresh_token. Это и делает SDA каждые сутки."""
    answer = await _post(
        request, TOKEN_URL,
        {"refresh_token": refresh_token, "steamid": steam_id},
        "обновление токена", timeout_ms,
    )
    access = str(answer.get("access_token") or "")
    if not access:
        raise AuthError("Steam не вернул access_token — refresh_token больше не годится")
    return access
