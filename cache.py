"""SQLite cache for public GitHub metadata only."""

import json
from pathlib import Path
import sqlite3
import threading
import time


class PublicCache:
    def __init__(self, path=None):
        self.path = Path(path or Path(__file__).resolve().parent / ".github_cache.sqlite3")
        self.lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS public_cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, body TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def get(self, key, allow_expired=False):
        with self.lock, self._connect() as db:
            row = db.execute("SELECT expires, body FROM public_cache WHERE key=?", (key,)).fetchone()
        if row and (allow_expired or row[0] > time.time()):
            return json.loads(row[1])
        return None

    def put(self, key, body, ttl):
        with self.lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO public_cache(key, expires, body) VALUES(?,?,?)",
                       (key, time.time() + ttl, json.dumps(body, ensure_ascii=False)))


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key, allow_expired=False):
        expires, body = self.values.get(key, (0, None))
        return body if (allow_expired or expires > time.time()) else None

    def put(self, key, body, ttl):
        self.values[key] = (time.time() + ttl, body)
