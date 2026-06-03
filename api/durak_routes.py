"""
durak_routes.py
────────────────
Роуты для Durak Online (лобби + игра + WS + история + экономика).
"""

import json
import time
import asyncio
import logging
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Query, Depends
from pydantic import BaseModel
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

try:
    from api import database as db
    from api.durak_game import DurakGame, Card
    from api.tg_auth import require_webapp_user
except ImportError:
    import database as db
    from durak_game import DurakGame, Card
    from tg_auth import require_webapp_user

router = APIRouter(prefix="/api/durak", tags=["durak"])

# Временное хранилище активных игр (позже заменим на Redis / БД)
active_games: dict[int, DurakGame] = {}  # lobby_id -> DurakGame

# ── WebSocket Connection Manager для Durak (реальный realtime) ─────
class DurakConnectionManager:
    def __init__(self):
        # lobby_id -> list of {user_id: int, websocket: WebSocket}
        self.active_connections: Dict[int, List[Dict]] = {}
        # lobby_id -> {user_id: timestamp отключения}
        self.disconnect_times: Dict[int, Dict[int, float]] = {}

    async def connect(self, lobby_id: int, user_id: int, websocket: WebSocket):
        await websocket.accept()
        if lobby_id not in self.active_connections:
            self.active_connections[lobby_id] = []
        self.active_connections[lobby_id].append({"user_id": user_id, "websocket": websocket})
        # игрок вернулся — снимаем отметку отключения
        self.disconnect_times.get(lobby_id, {}).pop(user_id, None)
        logger.info(f"[DURAK WS] User {user_id} connected to lobby {lobby_id}")

    def disconnect(self, lobby_id: int, user_id: int):
        if lobby_id in self.active_connections:
            self.active_connections[lobby_id] = [
                conn for conn in self.active_connections[lobby_id]
                if conn["user_id"] != user_id
            ]
            logger.info(f"[DURAK WS] User {user_id} disconnected from lobby {lobby_id}")
        # фиксируем время отключения (для авто-форфейта по долгому отвалу)
        self.disconnect_times.setdefault(lobby_id, {})[user_id] = time.time()

    def connected_user_ids(self, lobby_id: int) -> List[int]:
        return [c["user_id"] for c in self.active_connections.get(lobby_id, [])]

    def disconnected_since(self, lobby_id: int, user_id: int):
        """Время отключения игрока (если сейчас не подключён), иначе None."""
        if user_id in self.connected_user_ids(lobby_id):
            return None
        return self.disconnect_times.get(lobby_id, {}).get(user_id)

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
async def create_lobby(req: CreateLobbyRequest, tg_user: dict = Depends(require_webapp_user)):
    """Создать новое лобби."""
    uid = int(tg_user["id"])
    first_name = tg_user.get("first_name") or req.first_name or ""
    photo_url = tg_user.get("photo_url") or req.photo_url
    if db.is_durak_banned(uid):
        raise HTTPException(status_code=403, detail="You are banned from Durak")
    if req.max_players < 2 or req.max_players > 6:
        raise HTTPException(status_code=400, detail="max_players must be between 2 and 6")
    if req.deck_size not in (24, 36, 52):
        raise HTTPException(status_code=400, detail="Invalid deck_size")
    # В колоде должно хватать карт на раздачу по 6 каждому
    _deck_max_players = {24: 4, 36: 6, 52: 6}
    if req.max_players > _deck_max_players.get(req.deck_size, 6):
        raise HTTPException(
            status_code=400,
            detail=f"Колода на {req.deck_size} карт рассчитана максимум на {_deck_max_players[req.deck_size]} игроков",
        )
    if req.game_type not in ("podkidnoy", "perevodnoy"):
        raise HTTPException(status_code=400, detail="Invalid game_type")
    if req.bet_amount < 0:
        raise HTTPException(status_code=400, detail="bet_amount cannot be negative")
    if req.bet_amount > 0 and (req.bet_amount < 5 or req.bet_amount > 100000):
        raise HTTPException(status_code=400, detail="Ставка должна быть от 5 до 100000 ⭐")
    # Создатель сразу вносит ставку — проверяем баланс кошелька
    if req.bet_amount > 0:
        wallet = db.get_wallet(uid)
        if wallet["balance"] < req.bet_amount:
            raise HTTPException(status_code=402, detail={
                "reason": "insufficient_funds",
                "need": req.bet_amount,
                "balance": wallet["balance"],
                "short": req.bet_amount - wallet["balance"],
            })

    # Проверка на запрещённые слова в названии
    if req.name and not is_lobby_name_allowed(req.name):
        raise HTTPException(status_code=400, detail="Название лобби содержит недопустимые слова. Выберите другое название.")

    # Проверка: пользователь не должен уже быть в активном лобби
    existing = db.is_user_in_active_lobby(uid)
    if existing:
        raise HTTPException(status_code=400, detail="You are already in another active lobby. Leave it first.")

    try:
        lobby_id = db.create_durak_lobby(
            creator_id=uid,
            creator_name=first_name,
            name=req.name,
            max_players=req.max_players,
            deck_size=req.deck_size,
            game_type=req.game_type,
            cheating_enabled=req.cheating_enabled,
            bet_amount=req.bet_amount,
            photo_url=photo_url
        )
    except Exception:
        # Чаще всего — не хватило Stars на ставку (гонка после pre-check)
        logger.exception(f"create_durak_lobby failed for user {uid}")
        raise HTTPException(status_code=402, detail={"reason": "insufficient_funds", "need": req.bet_amount})
    logger.info(f"[DURAK] Lobby #{lobby_id} created by user {uid}")
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
async def join_lobby(lobby_id: int, req: JoinLobbyRequest, tg_user: dict = Depends(require_webapp_user)):
    uid = int(tg_user["id"])
    first_name = tg_user.get("first_name") or req.first_name or ""
    photo_url = tg_user.get("photo_url") or req.photo_url
    if db.is_durak_banned(uid):
        raise HTTPException(status_code=403, detail="You are banned from Durak")
    # Проверка: пользователь уже не в другом активном лобби
    existing = db.is_user_in_active_lobby(uid)
    if existing and existing != lobby_id:
        raise HTTPException(status_code=400, detail="You are already in another active lobby")

    result = db.join_durak_lobby(lobby_id, uid, first_name, photo_url)
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "insufficient_funds":
            # 402 → клиент предложит пополнить кошелёк в магазине
            raise HTTPException(status_code=402, detail={
                "reason": "insufficient_funds",
                "need": result.get("need"),
                "balance": result.get("balance"),
                "short": result.get("short"),
            })
        msgs = {
            "already_in_lobby": "You are already in another active lobby",
            "full": "Lobby is full",
            "not_available": "Lobby not available",
        }
        raise HTTPException(status_code=400, detail=msgs.get(reason, "Cannot join lobby"))

    # Broadcast
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "player_joined",
        "user_id": uid,
        "first_name": first_name
    })

    return {"status": "joined"}


