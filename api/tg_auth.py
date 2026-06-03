"""
api/tg_auth.py
──────────────
Проверка подписи Telegram WebApp initData (HMAC-SHA256).

Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Использование в FastAPI:
    from api.tg_auth import require_webapp_user

    @app.post("/api/save_score")
    async def save_score(request: Request, tg_user=Depends(require_webapp_user)):
        # tg_user["id"] гарантированно совпадает с подписанным Telegram user_id
        ...
"""

import hashlib
import hmac
import json
import os
import time
from typing import Optional
from urllib.parse import parse_qsl, unquote

from fastapi import Depends, Header, HTTPException, Request

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
INTERNAL_SECRET: str = os.getenv("INTERNAL_SECRET", "")
# Фоллбэк: если INTERNAL_SECRET не задан, выводим его детерминированно из BOT_TOKEN.
# Бот делает то же самое (та же строка) → секреты совпадают без отдельной env-переменной.
if not INTERNAL_SECRET and BOT_TOKEN:
    INTERNAL_SECRET = hashlib.sha256(("chin-internal:" + BOT_TOKEN).encode()).hexdigest()

# Окно валидности initData — 1 час. Telegram рекомендует не больше суток,
# мы берём 1 час: достаточно для сессии, слишком мало для реплея.
INIT_DATA_MAX_AGE_SEC = 3600


def _make_secret_key(bot_token: str) -> bytes:
    """HMAC key = HMAC-SHA256("WebAppData", bot_token)"""
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def verify_init_data(init_data: str, bot_token: str) -> dict:
    """
    Проверяет подпись initData и возвращает распарсенный объект user.
    Бросает ValueError с описанием причины при любой ошибке.
    """
    if not init_data:
        raise ValueError("empty initData")
    if not bot_token:
        raise ValueError("BOT_TOKEN not configured")

    params = dict(parse_qsl(init_data, keep_blank_values=True))

    received_hash = params.pop("hash", None)
    if not received_hash:
        raise ValueError("missing hash")

    # Проверяем свежесть
    auth_date = params.get("auth_date")
    if not auth_date:
        raise ValueError("missing auth_date")
    age = int(time.time()) - int(auth_date)
    if age > INIT_DATA_MAX_AGE_SEC:
        raise ValueError(f"initData expired ({age}s old)")

    # Строим data-check-string: отсортированные пары key=value через \n
    data_check = "\n".join(
        f"{k}={v}" for k, v in sorted(params.items())
    )

    secret_key = _make_secret_key(bot_token)
    expected_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise ValueError("invalid signature")

    user_raw = params.get("user")
    if not user_raw:
        raise ValueError("missing user field")

    try:
        user = json.loads(unquote(user_raw))
    except Exception:
        raise ValueError("malformed user JSON")

    if not user.get("id"):
        raise ValueError("user.id missing")

    return user


# ── FastAPI dependencies ───────────────────────────────────────────

def _extract_init_data(request: Request) -> Optional[str]:
    """Достаём initData из заголовка X-Telegram-Init-Data."""
    return request.headers.get("X-Telegram-Init-Data")


async def require_webapp_user(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(default=None),
) -> dict:
    """
    Dependency: проверяет initData и возвращает tg_user dict.
    Бросает 401 если подпись невалидна или устарела.
    """
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Init-Data header")
    try:
        user = verify_init_data(x_telegram_init_data, BOT_TOKEN)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid initData: {e}")
    return user


async def require_internal(
    x_internal_secret: Optional[str] = Header(default=None),
) -> None:
    """
    Dependency для endpoints которые вызывает только бот (не WebApp).
    Проверяет заголовок X-Internal-Secret.
    """
    if not INTERNAL_SECRET:
        raise HTTPException(status_code=500, detail="INTERNAL_SECRET not configured")
    if x_internal_secret != INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
