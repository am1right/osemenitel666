from typing import Dict, Any, List, Optional

from api.db.connection import get_connection, _cursor
from api.db.energy import ENERGY_MAX

REFERRAL_STARS        = 2
REFERRAL_ENERGY       = 10   # +10% заряда батареи
REFERRAL_GAMES_NEEDED = 3
FRAUD_DAILY_LIMIT     = 8
FRAUD_INACTIVE_RATIO  = 0.70


def register_referral(inviter_id: int, invitee_id: int, invitee_name: str) -> bool:
    conn = get_connection()
    cur  = _cursor(conn)
    try:
        cur.execute('''
            INSERT INTO referrals (inviter_id, invitee_id, first_name, policy_accepted)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (invitee_id) DO NOTHING
        ''', (inviter_id, invitee_id, invitee_name))
        inserted = cur.rowcount == 1
        conn.commit()
        return inserted
    finally:
        cur.close()
        conn.close()


def accept_referral_policy(invitee_id: int) -> bool:
    conn = get_connection()
    cur  = _cursor(conn)
    try:
        cur.execute('''
            UPDATE referrals
            SET policy_accepted = 1, accepted_at = NOW()
            WHERE invitee_id = %s AND policy_accepted = 0
        ''', (invitee_id,))
        updated = cur.rowcount == 1
        conn.commit()
        return updated
    finally:
        cur.close()
        conn.close()


def get_referral_by_invitee(invitee_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('SELECT * FROM referrals WHERE invitee_id = %s', (invitee_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def get_invitee_total_games(invitee_id: int) -> int:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('''
        SELECT COALESCE(SUM(games_played), 0) as total
        FROM scores WHERE user_id = %s
    ''', (invitee_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["total"] if row else 0


def try_grant_referral_reward(invitee_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur  = _cursor(conn)
    try:
        cur.execute('''
            SELECT inviter_id, invitee_id FROM referrals
            WHERE invitee_id = %s AND reward_sent = 0 AND policy_accepted = 1
        ''', (invitee_id,))
        ref_row = cur.fetchone()
        if not ref_row:
            return None

        inviter_id = ref_row["inviter_id"]

        cur.execute('''
            SELECT COALESCE(SUM(games_played), 0) as total
            FROM scores WHERE user_id = %s
        ''', (invitee_id,))
        total_games = cur.fetchone()["total"]
        if total_games < REFERRAL_GAMES_NEEDED:
            return None

        cur.execute('''
            INSERT INTO wallet (user_id, balance, total_topped_up, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                balance         = wallet.balance + EXCLUDED.balance,
                total_topped_up = wallet.total_topped_up + EXCLUDED.total_topped_up,
                updated_at      = NOW()
        ''', (inviter_id, REFERRAL_STARS, REFERRAL_STARS))

        cur.execute('''
            INSERT INTO wallet_transactions (user_id, type, amount, description)
            VALUES (%s, 'topup', %s, %s)
        ''', (inviter_id, REFERRAL_STARS, f"Реферал: {invitee_id} отыграл {REFERRAL_GAMES_NEEDED}+ игр"))

        cur.execute('''
            INSERT INTO energy (user_id, amount, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                amount     = LEAST(energy.amount + EXCLUDED.amount, %s),
                updated_at = NOW()
        ''', (inviter_id, REFERRAL_ENERGY, ENERGY_MAX))

        cur.execute("UPDATE referrals SET reward_sent = 1 WHERE invitee_id = %s", (invitee_id,))

        cur.execute('SELECT balance FROM wallet WHERE user_id = %s', (inviter_id,))
        new_balance = cur.fetchone()["balance"]
        cur.execute('SELECT amount FROM energy WHERE user_id = %s', (inviter_id,))
        energy_row = cur.fetchone()
        new_energy = energy_row["amount"] if energy_row else REFERRAL_ENERGY

        conn.commit()
        return {
            "stars":       REFERRAL_STARS,
            "energy":      REFERRAL_ENERGY,
            "new_balance": new_balance,
            "new_energy":  new_energy,
            "inviter_id":  inviter_id,
        }
    finally:
        cur.close()
        conn.close()


def claim_referral_reward(inviter_id: int, invitee_id: int) -> Optional[Dict[str, Any]]:
    return try_grant_referral_reward(invitee_id)


def get_referral_stats(inviter_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('''
        SELECT COUNT(*) as total,
               SUM(reward_sent) as rewarded,
               SUM(policy_accepted) as accepted
        FROM referrals WHERE inviter_id = %s
    ''', (inviter_id,))
    row = cur.fetchone()
    # В профиле показываем только тех, по кому бонус ещё НЕ получен.
    # Полученные остаются в базе (для статистики), но визуально не нужны.
    cur.execute('''
        SELECT invitee_id, first_name, reward_sent, policy_accepted, created_at
        FROM referrals WHERE inviter_id = %s AND reward_sent = 0
        ORDER BY created_at DESC LIMIT 20
    ''', (inviter_id,))
    recent_rows = cur.fetchall()
    cur.close()
    conn.close()

    recent = []
    for r in recent_rows:
        item = dict(r)
        item["games_played"] = get_invitee_total_games(r["invitee_id"])
        recent.append(item)

    return {
        "total":        row["total"]    or 0,
        "rewarded":     row["rewarded"] or 0,
        "accepted":     row["accepted"] or 0,
        "recent":       recent,
        "games_needed": REFERRAL_GAMES_NEEDED,
    }


def admin_reset_referrals(inviter_id: int) -> Dict[str, Any]:
    """Админ: удаляет всех рефералов игрока (как пригласившего)."""
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("DELETE FROM referrals WHERE inviter_id = %s", (inviter_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "deleted": deleted}


def is_already_referred(invitee_id: int) -> bool:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('SELECT 1 FROM referrals WHERE invitee_id = %s', (invitee_id,))
    found = cur.fetchone() is not None
    cur.close()
    conn.close()
    return found


# ── Fraud detection ────────────────────────────────────────────────

def check_fraud_daily_flood(inviter_id: int) -> Optional[int]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('''
        SELECT COUNT(*) as cnt FROM referrals
        WHERE inviter_id = %s AND created_at >= NOW() - INTERVAL '1 day'
    ''', (inviter_id,))
    cnt = cur.fetchone()["cnt"]
    cur.close()
    conn.close()
    return cnt if cnt > FRAUD_DAILY_LIMIT else None


def check_fraud_inactive_ratio(inviter_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('''
        SELECT invitee_id FROM referrals
        WHERE inviter_id = %s AND policy_accepted = 1
    ''', (inviter_id,))
    invitees = [r["invitee_id"] for r in cur.fetchall()]
    cur.close()
    conn.close()

    if len(invitees) < 5:
        return None

    inactive = 0
    for inv_id in invitees:
        games = get_invitee_total_games(inv_id)
        if games < REFERRAL_GAMES_NEEDED:
            inactive += 1

    ratio = inactive / len(invitees)
    if ratio > FRAUD_INACTIVE_RATIO:
        return {"total": len(invitees), "inactive": inactive, "ratio": ratio}
    return None
