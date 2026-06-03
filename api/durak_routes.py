"""
durak_routes.py
────────────────
Роуты для Durak Online (лобби + игра + WS + история + экономика).
"""

import json
import time
import logging
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

try:
    from api import database as db
    from api.durak_game import DurakGame, Card
except ImportError:
    import database as db
    from durak_game import DurakGame, Card

router = APIRouter(prefix="/api/durak", tags=["durak"])

# Временное хранилище активных игр (позже заменим на Redis / БД)
active_games: dict[int, DurakGame] = {}  # lobby_id -> DurakGame

# ── WebSocket Connection Manager для Durak (реальный realtime) ─────
class DurakConnectionManager:
    def __init__(self):
        # lobby_id -> list of {user_id: int, websocket: WebSocket}
        self.active_connections: Dict[int, List[Dict]] = {}

    async def connect(self, lobby_id: int, user_id: int, websocket: WebSocket):
        await websocket.accept()
        if lobby_id not in self.active_connections:
            self.active_connections[lobby_id] = []
        self.active_connections[lobby_id].append({"user_id": user_id, "websocket": websocket})
        logger.info(f"[DURAK WS] User {user_id} connected to lobby {lobby_id}")

    def disconnect(self, lobby_id: int, user_id: int):
        if lobby_id in self.active_connections:
            self.active_connections[lobby_id] = [
                conn for conn in self.active_connections[lobby_id]
                if conn["user_id"] != user_id
            ]
            logger.info(f"[DURAK WS] User {user_id} disconnected from lobby {lobby_id}")

    async def broadcast(self, lobby_id: int, message: dict):
        """Отправить сообщение всем подключённым в лобби."""
        if lobby_id not in self.active_connections:
            return
        dead = []
        for conn in self.active_connections[lobby_id]:
            try:
                await conn["websocket"].send_json(message)
            except Exception:
                dead.append(conn)
        # очистка мёртвых соединений
        for conn in dead:
            try:
                self.active_connections[lobby_id].remove(conn)
            except ValueError:
                pass

# Глобальный менеджер
durak_ws_manager = DurakConnectionManager()

# ── Blacklist для названий лобби ─────────────────────────────────
FORBIDDEN_LOBBY_WORDS = {
    # Русский мат и оскорбления
    "хуй", "хуи", "хуя", "хуёв", "хуев", "хер", "херов",
    "пизда", "пизд", "пиздец", "пиздюк",
    "ебать", "ебал", "ебёт", "ебан", "еблани", "уеб", "уёб", "уебок", "еблан",
    "бля", "блядь", "блядина", "бляди",
    "сука", "суки", "сукин",
    "мудак", "мудаки", "мудило", "муди",
    "дроч", "дрочить", "дрочер",
    "пидор", "пидр", "педик", "пидорас",
    "нацист", "фашист", "гитлер",
    # Английский
    "fuck", "shit", "cunt", "dick", "pussy", "asshole", "bastard",
}

def is_lobby_name_allowed(name: str) -> bool:
    """Проверяет название лобби на запрещённые слова."""
    if not name:
        return True
    name_lower = name.lower()
    for word in FORBIDDEN_LOBBY_WORDS:
        if word in name_lower:
            return False
    return True

# ── Pydantic модели ─────────────────────────────────────────────

class CreateLobbyRequest(BaseModel):
    user_id: int
    first_name: str = ""
    name: str | None = None
    max_players: int = 4
    deck_size: int = 36
    game_type: str = "podkidnoy"          # podkidnoy | perevodnoy
    cheating_enabled: bool = False
    bet_amount: int = 0
    photo_url: str | None = None


