import time
from typing import Dict, Any

from api.db.connection import get_connection, _cursor

ENERGY_MAX      = 8
ENERGY_REGEN_MS = 12 * 60 * 1000


def _apply_energy_regen(amount: int, last_regen: int) -> tuple:
    now = int(time.time() * 1000)
    if amount >= ENERGY_MAX:
        return amount, now
    elapsed = now - last_regen
    gained  = elapsed // ENERGY_REGEN_MS
    if gained > 0:
        amount     = min(ENERGY_MAX, amount + gained)
        last_regen = last_regen + gained * ENERGY_REGEN_MS
        if amount >= ENERGY_MAX:
            last_regen = now
    return amount, last_regen


def _ensure_energy_row(cur, user_id: int) -> tuple:
    cur.execute("SELECT amount, last_regen FROM energy WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if not row:
        now = int(time.time() * 1000)
        cur.execute(
            "INSERT INTO energy (user_id, amount, last_regen) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, ENERGY_MAX, now),
        )
        return ENERGY_MAX, now
    amount, last_regen = int(row["amount"]), int(row["last_regen"])
    new_amount, new_last = _apply_energy_regen(amount, last_regen)
    if new_amount != amount or new_last != last_regen:
        cur.execute(
            "UPDATE energy SET amount = %s, last_regen = %s, updated_at = NOW() WHERE user_id = %s",
            (new_amount, new_last, user_id),
        )
    return new_amount, new_last


def get_energy(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen = _ensure_energy_row(cur, user_id)
    conn.commit()
    cur.close()
    conn.close()
    next_recharge_in = None
    if amount < ENERGY_MAX:
        elapsed          = int(time.time() * 1000) - last_regen
        next_recharge_in = max(0, ENERGY_REGEN_MS - elapsed)
    return {
        "amount":           amount,
        "max":              ENERGY_MAX,
        "last_regen":       last_regen,
        "next_recharge_in": next_recharge_in,
        "overflow":         amount > ENERGY_MAX,
    }


def spend_energy(user_id: int, cost: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen = _ensure_energy_row(cur, user_id)
    if amount < cost:
        cur.close()
        conn.close()
        return {"ok": False, "amount": amount, "last_regen": last_regen}
    amount -= cost
    if amount < ENERGY_MAX:
        now = int(time.time() * 1000)
        if last_regen <= now - ENERGY_REGEN_MS:
            last_regen = now
    cur.execute(
        "UPDATE energy SET amount = %s, last_regen = %s, updated_at = NOW() WHERE user_id = %s",
        (amount, last_regen, user_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "amount": amount, "last_regen": last_regen}


def admin_adjust_energy(user_id: int, delta: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen = _ensure_energy_row(cur, user_id)
    conn.commit()  # фиксируем INSERT если строки не было
    new_amount = max(0, amount + delta)
    cur.execute(
        """
        INSERT INTO energy (user_id, amount, last_regen, updated_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET amount = EXCLUDED.amount, updated_at = NOW()
        """,
        (user_id, new_amount, last_regen),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"amount": new_amount, "last_regen": last_regen}
