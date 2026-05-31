import json
import os
import random
import time
from typing import List, Dict, Any, Optional, Set

import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TEST_PLAYER_IDS = (999001, 777888)
TEST_ID_RANGE = (888_000, 1_010_000)


def get_connection():
    db_url = DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set — добавьте PostgreSQL URL из Render Dashboard")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def _cursor(conn):
    """DictCursor — аналог sqlite3.Row: доступ по имени колонки."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_connection()
    cur = _cursor(conn)

    cur.execute('''
        CREATE TABLE IF NOT EXISTS scores (
            id           SERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            first_name   TEXT,
            game_name    TEXT NOT NULL,
            score        INTEGER NOT NULL,
            last_score   INTEGER DEFAULT 0,
            games_played INTEGER DEFAULT 1,
            created_at   TIMESTAMP DEFAULT NOW(),
            updated_at   TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, game_name)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_game_score ON scores(game_name, score DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_user_game  ON scores(user_id, game_name)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id              SERIAL PRIMARY KEY,
            inviter_id      BIGINT NOT NULL,
            invitee_id      BIGINT NOT NULL,
            first_name      TEXT,
            reward_sent     INTEGER DEFAULT 0,
            policy_accepted INTEGER DEFAULT 0,
            accepted_at     TIMESTAMP DEFAULT NULL,
            created_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(invitee_id)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_referrals_inviter ON referrals(inviter_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_referrals_created ON referrals(created_at)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS contests (
            id             SERIAL PRIMARY KEY,
            game_name      TEXT NOT NULL,
            prize_type     TEXT NOT NULL,
            prize_value    INTEGER DEFAULT 0,
            gift_id        TEXT DEFAULT NULL,
            split_prize    INTEGER DEFAULT 0,
            winners_count  INTEGER NOT NULL,
            started_by     BIGINT NOT NULL,
            started_at     TIMESTAMP DEFAULT NOW(),
            ends_at        TIMESTAMP NOT NULL,
            status         TEXT DEFAULT 'active',
            snapshot_start TEXT DEFAULT NULL
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS contest_results (
            id          SERIAL PRIMARY KEY,
            contest_id  INTEGER NOT NULL,
            user_id     BIGINT NOT NULL,
            first_name  TEXT,
            place       INTEGER NOT NULL,
            score       INTEGER NOT NULL,
            prize_sent  INTEGER DEFAULT 0,
            sent_at     TIMESTAMP DEFAULT NULL,
            FOREIGN KEY(contest_id) REFERENCES contests(id)
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS wallet (
            user_id         BIGINT PRIMARY KEY,
            first_name      TEXT,
            balance         INTEGER DEFAULT 0,
            total_topped_up INTEGER DEFAULT 0,
            total_spent     INTEGER DEFAULT 0,
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            type        TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            description TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS admin_bans (
            user_id      BIGINT PRIMARY KEY,
            blocked      INTEGER DEFAULT 0,
            ref_disabled INTEGER DEFAULT 0,
            reason       TEXT,
            updated_at   TIMESTAMP DEFAULT NOW()
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS energy (
            user_id    BIGINT PRIMARY KEY,
            amount     INTEGER NOT NULL DEFAULT 8,
            last_regen BIGINT  NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS case_settings (
            id                    INTEGER PRIMARY KEY CHECK (id = 1),
            nft_gifts             TEXT NOT NULL DEFAULT '[]',
            valuable_chance       REAL NOT NULL DEFAULT 0.4,
            valuable_cooldown_min INTEGER NOT NULL DEFAULT 60,
            updated_at            TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('''
        INSERT INTO case_settings (id, nft_gifts, valuable_chance, valuable_cooldown_min)
        VALUES (1, '[]', 0.4, 60)
        ON CONFLICT (id) DO NOTHING
    ''')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS case_rewards (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            reward_json TEXT NOT NULL,
            is_valuable INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    ''')

    _purge_test_players(cur)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ База данных инициализирована (PostgreSQL)")


# ── Helpers ────────────────────────────────────────────────────────

def is_test_user_id(user_id: int) -> bool:
    if user_id in TEST_PLAYER_IDS:
        return True
    lo, hi = TEST_ID_RANGE
    return lo <= user_id <= hi


def get_protected_user_ids() -> Set[int]:
    protected: Set[int] = set()
    raw = os.getenv("ADMIN_ID", "0")
    try:
        admin_id = int(raw)
        if admin_id > 0:
            protected.add(admin_id)
    except ValueError:
        pass
    return protected


def _delete_player(cur, user_id: int) -> None:
    for table, col in [
        ("contest_results",    "user_id"),
        ("case_rewards",       "user_id"),
        ("scores",             "user_id"),
        ("wallet_transactions","user_id"),
        ("wallet",             "user_id"),
        ("energy",             "user_id"),
        ("admin_bans",         "user_id"),
    ]:
        try:
            cur.execute(f"DELETE FROM {table} WHERE {col} = %s", (user_id,))
        except Exception:
            pass
    cur.execute(
        "DELETE FROM referrals WHERE inviter_id = %s OR invitee_id = %s",
        (user_id, user_id),
    )


def _purge_test_players(cur) -> None:
    protected = get_protected_user_ids()
    cur.execute("""
        SELECT DISTINCT user_id FROM (
            SELECT user_id FROM scores
            UNION
            SELECT user_id FROM wallet
            UNION
            SELECT user_id FROM energy
        ) u
    """)
    rows = cur.fetchall()
    all_ids = [r["user_id"] for r in rows] if rows else []
    for uid in all_ids:
        if uid in protected or not is_test_user_id(uid):
            continue
        _delete_player(cur, uid)
    cur.execute("DELETE FROM scores WHERE first_name = 'AdminTest'")
    for uid in TEST_PLAYER_IDS:
        if uid not in protected:
            _delete_player(cur, uid)


def admin_purge_test_players() -> Dict[str, Any]:
    protected = get_protected_user_ids()
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute("""
        SELECT DISTINCT user_id FROM (
            SELECT user_id FROM scores
            UNION
            SELECT user_id FROM wallet
            UNION
            SELECT user_id FROM energy
        ) u
    """)
    rows = cur.fetchall()
    all_ids = [r["user_id"] for r in rows] if rows else []

    deleted: List[int] = []
    kept:    List[int] = []
    for uid in all_ids:
        if uid in protected or not is_test_user_id(uid):
            kept.append(uid)
            continue
        _delete_player(cur, uid)
        deleted.append(uid)

    cur.execute("DELETE FROM scores WHERE first_name = 'AdminTest'")
    for uid in TEST_PLAYER_IDS:
        if uid not in protected and uid not in deleted:
            _delete_player(cur, uid)
            deleted.append(uid)

    conn.commit()
    cur.close()
    conn.close()
    return {"deleted": len(deleted), "kept": len(kept), "deleted_ids": deleted[:50]}


# ── Scores ─────────────────────────────────────────────────────────

def save_score(user_id: int, first_name: str, game_name: str, score: int):
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
        SELECT user_id, first_name, score FROM scores
        WHERE game_name = %s ORDER BY score DESC LIMIT %s
    ''', (game_name, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"user_id": r["user_id"], "first_name": r["first_name"] or "Игрок", "score": r["score"]} for r in rows]


# ── Contests ───────────────────────────────────────────────────────

def create_contest(game_name: str, prize_type: str, prize_value: int,
                   gift_id: Optional[str], split_prize: bool,
                   winners_count: int, started_by: int,
                   ends_at: str) -> int:
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


# ── Wallet ─────────────────────────────────────────────────────────

def get_wallet(user_id: int) -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('SELECT * FROM wallet WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            'INSERT INTO wallet (user_id, balance) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING',
            (user_id,)
        )
        conn.commit()
        balance = total_topped_up = total_spent = 0
    else:
        balance         = row["balance"]
        total_topped_up = row["total_topped_up"] or 0
        total_spent     = row["total_spent"]     or 0
    cur.close()
    conn.close()
    return {"balance": balance, "total_topped_up": total_topped_up, "total_spent": total_spent}


def topup_wallet(user_id: int, first_name: str, amount: int, description: str = "Пополнение") -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        INSERT INTO wallet (user_id, first_name, balance, total_topped_up, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            balance         = wallet.balance + EXCLUDED.balance,
            total_topped_up = wallet.total_topped_up + EXCLUDED.total_topped_up,
            first_name      = EXCLUDED.first_name,
            updated_at      = NOW()
    ''', (user_id, first_name, amount, amount))
    cur.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, description)
        VALUES (%s, 'topup', %s, %s)
    ''', (user_id, amount, description))
    cur.execute('SELECT balance FROM wallet WHERE user_id = %s', (user_id,))
    new_balance = cur.fetchone()["balance"]
    conn.commit()
    cur.close()
    conn.close()
    return {"balance": new_balance}


def spend_wallet(user_id: int, amount: int, description: str = "Покупка") -> Dict[str, Any]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('SELECT balance FROM wallet WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    current = row["balance"] if row else 0
    if current < amount:
        cur.close()
        conn.close()
        return {"ok": False, "balance": current, "short": amount - current}
    cur.execute('''
        UPDATE wallet SET balance = balance - %s, total_spent = total_spent + %s,
            updated_at = NOW()
        WHERE user_id = %s
    ''', (amount, amount, user_id))
    cur.execute('''
        INSERT INTO wallet_transactions (user_id, type, amount, description)
        VALUES (%s, 'spend', %s, %s)
    ''', (user_id, amount, description))
    cur.execute('SELECT balance FROM wallet WHERE user_id = %s', (user_id,))
    new_balance = cur.fetchone()["balance"]
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "balance": new_balance}


def get_wallet_transactions(user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = _cursor(conn)
    cur.execute('''
        SELECT type, amount, description, created_at
        FROM wallet_transactions
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s
    ''', (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ── Energy ─────────────────────────────────────────────────────────

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


# ── Case settings ──────────────────────────────────────────────────

CASE_PRICE                      = 1000
CASE_REWARD_DEDUP_SEC           = 20
CASE_VALUABLE_CHANCE_DEFAULT    = 0.4
CASE_NFT_IN_VALUABLE_SHARE      = 0.45
CASE_VALUABLE_COOLDOWN_MIN_DEFAULT = 60


def get_case_settings() -> Dict[str, Any]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute("SELECT nft_gifts, valuable_chance, valuable_cooldown_min FROM case_settings WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {
            "nft_gifts":             [],
            "valuable_chance":       CASE_VALUABLE_CHANCE_DEFAULT,
            "valuable_cooldown_min": CASE_VALUABLE_COOLDOWN_MIN_DEFAULT,
        }
    try:
        gifts = json.loads(row["nft_gifts"] or "[]")
    except json.JSONDecodeError:
        gifts = []
    if not isinstance(gifts, list):
        gifts = []
    gifts = [u.strip() for u in gifts if isinstance(u, str) and u.strip().startswith("http")]
    chance = float(row["valuable_chance"] if row["valuable_chance"] is not None else CASE_VALUABLE_CHANCE_DEFAULT)
    chance = max(0.05, min(0.95, chance))
    try:
        cooldown_min = int(row["valuable_cooldown_min"])
    except (KeyError, TypeError, ValueError):
        cooldown_min = CASE_VALUABLE_COOLDOWN_MIN_DEFAULT
    cooldown_min = max(5, min(24 * 60, cooldown_min))
    return {"nft_gifts": gifts, "valuable_chance": chance, "valuable_cooldown_min": cooldown_min}


def save_case_settings(
    nft_gifts: List[str],
    valuable_chance: float = CASE_VALUABLE_CHANCE_DEFAULT,
    valuable_cooldown_min: int = CASE_VALUABLE_COOLDOWN_MIN_DEFAULT,
) -> Dict[str, Any]:
    cleaned = []
    for url in nft_gifts:
        if not isinstance(url, str):
            continue
        u = url.strip()
        if u.startswith("http") and u not in cleaned:
            cleaned.append(u)
    chance       = max(0.05, min(0.95, float(valuable_chance)))
    cooldown_min = max(5, min(24 * 60, int(valuable_cooldown_min)))
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('''
        INSERT INTO case_settings (id, nft_gifts, valuable_chance, valuable_cooldown_min, updated_at)
        VALUES (1, %s, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE SET
            nft_gifts             = EXCLUDED.nft_gifts,
            valuable_chance       = EXCLUDED.valuable_chance,
            valuable_cooldown_min = EXCLUDED.valuable_cooldown_min,
            updated_at            = NOW()
    ''', (json.dumps(cleaned, ensure_ascii=False), chance, cooldown_min))
    conn.commit()
    cur.close()
    conn.close()
    return {"nft_gifts": cleaned, "valuable_chance": chance, "valuable_cooldown_min": cooldown_min}


# ── Case rewards ───────────────────────────────────────────────────

def _roll_common_stars() -> int:
    r = random.random()
    if r < 0.04: return 200
    if r < 0.14: return random.randint(100, 199)
    if r < 0.38: return random.randint(40, 99)
    return random.randint(5, 39)


def _pick_common_reward() -> Dict[str, Any]:
    if random.random() < 0.5:
        amount = random.randint(1, 20)
        return {"type": "energy", "amount": amount, "title": f"+{amount} энергии"}
    amount = _roll_common_stars()
    title  = "Джекпот +200 ⭐" if amount >= 200 else f"+{amount} ⭐"
    return {"type": "stars", "amount": amount, "title": title}


def _pick_valuable_reward(nft_gifts: List[str]) -> Dict[str, Any]:
    if nft_gifts and random.random() < CASE_NFT_IN_VALUABLE_SHARE:
        url = random.choice(nft_gifts)
        return {"type": "nft", "gift_url": url, "amount": 0, "title": "NFT-подарок!"}
    roll = random.random()
    if roll < 0.34:
        amount = random.randint(15, 20)
        return {"type": "energy", "amount": amount, "title": f"+{amount} энергии"}
    if roll < 0.67:
        amount = random.randint(100, 199)
        return {"type": "stars", "amount": amount, "title": f"+{amount} ⭐"}
    return {"type": "stars", "amount": 200, "title": "Джекпот +200 ⭐"}


def _is_global_valuable_on_cooldown(cooldown_sec: int) -> bool:
    if cooldown_sec <= 0:
        return False
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT 1 FROM case_rewards WHERE is_valuable = 1 AND created_at >= NOW() - (%s * INTERVAL '1 second') LIMIT 1",
        (cooldown_sec,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def get_case_valuable_cooldown_status() -> Dict[str, Any]:
    settings     = get_case_settings()
    cooldown_sec = settings["valuable_cooldown_min"] * 60
    base = {"on_cooldown": False, "cooldown_min": settings["valuable_cooldown_min"], "seconds_left": 0}
    if not _is_global_valuable_on_cooldown(cooldown_sec):
        return base
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT GREATEST(0, EXTRACT(EPOCH FROM (MAX(created_at) + (%s * INTERVAL '1 second') - NOW()))::INTEGER) AS seconds_left FROM case_rewards WHERE is_valuable = 1",
        (cooldown_sec,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    seconds_left = int(row["seconds_left"]) if row and row["seconds_left"] is not None else 0
    return {"on_cooldown": True, "cooldown_min": settings["valuable_cooldown_min"], "seconds_left": seconds_left}


def get_recent_case_reward(user_id: int, within_sec: int = CASE_REWARD_DEDUP_SEC) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute(
        "SELECT reward_json FROM case_rewards WHERE user_id = %s AND created_at >= NOW() - (%s * INTERVAL '1 second') ORDER BY id DESC LIMIT 1",
        (user_id, within_sec),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return json.loads(row["reward_json"])


def _save_case_reward(cur, user_id: int, reward: Dict[str, Any]) -> None:
    is_valuable = 1 if reward.get("tier") == "valuable" else 0
    cur.execute(
        'INSERT INTO case_rewards (user_id, reward_json, is_valuable) VALUES (%s, %s, %s)',
        (user_id, json.dumps(reward, ensure_ascii=False), is_valuable),
    )


def _apply_case_pick(user_id: int, first_name: str, pick: Dict[str, Any]) -> Dict[str, Any]:
    reward_type = pick["type"]
    amount      = pick.get("amount", 0)
    title       = pick["title"]
    if reward_type == "nft":
        return {"type": "nft", "gift_url": pick["gift_url"], "amount": 0, "title": title}
    if reward_type == "energy":
        result = admin_adjust_energy(user_id, amount)
        return {"type": "energy", "amount": amount, "title": title, "energy": result["amount"]}
    wallet = topup_wallet(user_id, first_name or "Игрок", amount, description="Награда из кейса")
    return {"type": "stars", "amount": amount, "title": title, "balance": wallet["balance"]}


def grant_case_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    settings        = get_case_settings()
    nft_gifts       = settings["nft_gifts"]
    valuable_chance = settings["valuable_chance"]
    cooldown_sec    = settings["valuable_cooldown_min"] * 60
    valuable_blocked = _is_global_valuable_on_cooldown(cooldown_sec)
    roll_valuable    = random.random() < valuable_chance
    if roll_valuable and valuable_blocked:
        roll_valuable = False
    if roll_valuable:
        pick = _pick_valuable_reward(nft_gifts)
        tier = "valuable"
    else:
        pick = _pick_common_reward()
        tier = "common"
    reward        = _apply_case_pick(user_id, first_name, pick)
    reward["tier"] = tier
    conn = get_connection()
    cur  = _cursor(conn)
    _save_case_reward(cur, user_id, reward)
    conn.commit()
    cur.close()
    conn.close()
    return reward


def confirm_case_reward(user_id: int, first_name: str = "") -> Dict[str, Any]:
    recent = get_recent_case_reward(user_id)
    if recent:
        return recent
    return grant_case_reward(user_id, first_name)


# ── User flags ─────────────────────────────────────────────────────

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


# ── Referrals ──────────────────────────────────────────────────────

REFERRAL_STARS        = 2
REFERRAL_ENERGY       = 2
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
    cur.execute('''
        SELECT invitee_id, first_name, reward_sent, policy_accepted, created_at
        FROM referrals WHERE inviter_id = %s
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


def is_already_referred(invitee_id: int) -> bool:
    conn = get_connection()
    cur  = _cursor(conn)
    cur.execute('SELECT 1 FROM referrals WHERE invitee_id = %s', (invitee_id,))
    found = cur.fetchone() is not None
    cur.close()
    conn.close()
    return found


# ── Admin Panel ────────────────────────────────────────────────────

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
                COALESCE(bl.ref_disabled, 0)   AS ref_disabled
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
    conn.commit()  # фиксируем INSERT если строки не было
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
