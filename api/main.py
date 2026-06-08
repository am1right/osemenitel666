import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any
from pathlib import Path
from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from telegram import Bot, LabeledPrice

from api.database import (
    init_db, save_score, get_leaderboard, get_user_stats,
    create_contest, get_active_contests, get_contest,
    get_unannounced_finished_contests, mark_contest_announced,
    finish_contest, mark_prize_sent, cancel_contest,
    get_wallet, spend_wallet, topup_wallet, get_wallet_transactions,
    register_referral, claim_referral_reward, get_referral_stats,
    is_already_referred, REFERRAL_STARS, REFERRAL_ENERGY,
    try_grant_referral_reward, get_referral_by_invitee,
    admin_adjust_energy, upgrade_regen_speed,
    get_energy, spend_energy, get_user_flags,
    CASE_PRICE, grant_case_reward, confirm_case_reward,
    get_case_settings, save_case_settings, get_case_valuable_cooldown_status,
    add_announce_chat, remove_announce_chat, get_announce_chats,
    upsert_tg_username, admin_get_all_players,
    get_user_bonus_status, grant_bonus, daily_checkin, get_daily_checkin_status,
    BONUS_CHANNEL, BONUS_CHAT, BONUS_SHARE,
    BONUS_CHANNEL_STARS, BONUS_CHAT_STARS, BONUS_SHARE_STARS, DAILY_CHECKIN_STARS,
)

try:
    from api.tg_auth import require_webapp_user, require_internal
except ImportError:
    from tg_auth import require_webapp_user, require_internal

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / "config" / ".env")

app = FastAPI(title="Chin Games API")

# ── Referral Routes ─────────────────────────────────────────────────
from api.referral_routes import router as referral_router

if referral_router is not None:
    app.include_router(referral_router, prefix="/api/referral")
else:
    logger.error("❌ referral_router не инициализирован — проверь логи referral_routes.py")

from api.admin_routes import router as admin_router, is_admin

if admin_router is not None:
    app.include_router(admin_router, prefix="/api/admin")
else:
    logger.error("❌ admin_router не инициализирован — проверь логи admin_routes.py")

# ── Durak Online Routes ────────────────────────────────
_start_durak_sweeper = None
try:
    from api.durak_routes import router as durak_router, start_durak_sweeper as _start_durak_sweeper
    if durak_router is not None:
        app.include_router(durak_router)
        logger.info("✅ Durak routes mounted at /api/durak")
    else:
        logger.warning("⚠️ durak_routes router is None")
except Exception as e:
    logger.error(f"❌ Не удалось подключить Durak routes: {e}")


@app.on_event("startup")
async def _start_background_tasks():
    # Фоновый sweeper брошенных игр Дурака
    if _start_durak_sweeper is not None:
        try:
            _start_durak_sweeper()
        except Exception as e:
            logger.error(f"❌ Не удалось запустить Durak sweeper: {e}")
    # Фоновый напоминатель «остался 1 час» по соревнованиям
    try:
        asyncio.create_task(_contest_reminder_loop())
    except Exception as e:
        logger.error(f"❌ Не удалось запустить contest reminder: {e}")

WEBAPP_URL    = os.getenv("WEBAPP_URL", "http://localhost:8000")
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "chingamebot")  # юзернейм бота без @
# Числовые TG ID админов для дублирования уведомлений о призах
ADMIN_TG_IDS  = [int(x) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().lstrip("-").isdigit()]

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@ZeroOrOneOFF")
REQUIRED_CHAT    = os.getenv("REQUIRED_CHAT",    "@zeroandonechat")

_sub_cache: Dict[int, Any] = {}  # user_id -> (timestamp, result)
_SUB_CACHE_TTL = 120  # секунд

async def _check_subscription(user_id: int) -> Dict[str, bool]:
    """Проверяет подписку пользователя на канал и чат через Bot API (кеш 2 мин)."""
    import time
    now = time.monotonic()
    cached = _sub_cache.get(user_id)
    if cached and now - cached[0] < _SUB_CACHE_TTL:
        return cached[1]

    result = {"channel": False, "chat": False}
    if not BOT_TOKEN:
        return {"channel": True, "chat": True}  # dev-режим
    try:
        bot = Bot(token=BOT_TOKEN)
        async with asyncio.timeout(5):
            for key, chat_id in [("channel", REQUIRED_CHANNEL), ("chat", REQUIRED_CHAT)]:
                try:
                    member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                    result[key] = member.status not in ("left", "kicked", "banned")
                except Exception:
                    result[key] = False
    except Exception:
        pass
    _sub_cache[user_id] = (now, result)
    return result

VALID_GAMES = ("math", "2048", "snake", "flappy")
GAME_LABELS = {"math": "Math Master", "2048": "2048", "snake": "Snake", "flappy": "Flappy Chin"}

TG_GIFT_EMOJI = {
    "bear": "🧸", "rose": "🌹", "cake": "🎂", "heart": "❤️",
    "star": "⭐", "fire": "🔥", "diamond": "💎", "crown": "👑",
    "gift": "🎁", "balloon": "🎈", "trophy": "🏆", "flower": "🌸",
    "cookie": "🍪", "candy": "🍬", "ring": "💍", "kiss": "💋",
}

def _tg_gift_label(gift_id: str) -> str:
    g = (gift_id or "").lower().strip()
    return TG_GIFT_EMOJI.get(g, gift_id or "подарок")
# Постим в канал (он сам пересылает в связанный чат, если он есть)
ANNOUNCE_CHATS = ["@ZeroOrOneOFF"] + \
    [c.strip() for c in os.getenv("ANNOUNCE_CHATS", "").split(",") if c.strip()]
ANNOUNCE_EXCLUDE = set()

# ── Анти-чит: токены игровой сессии ─────────────────────────────────
# Счёт нельзя засчитать без одноразового токена, выданного сервером на старте
# партии (подписан HMAC, привязан к id+игре+времени). Защищает от подделки
# счёта из devtools/replay и от сабмита без реальной игры.
import hmac as _hmac, hashlib as _hashlib, time as _time, secrets as _secrets
_GAME_TOKEN_SECRET = _hashlib.sha256(("chin-game-token:" + BOT_TOKEN).encode()).digest()
_used_game_nonces: dict[str, float] = {}        # nonce -> expiry ts (одноразовость)
_GAME_TOKEN_TTL = 3 * 60 * 60                    # токен живёт 3 часа
# Правдоподобный прирост очков в секунду по играм (для проверки скорости).
# base — стартовый запас, rate — макс очков/сек. Сделано с запасом, чтобы не
# резать честных игроков, но резать «999 за 3 секунды».
_SCORE_RATE = {
    "math":   {"base": 20,  "rate": 6.0},
    "snake":  {"base": 100, "rate": 40.0},
    "flappy": {"base": 30,  "rate": 12.0},
    "2048":   {"base": 1000, "rate": 3000.0},
}


