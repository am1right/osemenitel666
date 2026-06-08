from datetime import date
from typing import Dict, Any, Optional
from api.db.connection import get_connection, _cursor
from api.db.wallet import topup_wallet

BONUS_CHANNEL_STARS   = 10
BONUS_CHAT_STARS      = 10
BONUS_SHARE_STARS     = 5
DAILY_CHECKIN_STARS   = 2

BONUS_CHANNEL  = "sub_channel"
BONUS_CHAT     = "sub_chat"
BONUS_SHARE    = "share_game"


def get_user_bonus_status(user_id: int) -> Dict[str, bool]:
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("SELECT bonus_type FROM user_bonuses WHERE user_id = %s", (user_id,))
    rows = {r["bonus_type"] for r in cur.fetchall()}
    cur.close(); conn.close()
    return {
        "sub_channel": BONUS_CHANNEL in rows,
        "sub_chat":    BONUS_CHAT    in rows,
        "share_game":  BONUS_SHARE   in rows,
    }


def grant_bonus(user_id: int, first_name: str, bonus_type: str) -> Dict[str, Any]:
    """Начисляет одноразовый бонус. Возвращает {ok, already, stars}."""
    amounts = {BONUS_CHANNEL: BONUS_CHANNEL_STARS, BONUS_CHAT: BONUS_CHAT_STARS, BONUS_SHARE: BONUS_SHARE_STARS}
    stars = amounts.get(bonus_type, 0)
    conn = get_connection(); cur = _cursor(conn)
    try:
        cur.execute(
            "INSERT INTO user_bonuses (user_id, bonus_type) VALUES (%s, %s)",
            (user_id, bonus_type)
        )
        conn.commit()
    except Exception:
        conn.rollback(); cur.close(); conn.close()
        return {"ok": False, "already": True, "stars": 0}
    cur.close(); conn.close()
    if stars > 0:
        topup_wallet(user_id, first_name, stars, description=f"Бонус: {bonus_type}")
    return {"ok": True, "already": False, "stars": stars}


def daily_checkin(user_id: int, first_name: str) -> Dict[str, Any]:
    """Ежедневный вход. Возвращает {ok, already_today, stars, streak}."""
    today = date.today()
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("SELECT last_checkin, streak FROM daily_checkins WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if row:
        last = row["last_checkin"]
        if isinstance(last, str):
            from datetime import datetime
            last = datetime.strptime(last, "%Y-%m-%d").date()
        if last == today:
            cur.close(); conn.close()
            return {"ok": False, "already_today": True, "stars": 0, "streak": row["streak"]}
        from datetime import timedelta
        streak = row["streak"] + 1 if last == today - timedelta(days=1) else 1
        cur.execute(
            "UPDATE daily_checkins SET last_checkin = %s, streak = %s WHERE user_id = %s",
            (today, streak, user_id)
        )
    else:
        streak = 1
        cur.execute(
            "INSERT INTO daily_checkins (user_id, last_checkin, streak) VALUES (%s, %s, 1)",
            (user_id, today)
        )
    conn.commit(); cur.close(); conn.close()
    topup_wallet(user_id, first_name, DAILY_CHECKIN_STARS, description="Ежедневный вход")
    return {"ok": True, "already_today": False, "stars": DAILY_CHECKIN_STARS, "streak": streak}


def get_daily_checkin_status(user_id: int) -> Dict[str, Any]:
    today = date.today()
    conn = get_connection(); cur = _cursor(conn)
    cur.execute("SELECT last_checkin, streak FROM daily_checkins WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return {"checked_in_today": False, "streak": 0}
    last = row["last_checkin"]
    if isinstance(last, str):
        from datetime import datetime
        last = datetime.strptime(last, "%Y-%m-%d").date()
    return {"checked_in_today": last == today, "streak": row["streak"]}