class JoinLobbyRequest(BaseModel):
    user_id: int
    first_name: str = ""
    photo_url: str | None = None


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
    if db.is_durak_banned(req.user_id):
        raise HTTPException(status_code=403, detail="You are banned from Durak")
    if req.max_players < 2 or req.max_players > 6:
        raise HTTPException(status_code=400, detail="max_players must be between 2 and 6")
    if req.deck_size not in (24, 36, 52):
        raise HTTPException(status_code=400, detail="Invalid deck_size")
    if req.game_type not in ("podkidnoy", "perevodnoy"):
        raise HTTPException(status_code=400, detail="Invalid game_type")
    if req.bet_amount < 0:
        raise HTTPException(status_code=400, detail="bet_amount cannot be negative")

    # Проверка на запрещённые слова в названии
    if req.name and not is_lobby_name_allowed(req.name):
        raise HTTPException(status_code=400, detail="Название лобби содержит недопустимые слова. Выберите другое название.")

    # Проверка: пользователь не должен уже быть в активном лобби
    existing = db.is_user_in_active_lobby(req.user_id)
    if existing:
        raise HTTPException(status_code=400, detail="You are already in another active lobby. Leave it first.")

    lobby_id = db.create_durak_lobby(
        creator_id=req.user_id,
        creator_name=req.first_name,
        name=req.name,
        max_players=req.max_players,
        deck_size=req.deck_size,
        game_type=req.game_type,
        cheating_enabled=req.cheating_enabled,
        bet_amount=req.bet_amount,
        photo_url=req.photo_url
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
    if db.is_durak_banned(req.user_id):
        raise HTTPException(status_code=403, detail="You are banned from Durak")
    # Проверка: пользователь уже не в другом активном лобби
    existing = db.is_user_in_active_lobby(req.user_id)
    if existing and existing != lobby_id:
        raise HTTPException(status_code=400, detail="You are already in another active lobby")

    success = db.join_durak_lobby(lobby_id, req.user_id, req.first_name, req.photo_url)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot join lobby (full, already in another lobby, or not available)")

    # Broadcast
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "player_joined",
        "user_id": req.user_id,
        "first_name": req.first_name
    })

    return {"status": "joined"}


@router.post("/lobbies/{lobby_id}/leave")
async def leave_lobby(lobby_id: int, req: LeaveLobbyRequest):
    db.leave_durak_lobby(lobby_id, req.user_id)

    # Broadcast
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "player_left",
        "user_id": req.user_id
    })

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

    # Broadcast settings update
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "lobby_updated",
        "settings": updates
    })

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

    # Broadcast realtime
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "player_ready_changed",
        "user_id": req.user_id,
        "is_ready": req.is_ready
    })

    return {"status": "ok", "is_ready": req.is_ready}


class StartGameRequest(BaseModel):
    user_id: int


class GameActionRequest(BaseModel):
    user_id: int
    action: str                     # attack | throw_in | beat | take_table | finish_attack
    card: Optional[str] = None      # "10♥" для attack/throw_in
    attack_card: Optional[str] = None  # "K♠" для beat
    beat_card: Optional[str] = None    # "A♥" для beat


@router.post("/lobbies/{lobby_id}/start")
async def start_game(lobby_id: int, req: StartGameRequest):
    """Создатель запускает игру. Создаёт авторитетный DurakGame."""
    # 1. Базовые проверки через DB helpers
    lobby = db.get_durak_lobby_by_id(lobby_id)
    if not lobby:
        raise HTTPException(status_code=404, detail="Lobby not found")

    if lobby.get("creator_id") != req.user_id:
        raise HTTPException(status_code=403, detail="Only the creator can start the game")

    if lobby.get("status") != "waiting":
        raise HTTPException(status_code=400, detail="Lobby is not in waiting state")

    players = db.get_lobby_players(lobby_id)
    total = len(players)
    ready_count = sum(1 for p in players if p.get("is_ready"))

    if total < 2:
        raise HTTPException(status_code=400, detail="At least 2 players are required to start")
    if ready_count < total:
        raise HTTPException(status_code=400, detail="All players must be ready to start the game")

    # 2. Переводим лобби в playing (атомарно, с проверкой создателя)
    started = db.start_durak_game(lobby_id, req.user_id)
    if not started:
        raise HTTPException(status_code=400, detail="Failed to start game (state changed?)")

    # 3. Создаём авторитетную игровую сессию
    game_created = False
    try:
        player_ids = [int(p["user_id"]) for p in players]
        deck_size = int(lobby.get("deck_size", 36))
        game_type = lobby.get("game_type", "podkidnoy")

        if player_ids:
            game = DurakGame(player_ids, deck_size=deck_size, game_type=game_type)
            active_games[lobby_id] = game
            game.start_game()
            _persist(lobby_id, game)
            game_created = True
            logger.info(f"[DURAK] Game started for lobby #{lobby_id} with {len(player_ids)} players")
    except Exception as e:
        logger.exception(f"Failed to instantiate DurakGame for lobby {lobby_id}: {e}")
        # Не откатываем статус лобби — клиент увидит playing без game instance (редкий кейс)
        # В будущем можно добавить recovery

    initial_state = None
    if game_created and lobby_id in active_games:
        # Возвращаем начальное состояние (без конкретного viewer — клиент запросит сам)
        initial_state = active_games[lobby_id].get_full_game_state()

    # Broadcast game started to all in lobby (real-time)
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "game_started",
        "started_by": req.user_id,
        "initial_state": initial_state
    })

    return {
        "status": "started",
        "game_created": game_created,
        "initial_state": initial_state
    }


