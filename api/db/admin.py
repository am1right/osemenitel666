import time
from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor, _delete_player


def upsert_tg_username(user_id: int, username: str) -> None:
    """Сохраняет @username игрока (для перехода к аккаунту из админки)."""
    if not username:
        return
    conn = get_connection(); cur = _cursor(conn)
    cur.execute(
        "INSERT INTO tg_users (user_id, username, updated_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, updated_at = NOW()",
        (user_id, username),
    )
    conn.commit(); cur.close(); conn.close()


# ── Чаты для автопоста соревнований ────────────────────────────────

def add_announce_chat(chat_id: int, title: str = "") -> Dict[str, Any]:
    conn = get_connection(); cur = _cursor(conn)
    cur.execute(
        "INSERT INTO announce_chats (chat_id, title) VALUES (%s, %s) "
        "ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title",
        (chat_id, title or ""),
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "chat_id": chat_id}


def remove_announce_chat(chat_id: int) -> Dict[str, Any]:
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("DELETE FROM announce_chats WHERE chat_id = %s", (chat_id,))
    n = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "removed": n}


def get_announce_chats() -> List[int]:
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("SELECT chat_id FROM announce_chats")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [r["chat_id"] for r in rows]


# ── Админские сбросы (персональные и массовые) ─────────────────────

def admin_reset_player_scores(user_id: int) -> Dict[str, Any]:
    """Обнуляет очки игрока во всех играх (score/last_score → 0)."""
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("UPDATE scores SET score = 0, last_score = 0, updated_at = NOW() WHERE user_id = %s", (user_id,))
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "affected": affected}


def admin_reset_all_scores() -> Dict[str, Any]:
    """Обнуляет очки во всех играх у всех игроков."""
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("UPDATE scores SET score = 0, last_score = 0, updated_at = NOW()")
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "affected": affected}


def admin_set_energy(user_id: int, amount: int = 8) -> Dict[str, Any]:
    """Устанавливает энергию игрока в абсолютное значение (по умолчанию 8)."""
    conn = get_connection(); cur = _cursor(conn)
    now_ms = int(time.time() * 1000)
    cur.execute(
        """
        INSERT INTO energy (user_id, amount, last_regen, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET amount = EXCLUDED.amount, updated_at = NOW()
        """,
        (user_id, amount, now_ms),
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "amount": amount}


def admin_set_all_energy(amount: int = 8) -> Dict[str, Any]:
    """Устанавливает энергию у всех игроков (по умолчанию 8)."""
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("UPDATE energy SET amount = %s, updated_at = NOW()", (amount,))
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "amount": amount, "affected": affected}


def admin_zero_wallet(user_id: int) -> Dict[str, Any]:
    """Обнуляет баланс Stars игрока."""
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("UPDATE wallet SET balance = 0, updated_at = NOW() WHERE user_id = %s", (user_id,))
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "affected": affected}


def admin_zero_all_wallets() -> Dict[str, Any]:
    """Обнуляет балансы Stars у всех игроков."""
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("UPDATE wallet SET balance = 0, updated_at = NOW()")
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "affected": affected}


