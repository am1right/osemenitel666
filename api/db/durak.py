import json
import logging
from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor
from api.db.wallet import spend_wallet, topup_wallet

logger = logging.getLogger(__name__)


def create_durak_lobby(
    creator_id: int,
    creator_name: str,
    max_players: int,
    deck_size: int,
    game_type: str,
    cheating_enabled: bool,
    bet_amount: int = 0,
    name: str = None,
    photo_url: str = None,
) -> int:
    """Создаёт новое лобби Дурака и добавляет создателя как первого игрока."""
    conn = get_connection()
    cur = _cursor(conn)

    if bet_amount > 0:
        wallet = spend_wallet(creator_id, bet_amount, "Ставка за создание лобби Дурак")
        if not wallet.get("ok"):
            cur.close()
            conn.close()
            raise Exception("Недостаточно Stars для создания лобби со ставкой")

    final_name = name
    if not final_name or not final_name.strip():
        final_name = f"Лобби {creator_name or 'Игрок'}"

    cur.execute('''
        INSERT INTO durak_lobbies
            (name, creator_id, creator_name, max_players, deck_size, game_type, cheating_enabled, bet_amount)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (final_name, creator_id, creator_name, max_players, deck_size, game_type, cheating_enabled, bet_amount))

    lobby_id = cur.fetchone()["id"]

    cur.execute('''
        INSERT INTO durak_lobby_players (lobby_id, user_id, first_name, is_ready, photo_url)
        VALUES (%s, %s, %s, TRUE, %s)
    ''', (lobby_id, creator_id, creator_name, photo_url))

    conn.commit()
    cur.close()
    conn.close()
    return lobby_id


def get_active_durak_lobbies(limit: int = 50) -> List[Dict[str, Any]]:
    """Возвращает список активных (waiting) лобби."""
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        SELECT
            l.*,
            COUNT(p.id) as current_players
        FROM durak_lobbies l
        LEFT JOIN durak_lobby_players p ON p.lobby_id = l.id
        WHERE l.status = 'waiting'
        GROUP BY l.id
        ORDER BY l.created_at DESC
        LIMIT %s
    ''', (limit,))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = []
    for r in rows:
        result.append({
            "id":               r["id"],
            "name":             r.get("name"),
            "creator_id":       r["creator_id"],
            "creator_name":     r["creator_name"],
            "max_players":      r["max_players"],
            "current_players":  r["current_players"],
            "deck_size":        r["deck_size"],
            "game_type":        r["game_type"],
            "cheating_enabled": r["cheating_enabled"],
            "bet_amount":       r["bet_amount"],
            "pot":              r.get("pot", 0) or 0,
            "created_at":       r["created_at"].isoformat() if r["created_at"] else None,
        })
    return result