@router.post("/lobbies/{lobby_id}/leave")
async def leave_lobby(lobby_id: int, req: LeaveLobbyRequest, tg_user: dict = Depends(require_webapp_user)):
    uid = int(tg_user["id"])
    db.leave_durak_lobby(lobby_id, uid)

    # Broadcast
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "player_left",
        "user_id": uid
    })

    return {"status": "left"}


@router.get("/lobbies/{lobby_id}/players")
async def get_lobby_players(lobby_id: int):
    players = db.get_lobby_players(lobby_id)
    return {"players": players}


@router.post("/lobbies/{lobby_id}/settings")
async def update_settings(lobby_id: int, req: UpdateSettingsRequest, tg_user: dict = Depends(require_webapp_user)):
    """Обновить настройки лобби (только создатель)."""
    uid = int(tg_user["id"])
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

    success = db.update_lobby_settings(lobby_id, uid, **updates)
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
async def set_ready(lobby_id: int, req: SetReadyRequest, tg_user: dict = Depends(require_webapp_user)):
    """Игрок отмечает себя готовым / не готовым."""
    uid = int(tg_user["id"])
    success = db.set_player_ready(lobby_id, uid, req.is_ready)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update ready status")

    # Broadcast realtime
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "player_ready_changed",
        "user_id": uid,
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
async def start_game(lobby_id: int, req: StartGameRequest, tg_user: dict = Depends(require_webapp_user)):
    """Создатель запускает игру. Создаёт авторитетный DurakGame."""
    uid = int(tg_user["id"])
    # 1. Базовые проверки через DB helpers
    lobby = db.get_durak_lobby_by_id(lobby_id)
    if not lobby:
        raise HTTPException(status_code=404, detail="Lobby not found")

    if lobby.get("creator_id") != uid:
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
    started = db.start_durak_game(lobby_id, uid)
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
        "started_by": uid,
        "initial_state": initial_state
    })

    return {
        "status": "started",
        "game_created": game_created,
        "initial_state": initial_state
    }


