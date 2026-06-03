import os
from typing import Set

import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TEST_PLAYER_IDS = (999001, 777888)
TEST_ID_RANGE = (888_000, 1_010_000)


def get_connection():
    db_url = DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set - добавьте PostgreSQL URL из Render Dashboard")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def _cursor(conn):
    """DictCursor - аналог sqlite3.Row: доступ по имени колонки."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


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


def admin_purge_test_players():
    from typing import Dict, Any, List
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

    cur.execute('''
        CREATE TABLE IF NOT EXISTS durak_lobbies (
            id                SERIAL PRIMARY KEY,
            name              TEXT,
            creator_id        BIGINT NOT NULL,
            creator_name      TEXT,
            max_players       INTEGER NOT NULL CHECK (max_players BETWEEN 2 AND 6),
            deck_size         INTEGER NOT NULL CHECK (deck_size IN (24, 36, 52)),
            game_type         TEXT NOT NULL DEFAULT 'podkidnoy' CHECK (game_type IN ('podkidnoy', 'perevodnoy')),
            cheating_enabled  BOOLEAN DEFAULT FALSE,
            bet_amount        INTEGER DEFAULT 0,
            pot               INTEGER DEFAULT 0,
            status            TEXT DEFAULT 'waiting' CHECK (status IN ('waiting', 'playing', 'finished', 'cancelled')),
            created_at        TIMESTAMP DEFAULT NOW(),
            updated_at        TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_durak_lobbies_status ON durak_lobbies(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_durak_lobbies_created ON durak_lobbies(created_at)')

    # Миграции для существующих БД
    for migration in [
        "ALTER TABLE durak_lobbies ADD COLUMN IF NOT EXISTS pot INTEGER DEFAULT 0",
        "ALTER TABLE durak_lobbies ADD COLUMN IF NOT EXISTS name TEXT",
        "ALTER TABLE durak_lobbies ADD COLUMN IF NOT EXISTS started_at TIMESTAMP",
    ]:
        try:
            cur.execute(migration)
        except Exception:
            pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS durak_lobby_players (
            id         SERIAL PRIMARY KEY,
            lobby_id   INTEGER NOT NULL REFERENCES durak_lobbies(id) ON DELETE CASCADE,
            user_id    BIGINT NOT NULL,
            first_name TEXT,
            joined_at  TIMESTAMP DEFAULT NOW(),
            is_ready   BOOLEAN DEFAULT FALSE,
            photo_url  TEXT,
            UNIQUE(lobby_id, user_id)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_durak_players_lobby ON durak_lobby_players(lobby_id)')
    try:
        cur.execute("ALTER TABLE durak_lobby_players ADD COLUMN IF NOT EXISTS photo_url TEXT")
    except Exception:
        pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS durak_game_history (
            id            SERIAL PRIMARY KEY,
            lobby_id      INTEGER,
            winner_id     BIGINT,
            pot           INTEGER DEFAULT 0,
            players       JSONB,
            started_at    TIMESTAMP,
            ended_at      TIMESTAMP DEFAULT NOW(),
            duration_sec  INTEGER
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_durak_history_winner ON durak_game_history(winner_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_durak_history_ended ON durak_game_history(ended_at)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS durak_bans (
            user_id BIGINT PRIMARY KEY,
            banned_at TIMESTAMP DEFAULT NOW(),
            reason TEXT
        )
    ''')

    # Снимок состояния активной игры (восстановление после рестарта)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS durak_game_state (
            lobby_id   INTEGER PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    ''')

    _purge_test_players(cur)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ База данных инициализирована (PostgreSQL)")