def join_durak_lobby(lobby_id: int, user_id: int, first_name: str, photo_url: str = None) -> bool:
    """Присоединяет игрока к лобби. Возвращает True если успешно."""
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        SELECT 1 FROM durak_lobby_players pl
        JOIN durak_lobbies l ON l.id = pl.lobby_id
        WHERE pl.user_id = %s AND l.status = 'waiting'
        LIMIT 1
    ''', (user_id,))
    if cur.fetchone() is not None:
        cur.close()
        conn.close()
        return False

    cur.execute('''
        SELECT l.max_players, l.status, l.bet_amount, COUNT(p.id) as current
        FROM durak_lobbies l
        LEFT JOIN durak_lobby_players p ON p.lobby_id = l.id
        WHERE l.id = %s
        GROUP BY l.id, l.max_players, l.status, l.bet_amount
    ''', (lobby_id,))

    row = cur.fetchone()
    if not row or row["status"] != "waiting":
        cur.close()
        conn.close()
        return False

    if row["current"] >= row["max_players"]:
        cur.close()
        conn.close()
        return False

    bet = row["bet_amount"] or 0

    if bet > 0:
        wallet_result = spend_wallet(user_id, bet, f"Ставка в лобби Дурак #{lobby_id}")
        if not wallet_result.get("ok"):
            cur.close()
            conn.close()
            return False

    try:
        cur.execute('''
            INSERT INTO durak_lobby_players (lobby_id, user_id, first_name, photo_url)
            VALUES (%s, %s, %s, %s)
        ''', (lobby_id, user_id, first_name, photo_url))
        conn.commit()
        success = True
    except Exception:
        if bet > 0:
            topup_wallet(user_id, "", bet, f"Возврат ставки (ошибка входа в лобби #{lobby_id})")
        success = False

    cur.close()
    conn.close()
    return success


def get_lobby_players(lobby_id: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT user_id, first_name, is_ready, joined_at, photo_url
        FROM durak_lobby_players
        WHERE lobby_id = %s
        ORDER BY joined_at
    ''', (lobby_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def save_durak_game_state(lobby_id: int, state_json: str) -> None:
    """Сохраняет снимок состояния игры (для восстановления после рестарта)."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        INSERT INTO durak_game_state (lobby_id, state_json, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (lobby_id) DO UPDATE SET
            state_json = EXCLUDED.state_json, updated_at = NOW()
    ''', (lobby_id, state_json))
    conn.commit()
    cur.close()
    conn.close()


def load_durak_game_state(lobby_id: int) -> Optional[str]:
    """Возвращает сохранённый снимок состояния игры (или None)."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("SELECT state_json FROM durak_game_state WHERE lobby_id = %s", (lobby_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["state_json"] if row else None


def delete_durak_game_state(lobby_id: int) -> None:
    """Удаляет снимок состояния (игра завершена)."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("DELETE FROM durak_game_state WHERE lobby_id = %s", (lobby_id,))
    conn.commit()
    cur.close()
    conn.close()


def finish_durak_lobby(lobby_id: int) -> None:
    """Помечает лобби завершённым (status='finished')."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute(
        "UPDATE durak_lobbies SET status = 'finished', updated_at = NOW() WHERE id = %s",
        (lobby_id,),
    )
    conn.commit()
    cur.close()
    conn.close()


