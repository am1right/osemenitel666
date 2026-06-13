import time
from typing import Dict, Any

from api.db.connection import get_connection, _cursor

# ── Модель энергии (батарея 0..100%) ────────────────────────────────
# Энергия как заряд телефона: 100% → 0%. Списывается на входе + плавно
# тратится по ходу игры (тики с клиента). Полный заряд сессии ≈ 40–60 мин
# игры. Восстановление с нуля до 100% ≈ 3 часа (база), ускоряется апгрейдом
# скорости регена из магазина (regen_mult: 1.0, 2.0, 3.0 …).
ENERGY_MAX      = 100                 # проценты заряда
ENERGY_REGEN_MS = 10 * 60 * 1000      # база: 10 минут на 1% → 100% за ~16.6ч


def _effective_regen_ms(mult: float) -> int:
    return max(1000, int(ENERGY_REGEN_MS / max(0.1, float(mult or 1.0))))


def _apply_energy_regen(amount: int, last_regen: int, regen_ms: int) -> tuple:
    now = int(time.time() * 1000)
    if amount >= ENERGY_MAX:
        return amount, now
    elapsed = now - last_regen
    gained  = elapsed // regen_ms
    if gained > 0:
        amount     = min(ENERGY_MAX, amount + gained)
        last_regen = last_regen + gained * regen_ms
        if amount >= ENERGY_MAX:
            last_regen = now
    return amount, last_regen


def _ensure_energy_row(cur, user_id: int) -> tuple:
    """Возвращает (amount, last_regen, regen_mult, boost_until) с применённой регенерацией.
    regen_mult действует только пока now < boost_until (временный бустер на час)."""
    cur.execute("SELECT amount, last_regen, regen_mult, regen_boost_until FROM energy WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    if not row:
        now = int(time.time() * 1000)
        cur.execute(
            "INSERT INTO energy (user_id, amount, last_regen, regen_mult, regen_boost_until) VALUES (%s, %s, %s, 1.0, 0) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id, ENERGY_MAX, now),
        )
        return ENERGY_MAX, now, 1.0, 0
    amount, last_regen = int(row["amount"]), int(row["last_regen"])
    boost_until = int(row.get("regen_boost_until") or 0)
    now = int(time.time() * 1000)
    mult = float(row.get("regen_mult") or 1.0) if now < boost_until else 1.0
    new_amount, new_last = _apply_energy_regen(amount, last_regen, _effective_regen_ms(mult))
    if new_amount != amount or new_last != last_regen:
        cur.execute(
            "UPDATE energy SET amount = %s, last_regen = %s, updated_at = NOW() WHERE user_id = %s",
            (new_amount, new_last, user_id),
        )
    return new_amount, new_last, mult, boost_until


def get_energy(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen, mult, boost_until = _ensure_energy_row(cur, user_id)
    conn.commit()
    cur.close()
    conn.close()
    regen_ms = _effective_regen_ms(mult)
    next_recharge_in = None
    if amount < ENERGY_MAX:
        elapsed          = int(time.time() * 1000) - last_regen
        next_recharge_in = max(0, regen_ms - elapsed)
    now = int(time.time() * 1000)
    return {
        "amount":           amount,
        "max":              ENERGY_MAX,
        "regen_ms":         regen_ms,
        "regen_mult":       mult,
        "last_regen":       last_regen,
        "next_recharge_in": next_recharge_in,
        "boost_active":     now < boost_until,
        "boost_seconds_left": max(0, (boost_until - now) // 1000) if now < boost_until else 0,
    }


def spend_energy(user_id: int, cost: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen, mult, boost_until = _ensure_energy_row(cur, user_id)
    if amount < cost:
        cur.close()
        conn.close()
        return {"ok": False, "amount": amount, "last_regen": last_regen}
    amount -= cost
    if amount < ENERGY_MAX:
        # Любая трата сбрасывает таймер регена → пока идёт игра (частые списания
        # расхода) энергия не восстанавливается; реген стартует после остановки.
        last_regen = int(time.time() * 1000)
    cur.execute(
        "UPDATE energy SET amount = %s, last_regen = %s, updated_at = NOW() WHERE user_id = %s",
        (amount, last_regen, user_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "amount": amount, "last_regen": last_regen}


REGEN_BOOST_DURATION_MS = 60 * 60 * 1000  # 1 час


def boost_regen_speed(user_id: int, mult: float) -> Dict[str, Any]:
    """Включает временный бустер скорости восстановления на 1 час (покупка из магазина)."""
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen, _cur_mult, _boost_until = _ensure_energy_row(cur, user_id)
    boost_until = int(time.time() * 1000) + REGEN_BOOST_DURATION_MS
    cur.execute(
        "UPDATE energy SET regen_mult = %s, regen_boost_until = %s, updated_at = NOW() WHERE user_id = %s",
        (float(mult), boost_until, user_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "regen_mult": float(mult), "amount": amount, "boost_until": boost_until}


def admin_adjust_energy(user_id: int, delta: int) -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    amount, last_regen, mult, boost_until = _ensure_energy_row(cur, user_id)
    conn.commit()  # фиксируем INSERT если строки не было
    # Покупка/выдача складывается с текущим зарядом (можно >100%);
    # реген выше 100% не работает (см. _apply_energy_regen).
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