def _issue_game_token(uid: int, game: str) -> str:
    ts = int(_time.time())
    nonce = _secrets.token_hex(8)
    payload = f"{uid}.{game}.{ts}.{nonce}"
    sig = _hmac.new(_GAME_TOKEN_SECRET, payload.encode(), _hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _verify_game_token(token: str, uid: int, game: str, score: int) -> tuple[bool, str]:
    """Проверяет токен сессии. Возвращает (ok, причина)."""
    if not token:
        return False, "no token"
    try:
        p_uid, p_game, p_ts, p_nonce, sig = token.split(".")
    except ValueError:
        return False, "bad format"
    payload = f"{p_uid}.{p_game}.{p_ts}.{p_nonce}"
    expected = _hmac.new(_GAME_TOKEN_SECRET, payload.encode(), _hashlib.sha256).hexdigest()[:32]
    if not _hmac.compare_digest(expected, sig):
        return False, "bad sig"
    if p_uid != str(uid) or p_game != game:
        return False, "mismatch"
    now = _time.time()
    age = now - int(p_ts)
    if age < 0 or age > _GAME_TOKEN_TTL:
        return False, "expired"
    # Допускаем несколько сохранений по одному токену (смерть → «продолжить» →
    # снова конец): защищает rate-проверка ниже (счёт растёт пропорционально
    # реально прошедшему времени), поэтому жёсткой одноразовости нет.
    # Скорость: счёт должен быть правдоподобен для прошедшего времени
    rc = _SCORE_RATE.get(game)
    if rc and score > rc["base"] + rc["rate"] * age:
        return False, f"too fast ({score} in {int(age)}s)"
    return True, "ok"


STATIC_DIR = BASE_DIR / "webapp"

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="webapp")

init_db()

logger.info(f"🔑 BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING'}")


# ── Helpers ────────────────────────────────────────────────────────

def get_bot() -> Bot:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN not configured")
    return Bot(token=BOT_TOKEN)


# ── Scores ─────────────────────────────────────────────────────────

CONTINUE_COST_STARS = 10


@app.post("/api/game/continue")
async def api_game_continue(tg_user: dict = Depends(require_webapp_user)):
    """Продолжить игру после проигрыша за 10 ⭐ (списание с кошелька)."""
    uid = int(tg_user["id"])
    res = spend_wallet(uid, CONTINUE_COST_STARS, "Продолжение игры")
    if not res.get("ok"):
        raise HTTPException(status_code=402, detail={
            "reason": "insufficient_funds", "need": CONTINUE_COST_STARS,
            "balance": res.get("balance", 0),
        })
    return {"ok": True, "balance": res["balance"], "cost": CONTINUE_COST_STARS}


@app.post("/api/game/start")
async def api_game_start(request: Request, tg_user: dict = Depends(require_webapp_user)):
    """Выдаёт одноразовый токен сессии. Без него счёт не засчитается."""
    data = await request.json()
    game = (data.get("game") or "").strip()
    if game not in VALID_GAMES:
        raise HTTPException(status_code=400, detail="Unknown game")
    return {"token": _issue_game_token(int(tg_user["id"]), game)}