def leave_durak_lobby(lobby_id: int, user_id: int) -> bool:
    """Выходит из лобби. Если уходит создатель — передаёт владение или закрывает лобби.
    При наличии ставки деньги переходят в pot (не возвращаются).
    """
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('SELECT creator_id, bet_amount FROM durak_lobbies WHERE id = %s', (lobby_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False

    is_creator = row["creator_id"] == user_id
    bet = row["bet_amount"] or 0

    if bet > 0:
        cur.execute('UPDATE durak_lobbies SET pot = pot + %s WHERE id = %s', (bet, lobby_id))

    cur.execute('DELETE FROM durak_lobby_players WHERE lobby_id = %s AND user_id = %s', (lobby_id, user_id))

    if is_creator:
        cur.execute('''
            SELECT user_id FROM durak_lobby_players
            WHERE lobby_id = %s
            ORDER BY joined_at
            LIMIT 1
        ''', (lobby_id,))
        next_owner = cur.fetchone()

        if next_owner:
            cur.execute('''
                UPDATE durak_lobbies
                SET creator_id = %s, creator_name = (
                    SELECT first_name FROM durak_lobby_players WHERE lobby_id = %s AND user_id = %s
                )
                WHERE id = %s
            ''', (next_owner["user_id"], lobby_id, next_owner["user_id"], lobby_id))
        else:
            cur.execute("UPDATE durak_lobbies SET status = 'cancelled' WHERE id = %s", (lobby_id,))

    cur.execute('SELECT COUNT(*) as cnt FROM durak_lobby_players WHERE lobby_id = %s', (lobby_id,))
    remaining = cur.fetchone()["cnt"]
    if remaining == 0:
        cur.execute('DELETE FROM durak_lobbies WHERE id = %s', (lobby_id,))

    conn.commit()
    cur.close()
    conn.close()
    return True


def update_lobby_settings(lobby_id: int, creator_id: int, **kwargs) -> bool:
    """Обновляет настройки лобби (только создатель, только пока один игрок и никто не готов)."""
    allowed = {"max_players", "deck_size", "game_type", "cheating_enabled", "bet_amount"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}

    if not updates:
        return False

    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('SELECT creator_id FROM durak_lobbies WHERE id = %s', (lobby_id,))
    row = cur.fetchone()
    if not row or row["creator_id"] != creator_id:
        cur.close()
        conn.close()
        return False

    cur.execute('''
        SELECT COUNT(*) as total_players,
               SUM(CASE WHEN is_ready = TRUE THEN 1 ELSE 0 END) as ready_players
        FROM durak_lobby_players
        WHERE lobby_id = %s
    ''', (lobby_id,))

    stats = cur.fetchone()
    total = stats["total_players"] or 0
    ready = stats["ready_players"] or 0

    if ready > 0 or total > 1:
        cur.close()
        conn.close()
        return False

    set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
    values = list(updates.values()) + [lobby_id]

    cur.execute(f'''
        UPDATE durak_lobbies
        SET {set_clause}, updated_at = NOW()
        WHERE id = %s
    ''', values)

    conn.commit()
    cur.close()
    conn.close()
    return True


def set_player_ready(lobby_id: int, user_id: int, is_ready: bool) -> bool:
    """Устанавливает статус готовности игрока."""
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        UPDATE durak_lobby_players
        SET is_ready = %s
        WHERE lobby_id = %s AND user_id = %s
    ''', (is_ready, lobby_id, user_id))

    success = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return success


def is_user_in_active_lobby(user_id: int) -> Optional[int]:
    """Возвращает ID активного лобби пользователя или None."""
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        SELECT pl.lobby_id
        FROM durak_lobby_players pl
        JOIN durak_lobbies l ON l.id = pl.lobby_id
        WHERE pl.user_id = %s AND l.status = 'waiting'
        LIMIT 1
    ''', (user_id,))

    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["lobby_id"] if row else None


def start_durak_game(lobby_id: int, creator_id: int) -> bool:
    """Переводит лобби в статус 'playing'. Только создатель и только из waiting."""
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        SELECT creator_id, status
        FROM durak_lobbies
        WHERE id = %s
    ''', (lobby_id,))
    row = cur.fetchone()
    if not row or row["creator_id"] != creator_id or row["status"] != "waiting":
        cur.close()
        conn.close()
        return False

    cur.execute('''
        UPDATE durak_lobbies
        SET status = 'playing', updated_at = NOW(), started_at = NOW()
        WHERE id = %s
    ''', (lobby_id,))

    conn.commit()
    cur.close()
    conn.close()
    return True


def get_durak_lobby_by_id(lobby_id: int) -> Optional[Dict[str, Any]]:
    """Получает лобби по id (включая playing)."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT l.*,
               COUNT(p.id) as current_players
        FROM durak_lobbies l
        LEFT JOIN durak_lobby_players p ON p.lobby_id = l.id
        WHERE l.id = %s
        GROUP BY l.id
    ''', (lobby_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return None

    return {
        "id":               row["id"],
        "name":             row.get("name"),
        "creator_id":       row["creator_id"],
        "creator_name":     row["creator_name"],
        "max_players":      row["max_players"],
        "current_players":  row["current_players"],
        "deck_size":        row["deck_size"],
        "game_type":        row["game_type"],
        "cheating_enabled": row["cheating_enabled"],
        "bet_amount":       row["bet_amount"] or 0,
        "pot":              row.get("pot", 0) or 0,
        "status":           row["status"],
        "created_at":       row["created_at"].isoformat() if row["created_at"] else None,
        "started_at":       row.get("started_at").isoformat() if row.get("started_at") else None,
    }


def save_durak_game_history(
    lobby_id: int,
    winner_id: Optional[int],
    pot: int,
    players: list,
    started_at: Optional[str] = None,
) -> bool:
    """Сохраняет результат завершённой игры Дурака."""
    conn = get_connection()
    cur = _cursor(conn)
    try:
        if not started_at:
            cur.execute("SELECT started_at FROM durak_lobbies WHERE id = %s", (lobby_id,))
            row = cur.fetchone()
            started_at = row["started_at"] if row and row["started_at"] else None

        players_json = json.dumps(players) if players else "[]"

        cur.execute('''
            INSERT INTO durak_game_history (lobby_id, winner_id, pot, players, started_at, ended_at, duration_sec)
            VALUES (%s, %s, %s, %s, %s, NOW(),
                    CASE WHEN %s IS NOT NULL THEN EXTRACT(EPOCH FROM (NOW() - %s))::int ELSE NULL END
            )
        ''', (lobby_id, winner_id, pot, players_json, started_at, started_at, started_at))

        conn.commit()
        return True
    except Exception:
        logger.exception(f"Failed to save durak history for lobby {lobby_id}")
        return False
    finally:
        cur.close()
        conn.close()


def get_durak_history(user_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """История игр Дурака, опционально для конкретного игрока."""
    conn = get_connection()
    cur = _cursor(conn)
    if user_id:
        cur.execute('''
            SELECT h.*, l.name as lobby_name
            FROM durak_game_history h
            LEFT JOIN durak_lobbies l ON l.id = h.lobby_id
            WHERE h.players @> %s::jsonb
            ORDER BY h.ended_at DESC
            LIMIT %s
        ''', (json.dumps([{"user_id": user_id}]), limit))
    else:
        cur.execute('''
            SELECT h.*, l.name as lobby_name
            FROM durak_game_history h
            LEFT JOIN durak_lobbies l ON l.id = h.lobby_id
            ORDER BY h.ended_at DESC
            LIMIT %s
        ''', (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    history = []
    for row in rows:
        try:
            players = json.loads(row["players"]) if row["players"] else []
        except Exception:
            players = []
        history.append({
            "id":           row["id"],
            "lobby_id":     row["lobby_id"],
            "lobby_name":   row.get("lobby_name"),
            "winner_id":    row["winner_id"],
            "pot":          row["pot"] or 0,
            "players":      players,
            "started_at":   row["started_at"].isoformat() if row["started_at"] else None,
            "ended_at":     row["ended_at"].isoformat() if row["ended_at"] else None,
            "duration_sec": row["duration_sec"],
        })
    return history


def get_durak_user_stats(user_id: int) -> Dict[str, Any]:
    """Личная статистика игрока по Дураку: партий / побед / win-rate."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT COUNT(*) AS games,
               COUNT(*) FILTER (WHERE winner_id = %s) AS wins
        FROM durak_game_history
        WHERE players @> %s::jsonb
    ''', (user_id, json.dumps([{"user_id": user_id}])))
    row = cur.fetchone()
    cur.close()
    conn.close()
    games = (row["games"] if row else 0) or 0
    wins = (row["wins"] if row else 0) or 0
    return {"games": games, "wins": wins, "win_rate": round(wins / games * 100) if games else 0}


def get_durak_ratings(limit: int = 20) -> List[Dict[str, Any]]:
    """Рейтинг: сыграно / побед / win-rate по каждому участнику (из истории)."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        WITH participants AS (
            SELECT (p->>'user_id')::bigint AS user_id,
                   p->>'first_name'        AS first_name,
                   h.winner_id,
                   h.ended_at
            FROM durak_game_history h
            CROSS JOIN LATERAL jsonb_array_elements(h.players) AS p
        )
        SELECT user_id,
               (array_agg(first_name ORDER BY ended_at DESC))[1] AS first_name,
               COUNT(*) AS games,
               COUNT(*) FILTER (WHERE winner_id = user_id) AS wins
        FROM participants
        WHERE user_id IS NOT NULL
        GROUP BY user_id
        ORDER BY wins DESC, games DESC
        LIMIT %s
    ''', (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = []
    for r in rows:
        games = r["games"] or 0
        wins = r["wins"] or 0
        result.append({
            "user_id": r["user_id"],
            "first_name": r["first_name"],
            "games": games,
            "wins": wins,
            "win_rate": round(wins / games * 100) if games else 0,
        })
    return result


def ban_durak_user(user_id: int, reason: str = "") -> bool:
    conn = get_connection()
    cur = _cursor(conn)
    try:
        cur.execute('''
            INSERT INTO durak_bans (user_id, reason) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET banned_at=NOW(), reason=%s
        ''', (user_id, reason, reason))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        cur.close()
        conn.close()


def is_durak_banned(user_id: int) -> bool:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('SELECT 1 FROM durak_bans WHERE user_id = %s', (user_id,))
    res = cur.fetchone() is not None
    cur.close()
    conn.close()
    return res
