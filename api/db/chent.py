from typing import Dict, Any, List

from api.db.connection import get_connection, _cursor


def get_chent_wallet(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('SELECT * FROM chent_wallet WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            'INSERT INTO chent_wallet (user_id, balance) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING',
            (user_id,)
        )
        conn.commit()
        balance = total_earned = total_spent = 0
    else:
        balance      = row["balance"]
        total_earned = row["total_earned"] or 0
        total_spent  = row["total_spent"]  or 0
    cur.close()
    conn.close()
    return {"balance": balance, "total_earned": total_earned, "total_spent": total_spent}


def topup_chent(user_id: int, first_name: str, amount: int, description: str = "Начисление") -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        INSERT INTO chent_wallet (user_id, first_name, balance, total_earned, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            balance      = chent_wallet.balance + EXCLUDED.balance,
            total_earned = chent_wallet.total_earned + EXCLUDED.total_earned,
            first_name   = EXCLUDED.first_name,
            updated_at   = NOW()
    ''', (user_id, first_name, amount, amount))
    cur.execute('''
        INSERT INTO chent_transactions (user_id, type, amount, description)
        VALUES (%s, 'topup', %s, %s)
    ''', (user_id, amount, description))
    cur.execute('SELECT balance FROM chent_wallet WHERE user_id = %s', (user_id,))
    new_balance = cur.fetchone()["balance"]
    conn.commit()
    cur.close()
    conn.close()
    return {"balance": new_balance}


def spend_chent(user_id: int, amount: int, description: str = "Покупка") -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('SELECT balance FROM chent_wallet WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    current = row["balance"] if row else 0
    if current < amount:
        cur.close()
        conn.close()
        return {"ok": False, "balance": current, "short": amount - current}
    cur.execute('''
        UPDATE chent_wallet SET balance = balance - %s, total_spent = total_spent + %s,
            updated_at = NOW()
        WHERE user_id = %s
    ''', (amount, amount, user_id))
    cur.execute('''
        INSERT INTO chent_transactions (user_id, type, amount, description)
        VALUES (%s, 'spend', %s, %s)
    ''', (user_id, amount, description))
    cur.execute('SELECT balance FROM chent_wallet WHERE user_id = %s', (user_id,))
    new_balance = cur.fetchone()["balance"]
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "balance": new_balance}


def get_chent_transactions(user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT type, amount, description, created_at
        FROM chent_transactions
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s
    ''', (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]
