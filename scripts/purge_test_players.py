"""One-off: purge synthetic test players from PostgreSQL."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "config" / ".env")
os.chdir(ROOT)

from api.database import admin_purge_test_players, get_connection, _cursor

result = admin_purge_test_players()
print("Purge result:", result)

conn = get_connection()
cur = _cursor(conn)
cur.execute("""
    SELECT COUNT(*) AS n FROM (
        SELECT user_id FROM scores
        UNION
        SELECT user_id FROM wallet
        UNION
        SELECT user_id FROM energy
    ) u
""")
remaining = cur.fetchone()["n"]
cur.close()
conn.close()
print("Remaining players:", remaining)