# ── Игровые эндпоинты ─────────────────

@router.get("/lobbies/{lobby_id}/state")
async def get_game_state(lobby_id: int, tg_user: dict = Depends(require_webapp_user)):
    """Возвращает состояние игры. Руки видит только их владелец (viewer = проверенный id)."""
    user_id = int(tg_user["id"])
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
    state["connected"] = durak_ws_manager.connected_user_ids(lobby_id)
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


def _apply_turn_timeout(game, force: bool = False):
    """Если ход просрочен — авто-действие. force=True игнорирует таймер (для свипера)."""
    if game.game_over:
        return None
    if not force and (time.time() - game.last_action_at) < TURN_TIMEOUT_SEC:
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


# ── Фоновый sweeper брошенных игр ──────────────────────────────────
DURAK_ABANDON_SEC = 120              # игра без ходов столько секунд считается брошенной
DURAK_SWEEP_INTERVAL = 60            # как часто проверять
DURAK_DISCONNECT_FORFEIT_SEC = 90    # отключён дольше — авто-форфейт (если соперник на связи)


async def _sweep_abandoned_games():
    """Авто-доигрывает/завершает игры, в которых давно никто не ходит."""
    try:
        lobby_ids = db.list_active_durak_game_lobbies()
    except Exception:
        logger.exception("sweeper: failed to list game states")
        return
    now = time.time()
    for lobby_id in lobby_ids:
        try:
            game = _get_game(lobby_id)
            if not game:
                # снимок завершённой/битой игры — чистим
                db.delete_durak_game_state(lobby_id)
                continue
            if game.game_over:
                _finalize_game(lobby_id, game)
                continue

            # Авто-форфейт: 1×1, один игрок давно отвалился, соперник на связи
            if len(game.player_ids) == 2:
                connected = durak_ws_manager.connected_user_ids(lobby_id)
                forfeited = None
                for pid in list(game.player_ids):
                    ds = durak_ws_manager.disconnected_since(lobby_id, pid)
                    opponent_online = any(p in connected for p in game.player_ids if p != pid)
                    if ds and (now - ds) > DURAK_DISCONNECT_FORFEIT_SEC and opponent_online:
                        game.forfeit(pid)
                        forfeited = pid
                        break
                if forfeited is not None:
                    logger.info(f"[DURAK] Sweeper: forfeit disconnected {forfeited} in lobby {lobby_id}")
                    await broadcast_game_state(lobby_id, game, "game_ended")
                    _finalize_game(lobby_id, game)
                    continue

            if now - game.last_action_at < DURAK_ABANDON_SEC:
                continue
            logger.info(f"[DURAK] Sweeper: auto-finishing abandoned game lobby {lobby_id}")
            steps = 0
            while not game.game_over and steps < 300:
                if not _apply_turn_timeout(game, force=True):
                    break
                steps += 1
            if not game.game_over:
                # не удалось доиграть — принудительно закрываем без победителя
                game.game_over = True
            await broadcast_game_state(lobby_id, game, "game_ended")
            _finalize_game(lobby_id, game)
        except Exception:
            logger.exception(f"sweeper: error on lobby {lobby_id}")


async def _durak_sweeper_loop():
    logger.info("[DURAK] Sweeper started")
    while True:
        try:
            await asyncio.sleep(DURAK_SWEEP_INTERVAL)
            await _sweep_abandoned_games()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("durak sweeper loop error")


_sweeper_task = None


