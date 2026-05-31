"""
referral_routes.py
──────────────────
Подключи этот роутер к своему FastAPI или Flask приложению.

FastAPI:
    from referral_routes import router as referral_router
    app.include_router(referral_router, prefix="/api/referral")

Flask:
    from referral_routes import bp as referral_bp
    app.register_blueprint(referral_bp, url_prefix="/api/referral")

Переменные окружения:
    HCAPTCHA_SECRET   — секретный ключ hCaptcha (получить на hcaptcha.com)
    HCAPTCHA_SITEKEY  — публичный sitekey для фронтенда
    ADMIN_ID          — Telegram user_id администратора
    BOT_TOKEN         — токен бота для отправки уведомлений
"""

import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

HCAPTCHA_SECRET  = os.getenv("HCAPTCHA_SECRET", "")
HCAPTCHA_SITEKEY = os.getenv("HCAPTCHA_SITEKEY", "")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")

# ── Вспомогательные функции ─────────────────────────────────────────

async def verify_hcaptcha(token: str) -> bool:
    """Верифицирует токен hCaptcha через API."""
    if not HCAPTCHA_SECRET:
        logger.warning("[CAPTCHA] HCAPTCHA_SECRET не задан — пропускаем проверку")
        return True  # В dev-режиме без ключа пропускаем
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://hcaptcha.com/siteverify",
                data={"secret": HCAPTCHA_SECRET, "response": token}
            )
            result = resp.json()
            return result.get("success", False)
    except Exception as e:
        logger.error(f"[CAPTCHA] Ошибка верификации: {e}")
        return False


async def send_admin_alert(text: str):
    """Отправляет уведомление администратору через Telegram Bot API."""
    if not ADMIN_ID or not BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
            )
    except Exception as e:
        logger.error(f"[ADMIN] Ошибка отправки уведомления: {e}")