# ── Игровые эндпоинты ─────────────────

@router.get("/lobbies/{lobby_id}/state")
async def get_game_state(lobby_id: int, user_id: Optional[int] = None):
    """Возвращает текущее состояние игры (если лобби в playing и игра создана)."""
    game = _get_game(lobby_id)
    if not game:
        # Попробуем проверить статус лобби
        lobby = db.get_durak_lobby_by_id(lobby_id)
        if lobby and lobby.get("status") == "playing":
            return {"status": "playing", "game_instance": False, "message": "Game state not yet loaded on this node"}
        raise HTTPException(status_code=404, detail="No active game for this lobby")

    # Таймаут хода: если игрок завис — авто-действие, затем рассылка остальным
    applied = _apply_turn_timeout(game)
    if applied:
        logger.info(f"[DURAK] Turn timeout in lobby {lobby_id}: auto '{applied}'")
        if game.game_over:
            await broadcast_game_state(lobby_id, game, "game_ended")
            _finalize_game(lobby_id, game)
        else:
            _persist(lobby_id, game)
            await broadcast_game_state(lobby_id, game, "game_action", {"timeout": True})

    state = game.get_full_game_state(viewer_id=user_id)
    return {"status": "ok", "state": state}


def _parse_card(s: Optional[str]) -> Optional[Card]:
    if not s:
        return None
    try:
        return Card.from_str(s)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid card format: {s} ({e})")


DURAK_COMMISSION_RATE = 0.05  # комиссия с банка при выплате победителю
TURN_TIMEOUT_SEC = 60         # таймаут хода: после него — авто-действие


def _apply_turn_timeout(game):
    """Если ход просрочен — авто-действие. Возвращает применённое действие или None."""
    if game.game_over or (time.time() - game.last_action_at) < TURN_TIMEOUT_SEC:
        return None
    applied = None
    if game._get_unbeaten_count() > 0:
        # защитник не успел отбиться — берёт карты
        if game.take_table(game.current_defender):
            applied = "take_table"
    elif len(game.table) > 0:
        # всё отбито — атакующий не объявил «бито»
        if game.finish_attack(caller_id=game.current_attacker):
            applied = "finish_attack"
    else:
        # стол пуст — атакующий не начал; играем младшую легальную (не козырь по возможности)
        legal = game.get_legal_attacks(game.current_attacker)
        if legal:
            def _rv(c):
                return (100 if c.suit == game.trump_suit else 0) + c.rank.value
            if game.attack(game.current_attacker, min(legal, key=_rv)):
                applied = "attack"
    if applied:
        game.last_action_at = time.time()
    return applied


def _persist(lobby_id: int, game) -> None:
    """Сохраняет снимок игры в БД (чтобы пережить рестарт сервера)."""
    try:
        db.save_durak_game_state(lobby_id, json.dumps(game.to_dict()))
    except Exception:
        logger.exception(f"Failed to persist game state for lobby {lobby_id}")


def _get_game(lobby_id: int):
    """Возвращает игру из памяти или восстанавливает из БД."""
    game = active_games.get(lobby_id)
    if game:
        return game
    try:
        raw = db.load_durak_game_state(lobby_id)
        if raw:
            game = DurakGame.from_dict(json.loads(raw))
            active_games[lobby_id] = game
            logger.info(f"[DURAK] Restored game state for lobby {lobby_id} from DB")
            return game
    except Exception:
        logger.exception(f"Failed to restore game state for lobby {lobby_id}")
    return None


def _finalize_game(lobby_id: int, game) -> None:
    """Завершение партии: выплата победителю, история, статус лобби, очистка памяти."""
    winner = game.winner
    pot = 0
    try:
        lobby = db.get_durak_lobby_by_id(lobby_id) or {}
        pot = lobby.get('pot', 0) or 0
        if winner and pot > 0:
            commission = int(pot * DURAK_COMMISSION_RATE)
            award = pot - commission
            db.topup_wallet(winner, '', award, f"Победа в дураке (лобби #{lobby_id})")
            logger.info(f"[DURAK PAYOUT] {award} to {winner} (comm {commission}) from pot {pot}")
    except Exception:
        logger.exception("Payout error")
    try:
        players = db.get_lobby_players(lobby_id) or []
        players_data = [
            {"user_id": p["user_id"], "first_name": p.get("first_name"), "is_winner": p["user_id"] == winner}
            for p in players
        ]
        db.save_durak_game_history(lobby_id, winner, pot, players_data)
        db.finish_durak_lobby(lobby_id)
    except Exception:
        logger.exception("History save error")
    try:
        db.delete_durak_game_state(lobby_id)
    except Exception:
        logger.exception("Delete game state error")
    active_games.pop(lobby_id, None)


