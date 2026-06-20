import sqlite3
from pathlib import Path

from .config import settings

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()
_SEED = Path(__file__).with_name("seed.sql").read_text()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table's CREATE IF NOT EXISTS already ran
    on someone's existing DB. SQLite has no ADD COLUMN IF NOT EXISTS, so check
    PRAGMA table_info first.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(venues)")}
    if "address" not in cols:
        conn.execute("ALTER TABLE venues ADD COLUMN address TEXT")


def init_db(seed: bool = True) -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        if seed:
            # Seed is idempotent (INSERT OR IGNORE / guarded by UNIQUE).
            conn.executescript(_SEED)
