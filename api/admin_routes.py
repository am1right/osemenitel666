"""
admin_routes.py
───────────────
FastAPI роутер для панели администратора.

    from api.admin_routes import router as admin_router, is_admin
    app.include_router(admin_router, prefix="/api/admin")

Переменные окружения:
    ADMIN_USERNAMES — список username через запятую, например: rostips,am1right
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ADMIN_USERNAMES: set[str] = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "rostips,am1right").split(",")
    if u.strip()
}

VALID_GAMES = frozenset({"math", "2048", "snake", "flappy"})


def is_admin(username: Optional[str]) -> bool:
    return (username or "").lstrip("@").lower() in ADMIN_USERNAMES


def _check_admin(username: Optional[str]) -> None:
    if not is_admin(username):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Forbidden")


try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    try:
        from api import database as db
    except ImportError:
        import database as db

    router = APIRouter()

    class AdminScoreRequest(BaseModel):
        username: str
        game_name: str
        delta: int

    class AdminWalletRequest(BaseModel):
        username: str
        amount: int
        description: str = "Изменение админом"

    class AdminEnergyRequest(BaseModel):
        username: str
        delta: int = 0
        amount: int = 0

    class AdminBlockRequest(BaseModel):
        username: str
        blocked: bool
        reason: str = ""

    class AdminRefDisableRequest(BaseModel):
        username: str
        disabled: bool

    class AdminEnsureSelfRequest(BaseModel):
        username: str
        user_id: int
        first_name: str = ""

    @router.get("/stats")
    async def admin_stats(username: str):
        _check_admin(username)
        return db.admin_get_summary_stats()

    @router.get("/players")
    async def admin_players(
        username: str,
        limit: int = 50,
        offset: int = 0,
        search: str = "",
    ):
        _check_admin(username)
        return db.admin_get_all_players(limit=limit, offset=offset, search=search)

    @router.get("/player/{user_id}")
    async def admin_get_player(user_id: int, username: str):
        _check_admin(username)
        player = db.admin_get_player(user_id)
        if not player:
            raise HTTPException(status_code=404, detail="Player not found")
        return player

    @router.post("/player/{user_id}/score")
    async def admin_adjust_score(user_id: int, req: AdminScoreRequest):
        _check_admin(req.username)
        if req.delta == 0:
            raise HTTPException(status_code=400, detail="delta_must_be_nonzero")
        game_name = (req.game_name or "").strip()
        if game_name not in VALID_GAMES:
            raise HTTPException(status_code=400, detail="Unknown game")
        return db.admin_adjust_score(user_id, game_name, req.delta)

    @router.post("/player/{user_id}/wallet")
    async def admin_adjust_wallet(user_id: int, req: AdminWalletRequest):
        _check_admin(req.username)
        if req.amount == 0:
            raise HTTPException(status_code=400, detail="amount_must_be_nonzero")
        desc = req.description or ("Начисление" if req.amount > 0 else "Списание")
        return db.admin_adjust_wallet(user_id, req.amount, f"[Admin] {desc}")

    @router.post("/player/{user_id}/energy")
    async def admin_adjust_energy(user_id: int, req: AdminEnergyRequest):
        _check_admin(req.username)
        delta = req.delta if req.delta != 0 else req.amount
        if delta == 0:
            raise HTTPException(status_code=400, detail="delta_must_be_nonzero")
        return db.admin_adjust_energy(user_id, delta)

    @router.post("/player/{user_id}/block")
    async def admin_block_player(user_id: int, req: AdminBlockRequest):
        _check_admin(req.username)
        db.admin_set_blocked(user_id, req.blocked, req.reason or "")
        return {"status": "ok", "blocked": req.blocked}

    @router.post("/player/{user_id}/ref_disable")
    async def admin_ref_disable(user_id: int, req: AdminRefDisableRequest):
        _check_admin(req.username)
        db.admin_set_ref_disabled(user_id, req.disabled)
        return {"status": "ok", "ref_disabled": req.disabled}

    @router.delete("/player/{user_id}")
    async def admin_delete_player(user_id: int, username: str):
        _check_admin(username)
        db.admin_delete_player(user_id)
        return {"status": "ok"}

    @router.post("/purge-test-players")
    async def admin_purge_test_players(username: str):
        _check_admin(username)
        return db.admin_purge_test_players()

    @router.post("/ensure_self")
    async def admin_ensure_self(req: AdminEnsureSelfRequest):
        _check_admin(req.username)
        if not req.user_id:
            raise HTTPException(status_code=400, detail="user_id required")
        result = db.admin_ensure_self(req.user_id, (req.first_name or req.username or "Игрок").strip())
        return {"status": "ok", **result}

    # ── Быстрые сбросы ──────────────────────────────────────────
    class AdminActionRequest(BaseModel):
        username: str

    # Персональные (для конкретного игрока)
    @router.post("/player/{user_id}/reset_scores")
    async def admin_reset_scores(user_id: int, req: AdminActionRequest):
        _check_admin(req.username)
        return db.admin_reset_player_scores(user_id)

    @router.post("/player/{user_id}/reset_scores/{game_name}")
    async def admin_reset_scores_game(user_id: int, game_name: str, req: AdminActionRequest):
        _check_admin(req.username)
        return db.admin_reset_player_scores_game(user_id, game_name)

    @router.post("/player/{user_id}/reset_durak")
    async def admin_reset_player_durak(user_id: int, req: AdminActionRequest):
        _check_admin(req.username)
        return db.admin_reset_durak_player(user_id)

    @router.post("/player/{user_id}/reset_energy")
    async def admin_reset_player_energy(user_id: int, req: AdminActionRequest):
        _check_admin(req.username)
        return db.admin_set_energy(user_id, 100)

    @router.post("/player/{user_id}/reset_wallet")
    async def admin_reset_player_wallet(user_id: int, req: AdminActionRequest):
        _check_admin(req.username)
        return db.admin_zero_wallet(user_id)

    @router.post("/player/{user_id}/reset_referrals")
    async def admin_reset_player_referrals(user_id: int, req: AdminActionRequest):
        _check_admin(req.username)
        return db.admin_reset_referrals(user_id)

    # Массовые (для всех игроков)
    @router.post("/reset_all/scores")
    async def admin_reset_all_scores_ep(username: str):
        _check_admin(username)
        return db.admin_reset_all_scores()

    @router.post("/reset_all/scores/{game_name}")
    async def admin_reset_all_scores_game_ep(game_name: str, username: str):
        _check_admin(username)
        return db.admin_reset_all_scores_game(game_name)

    @router.post("/reset_all/durak")
    async def admin_reset_all_durak_ep(username: str):
        _check_admin(username)
        return db.admin_reset_durak_all()

    @router.post("/reset_all/energy")
    async def admin_reset_all_energy_ep(username: str):
        _check_admin(username)
        return db.admin_set_all_energy(100)

    @router.post("/reset_all/wallets")
    async def admin_reset_all_wallets_ep(username: str):
        _check_admin(username)
        return db.admin_zero_all_wallets()

    @router.post("/reset_sub_verified")
    async def admin_reset_sub_verified(username: str):
        """Сбросить sub_verified у всех — при следующем /start все увидят gate."""
        _check_admin(username)
        count = db.reset_all_sub_verified()
        return {"ok": True, "reset_count": count}

    @router.get("/star_balance")
    async def admin_star_balance(username: str):
        """Баланс Stars бота + транзакции по дням (только для админов)."""
        _check_admin(username)
        import os, httpx
        from collections import defaultdict

        bot_token = os.getenv("BOT_TOKEN", "")
        if not bot_token:
            raise HTTPException(status_code=503, detail="BOT_TOKEN not set")

        # Используем Bot API напрямую через httpx — надёжнее чем ptb-обёртка
        all_tx = []
        offset = 0
        async with httpx.AsyncClient(timeout=15) as client:
            while offset < 500:
                resp = await client.get(
                    f"https://api.telegram.org/bot{bot_token}/getStarTransactions",
                    params={"offset": offset, "limit": 100}
                )
                data = resp.json()
                if not data.get("ok"):
                    raise HTTPException(status_code=502, detail=data.get("description", "TG API error"))
                txs = data["result"].get("transactions", [])
                if not txs:
                    break
                all_tx.extend(txs)
                if len(txs) < 100:
                    break
                offset += 100

        total_in = 0
        total_out = 0
        by_day: dict = defaultdict(int)

        for tx in all_tx:
            # amount в nanostar (1 star = 1_000_000_000) в новых версиях API,
            # но в старых — целые звёзды. Нормализуем:
            raw = tx.get("nanostar_amount") or tx.get("amount", 0)
            # если > 1_000_000 — это nanostar
            amount = raw // 1_000_000_000 if raw > 1_000_000 else raw

            import datetime
            dt = datetime.datetime.fromtimestamp(tx["date"], tz=datetime.timezone.utc)
            day = dt.strftime("%Y-%m-%d")

            if tx.get("source") is not None:
                total_in += amount
                by_day[day] += amount
            elif tx.get("receiver") is not None:
                total_out += amount
                by_day[day] -= amount

        days_sorted = sorted(by_day.items())

        return {
            "total_in": total_in,
            "total_out": total_out,
            "balance": total_in - total_out,
            "by_day": [{"date": d, "amount": a} for d, a in days_sorted],
            "tx_count": len(all_tx),
        }

except ImportError as e:
    router = None
    logger.error("[ADMIN_ROUTES] FastAPI не найден: %s", e)
except Exception as e:
    router = None
    logger.error("[ADMIN_ROUTES] Ошибка инициализации: %s", e, exc_info=True)