@app.post("/api/save_score")
async def api_save_score(request: Request, tg_user: dict = Depends(require_webapp_user)):
    try:
        data = await request.json()
        user_id    = data.get("user_id")
        first_name = data.get("first_name") or data.get("username") or "Игрок"
        game_name  = data.get("game")
        score      = data.get("score")
        if not all([user_id, game_name, score is not None]):
            raise HTTPException(status_code=400, detail="Missing fields")
        if int(user_id) != int(tg_user["id"]):
            raise HTTPException(status_code=403, detail="user_id mismatch")
        # Серверный потолок очков на игру — отсекает накрутку клиентского счёта.
        score = int(score)
        if score < 0:
            raise HTTPException(status_code=400, detail="Invalid score")
        _max = {"math": 200, "snake": 2300, "flappy": 1000, "2048": 200000}.get(game_name)
        if _max is not None and score > _max:
            logger.warning(f"[ANTICHEAT] {user_id} score {score} > cap {_max} for {game_name}")
            raise HTTPException(status_code=400, detail="Score exceeds plausible limit")
        # Токен сессии: счёт засчитывается только при валидном одноразовом токене,
        # выданном /api/game/start (привязка к id+игре+времени + проверка скорости).
        ok, reason = _verify_game_token(data.get("token"), int(tg_user["id"]), game_name, score)
        if not ok:
            logger.warning(f"[ANTICHEAT] {user_id} {game_name} score {score} rejected: {reason}")
            raise HTTPException(status_code=403, detail="Invalid game session")
        try:
            if tg_user.get("username"):
                upsert_tg_username(int(tg_user["id"]), tg_user["username"])
        except Exception:
            pass
        result = save_score(user_id, first_name, game_name, score)

        # ── Реферальная проверка: начислить награду если реферал достиг 3 игр ──
        reward = None
        try:
            reward = try_grant_referral_reward(user_id)
            if reward:
                inviter_id  = reward["inviter_id"]
                stars       = reward["stars"]
                energy      = reward["energy"]
                new_balance = reward["new_balance"]
                invitee_ref = get_referral_by_invitee(user_id)
                invitee_name = invitee_ref["first_name"] if invitee_ref else str(user_id)
                bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
                if bot:
                    try:
                        await bot.send_message(
                            chat_id=inviter_id,
                            parse_mode="HTML",
                            text=(
                                f"🎉 Твой реферал <b>{invitee_name}</b> отыграл 3 игры!\n\n"
                                f"Ты получил:\n"
                                f"⭐ +{stars} Stars на кошелёк (баланс: {new_balance})\n"
                                f"⚡ +{energy} энергии\n\n"
                                f"Продолжай приглашать друзей! 🚀"
                            )
                        )
                    except Exception as notify_err:
                        logger.warning(f"[REF] Не удалось уведомить inviter {inviter_id}: {notify_err}")
                logger.info(f"[REF] Reward granted: invitee={user_id} inviter={inviter_id} +{stars}⭐ +{energy}⚡")
        except Exception as ref_err:
            logger.warning(f"[REF] check_reward error for user {user_id}: {ref_err}")

        return {
            "status": "success",
            "new_record": result.get("new_record", False),
            "referral_reward": {"stars": reward["stars"], "energy": reward["energy"]} if reward else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/leaderboard/{game_name}")
async def api_get_leaderboard(game_name: str):
    try:
        return {"game": game_name, "leaders": get_leaderboard(game_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats/{user_id}/{game_name}")
async def api_get_stats(user_id: int, game_name: str):
    try:
        stats = get_user_stats(user_id, game_name)
        if stats is None:
            raise HTTPException(status_code=404, detail="No stats found")
        return stats
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Shop ───────────────────────────────────────────────────────────

# amount (энергия) → цена в Stars. Должно совпадать с shop.html и energy.js
# Энергия теперь батарея 0..100%. amount = % заряда → цена в Stars.
# ~0.32⭐ за 1% (эквивалент старому: 4⭐/энергия × 8 = полный бак 32⭐).
ENERGY_PACKS: dict[int, int] = {15: 5, 30: 10, 50: 16, 75: 24, 100: 32}
ENERGY_MAX = 100  # батарея


@app.post("/api/shop/create_invoice")
async def api_create_invoice(request: Request):
    try:
        data     = await request.json()
        user_id  = data.get("user_id")
        amount   = int(data.get("amount", 0))
        stars    = int(data.get("stars", 0))
        label    = data.get("label", f"+{amount} ⚡ Энергия")

        if not user_id:
            raise HTTPException(status_code=400, detail="Missing user_id")
        if int(user_id) != int(tg_user["id"]):
            raise HTTPException(status_code=403, detail="user_id mismatch")
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Invalid amount")

        expected_stars = ENERGY_PACKS.get(amount)
        if expected_stars is None or expected_stars != stars:
            logger.warning(f"Invalid pack: amount={amount}, stars={stars}. Known packs: {ENERGY_PACKS}")
            raise HTTPException(status_code=400, detail=f"Invalid pack or price. Expected: {ENERGY_PACKS.get(amount)}")

        bot = get_bot()
        invoice_link = await bot.create_invoice_link(
            title=label,
            description=f"Покупка {amount} единиц энергии в Chin Games",
            payload=f"energy:{user_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=label, amount=expected_stars)],
        )
        return {"invoice_url": invoice_link}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_invoice error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/shop/confirm")
async def api_shop_confirm(request: Request, _: None = Depends(require_internal)):
    try:
        data = await request.json()
        user_id = int(data.get("user_id", 0))
        amount = int(data.get("amount", 0))
        if user_id and amount > 0:
            admin_adjust_energy(user_id, amount)
        logger.info(f"[SHOP] confirm: user={user_id} +{amount} energy")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/shop/buy_energy")
async def api_buy_energy(request: Request, tg_user: dict = Depends(require_webapp_user)):
    """
    Покупка энергии с приоритетом внутреннего кошелька.

    Логика:
    1. Проверяем баланс кошелька пользователя.
    2. Если хватает — списываем Stars из кошелька, возвращаем {"method": "wallet", "balance": N}.
    3. Если не хватает — возвращаем {"method": "invoice", "invoice_url": "...", "short": K},
       фронт открывает Telegram Invoice на полную стоимость.
    """
    try:
        data       = await request.json()
        user_id    = data.get("user_id")
        amount     = int(data.get("amount", 0))   # единицы энергии
        stars      = int(data.get("stars", 0))     # цена пака в Stars

        if not user_id:
            raise HTTPException(status_code=400, detail="Missing user_id")
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Invalid amount")

        expected_stars = ENERGY_PACKS.get(amount)
        if expected_stars is None or expected_stars != stars:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid pack or price. Expected: {ENERGY_PACKS.get(amount)}"
            )

        # ── Проверяем баланс кошелька ────────────────────────────
        wallet  = get_wallet(user_id)
        balance = wallet["balance"]

        if balance >= stars:
            # Достаточно звёзд — списываем из кошелька
            result = spend_wallet(
                user_id=user_id,
                amount=stars,
                description=f"Покупка {amount} ⚡ энергии"
            )
            if result["ok"]:
                admin_adjust_energy(user_id, amount)   # зачисляем энергию на сервере
                logger.info(f"[SHOP] wallet buy: user={user_id} -{stars}⭐ +{amount}⚡ balance={result['balance']}")
                return {
                    "method": "wallet",
                    "balance": result["balance"],
                    "amount": amount,
                }
            # Гонка: баланс изменился между get и spend — падаем на invoice
            logger.warning(f"[SHOP] Wallet race condition for user {user_id}, falling back to invoice")
            balance = result["balance"]  # актуальный баланс после неудачной попытки

        # ── Не хватает — создаём Telegram Invoice ────────────────
        short = stars - balance
        bot   = get_bot()
        label = f"+{amount} ⚡ Энергия"
        invoice_link = await bot.create_invoice_link(
            title=label,
            description=f"Покупка {amount} единиц энергии в Chin Games",
            payload=f"energy:{user_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=label, amount=stars)],
        )
        logger.info(f"[SHOP] invoice buy: user={user_id} amount={amount} stars={stars} short={short}")
        return {
            "method": "invoice",
            "invoice_url": invoice_link,
            "balance": balance,
            "short": short,
            "amount": amount,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"buy_energy error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Апгрейд скорости восстановления энергии (батарея) ──────────────
# level → {mult, stars}. mult — во сколько раз быстрее реген (база 3ч):
#   2× → полный заряд за 1.5ч, 3× → за 1ч.
REGEN_UPGRADES: dict[int, dict] = {
    2: {"mult": 2.0, "stars": 150},
    3: {"mult": 3.0, "stars": 350},
}


@app.get("/api/shop/regen_upgrades")
async def api_regen_upgrades(user_id: int):
    """Список апгрейдов скорости регена + текущий множитель игрока."""
    cur = get_energy(user_id).get("regen_mult", 1.0)
    return {
        "current_mult": cur,
        "upgrades": [
            {"level": lvl, "mult": u["mult"], "stars": u["stars"],
             "owned": cur >= u["mult"]}
            for lvl, u in sorted(REGEN_UPGRADES.items())
        ],
    }


@app.post("/api/shop/buy_regen")
async def api_buy_regen(request: Request, tg_user: dict = Depends(require_webapp_user)):
    """Покупка апгрейда скорости регена (кошелёк → иначе invoice)."""
    try:
        data    = await request.json()
        user_id = data.get("user_id")
        level   = int(data.get("level", 0))
        if not user_id or int(user_id) != int(tg_user["id"]):
            raise HTTPException(status_code=403, detail="user_id mismatch")
        up = REGEN_UPGRADES.get(level)
        if not up:
            raise HTTPException(status_code=400, detail="Unknown upgrade level")
        stars = up["stars"]

        wallet = get_wallet(user_id)
        if wallet["balance"] >= stars:
            res = spend_wallet(user_id, stars, f"Апгрейд скорости энергии ×{up['mult']:g}")
            if res["ok"]:
                upgrade_regen_speed(user_id, up["mult"])
                logger.info(f"[SHOP] wallet regen buy: user={user_id} lvl={level} -{stars}⭐")
                return {"method": "wallet", "balance": res["balance"], "mult": up["mult"]}

        bot = get_bot()
        label = f"⚡ Реген энергии ×{up['mult']:g}"
        invoice_link = await bot.create_invoice_link(
            title=label,
            description=f"Ускорение восстановления энергии в Chin Games (×{up['mult']:g})",
            payload=f"regen:{user_id}:{level}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=label, amount=stars)],
        )
        return {"method": "invoice", "invoice_url": invoice_link, "stars": stars}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"buy_regen error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/shop/confirm_regen")
async def api_confirm_regen(request: Request, _: None = Depends(require_internal)):
    """Бот подтверждает оплату апгрейда регена."""
    try:
        data    = await request.json()
        user_id = int(data.get("user_id", 0))
        level   = int(data.get("level", 0))
        up = REGEN_UPGRADES.get(level)
        if user_id and up:
            upgrade_regen_speed(user_id, up["mult"])
        logger.info(f"[SHOP] confirm_regen: user={user_id} lvl={level}")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/shop/case/status")
async def api_shop_case_status():
    """Глобальный кулдаун на ценные призы из кейса."""
    return get_case_valuable_cooldown_status()


@app.post("/api/shop/buy_case")
async def api_buy_case(request: Request, tg_user: dict = Depends(require_webapp_user)):
    """Покупка случайного кейса за Stars (кошелёк или invoice)."""
    try:
        data = await request.json()
        user_id = data.get("user_id")
        stars = int(data.get("stars", 0))
        first_name = data.get("first_name", "")

        if not user_id:
            raise HTTPException(status_code=400, detail="Missing user_id")
        if int(user_id) != int(tg_user["id"]):
            raise HTTPException(status_code=403, detail="user_id mismatch")
        if stars != CASE_PRICE:
            raise HTTPException(status_code=400, detail=f"Invalid price. Expected: {CASE_PRICE}")

        wallet = get_wallet(user_id)
        balance = wallet["balance"]

        if balance >= stars:
            result = spend_wallet(
                user_id=user_id,
                amount=stars,
                description="Покупка случайного кейса",
            )
            if result["ok"]:
                reward = grant_case_reward(user_id, first_name)
                logger.info(
                    f"[SHOP] case wallet: user={user_id} -{stars}⭐ reward={reward['type']}:{reward['amount']}"
                )
                return {
                    "method": "wallet",
                    "balance": result["balance"],
                    "reward": reward,
                }
            balance = result["balance"]

        bot = get_bot()
        label = "Случайный кейс"
        invoice_link = await bot.create_invoice_link(
            title=label,
            description="Случайная награда: энергия, звёзды или NFT-подарок",
            payload=f"case:{user_id}:{CASE_PRICE}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=label, amount=stars)],
        )
        logger.info(f"[SHOP] case invoice: user={user_id} stars={stars} short={stars - balance}")
        return {
            "method": "invoice",
            "invoice_url": invoice_link,
            "balance": balance,
            "short": stars - balance,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"buy_case error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/shop/confirm_case")
async def api_confirm_case(request: Request, _: None = Depends(require_internal)):
    """Выдача награды после оплаты кейса через Telegram Invoice."""
    try:
        data = await request.json()
        user_id = int(data.get("user_id", 0))
        first_name = data.get("first_name", "")
        if not user_id:
            raise HTTPException(status_code=400, detail="Missing user_id")

        reward = confirm_case_reward(user_id, first_name)
        logger.info(f"[SHOP] case confirm: user={user_id} reward={reward['type']}:{reward['amount']}")
        return {"status": "ok", "reward": reward}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"confirm_case error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Wallet API ──────────────────────────────────────────────────────

@app.get("/api/wallet/balance")
async def api_wallet_balance(user_id: int):
    return get_wallet(user_id)


@app.get("/api/user/flags")
async def api_user_flags(user_id: int):
    return get_user_flags(user_id)


@app.get("/api/energy/balance")
async def api_energy_balance(user_id: int):
    _online[int(user_id)] = _time.time()   # отметка присутствия
    return get_energy(user_id)


# ── Счётчик «онлайн прямо сейчас» (in-memory) ───────────────────────
_online: dict[int, float] = {}
_ONLINE_WINDOW = 90   # секунд без активности = ушёл


@app.get("/api/online")
async def api_online(user_id: int | None = None):
    now = _time.time()
    if user_id:
        _online[int(user_id)] = now
    for u, t in list(_online.items()):
        if now - t > _ONLINE_WINDOW:
            _online.pop(u, None)
    return {"online": len(_online)}


@app.post("/api/energy/add")
async def api_energy_add(request: Request, tg_user: dict = Depends(require_webapp_user)):
    """Добавить энергию после покупки в магазине."""
    data = await request.json()
    user_id = int(data.get("user_id", 0))
    amount = int(data.get("amount", 0))
    if not user_id or amount <= 0 or amount > 100:
        raise HTTPException(status_code=400, detail="Invalid user_id or amount")
    if user_id != int(tg_user["id"]):
        raise HTTPException(status_code=403, detail="user_id mismatch")
    return admin_adjust_energy(user_id, amount)


@app.post("/api/energy/spend")
async def api_energy_spend(request: Request, tg_user: dict = Depends(require_webapp_user)):
    data = await request.json()
    user_id = int(data.get("user_id", 0))
    cost = int(data.get("cost", 1))
    if not user_id or cost <= 0:
        raise HTTPException(status_code=400, detail="Invalid user_id or cost")
    if user_id != int(tg_user["id"]):
        raise HTTPException(status_code=403, detail="user_id mismatch")
    result = spend_energy(user_id, cost)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail="Not enough energy")
    return result


@app.post("/api/wallet/create_topup_invoice")
async def api_wallet_create_topup_invoice(request: Request):
    try:
        data    = await request.json()
        user_id = data.get("user_id")
        amount  = int(data.get("amount", 0))
        label   = data.get("label") or f"Пополнение кошелька на {amount} ⭐"

        if not user_id:
            raise HTTPException(status_code=400, detail="Missing user_id")
        if amount < 1 or amount > 10000:
            raise HTTPException(status_code=400, detail="Некорректная сумма")

        bot   = get_bot()
        title = "Кошелёк Stars"
        link  = await bot.create_invoice_link(
            title=title,
            description=label,
            payload=f"wallet:{user_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
        )
        return {"invoice_url": link}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Wallet invoice error: {e}")
        raise HTTPException(status_code=500, detail="Ошибка создания инвойса")


@app.post("/api/wallet/confirm_topup")
async def api_wallet_confirm_topup(request: Request, _: None = Depends(require_internal)):
    try:
        data       = await request.json()
        user_id    = data.get("user_id")
        amount     = int(data.get("amount", 0))
        first_name = data.get("first_name") or ""

        if not user_id or amount < 1:
            raise HTTPException(status_code=400, detail="Некорректные данные")

        result = topup_wallet(
            user_id=user_id,
            first_name=first_name,
            amount=amount,
            description=f"Пополнение кошелька +{amount}⭐"
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/wallet/spend")
async def api_wallet_spend(request: Request, tg_user: dict = Depends(require_webapp_user)):
    try:
        data        = await request.json()
        user_id     = data.get("user_id")
        amount      = int(data.get("amount", 0))
        description = data.get("description") or "Покупка"
        if not user_id or amount < 1:
            raise HTTPException(status_code=400, detail="Некорректные данные")
        if int(user_id) != int(tg_user["id"]):
            raise HTTPException(status_code=403, detail="user_id mismatch")
        return spend_wallet(user_id, amount, description)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/wallet/transactions")
async def api_wallet_transactions(user_id: int, limit: int = 20):
    return {"transactions": get_wallet_transactions(user_id, limit)}


# ── Referrals ───────────────────────────────────────────────────────

@app.get("/api/bonuses/status/{user_id}")
async def api_bonus_status(user_id: int):
    sub = await _check_subscription(user_id)
    bonuses = get_user_bonus_status(user_id)
    checkin = get_daily_checkin_status(user_id)
    return {
        "subscribed_channel": sub["channel"],
        "subscribed_chat":    sub["chat"],
        "bonuses": bonuses,
        "daily": checkin,
        "config": {
            "channel": REQUIRED_CHANNEL,
            "chat":    REQUIRED_CHAT,
            "channel_stars": BONUS_CHANNEL_STARS,
            "chat_stars":    BONUS_CHAT_STARS,
            "share_stars":   BONUS_SHARE_STARS,
            "daily_stars":   DAILY_CHECKIN_STARS,
        }
    }


@app.post("/api/bonuses/claim")
async def api_bonus_claim(request: Request):
    data = await request.json()
    tg_user = require_webapp_user(request)
    user_id    = tg_user["id"]
    first_name = tg_user.get("first_name", "Игрок")
    bonus_type = data.get("bonus_type", "")
    if bonus_type not in (BONUS_CHANNEL, BONUS_CHAT, BONUS_SHARE):
        raise HTTPException(status_code=400, detail="Unknown bonus_type")
    # Для sub-бонусов проверяем реальную подписку
    if bonus_type in (BONUS_CHANNEL, BONUS_CHAT):
        sub = await _check_subscription(user_id)
        key = "channel" if bonus_type == BONUS_CHANNEL else "chat"
        if not sub[key]:
            raise HTTPException(status_code=403, detail="Not subscribed")
    result = grant_bonus(user_id, first_name, bonus_type)
    return result


@app.post("/api/bonuses/daily_checkin")
async def api_daily_checkin(request: Request):
    tg_user = require_webapp_user(request)
    user_id    = tg_user["id"]
    first_name = tg_user.get("first_name", "Игрок")
    return daily_checkin(user_id, first_name)


@app.get("/api/bonuses/check_subscription/{user_id}")
async def api_check_subscription(user_id: int):
    """Проверка подписки — для гейтинга доступа."""
    sub = await _check_subscription(user_id)
    allowed = sub["channel"] and sub["chat"]
    return {"allowed": allowed, **sub}


@app.get("/api/referral/stats/{user_id}")
async def api_referral_stats(user_id: int):
    """Статистика рефералов и реферальная ссылка для пользователя."""
    try:
        stats = get_referral_stats(user_id)
        ref_url = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        return {**stats, "ref_url": ref_url, "reward_per_ref": {"stars": REFERRAL_STARS, "energy": REFERRAL_ENERGY}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bot-username")
async def api_bot_username():
    """Возвращает юзернейм бота для генерации deep links (приглашения и т.д.)."""
    return {"username": BOT_USERNAME}


# ── Contests ───────────────────────────────────────────────────────

@app.get("/api/contests/active")
async def api_active_contests():
    """Все активные соревнования (для главного меню и профиля)."""
    try:
        contests = get_active_contests()
        now = datetime.now(timezone.utc)
        result = []
        for c in contests:
            ev = c["ends_at"]
            ends_at = ev if isinstance(ev, datetime) else datetime.fromisoformat(str(ev).replace("Z", "+00:00"))
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=timezone.utc)
            if ends_at <= now:
                # Автофиниш просроченных
                await _finish_contest_auto(c["id"])
                continue
            result.append({
                "id": c["id"],
                "game_name": c["game_name"],
                "prize_type": c["prize_type"],
                "prize_value": c["prize_value"],
                "gift_id": c["gift_id"],
                "winners_count": c["winners_count"],
                "split_prize": bool(c["split_prize"]),
                "ends_at": c["ends_at"],
                "seconds_left": int((ends_at - now).total_seconds()),
            })
        return {"contests": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _contest_targets():
    # Постим только в канал из ANNOUNCE_CHATS. Привязанные через /bind чаты не используем.
    return list(dict.fromkeys(ANNOUNCE_CHATS))


async def _post_to_chats(text):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Играть", url=f"https://t.me/{BOT_USERNAME}?startapp=play")]])
    except Exception:
        kb = None
    bot = get_bot()
    for chat in _contest_targets():
        try:
            cid = int(chat) if str(chat).lstrip("-").isdigit() else chat
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning(f"[ANNOUNCE] post to {chat} failed: {e}")


_reminded_contests = set()


async def _contest_reminder_loop():
    """Раз в ~1ч до конца соревнований шлёт одно сводное сообщение «остался 1 час»."""
    while True:
        try:
            await asyncio.sleep(120)
            contests = get_active_contests()
            now = datetime.now(timezone.utc)
            active, newly = [], []
            for c in contests:
                ev = c["ends_at"]
                ends_at = ev if isinstance(ev, datetime) else datetime.fromisoformat(str(ev).replace("Z", "+00:00"))
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=timezone.utc)
                left = (ends_at - now).total_seconds()
                if left <= 0:
                    continue
                active.append((c, left))
                if left <= 3600 and c["id"] not in _reminded_contests:
                    newly.append(c["id"])
            if newly:
                for cid in newly:
                    _reminded_contests.add(cid)
                lines = []
                for c, left in active:
                    label = GAME_LABELS.get(c["game_name"], c["game_name"])
                    if c["prize_type"] == "gift":
                        gid = c.get("gift_id") or ""
                        prize = f'🎁 <a href="{gid}">NFT-приз</a>' if gid else "🎁 NFT-приз"
                    elif c["prize_type"] == "tg_gift":
                        prize = _tg_gift_label(c.get('gift_id') or '')
                    else:
                        prize = f"⭐ {c['prize_value']} Stars"
                    lines.append(f"• {label} — {prize} (≈{int(left // 60)} мин)")
                text = ("⏳ <b>Остался ~1 час до конца соревнований!</b>\n\n"
                        + "\n".join(lines) + "\n\nУспей ворваться в топ!")
                await _post_to_chats(text)

            # Завершаем просрочённые активные соревнования
            for c in contests:
                ev = c["ends_at"]
                ends_at = ev if isinstance(ev, datetime) else datetime.fromisoformat(str(ev).replace("Z", "+00:00"))
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=timezone.utc)
                if ends_at <= now:
                    try:
                        await _finish_contest_auto(c["id"])
                    except Exception as e:
                        logger.warning(f"[ANNOUNCE] finish #{c['id']} failed: {e}")

            # Сводный итог через 5 мин после конца — ОДНО сообщение по всем
            finished = get_unannounced_finished_contests(300)
            if finished:
                blocks = []
                for c in finished:
                    label = GAME_LABELS.get(c["game_name"], c["game_name"])
                    if c["prize_type"] == "gift":
                        gid = c.get("gift_id") or ""
                        prize = f'🎁 <a href="{gid}">NFT-приз</a>' if gid else "🎁 NFT-приз"
                    elif c["prize_type"] == "tg_gift":
                        prize = _tg_gift_label(c.get('gift_id') or '')
                    else:
                        prize = f"⭐ {c['prize_value']} Stars"
                    lb = get_leaderboard(c["game_name"], 1)
                    top = (lb[0]["first_name"] if lb and lb[0].get("first_name") else "—")
                    blocks.append(f"🎮 <b>{label}</b> — {prize}\n🥇 Топ 1: {top}")
                    mark_contest_announced(c["id"])
                await _post_to_chats("🏆 <b>Победители!</b>\n\n" + "\n\n".join(blocks))
        except Exception as e:
            logger.warning(f"[ANNOUNCE] reminder loop error: {e}")


async def _announce_contest(game_name, prize_type, prize_value, gift_id, duration_min):
    """Короткий автопост о старте соревнования в привязанные чаты/каналы."""
    if not ANNOUNCE_CHATS:
        return
    label = GAME_LABELS.get(game_name, game_name)
    if prize_type == "gift":
        prize = f'🎁 <a href="{gift_id}">NFT-приз</a>' if gift_id else "🎁 NFT-приз"
    elif prize_type == "tg_gift":
        prize = _tg_gift_label(gift_id or '')
    else:
        prize = f"⭐ {prize_value} Stars"
    text = (
        "🏆 <b>Соревнование идёт!</b>\n"
        f"🎮 {label}\n"
        f"🥇 Приз: {prize}\n"
        f"⏱ {duration_min} мин — успей в топ!"
    )
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Играть", url=f"https://t.me/{BOT_USERNAME}?startapp=play")]])
    except Exception:
        kb = None
    # Чаты: из .env (ANNOUNCE_CHATS) + привязанные через бота (БД)
    targets = list(ANNOUNCE_CHATS)
    try:
        targets += [str(c) for c in get_announce_chats()]
    except Exception:
        pass
    bot = get_bot()
    for chat in dict.fromkeys(targets):   # дедуп, порядок сохранён
        try:
            cid = int(chat) if str(chat).lstrip("-").isdigit() else chat
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.warning(f"[ANNOUNCE] post to {chat} failed: {e}")


@app.post("/api/announce/bind")
async def api_announce_bind(request: Request, _: None = Depends(require_internal)):
    """Привязать чат для автопоста (вызывает бот по команде /bind)."""
    data = await request.json()
    chat_id = int(data.get("chat_id"))
    title = data.get("title", "") or ""
    return add_announce_chat(chat_id, title)


@app.post("/api/announce/unbind")
async def api_announce_unbind(request: Request, _: None = Depends(require_internal)):
    data = await request.json()
    return remove_announce_chat(int(data.get("chat_id")))


@app.post("/api/admin/backfill_usernames")
async def api_backfill_usernames(request: Request):
    """Подтянуть @username всех игроков через бота (get_chat)."""
    data = await request.json()
    uname = (data.get("username") or "").lstrip("@").lower()
    if not is_admin(uname):
        raise HTTPException(status_code=403, detail="Forbidden")
    bot = get_bot()
    players = admin_get_all_players(limit=2000).get("players", [])
    filled = 0
    for p in players:
        if p.get("username"):
            continue
        try:
            chat = await bot.get_chat(p["user_id"])
            if getattr(chat, "username", None):
                upsert_tg_username(p["user_id"], chat.username)
                filled += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return {"ok": True, "filled": filled}


@app.post("/api/announce/repost")
async def api_announce_repost(request: Request):
    """Повторно разослать анонсы по всем активным соревнованиям (для админа)."""
    data = await request.json()
    username = (data.get("username") or "").lstrip("@").lower()
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Forbidden")
    contests = get_active_contests()
    now = datetime.now(timezone.utc)
    reposted = 0
    for c in contests:
        ev = c["ends_at"]
        ends_at = ev if isinstance(ev, datetime) else datetime.fromisoformat(str(ev).replace("Z", "+00:00"))
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at <= now:
            continue
        mins = max(1, int((ends_at - now).total_seconds() // 60))
        try:
            await _announce_contest(c["game_name"], c["prize_type"], c["prize_value"], c["gift_id"], mins)
            reposted += 1
        except Exception as e:
            logger.warning(f"[ANNOUNCE] repost #{c['id']} failed: {e}")
    return {"ok": True, "reposted": reposted}


@app.post("/api/admin/repost_reminder")
async def api_repost_reminder(request: Request):
    """Переотправить напоминание об активных соревнованиях (сколько осталось)."""
    data = await request.json()
    username = (data.get("username") or "").lstrip("@").lower()
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Forbidden")
    contests = get_active_contests()
    now = datetime.now(timezone.utc)
    lines = []
    for c in contests:
        ev = c["ends_at"]
        ends_at = ev if isinstance(ev, datetime) else datetime.fromisoformat(str(ev).replace("Z", "+00:00"))
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at <= now:
            continue
        left = (ends_at - now).total_seconds()
        label = GAME_LABELS.get(c["game_name"], c["game_name"])
        if c["prize_type"] == "gift":
            gid = c.get("gift_id") or ""
            prize = f'🎁 <a href="{gid}">NFT-приз</a>' if gid else "🎁 NFT-приз"
        elif c["prize_type"] == "tg_gift":
            prize = _tg_gift_label(c.get('gift_id') or '')
        else:
            prize = f"⭐ {c['prize_value']} Stars"
        lines.append(f"• {label} — {prize} (≈{int(left // 60)} мин)")
    if not lines:
        return {"ok": False, "detail": "Нет активных соревнований"}
    text = "⏳ <b>Скоро конец соревнований!</b>\n\n" + "\n".join(lines) + "\n\nУспей ворваться в топ!"
    await _post_to_chats(text)
    return {"ok": True}


@app.post("/api/admin/repost_last_result")
async def api_repost_last_result(request: Request):
    """Переотправить итог последнего завершённого соревнования."""
    data = await request.json()
    username = (data.get("username") or "").lstrip("@").lower()
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Forbidden")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM contests WHERE status = 'finished' ORDER BY ends_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "detail": "Нет завершённых соревнований"}
    c = dict(row)
    label = GAME_LABELS.get(c["game_name"], c["game_name"])
    if c["prize_type"] == "gift":
        gid = c.get("gift_id") or ""
        prize = f'🎁 <a href="{gid}">NFT-приз</a>' if gid else "🎁 NFT-приз"
    elif c["prize_type"] == "tg_gift":
        prize = f"🎀 Подарок Telegram ({c.get('gift_id') or '—'})"
    else:
        prize = f"⭐ {c['prize_value']} Stars"
    lb = get_leaderboard(c["game_name"], 1)
    top = lb[0]["first_name"] if lb and lb[0].get("first_name") else "—"
    text = f"🏆 <b>Победители!</b>\n\n🎮 <b>{label}</b> — {prize}\n🥇 Топ 1: {top}"
    await _post_to_chats(text)
    return {"ok": True}


@app.post("/api/contests/create")
async def api_create_contest(request: Request):
    """Создать соревнование. Только для админов."""
    try:
        data = await request.json()

        username = (data.get("username") or "").lstrip("@").lower()
        if not is_admin(username):
            raise HTTPException(status_code=403, detail="Forbidden")

        game_name     = data.get("game_name", "").strip()
        prize_type    = data.get("prize_type", "")      # "stars" | "gift"
        prize_value   = int(data.get("prize_value", 0)) # звёзды
        gift_id       = data.get("gift_id", None)        # для gift
        split_prize   = bool(data.get("split_prize", False))
        winners_count = int(data.get("winners_count", 1))
        duration_min  = int(data.get("duration_minutes", 60))
        started_by    = int(data.get("user_id", 0))

        if game_name not in VALID_GAMES:
            raise HTTPException(status_code=400, detail="Unknown game")
        if prize_type not in ("stars", "gift", "tg_gift"):
            raise HTTPException(status_code=400, detail="prize_type must be stars, gift or tg_gift")
        max_winners = 5 if prize_type == "tg_gift" else 3
        if winners_count < 1 or winners_count > max_winners:
            raise HTTPException(status_code=400, detail=f"winners_count must be 1-{max_winners}")
        if prize_type == "stars" and prize_value <= 0:
            raise HTTPException(status_code=400, detail="prize_value required for stars")
        if prize_type in ("gift", "tg_gift"):
            if not gift_id:
                raise HTTPException(status_code=400, detail="gift_id required for gift prize")
        if duration_min < 1 or duration_min > 43200:  # max 30 дней
            raise HTTPException(status_code=400, detail="Invalid duration")

        from datetime import timedelta
        ends_at = (datetime.now(timezone.utc) + timedelta(minutes=duration_min)).isoformat()

        contest_id = create_contest(
            game_name=game_name,
            prize_type=prize_type,
            prize_value=prize_value,
            gift_id=gift_id,
            split_prize=split_prize,
            winners_count=winners_count,
            started_by=started_by,
            ends_at=ends_at,
        )

        logger.info(f"🏆 Contest #{contest_id} created by {username}: {game_name} | {prize_type} | winners={winners_count}")
        try:
            await _announce_contest(game_name, prize_type, prize_value, gift_id, duration_min)
        except Exception as e:
            logger.warning(f"[ANNOUNCE] contest #{contest_id} announce error: {e}")
        return {"status": "ok", "contest_id": contest_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_contest error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/contests/{contest_id}/cancel")
async def api_cancel_contest(contest_id: int, request: Request):
    """Отменить соревнование. Только для админов."""
    try:
        data = await request.json()
        username = (data.get("username") or "").lstrip("@").lower()
        if not is_admin(username):
            raise HTTPException(status_code=403, detail="Forbidden")
        c = get_contest(contest_id)
        if not c:
            raise HTTPException(status_code=404, detail="Contest not found")
        if c["status"] != "active":
            raise HTTPException(status_code=400, detail="Contest already finished/cancelled")
        cancel_contest(contest_id)
        logger.info(f"Contest #{contest_id} cancelled by {username}")
        return {"status": "cancelled"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/contests/{contest_id}/finish")
async def api_finish_contest(contest_id: int, request: Request):
    """Досрочно завершить и выдать призы. Только для админов."""
    try:
        data = await request.json()
        username = (data.get("username") or "").lstrip("@").lower()
        if not is_admin(username):
            raise HTTPException(status_code=403, detail="Forbidden")
        winners = await _finish_contest_auto(contest_id)
        return {"status": "finished", "winners": winners}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _finish_contest_auto(contest_id: int) -> list:
    """Внутренний хелпер: завершает соревнование и отправляет призы."""
    c = get_contest(contest_id)
    if not c or c["status"] != "active":
        return []

    current_lb = get_leaderboard(c["game_name"], c["winners_count"])

    winners_count = min(c["winners_count"], len(current_lb))
    winners = []
    for i, p in enumerate(current_lb[:winners_count]):
        winners.append({"user_id": p["user_id"], "first_name": p["first_name"],
                        "score": p["score"], "place": i + 1})

    finish_contest(contest_id, winners)

    # Отправляем призы
    bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
    if bot and winners:
        prize_type  = c["prize_type"]
        prize_value = c["prize_value"]
        split       = bool(c["split_prize"])
        gift_id     = c["gift_id"]

        for w in winners:
            try:
                if prize_type == "stars":
                    amount = (prize_value // len(winners)) if split else prize_value
                    await bot.send_message(
                        chat_id=w["user_id"],
                        text=(
                            f"🏆 Поздравляем! Ты занял {w['place']} место в соревновании по {c['game_name']}!\n"
                            f"💫 Твой приз: {amount} звёзд Telegram"
                        )
                    )
                    logger.info(f"⭐ Stars prize {amount} → user {w['user_id']} (place {w['place']})")

                elif prize_type == "gift":
                    gift_link = c["gift_id"] or ""
                    await bot.send_message(
                        chat_id=w["user_id"],
                        text=(
                            f"🏆 Поздравляем! Ты занял {w['place']} место в соревновании по {c['game_name']}!\n"
                            f"🎁 Твой приз — NFT-подарок. Администратор свяжется с тобой для его передачи.\n"
                            + (f"Подарок: {gift_link}" if gift_link else "")
                        )
                    )
                    logger.info(f"🎁 Gift (manual) prize_link={gift_link} → user {w['user_id']} (place {w['place']})")

                elif prize_type == "tg_gift":
                    gift_name = _tg_gift_label(c["gift_id"] or '')
                    await bot.send_message(
                        chat_id=w["user_id"],
                        text=(
                            f"🏆 Поздравляем! Ты занял {w['place']} место в соревновании по {c['game_name']}!\n"
                            f"Твой приз — {gift_name}. Администратор отправит его тебе в ближайшее время."
                        )
                    )
                    logger.info(f"🎀 TG Gift ({gift_name}) → user {w['user_id']} (place {w['place']})")

                mark_prize_sent(contest_id, w["user_id"])

                # Дублируем уведомление админам
                uname = w.get("username") or ""
                user_ref = f'<a href="tg://user?id={w["user_id"]}">{w["first_name"]}</a>'
                if uname:
                    user_ref += f' (@{uname})'
                game_label = GAME_LABELS.get(c["game_name"], c["game_name"])
                if prize_type == "stars":
                    prize_admin = f"⭐ {amount} Stars"
                elif prize_type == "gift":
                    prize_admin = f'🎁 NFT: {c["gift_id"] or "—"}'
                else:
                    prize_admin = f'🎀 {c["gift_id"] or "—"}'
                admin_text = (
                    f"📋 <b>Приз выдан</b>\n"
                    f"🎮 {game_label} · Топ {w['place']}\n"
                    f"👤 {user_ref}\n"
                    f"🆔 <code>{w['user_id']}</code>\n"
                    f"🏅 {prize_admin}"
                )
                for admin_id in ADMIN_TG_IDS:
                    try:
                        await bot.send_message(chat_id=admin_id, text=admin_text, parse_mode="HTML")
                    except Exception as ae:
                        logger.warning(f"Admin notify failed {admin_id}: {ae}")

            except Exception as e:
                logger.error(f"Prize send error for user {w['user_id']}: {e}")

    # Публичный итог отправляется отдельным сводным сообщением через 5 мин
    # (см. _contest_reminder_loop) — здесь только личные призы.
    logger.info(f"🏁 Contest #{contest_id} finished. Winners: {[w['user_id'] for w in winners]}")
    return winners


@app.get("/")
async def root():
    return {"status": "OK", "webapp": f"{WEBAPP_URL}/static/index.html"}


# ── NFT Meta / Lottie proxy (обход CORS для nft.fragment.com) ──────

import urllib.request

def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode())


@app.get("/api/nft_meta")
async def api_nft_meta(slug: str):
    """
    Проксирует JSON-метаданные NFT-подарка с nft.fragment.com.
    slug = например hexpot-10348  (из t.me/nft/HexPot-10348)
    """
    # Санитизация slug — только буквы/цифры/дефис
    import re
    if not re.match(r'^[a-z0-9\-]+$', slug):
        raise HTTPException(status_code=400, detail="Invalid slug")
    url = f"https://nft.fragment.com/gift/{slug}.json"
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, _fetch_json, url)
        if "lottie_url" not in data:
            data["lottie_url"] = f"https://nft.fragment.com/gift/{slug}.lottie.json"
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NFT meta fetch failed: {e}")


@app.get("/api/nft_lottie")
async def api_nft_lottie(slug: str):
    """
    Проксирует Lottie JSON-анимацию NFT-подарка с nft.fragment.com.
    """
    import re
    if not re.match(r'^[a-z0-9\-]+$', slug):
        raise HTTPException(status_code=400, detail="Invalid slug")
    url = f"https://nft.fragment.com/gift/{slug}.lottie.json"
    try:
        data = await asyncio.get_event_loop().run_in_executor(None, _fetch_json, url)
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NFT lottie fetch failed: {e}")


# ── Admin: case settings (остаётся в main) ─────────────────────────

@app.get("/api/admin/case/settings")
async def api_admin_case_settings_get(username: str):
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Forbidden")
    return get_case_settings()


@app.post("/api/admin/case/settings")
async def api_admin_case_settings_save(request: Request):
    try:
        data = await request.json()
        username = (data.get("username") or "").lstrip("@").lower()
        if not is_admin(username):
            raise HTTPException(status_code=403, detail="Forbidden")
        nft_gifts = data.get("nft_gifts") or []
        if not isinstance(nft_gifts, list):
            raise HTTPException(status_code=400, detail="nft_gifts must be a list")
        valuable_chance = float(data.get("valuable_chance", 0.4))
        valuable_cooldown_min = int(data.get("valuable_cooldown_min", 60))
        nft_chance = float(data.get("nft_chance", 0.18))
        result = save_case_settings(nft_gifts, valuable_chance, valuable_cooldown_min, nft_chance)
        logger.info(f"[CASE] settings updated by {username}: {len(result['nft_gifts'])} NFT URLs")
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"case settings save error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
