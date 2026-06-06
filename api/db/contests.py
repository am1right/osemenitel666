import json
from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor
from api.db.scores import get_leaderboard


def create_contest(
    game_name: str,
    prize_type: str,
    prize_value: int,
    gift_id: Optional[str],
    split_prize: bool,
    winners_count: int,
    started_by: int,
    ends_at: str,
) -> int:
    conn = get_connection()
    cur = _cursor(conn)
    snapshot = json.dumps(get_leaderboard(game_name, 50))
    cur.execute('''
        INSERT INTO contests
            (game_name, prize_type, prize_value, gift_id, split_prize,
             winners_count, started_by, ends_at, status, snapshot_start)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
        RETURNING id
    ''', (game_name, prize_type, prize_value, gift_id,
          1 if split_prize else 0, winners_count, started_by, ends_at, snapshot))
    contest_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return contest_id


def get_active_contests() -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("SELECT * FROM contests WHERE status = 'active' ORDER BY ends_at ASC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def get_unannounced_finished_contests(min_age_sec: int = 300, max_age_sec: int = 21600) -> List[Dict[str, Any]]:
    """Недавно завершённые соревнования без отправленного итога:
    конец между (min_age_sec) и (max_age_sec) назад — чтобы не вываливать всю историю."""
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute(
        "SELECT * FROM contests WHERE status = 'finished' AND COALESCE(result_announced,0) = 0 "
        "AND ends_at <= NOW() - (%s * INTERVAL '1 second') "
        "AND ends_at >= NOW() - (%s * INTERVAL '1 second')",
        (min_age_sec, max_age_sec),
    )
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]


def mark_contest_announced(contest_id: int) -> None:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("UPDATE contests SET result_announced = 1 WHERE id = %s", (contest_id,))
    conn.commit(); cur.close(); conn.close()


def get_contest(contest_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('SELECT * FROM contests WHERE id = %s', (contest_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def finish_contest(contest_id: int, results: list) -> None:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("UPDATE contests SET status='finished' WHERE id=%s", (contest_id,))
    for r in results:
        cur.execute('''
            INSERT INTO contest_results (contest_id, user_id, first_name, place, score)
            VALUES (%s, %s, %s, %s, %s)
        ''', (contest_id, r["user_id"], r["first_name"], r["place"], r["score"]))
    conn.commit()
    cur.close()
    conn.close()


def mark_prize_sent(contest_id: int, user_id: int) -> None:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        UPDATE contest_results SET prize_sent=1, sent_at=NOW()
        WHERE contest_id=%s AND user_id=%s
    ''', (contest_id, user_id))
    conn.commit()
    cur.close()
    conn.close()


def cancel_contest(contest_id: int) -> None:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("UPDATE contests SET status='cancelled' WHERE id=%s", (contest_id,))
    conn.commit()
    cur.close()
    conn.close()
