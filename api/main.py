import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from telegram import Bot, LabeledPrice

from api.database import (
    init_db, save_score, get_leaderboard, get_user_stats,
    create_contest, get_active_contests, get_contest,
    finish_contest, mark_prize_sent, cancel_contest,
    get_wallet, spend_wallet, topup_wallet, get_wallet_transactions,
    register_referral, claim_referral_reward, get_referral_stats,
    is_already_referred, REFERRAL_STARS, REFERRAL_ENERGY,
    try_grant_referral_reward, get_referral_by_invitee,
    admin_adjust_energy,
    get_energy, spend_energy, get_user_flags,
    CASE_PRICE, grant_case_reward, confirm_case_reward,
    get_case_settings, save_case_settings, get_case_valuable_cooldown_status,
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

# ── Durak Online Routes (Этап 2) ────────────────────────────────
try:
    from api.durak_routes import router as durak_router
    if durak_router is not None:
        app.include_router(durak_router)
        logger.info("✅ Durak routes mounted at /api/durak")
    else:
        logger.warning("⚠️ durak_routes router is None")
except Exception as e:
    logger.error(f"❌ Не удалось подключить Durak routes: {e}")

WEBAPP_URL    = os.getenv("WEBAPP_URL", "http://localhost:8000")
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "chingamebot")  # юзернейм бота без @

VALID_GAMES = ("math", "2048", "snake", "flappy")

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
ENERGY_PACKS: dict[int, int] = {3: 12, 5: 18, 8: 27, 14: 46, 22: 70}
ENERGY_MAX = 8  # базовый максимум (overflow разрешён после покупки)


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
    return get_energy(user_id)


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

@app.get("/api/referral/stats/{user_id}")
async def api_referral_stats(user_id: int):
    """Статистика рефералов и реферальная ссылка для пользователя."""
    try:
        stats = get_referral_stats(user_id)
        ref_url = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        return {**stats, "ref_url": ref_url, "reward_per_ref": {"stars": REFERRAL_STARS, "energy": REFERRAL_ENERGY}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Contests ───────────────────────────────────────────────────────

@app.get("/api/contests/active")
async def api_active_contests():
    """Все активные соревнования (для главного меню и профиля)."""
    try:
        contests = get_active_contests()
        now = datetime.now(timezone.utc)
        result = []
        for c in contests:
            ends_at = datetime.fromisoformat(c["ends_at"].replace("Z", "+00:00"))
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
        if prize_type not in ("stars", "gift"):
            raise HTTPException(status_code=400, detail="prize_type must be stars or gift")
        if winners_count not in (1, 2, 3):
            raise HTTPException(status_code=400, detail="winners_count must be 1-3")
        if prize_type == "stars" and prize_value <= 0:
            raise HTTPException(status_code=400, detail="prize_value required for stars")
        if prize_type == "gift":
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

    import json as _json
    snapshot_start = _json.loads(c["snapshot_start"] or "[]")
    current_lb = get_leaderboard(c["game_name"], 50)

    # Считаем прирост очков за время соревнования
    start_scores = {p["user_id"]: p["score"] for p in snapshot_start}
    delta = []
    for p in current_lb:
        uid = p["user_id"]
        gained = p["score"] - start_scores.get(uid, p["score"])
        delta.append({"user_id": uid, "first_name": p["first_name"],
                      "score": gained, "total": p["score"]})

    delta.sort(key=lambda x: x["score"], reverse=True)
    winners_count = min(c["winners_count"], len(delta))
    winners = []
    for i, p in enumerate(delta[:winners_count]):
        if p["score"] <= 0:
            break
        winners.append({**p, "place": i + 1})

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

                mark_prize_sent(contest_id, w["user_id"])

            except Exception as e:
                logger.error(f"Prize send error for user {w['user_id']}: {e}")

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
        result = save_case_settings(nft_gifts, valuable_chance, valuable_cooldown_min)
        logger.info(f"[CASE] settings updated by {username}: {len(result['nft_gifts'])} NFT URLs")
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"case settings save error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
