"""Migrate data from local arcade.db (SQLite) to PostgreSQL (DATABASE_URL)."""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "config" / ".env")

SQLITE_PATH = ROOT / "arcade.db"
if not SQLITE_PATH.exists():
    print(f"No {SQLITE_PATH} — nothing to migrate.")
    sys.exit(0)

if not os.getenv("DATABASE_URL", "").strip():
    print("Set DATABASE_URL to your Render PostgreSQL connection string first.")
    sys.exit(1)

from api.database import get_connection, init_db, _cursor

TABLES = [
    "scores",
    "referrals",
    "contests",
    "contest_results",
    "wallet",
    "wallet_transactions",
    "admin_bans",
    "energy",
    "case_settings",
    "case_rewards",
]

init_db()

src = sqlite3.connect(SQLITE_PATH)
src.row_factory = sqlite3.Row
dst = get_connection()
cur = _cursor(dst)

# Clear destination (child tables first)
for table in reversed(TABLES):
    cur.execute(f"DELETE FROM {table}")

for table in TABLES:
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  {table}: 0 rows")
        continue
    cols = rows[0].keys()
    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    for row in rows:
        cur.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
            tuple(row[c] for c in cols),
        )
    # Reset SERIAL sequences after manual inserts
    if table in ("scores", "referrals", "contests", "contest_results", "wallet_transactions", "case_rewards"):
        cur.execute(f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1)
            )
        """)
    print(f"  {table}: {len(rows)} rows")

dst.commit()
cur.close()
dst.close()
src.close()
print("Migration complete.")