def start_durak_sweeper():
    """Запускает фоновый sweeper (вызывать на старте приложения)."""
    global _sweeper_task
    if _sweeper_task is None:
        try:
            _sweeper_task = asyncio.create_task(_durak_sweeper_loop())
        except RuntimeError:
            logger.warning("[DURAK] No running loop to start sweeper")


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
    """Завершение партии: банк делится ПРОГРЕССИВНО по порядку выхода
    (раньше вышел — больше доля), дурак не получает ничего."""
    pot = 0
    durak = getattr(game, "durak", None)
    all_players = list(getattr(game, "all_player_ids", []) or [])
    finished = list(getattr(game, "finished", []) or [])
    # Победители в порядке выхода: сначала вышедшие (по очереди), затем прочие не-дураки
    if durak is not None and all_players:
        winners = [p for p in finished if p != durak]
        for p in all_players:
            if p != durak and p not in winners:
                winners.append(p)
    elif game.winner:
        winners = [game.winner]
    else:
        winners = []
    primary_winner = winners[0] if winners else game.winner
    try:
        lobby = db.get_durak_lobby_by_id(lobby_id) or {}
        pot = lobby.get('pot', 0) or 0
        if winners and pot > 0:
            commission = int(pot * DURAK_COMMISSION_RATE)
            prize = pot - commission
            n = len(winners)
            # Прогрессивные веса: [n, n-1, ..., 1] — первый вышедший получает больше всех
            weights = [n - i for i in range(n)]
            total_w = sum(weights)
            amounts = [prize * w // total_w for w in weights]
            amounts[0] += prize - sum(amounts)   # остаток округления — первому
            for w, amount in zip(winners, amounts):
                if amount > 0:
                    db.topup_wallet(w, '', amount, f"Выигрыш в дураке (лобби #{lobby_id})")
            logger.info(f"[DURAK PAYOUT] pot {pot} → {amounts} (comm {commission})")
    except Exception:
        logger.exception("Payout error")
    try:
        players = db.get_lobby_players(lobby_id) or []
        winset = set(winners)
        players_data = [
            {"user_id": p["user_id"], "first_name": p.get("first_name"), "is_winner": p["user_id"] in winset}
            for p in players
        ]
        db.save_durak_game_history(lobby_id, primary_winner, pot, players_data)
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
    connected = durak_ws_manager.connected_user_ids(lobby_id)
    dead = []
    for conn in conns:
        try:
            state = game.get_full_game_state(viewer_id=conn["user_id"])
            state["connected"] = connected
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


async def broadcast_presence(lobby_id: int):
    """Рассылает список подключённых игроков (для индикатора онлайн/офлайн)."""
    await durak_ws_manager.broadcast(lobby_id, {
        "type": "presence",
        "connected": durak_ws_manager.connected_user_ids(lobby_id),
    })


@router.post("/lobbies/{lobby_id}/action")
async def perform_game_action(lobby_id: int, req: GameActionRequest, tg_user: dict = Depends(require_webapp_user)):
    """
    Универсальный эндпоинт для совершения ходов.
    Клиент должен сначала запросить /state, чтобы получить allowed_actions и legal_*.
    Серверная валидация — жёсткая. Игрок берётся из проверенного initData.
    """
    game = _get_game(lobby_id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found or not started")
    if game.game_over:
        raise HTTPException(status_code=400, detail="Game is already over")

    pid = int(tg_user["id"])
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
async def forfeit_game(lobby_id: int, req: StartGameRequest, tg_user: dict = Depends(require_webapp_user)):
    """Игрок сдаётся: партия завершается, победа соперника (выплата/история/финиш)."""
    pid = int(tg_user["id"])
    game = _get_game(lobby_id)
    if not game:
        # Игры нет в памяти (рестарт/уже завершена) — просто помечаем лобби завершённым
        try:
            db.finish_durak_lobby(lobby_id)
        except Exception:
            logger.exception("forfeit finish (no game) error")
        return {"status": "ok", "game_instance": False}

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
async def admin_durak_lobbies(tg_user: dict = Depends(require_webapp_user)):
    _require_durak_admin(int(tg_user["id"]))
    try:
        lobbies = db.get_active_durak_lobbies(limit=100)
        return {"lobbies": lobbies}
    except Exception:
        logger.exception("admin lobbies error")
        raise HTTPException(500, "error")


@router.post("/admin/lobbies/{lobby_id}/force-end")
async def admin_force_end(lobby_id: int, tg_user: dict = Depends(require_webapp_user)):
    _require_durak_admin(int(tg_user["id"]))
    try:
        active_games.pop(lobby_id, None)
        db.finish_durak_lobby(lobby_id)
        db.delete_durak_game_state(lobby_id)
        return {"status": "ended"}
    except Exception:
        logger.exception("force-end error")
        raise HTTPException(500, "force-end failed")


@router.post("/admin/ban")
async def admin_ban_durak(target_user: int, reason: str = "", tg_user: dict = Depends(require_webapp_user)):
    _require_durak_admin(int(tg_user["id"]))
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
        # Подтверждение подключения + уведомляем остальных об онлайне
        await websocket.send_json({
            "type": "connected",
            "lobby_id": lobby_id,
            "user_id": user_id
        })
        await broadcast_presence(lobby_id)

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

    except Exception as e:
        logger.exception(f"[DURAK WS] error in lobby {lobby_id} user {user_id}: {e}")
    finally:
        durak_ws_manager.disconnect(lobby_id, user_id)
        try:
            await broadcast_presence(lobby_id)
        except Exception:
            pass