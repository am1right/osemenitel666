from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor


def save_score(user_id: int, first_name: str, game_name: str, score: int) -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        SELECT score, games_played FROM scores
        WHERE user_id = %s AND game_name = %s
    ''', (user_id, game_name))
    row = cur.fetchone()

    current_best = row["score"] if row else 0
    games_played = (row["games_played"] or 0) if row else 0
    new_record   = score > current_best
    best_score   = max(score, current_best)

    if row:
        cur.execute('''
            UPDATE scores
            SET score = %s, last_score = %s, games_played = %s,
                first_name = %s, updated_at = NOW()
            WHERE user_id = %s AND game_name = %s
        ''', (best_score, score, games_played + 1, first_name, user_id, game_name))
    else:
        cur.execute('''
            INSERT INTO scores (user_id, first_name, game_name, score, last_score, games_played)
            VALUES (%s, %s, %s, %s, %s, 1)
        ''', (user_id, first_name, game_name, score, score))

    conn.commit()
    cur.close()
    conn.close()
    return {"new_record": new_record, "best_score": best_score}


def get_user_stats(user_id: int, game_name: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT score, last_score, games_played, first_name
        FROM scores WHERE user_id = %s AND game_name = %s
    ''', (user_id, game_name))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        "score":        row["score"],
        "last_score":   row["last_score"] or 0,
        "games_played": row["games_played"] or 0,
        "first_name":   row["first_name"] or "Игрок",
    }


def get_leaderboard(game_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT s.user_id, s.first_name, s.score, tu.username
        FROM scores s
        LEFT JOIN tg_users tu ON tu.user_id = s.user_id
        WHERE s.game_name = %s ORDER BY s.score DESC LIMIT %s
    ''', (game_name, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"user_id": r["user_id"], "first_name": r["first_name"] or "Игрок",
         "score": r["score"], "username": r["username"] or ""}
        for r in rows
    ]
