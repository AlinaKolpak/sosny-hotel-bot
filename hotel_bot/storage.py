"""SQLite хранит диалог и операции между перезапусками."""

import json
import sqlite3
from pathlib import Path


def new_state():
    return {"mode": "bot", "fields": {}, "offers": [], "selected": None,
            "quote_token": None, "guest": None, "booking_id": None, "revision": 0}


class Store:
    def __init__(self, path, hotel):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, reply TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS rooms(
                id TEXT PRIMARY KEY, name TEXT, capacity INTEGER, stock INTEGER, nightly INTEGER);
            CREATE TABLE IF NOT EXISTS bookings(
                id TEXT PRIMARY KEY, intent TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL,
                room_id TEXT NOT NULL, check_in TEXT NOT NULL, check_out TEXT NOT NULL,
                total INTEGER NOT NULL, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, recipient TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(bookings)")}
        if "status" not in columns:
            self.db.execute("ALTER TABLE bookings ADD COLUMN status TEXT NOT NULL DEFAULT 'CONFIRMED'")
        if "message_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN message_id INTEGER")
        self.db.execute("PRAGMA user_version=3")
        for room in hotel["rooms"]:
            self.db.execute("INSERT OR IGNORE INTO rooms VALUES (:id,:name,:capacity,:stock,:nightly)", room)

    def state(self, session):
        row = self.db.execute("SELECT state FROM sessions WHERE id=?", (session,)).fetchone()
        return json.loads(row[0]) if row else new_state()

    def save(self, session, state):
        self.db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                        (session, json.dumps(state, ensure_ascii=False)))

    def event(self, event_id):
        row = self.db.execute("SELECT reply FROM events WHERE id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def meta(self, key, default="0"):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    def close(self):
        self.db.close()