# ════════════════════════════════════════════════════════════════════
#  FASTAPI роутер
# ════════════════════════════════════════════════════════════════════
router = None  # объявляем заранее, чтобы при любой ошибке не было None из-за необъявленной переменной

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel
    try:
        from api import database as db
    except ImportError:
        import database as db

    router = APIRouter()

    class ReferralRegisterRequest(BaseModel):
        inviter_id:   int
        invitee_id:   int
        invitee_name: str

    class PolicyAcceptRequest(BaseModel):
        invitee_id:     int
        captcha_token:  str

    class ScoreSaveNotify(BaseModel):
        """
        Вызывается из эндпоинта save_score после сохранения результата.
        Проверяет, не пора ли начислить реферальную награду.
        """
        user_id:   int
        game_name: str

    # ── POST /api/referral/register ─────────────────────────────────
    @router.post("/register")
    async def referral_register(req: ReferralRegisterRequest):
        """
        Шаг 1: Регистрирует реферала в БД (до принятия политики).
        Вызывается ботом при /start ref_XXX.
        """
        # Защита от саморефереала на уровне API
        if req.inviter_id == req.invitee_id:
            return {"new": False, "reason": "self_referral"}

        # Уже зарегистрирован через другого?
        if db.is_already_referred(req.invitee_id):
            return {"new": False, "reason": "already_referred"}

        new = db.register_referral(req.inviter_id, req.invitee_id, req.invitee_name)

        # Имя инвайтера для уведомлений (из wallet если есть)
        inviter_wallet = db.get_wallet(req.inviter_id)
        inviter_name   = str(req.inviter_id)  # fallback

        return {
            "new":          new,
            "inviter_name": inviter_name,
            "reward":       None,  # награда придёт позже, после 3 игр
        }

    # ── POST /api/referral/accept_policy ───────────────────────────
    @router.post("/accept_policy")
    async def referral_accept_policy(req: PolicyAcceptRequest):
        """
        Шаг 2: Принятие политики + проверка капчи.
        Вызывается с фронтенда (страница ref_policy.html).
        """
        # Верифицируем капчу
        captcha_ok = await verify_hcaptcha(req.captcha_token)
        if not captcha_ok:
            raise HTTPException(status_code=400, detail="captcha_failed")

        # Проверяем, есть ли такой реферал
        ref = db.get_referral_by_invitee(req.invitee_id)
        if not ref:
            raise HTTPException(status_code=404, detail="referral_not_found")

        if ref["policy_accepted"]:
            return {"ok": True, "already": True}

        updated = db.accept_referral_policy(req.invitee_id)
        return {"ok": updated}

    # ── GET /api/referral/status ────────────────────────────────────
    @router.get("/status")
    async def referral_status(invitee_id: int):
        """
        Возвращает статус реферала: зарегистрирован / принял политику / получил награду.
        """
        ref = db.get_referral_by_invitee(invitee_id)
        if not ref:
            return {"registered": False}

        games_total = db.get_invitee_total_games(invitee_id)
        return {
            "registered":      True,
            "policy_accepted": bool(ref["policy_accepted"]),
            "reward_sent":     bool(ref["reward_sent"]),
            "games_played":    games_total,
            "games_needed":    db.REFERRAL_GAMES_NEEDED,
        }

    # ── GET /api/referral/stats ─────────────────────────────────────
    @router.get("/stats/{inviter_id}")
    @router.get("/stats")
    async def referral_stats(inviter_id: int = 0):
        """Статистика реферальных приглашений пользователя."""
        stats = db.get_referral_stats(inviter_id)
        # Добавляем реферальную ссылку
        import os
        bot_username = os.getenv("BOT_USERNAME", "chingamebot")
        stats["ref_url"] = f"https://t.me/{bot_username}?start=ref_{inviter_id}"
        return stats

    # ── GET /api/referral/fraud_check ──────────────────────────────
    @router.get("/fraud_check")
    async def fraud_check(inviter_id: int):
        """
        Проверяет подозрительную активность.
        Возвращает флаги для бота, который сам отправляет уведомление.
        """
        result = {}

        daily_count = db.check_fraud_daily_flood(inviter_id)
        if daily_count:
            result["daily_count"] = daily_count

        inactive_ratio = db.check_fraud_inactive_ratio(inviter_id)
        if inactive_ratio:
            result["inactive_ratio"] = inactive_ratio

        return result

    # ── POST /api/referral/check_reward ────────────────────────────
    @router.post("/check_reward")
    async def check_referral_reward(req: ScoreSaveNotify):
        """
        Вызывается из save_score эндпоинта после каждого сохранения результата.
        Если реферал достиг порога — начисляет награду и уведомляет рефереров.
        """
        reward = db.try_grant_referral_reward(req.user_id)
        if not reward:
            return {"rewarded": False}

        inviter_id  = reward["inviter_id"]
        stars       = reward["stars"]
        energy      = reward["energy"]
        new_balance = reward["new_balance"]
        new_energy  = reward.get("new_energy", energy)

        # Уведомляем рефереру через Telegram
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                invitee_ref = db.get_referral_by_invitee(req.user_id)
                invitee_name = invitee_ref["first_name"] if invitee_ref else str(req.user_id)
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id":    inviter_id,
                        "parse_mode": "HTML",
                        "text": (
                            f"🎉 Твой реферал <b>{invitee_name}</b> "
                            f"отыграл 3 игры!\n\n"
                            f"Ты получил:\n"
                            f"⭐ +{stars} Stars на кошелёк (баланс: {new_balance})\n"
                            f"⚡ +{energy} энергии\n\n"
                            f"Продолжай приглашать друзей! 🚀"
                        )
                    }
                )
        except Exception as e:
            logger.warning(f"[REF] Не удалось уведомить inviter {inviter_id}: {e}")

        return {
            "rewarded":    True,
            "inviter_id":  inviter_id,
            "stars":       stars,
            "energy":      energy,
            "new_balance": new_balance,
        }

    # ── GET /api/referral/policy_sitekey ───────────────────────────
    @router.get("/policy_sitekey")
    async def get_sitekey():
        """Отдаёт публичный sitekey hCaptcha для фронтенда."""
        return {"sitekey": HCAPTCHA_SITEKEY}

except ImportError:
    router = None
    logger.info("[REFERRAL] FastAPI не найден, роутер не создан")
