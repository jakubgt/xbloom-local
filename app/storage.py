import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id():
    return uuid4().hex


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = self.root / "xbloom.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('schema_version','1');
            """)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, kind):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid DESC", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, kind, record_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, record):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO records(kind,id,data) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                       (kind, record["id"], json.dumps(record, allow_nan=False)))
        return record

    def put_all(self, items):
        """Commit related records atomically, including immutable recipe history."""
        with self.lock, self.connect() as db:
            for kind, record in items:
                db.execute("INSERT INTO records(kind,id,data) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                           (kind, record["id"], json.dumps(record, allow_nan=False)))

    def create(self, kind, data):
        return self.put(kind, {**data, "id": new_id(), "created_at": now(), "updated_at": now()})

    def delete(self, kind, record_id):
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, record_id))

    def audit(self, text, kind="info"):
        return self.create("activity", {"text": text, "kind": kind})
