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

except ImportError as e:
    router = None
    logger.error("[ADMIN_ROUTES] FastAPI не найден: %s", e)
except Exception as e:
    router = None
    logger.error("[ADMIN_ROUTES] Ошибка инициализации: %s", e, exc_info=True)