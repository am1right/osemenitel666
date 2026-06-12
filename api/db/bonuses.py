from datetime import date
from typing import Dict, Any, Optional
from api.db.connection import get_connection, _cursor
from api.db.chent import topup_chent

# choin -> chent: курс x10 (chent — гриндовая валюта, choin — донат/выводимая)
CHENT_PER_CHOIN = 10

BONUS_CHANNEL_CHENT   = 10 * CHENT_PER_CHOIN
BONUS_CHAT_CHENT      = 10 * CHENT_PER_CHOIN
BONUS_SHARE_CHENT     = 5  * CHENT_PER_CHOIN
DAILY_CHECKIN_CHENT   = 2  * CHENT_PER_CHOIN

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
    """Начисляет одноразовый бонус (в chent). Возвращает {ok, already, chent}."""
    amounts = {BONUS_CHANNEL: BONUS_CHANNEL_CHENT, BONUS_CHAT: BONUS_CHAT_CHENT, BONUS_SHARE: BONUS_SHARE_CHENT}
    chent = amounts.get(bonus_type, 0)
    conn = get_connection(); cur = _cursor(conn)
    try:
        cur.execute(
            "INSERT INTO user_bonuses (user_id, bonus_type) VALUES (%s, %s)",
            (user_id, bonus_type)
        )
        conn.commit()
    except Exception:
        conn.rollback(); cur.close(); conn.close()
        return {"ok": False, "already": True, "chent": 0}
    cur.close(); conn.close()
    if chent > 0:
        topup_chent(user_id, first_name, chent, description=f"Бонус: {bonus_type}")
    return {"ok": True, "already": False, "chent": chent}


def daily_checkin(user_id: int, first_name: str) -> Dict[str, Any]:
    """Ежедневный вход. Возвращает {ok, already_today, chent, streak}."""
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
            return {"ok": False, "already_today": True, "chent": 0, "streak": row["streak"]}
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
    topup_chent(user_id, first_name, DAILY_CHECKIN_CHENT, description="Ежедневный вход")
    return {"ok": True, "already_today": False, "chent": DAILY_CHECKIN_CHENT, "streak": streak}


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