async def broadcast_game_state(lobby_id: int, game, msg_type: str, extra: dict = None):
    """Рассылает каждому подключённому игроку ЕГО состояние (чужие руки скрыты)."""
    conns = list(durak_ws_manager.active_connections.get(lobby_id, []))
    dead = []
    for conn in conns:
        try:
            state = game.get_full_game_state(viewer_id=conn["user_id"])
            payload = {"type": msg_type, "state": state}
            if msg_type == "game_ended":
                payload["final_state"] = state
                payload["winner_id"] = game.winner
            if extra:
                payload.update(extra)
            await conn["websocket"].send_json(payload)
        except Exception:
            dead.append(conn)
    for c in dead:
        durak_ws_manager.disconnect(lobby_id, c["user_id"])


@router.post("/lobbies/{lobby_id}/action")
async def perform_game_action(lobby_id: int, req: GameActionRequest):
    """
    Универсальный эндпоинт для совершения ходов.
    Клиент должен сначала запросить /state, чтобы получить allowed_actions и legal_*.
    Серверная валидация — жёсткая.
    """
    game = _get_game(lobby_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found or not started")
    if game.game_over:
        raise HTTPException(status_code=400, detail="Game is already over")

    pid = req.user_id
    if pid not in game.player_ids:
        raise HTTPException(status_code=403, detail="You are not a participant in this game")

    action = (req.action or "").strip().lower()

    success = False
    message = ""

    try:
        if action in ("attack", "throw_in"):
            card = _parse_card(req.card)
            if not card:
                raise HTTPException(status_code=400, detail="card is required for attack/throw_in")
            # is_legal_attack уже учитывает wave + ранг + лимиты
            if not game.is_legal_attack(pid, card):
                raise HTTPException(status_code=400, detail="Illegal attack/throw_in")
            success = game.attack(pid, card)
            message = "attacked" if success else "attack failed"

        elif action == "beat":
            atk = _parse_card(req.attack_card)
            bt = _parse_card(req.beat_card)
            if not atk or not bt:
                raise HTTPException(status_code=400, detail="attack_card and beat_card required for beat")
            if not game.is_legal_beat(pid, atk, bt):
                raise HTTPException(status_code=400, detail="Illegal beat")
            success = game.beat(pid, atk, bt)
            message = "beat" if success else "beat failed"

        elif action == "take_table":
            success = game.take_table(pid)
            message = "took table" if success else "cannot take table now"

        elif action in ("finish_attack", "finish", "bito"):
            # "Бито" может объявить только атакующий
            success = game.finish_attack(caller_id=pid)
            message = "attack finished" if success else "cannot finish attack now"

        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Action {action} failed in lobby {lobby_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal game error")

    if success:
        game.last_action_at = time.time()

    new_state = game.get_full_game_state(viewer_id=pid)

    # Если игра закончилась — выплата/история/финиш лобби/очистка; иначе сохраняем снимок
    if game.game_over and lobby_id in active_games:
        logger.info(f"[DURAK] Game over in lobby {lobby_id}, winner={game.winner}")
        _finalize_game(lobby_id, game)
    elif success:
        _persist(lobby_id, game)

    # Рассылаем обновление каждому игроку (своё состояние). Один broadcast — без гонки.
    if game.game_over:
        await broadcast_game_state(lobby_id, game, "game_ended")
    else:
        await broadcast_game_state(lobby_id, game, "game_action",
                                   {"user_id": pid, "action": action})

    return {
        "status": "ok" if success else "error",
        "success": success,
        "message": message,
        "state": new_state
    }


@router.post("/lobbies/{lobby_id}/forfeit")
async def forfeit_game(lobby_id: int, req: StartGameRequest):
    """Игрок сдаётся: партия завершается, победа соперника (выплата/история/финиш)."""
    game = _get_game(lobby_id)
    if not game:
        # Игры нет в памяти (рестарт/уже завершена) — просто помечаем лобби завершённым
        try:
            db.finish_durak_lobby(lobby_id)
        except Exception:
            logger.exception("forfeit finish (no game) error")
        return {"status": "ok", "game_instance": False}

    pid = req.user_id
    if pid not in game.player_ids:
        raise HTTPException(status_code=403, detail="You are not a participant in this game")

    if not game.game_over:
        game.forfeit(pid)

    winner = game.winner
    await broadcast_game_state(lobby_id, game, "game_ended", {"forfeited_by": pid})
    _finalize_game(lobby_id, game)
    logger.info(f"[DURAK] Forfeit in lobby {lobby_id} by {pid}, winner={winner}")
    return {"status": "ok", "winner_id": winner}


@router.get("/history")
async def get_durak_history(user_id: Optional[int] = None, limit: int = 20):
    """История завершённых игр Дурака."""
    try:
        history = db.get_durak_history(user_id=user_id, limit=limit)
        return {"history": history}
    except Exception as e:
        logger.exception("get_durak_history error")
        raise HTTPException(status_code=500, detail="Failed to load history")


@router.get("/ratings")
async def get_durak_ratings(limit: int = 20):
    """Рейтинг игроков Дурака по победам."""
    try:
        ratings = db.get_durak_ratings(limit=limit)
        return {"ratings": ratings}
    except Exception as e:
        logger.exception("get_durak_ratings error")
        raise HTTPException(status_code=500, detail="Failed to load ratings")


@router.get("/stats/{user_id}")
async def durak_user_stats(user_id: int):
    """Личная статистика игрока по Дураку (партий/побед/винрейт)."""
    try:
        return db.get_durak_user_stats(user_id)
    except Exception:
        logger.exception("durak_user_stats error")
        return {"games": 0, "wins": 0, "win_rate": 0}


# --- Админка Дурака (доступ только для ADMIN_ID) ---

def _require_durak_admin(user_id: int) -> None:
    """Пропускает только админа (user_id из ADMIN_ID). Иначе 403."""
    try:
        admins = db.get_protected_user_ids()
    except Exception:
        admins = set()
    if user_id not in admins:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/admin/lobbies")
async def admin_durak_lobbies(user_id: int):
    _require_durak_admin(user_id)
    try:
        lobbies = db.get_active_durak_lobbies(limit=100)
        return {"lobbies": lobbies}
    except Exception:
        logger.exception("admin lobbies error")
        raise HTTPException(500, "error")


@router.post("/admin/lobbies/{lobby_id}/force-end")
async def admin_force_end(lobby_id: int, user_id: int):
    _require_durak_admin(user_id)
    try:
        active_games.pop(lobby_id, None)
        db.finish_durak_lobby(lobby_id)
        db.delete_durak_game_state(lobby_id)
        return {"status": "ended"}
    except Exception:
        logger.exception("force-end error")
        raise HTTPException(500, "force-end failed")


@router.post("/admin/ban")
async def admin_ban_durak(user_id: int, target_user: int, reason: str = ""):
    _require_durak_admin(user_id)
    try:
        db.ban_durak_user(target_user, reason)
        return {"status": "banned"}
    except Exception:
        logger.exception("ban error")
        raise HTTPException(500, "ban failed")



# ── Реальный WebSocket для Durak (реaltime обновления) ────────────

@router.websocket("/ws/{lobby_id}")
async def durak_websocket(websocket: WebSocket, lobby_id: int, user_id: int = Query(..., description="User ID")):
    """WebSocket соединение для лобби/игры Дурака.
    Клиент подключается: ws(s)://host/api/durak/ws/{lobby_id}?user_id=XXX
    """
    await durak_ws_manager.connect(lobby_id, user_id, websocket)
    try:
        # Отправляем подтверждение подключения
        await websocket.send_json({
            "type": "connected",
            "lobby_id": lobby_id,
            "user_id": user_id
        })

        while True:
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                # не-JSON сообщение — игнорируем
                continue

            action = data.get("action")
            payload = data.get("data", {}) or {}

            if action == "reaction":
                # Транслируем эмоцию остальным игрокам лобби (отправителю не дублируем)
                for conn in list(durak_ws_manager.active_connections.get(lobby_id, [])):
                    if conn["user_id"] == user_id:
                        continue
                    try:
                        await conn["websocket"].send_json({
                            "type": "reaction",
                            "user_id": user_id,
                            "emojiName": payload.get("emojiName"),
                            "position": payload.get("position", "self"),
                        })
                    except Exception:
                        pass
            elif action == "game_action":
                # Ходы принимаются только через REST (там жёсткая валидация)
                await websocket.send_json({
                    "type": "error",
                    "message": "Use REST POST /action for game moves",
                })
            # 'ready' и прочее идёт через REST — игнорируем

    except WebSocketDisconnect:
        durak_ws_manager.disconnect(lobby_id, user_id)
    except Exception as e:
        logger.exception(f"[DURAK WS] error in lobby {lobby_id} user {user_id}: {e}")
        durak_ws_manager.disconnect(lobby_id, user_id)