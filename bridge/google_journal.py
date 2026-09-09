"""Encrypted plans/receipts, with permanent claims preventing uncertain write retries."""
import json
import threading
import os
from pathlib import Path
import sqlite3
from cryptography.fernet import Fernet
from redis import Redis


class RedisGoogleJournal:
    def __init__(self, url, encryption_key):
        self.redis = Redis.from_url(url, socket_timeout=10, socket_connect_timeout=10)
        self.cipher = Fernet(encryption_key)

    def get(self, key):
        raw = self.redis.get('bridge-google:' + key)
        return json.loads(self.cipher.decrypt(raw)) if raw else None

    def put(self, key, value, *, once=False):
        raw = self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())
        return bool(self.redis.set('bridge-google:' + key, raw, nx=once, ex=86400))

    def claim(self, key):
        # No expiry: losing a receipt must NEVER make a send/create eligible again.
        return bool(self.redis.set('bridge-google-claimed:' + key, '1', nx=True))


class MemoryGoogleJournal:
    """Only for unit tests or a single local process; production always uses Redis."""
    def __init__(self):
        self.values, self.claims, self.lock = {}, set(), threading.Lock()

    def get(self, key):
        with self.lock:
            return json.loads(json.dumps(self.values[key])) if key in self.values else None

    def put(self, key, value, *, once=False):
        with self.lock:
            if once and key in self.values:
                return False
            self.values[key] = json.loads(json.dumps(value))
            return True

    def claim(self, key):
        with self.lock:
            if key in self.claims:
                return False
            self.claims.add(key)
            return True


class LocalGoogleJournal:
    """Durable local CLI receipts, encrypted with a host-only 0600 key."""
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = directory / 'journal.key'
        try:
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        self.path = directory / 'journal.sqlite3'
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS claims (key TEXT PRIMARY KEY)')
        self.path.chmod(0o600)

    def _db(self):
        return sqlite3.connect(self.path, timeout=20)

    def get(self, key):
        with self._db() as db:
            row = db.execute('SELECT value FROM records WHERE key=?', (key,)).fetchone()
        return json.loads(self.cipher.decrypt(row[0])) if row else None

    def put(self, key, value, *, once=False):
        raw = self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())
        with self._db() as db:
            query = 'INSERT OR IGNORE' if once else 'INSERT OR REPLACE'
            return bool(db.execute(query + ' INTO records VALUES (?,?)', (key, raw)).rowcount)

    def claim(self, key):
        with self._db() as db:
            return bool(db.execute('INSERT OR IGNORE INTO claims VALUES (?)', (key,)).rowcount)