def get_user_flags(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("SELECT blocked, ref_disabled FROM admin_bans WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {"blocked": 0, "ref_disabled": 0}
    return {"blocked": row["blocked"] or 0, "ref_disabled": row["ref_disabled"] or 0}


def admin_delete_player(user_id: int) -> None:
    conn = get_connection()
    cur  = _cursor(conn)
    _delete_player(cur, user_id)
    conn.commit()
    cur.close()
    conn.close()


def admin_get_all_players(limit: int = 100, offset: int = 0, search: str = "") -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)

    players_base = """
        WITH all_users AS (
            SELECT user_id FROM scores
            UNION
            SELECT user_id FROM wallet
            UNION
            SELECT user_id FROM energy
        ),
        player_agg AS (
            SELECT
                u.user_id,
                COALESCE(s.first_name, w.first_name, 'Игрок') AS first_name,
                COALESCE(s.total_games, 0)     AS total_games,
                COALESCE(w.balance, 0)         AS wallet_balance,
                COALESCE(w.total_topped_up, 0) AS total_topped_up,
                COALESCE(w.total_spent, 0)     AS total_spent,
                s.last_active,
                COALESCE(s.games_count, 0)     AS games_count,
                COALESCE(bl.blocked, 0)        AS blocked,
                COALESCE(bl.ref_disabled, 0)   AS ref_disabled,
                tu.username                    AS username
            FROM all_users u
            LEFT JOIN (
                SELECT user_id,
                       MAX(first_name)           AS first_name,
                       SUM(games_played)         AS total_games,
                       MAX(updated_at)           AS last_active,
                       COUNT(DISTINCT game_name) AS games_count
                FROM scores
                GROUP BY user_id
            ) s ON s.user_id = u.user_id
            LEFT JOIN wallet     w  ON w.user_id  = u.user_id
            LEFT JOIN admin_bans bl ON bl.user_id = u.user_id
            LEFT JOIN tg_users   tu ON tu.user_id = u.user_id
        )
        SELECT * FROM player_agg
    """

    if search:
        sv = f"%{search}%"
        cur.execute(
            f"SELECT COUNT(*) as total FROM ({players_base}) sub WHERE first_name ILIKE %s OR CAST(user_id AS TEXT) LIKE %s",
            (sv, sv),
        )
        total = cur.fetchone()["total"]
        cur.execute(
            f"{players_base} WHERE first_name ILIKE %s OR CAST(user_id AS TEXT) LIKE %s ORDER BY total_games DESC, user_id DESC LIMIT %s OFFSET %s",
            (sv, sv, limit, offset),
        )
    else:
        cur.execute(f"SELECT COUNT(*) as total FROM ({players_base}) sub")
        total = cur.fetchone()["total"]
        cur.execute(
            f"{players_base} ORDER BY total_games DESC, user_id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"total": total, "players": [dict(r) for r in rows]}


def admin_ensure_self(user_id: int, first_name: str) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("""
        INSERT INTO wallet (user_id, first_name, balance)
        VALUES (%s, %s, 0)
        ON CONFLICT (user_id) DO UPDATE SET
            first_name = COALESCE(EXCLUDED.first_name, wallet.first_name),
            updated_at = NOW()
    """, (user_id, first_name or "Игрок"))
    conn.commit()
    cur.close()
    conn.close()
    return {"user_id": user_id}


def admin_get_player(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur  = _cursor(conn)

    cur.execute("""
        SELECT user_id, first_name, game_name, score, last_score, games_played, updated_at
        FROM scores WHERE user_id = %s ORDER BY games_played DESC
    """, (user_id,))
    score_rows = cur.fetchall()

    cur.execute("SELECT * FROM wallet WHERE user_id = %s", (user_id,))
    wallet_row = cur.fetchone()

    cur.execute("SELECT user_id FROM energy WHERE user_id = %s", (user_id,))
    has_energy = cur.fetchone() is not None

    if not score_rows and not wallet_row and not has_energy:
        cur.close()
        conn.close()
        return None

    if score_rows:
        first_name  = score_rows[0]["first_name"]
        total_games = sum(r["games_played"] or 0 for r in score_rows)
    else:
        first_name  = (wallet_row["first_name"] if wallet_row else None) or "Игрок"
        total_games = 0

    cur.execute("""
        SELECT COUNT(*) as invited, SUM(reward_sent) as rewarded
        FROM referrals WHERE inviter_id = %s
    """, (user_id,))
    ref_as_inviter = cur.fetchone()

    cur.execute("SELECT inviter_id FROM referrals WHERE invitee_id = %s", (user_id,))
    ref_as_invitee = cur.fetchone()

    cur.execute("""
        SELECT type, amount, description, created_at
        FROM wallet_transactions WHERE user_id = %s
        ORDER BY created_at DESC LIMIT 20
    """, (user_id,))
    transactions = cur.fetchall()

    cur.execute("""
        SELECT cr.place, cr.score, cr.prize_sent, cr.sent_at,
               c.game_name, c.prize_type, c.prize_value, c.started_at
        FROM contest_results cr
        JOIN contests c ON c.id = cr.contest_id
        WHERE cr.user_id = %s
        ORDER BY c.started_at DESC LIMIT 10
    """, (user_id,))
    contest_results = cur.fetchall()

    cur.execute("SELECT blocked, ref_disabled FROM admin_bans WHERE user_id = %s", (user_id,))
    ban_row = cur.fetchone()

    cur.close()
    conn.close()

    return {
        "user_id":         user_id,
        "first_name":      first_name,
        "total_games":     total_games,
        "scores":          [dict(r) for r in score_rows],
        "wallet":          dict(wallet_row) if wallet_row else {"balance": 0, "total_topped_up": 0, "total_spent": 0},
        "referrals_given": {
            "invited":  ref_as_inviter["invited"]  or 0,
            "rewarded": ref_as_inviter["rewarded"] or 0,
        },
        "referred_by":     ref_as_invitee["inviter_id"] if ref_as_invitee else None,
        "transactions":    [dict(r) for r in transactions],
        "contest_results": [dict(r) for r in contest_results],
        "blocked":         ban_row["blocked"]      if ban_row else 0,
        "ref_disabled":    ban_row["ref_disabled"] if ban_row else 0,
    }


def admin_adjust_wallet(user_id: int, amount: int, description: str) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "INSERT INTO wallet (user_id, balance, total_topped_up, total_spent) VALUES (%s, 0, 0, 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    conn.commit()
    if amount >= 0:
        cur.execute("""
            UPDATE wallet SET
                balance         = balance + %s,
                total_topped_up = total_topped_up + %s,
                updated_at      = NOW()
            WHERE user_id = %s
        """, (amount, amount, user_id))
    else:
        deduct = abs(amount)
        cur.execute("""
            UPDATE wallet SET
                balance     = GREATEST(0, balance - %s),
                total_spent = total_spent + %s,
                updated_at  = NOW()
            WHERE user_id = %s
        """, (deduct, deduct, user_id))
    tx_type = "admin_topup" if amount > 0 else "admin_deduct"
    cur.execute("""
        INSERT INTO wallet_transactions (user_id, type, amount, description)
        VALUES (%s, %s, %s, %s)
    """, (user_id, tx_type, abs(amount), description))
    cur.execute("SELECT balance FROM wallet WHERE user_id = %s", (user_id,))
    new_balance = cur.fetchone()["balance"]
    conn.commit()
    cur.close()
    conn.close()
    return {"balance": new_balance}


def admin_adjust_score(user_id: int, game_name: str, delta: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("SELECT score FROM scores WHERE user_id = %s AND game_name = %s", (user_id, game_name))
    row = cur.fetchone()
    if row:
        new_score = max(0, row["score"] + delta)
        cur.execute(
            "UPDATE scores SET score = %s, updated_at = NOW() WHERE user_id = %s AND game_name = %s",
            (new_score, user_id, game_name),
        )
    else:
        new_score = max(0, delta)
        cur.execute(
            "INSERT INTO scores (user_id, first_name, game_name, score, last_score, games_played) VALUES (%s, 'Игрок', %s, %s, 0, 0)",
            (user_id, game_name, new_score),
        )
    conn.commit()
    cur.close()
    conn.close()
    return {"game_name": game_name, "new_score": new_score}


def admin_set_blocked(user_id: int, blocked: bool, reason: str = "") -> None:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("""
        INSERT INTO admin_bans (user_id, blocked, reason, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            blocked    = EXCLUDED.blocked,
            reason     = EXCLUDED.reason,
            updated_at = NOW()
    """, (user_id, 1 if blocked else 0, reason))
    conn.commit()
    cur.close()
    conn.close()


def admin_set_ref_disabled(user_id: int, disabled: bool) -> None:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("""
        INSERT INTO admin_bans (user_id, ref_disabled, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            ref_disabled = EXCLUDED.ref_disabled,
            updated_at   = NOW()
    """, (user_id, 1 if disabled else 0))
    conn.commit()
    cur.close()
    conn.close()


def admin_get_summary_stats() -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)

    cur.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM scores")
    total_players = cur.fetchone()["cnt"]

    cur.execute("SELECT COALESCE(SUM(games_played), 0) as cnt FROM scores")
    total_games = cur.fetchone()["cnt"]

    cur.execute("SELECT COALESCE(SUM(balance), 0) as s FROM wallet")
    total_wallet = cur.fetchone()["s"]

    cur.execute("SELECT COUNT(*) as cnt FROM referrals WHERE reward_sent = 1")
    total_referrals = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM scores WHERE updated_at >= NOW() - INTERVAL '1 day'")
    active_today = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM scores WHERE updated_at >= NOW() - INTERVAL '7 days'")
    active_week = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) as cnt FROM admin_bans WHERE blocked = 1")
    total_blocked = cur.fetchone()["cnt"]

    cur.close()
    conn.close()
    return {
        "total_players":        total_players,
        "total_games":          total_games,
        "total_wallet_stars":   total_wallet,
        "total_referrals_paid": total_referrals,
        "active_today":         active_today,
        "active_week":          active_week,
        "total_blocked":        total_blocked,
    }
