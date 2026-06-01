"""
durak_routes.py
────────────────
Роуты для системы лобби Дурак Онлайн (Этап 2).

Пока без игровой логики — только лобби, игроки, настройки.
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from api import database as db
except ImportError:
    import database as db

router = APIRouter(prefix="/api/durak", tags=["durak"])

# ── Pydantic модели ─────────────────────────────────────────────

class CreateLobbyRequest(BaseModel):
    user_id: int
    first_name: str = ""
    max_players: int = 4
    deck_size: int = 36
    game_type: str = "podkidnoy"          # podkidnoy | perevodnoy
    cheating_enabled: bool = False
    bet_amount: int = 0


class JoinLobbyRequest(BaseModel):
    user_id: int
    first_name: str = ""


class LeaveLobbyRequest(BaseModel):
    user_id: int


class UpdateSettingsRequest(BaseModel):
    user_id: int
    max_players: Optional[int] = None
    deck_size: Optional[int] = None
    game_type: Optional[str] = None
    cheating_enabled: Optional[bool] = None
    bet_amount: Optional[int] = None


# ── Эндпоинты ───────────────────────────────────────────────────

@router.post("/lobbies")
async def create_lobby(req: CreateLobbyRequest):
    """Создать новое лобби."""
    if req.max_players < 2 or req.max_players > 6:
        raise HTTPException(status_code=400, detail="max_players must be between 2 and 6")
    if req.deck_size not in (24, 36, 52):
        raise HTTPException(status_code=400, detail="Invalid deck_size")
    if req.game_type not in ("podkidnoy", "perevodnoy"):
        raise HTTPException(status_code=400, detail="Invalid game_type")
    if req.bet_amount < 0:
        raise HTTPException(status_code=400, detail="bet_amount cannot be negative")

    # Проверка: пользователь не должен уже быть в активном лобби
    existing = db.is_user_in_active_lobby(req.user_id)
    if existing:
        raise HTTPException(status_code=400, detail="You are already in another active lobby. Leave it first.")

    lobby_id = db.create_durak_lobby(
        creator_id=req.user_id,
        creator_name=req.first_name,
        max_players=req.max_players,
        deck_size=req.deck_size,
        game_type=req.game_type,
        cheating_enabled=req.cheating_enabled,
        bet_amount=req.bet_amount
    )
    logger.info(f"[DURAK] Lobby #{lobby_id} created by user {req.user_id}")
    return {"lobby_id": lobby_id}


@router.get("/lobbies")
async def list_lobbies():
    """Список активных лобби."""
    lobbies = db.get_active_durak_lobbies()
    return {"lobbies": lobbies}


@router.get("/lobbies/{lobby_id}")
async def get_lobby(lobby_id: int):
    """Получить информацию об одном лобби (включая playing)."""
    # Получаем даже playing лобби
    lobby = db.get_durak_lobby_by_id(lobby_id)  # если есть helper, иначе fallback

    if not lobby:
        # fallback на старый способ
        lobbies = db.get_active_durak_lobbies(200)
        lobby = next((l for l in lobbies if l["id"] == lobby_id), None)

    if not lobby:
        raise HTTPException(status_code=404, detail="Lobby not found or no longer active")

    players = db.get_lobby_players(lobby_id)
    lobby["players"] = players
    return lobby


@router.post("/lobbies/{lobby_id}/join")
async def join_lobby(lobby_id: int, req: JoinLobbyRequest):
    # Проверка: пользователь уже не в другом активном лобби
    existing = db.is_user_in_active_lobby(req.user_id)
    if existing and existing != lobby_id:
        raise HTTPException(status_code=400, detail="You are already in another active lobby")

    success = db.join_durak_lobby(lobby_id, req.user_id, req.first_name)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot join lobby (full, already in another lobby, or not available)")
    return {"status": "joined"}


@router.post("/lobbies/{lobby_id}/leave")
async def leave_lobby(lobby_id: int, req: LeaveLobbyRequest):
    db.leave_durak_lobby(lobby_id, req.user_id)
    return {"status": "left"}


@router.get("/lobbies/{lobby_id}/players")
async def get_lobby_players(lobby_id: int):
    players = db.get_lobby_players(lobby_id)
    return {"players": players}


@router.post("/lobbies/{lobby_id}/settings")
async def update_settings(lobby_id: int, req: UpdateSettingsRequest):
    """Обновить настройки лобби (только создатель)."""
    updates = {}
    if req.max_players is not None:
        updates["max_players"] = req.max_players
    if req.deck_size is not None:
        updates["deck_size"] = req.deck_size
    if req.game_type is not None:
        updates["game_type"] = req.game_type
    if req.cheating_enabled is not None:
        updates["cheating_enabled"] = req.cheating_enabled
    if req.bet_amount is not None:
        updates["bet_amount"] = req.bet_amount

    success = db.update_lobby_settings(lobby_id, req.user_id, **updates)
    if not success:
        raise HTTPException(status_code=403, detail="Not allowed or invalid settings")
    return {"status": "updated"}


class SetReadyRequest(BaseModel):
    user_id: int
    is_ready: bool


@router.post("/lobbies/{lobby_id}/ready")
async def set_ready(lobby_id: int, req: SetReadyRequest):
    """Игрок отмечает себя готовым / не готовым."""
    success = db.set_player_ready(lobby_id, req.user_id, req.is_ready)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update ready status")
    return {"status": "ok", "is_ready": req.is_ready}


class StartGameRequest(BaseModel):
    user_id: int


@router.post("/lobbies/{lobby_id}/start")
async def start_game(lobby_id: int, req: StartGameRequest):
    """Создатель запускает игру."""
    conn = get_connection()
    cur = _cursor(conn)

    # Получаем информацию о лобби
    cur.execute('''
        SELECT creator_id, status, max_players 
        FROM durak_lobbies 
        WHERE id = %s
    ''', (lobby_id,))
    lobby = cur.fetchone()

    if not lobby:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Lobby not found")

    if lobby["creator_id"] != req.user_id:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Only the creator can start the game")

    if lobby["status"] != "waiting":
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Lobby is not in waiting state")

    # Проверяем, что все игроки готовы и набрано минимальное количество
    cur.execute('''
        SELECT COUNT(*) as total_players,
               SUM(CASE WHEN is_ready = TRUE THEN 1 ELSE 0 END) as ready_players
        FROM durak_lobby_players
        WHERE lobby_id = %s
    ''', (lobby_id,))
    stats = cur.fetchone()

    total = stats["total_players"] or 0
    ready = stats["ready_players"] or 0

    if total < 2:
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="At least 2 players are required to start")

    if ready < total:
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="All players must be ready to start the game")

    # Запускаем игру
    cur.execute("UPDATE durak_lobbies SET status = 'playing', updated_at = NOW() WHERE id = %s", (lobby_id,))
    conn.commit()
    cur.close()
    conn.close()

    return {"status": "started"}