except Exception as e:
    router = None
    logger.error(f"[REFERRAL] Ошибка инициализации FastAPI роутера: {e}", exc_info=True)


# ════════════════════════════════════════════════════════════════════
#  FLASK blueprint (резервный вариант)
# ════════════════════════════════════════════════════════════════════
bp = None  # объявляем заранее

try:
    from flask import Blueprint, request, jsonify
    import asyncio
    try:
        from api import database as db
    except ImportError:
        import database as db

    bp = Blueprint("referral", __name__)

    @bp.route("/register", methods=["POST"])
    def flask_referral_register():
        data = request.get_json()
        inviter_id   = data["inviter_id"]
        invitee_id   = data["invitee_id"]
        invitee_name = data.get("invitee_name", "Игрок")

        if inviter_id == invitee_id:
            return jsonify({"new": False, "reason": "self_referral"})
        if db.is_already_referred(invitee_id):
            return jsonify({"new": False, "reason": "already_referred"})

        new = db.register_referral(inviter_id, invitee_id, invitee_name)
        return jsonify({"new": new, "reward": None})

    @bp.route("/accept_policy", methods=["POST"])
    def flask_accept_policy():
        data           = request.get_json()
        invitee_id     = data["invitee_id"]
        captcha_token  = data.get("captcha_token", "")

        captcha_ok = asyncio.run(verify_hcaptcha(captcha_token))
        if not captcha_ok:
            return jsonify({"ok": False, "error": "captcha_failed"}), 400

        ref = db.get_referral_by_invitee(invitee_id)
        if not ref:
            return jsonify({"ok": False, "error": "not_found"}), 404

        if ref["policy_accepted"]:
            return jsonify({"ok": True, "already": True})

        updated = db.accept_referral_policy(invitee_id)
        return jsonify({"ok": updated})

    @bp.route("/stats/<int:inviter_id>", methods=["GET"])
    @bp.route("/stats", methods=["GET"])
    def flask_referral_stats(inviter_id=None):
        if inviter_id is None:
            inviter_id = int(request.args.get("inviter_id", 0))
        import os
        bot_username = os.getenv("BOT_USERNAME", "chingamebot")
        stats = db.get_referral_stats(inviter_id)
        stats["ref_url"] = f"https://t.me/{bot_username}?start=ref_{inviter_id}"
        return jsonify(stats)

    @bp.route("/status", methods=["GET"])
    def flask_referral_status():
        invitee_id = int(request.args.get("invitee_id", 0))
        ref = db.get_referral_by_invitee(invitee_id)
        if not ref:
            return jsonify({"registered": False})
        games_total = db.get_invitee_total_games(invitee_id)
        return jsonify({
            "registered":      True,
            "policy_accepted": bool(ref["policy_accepted"]),
            "reward_sent":     bool(ref["reward_sent"]),
            "games_played":    games_total,
            "games_needed":    db.REFERRAL_GAMES_NEEDED,
        })

    @bp.route("/fraud_check", methods=["GET"])
    def flask_fraud_check():
        inviter_id = int(request.args.get("inviter_id", 0))
        result = {}
        daily = db.check_fraud_daily_flood(inviter_id)
        if daily:
            result["daily_count"] = daily
        inactive = db.check_fraud_inactive_ratio(inviter_id)
        if inactive:
            result["inactive_ratio"] = inactive
        return jsonify(result)

    @bp.route("/check_reward", methods=["POST"])
    def flask_check_reward():
        data    = request.get_json()
        user_id = data["user_id"]
        reward  = db.try_grant_referral_reward(user_id)
        if not reward:
            return jsonify({"rewarded": False})
        return jsonify({"rewarded": True, **reward})

    @bp.route("/policy_sitekey", methods=["GET"])
    def flask_policy_sitekey():
        return jsonify({"sitekey": HCAPTCHA_SITEKEY})

except ImportError:
    bp = None
    logger.info("[REFERRAL] Flask не найден, blueprint не создан")
except Exception as e:
    bp = None
    logger.error(f"[REFERRAL] Ошибка инициализации Flask blueprint: {e}", exc_info=True)
