"""Private SQLite storage for OAuth state, shared across process restarts.

All six provider maps use one database. Token exchange and revocation run in a
single SQLite transaction, so rotation cannot leave a reusable old token behind.
The database contains credentials and must stay on a private persistent volume.
"""
from __future__ import annotations

from collections.abc import Iterator, MutableMapping
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

from pydantic import BaseModel


class OAuthStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create privately before SQLite opens it (rather than chmod afterwards).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path, isolation_level=None, timeout=15, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS oauth_state (
            namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (namespace, key))""")

    def transaction(self):
        return Transaction(self)

    def mapping(self, namespace: str, model: type[BaseModel] | None = None):
        return SQLiteMapping(self, namespace, model)

    def close(self):
        with self.lock:
            self.connection.close()


class Transaction:
    # An explicit context manager preserves SDK frozen dataclass exceptions;
    # contextlib's generator manager attempts to assign their __traceback__.
    def __init__(self, store: OAuthStore):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        try:
            self.store.connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self.store.lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.store.connection.commit()
            else:
                self.store.connection.rollback()
        finally:
            self.store.lock.release()
        return False


class SQLiteMapping(MutableMapping[str, Any]):
    def __init__(self, store: OAuthStore, namespace: str,
                 model: type[BaseModel] | None):
        self.store, self.namespace, self.model = store, namespace, model

    def __getitem__(self, key: str) -> Any:
        with self.store.lock:
            row = self.store.connection.execute(
                "SELECT value FROM oauth_state WHERE namespace=? AND key=?",
                (self.namespace, key)).fetchone()
        if row is None:
            raise KeyError(key)
        return (self.model.model_validate_json(row[0]) if self.model
                else json.loads(row[0]))

    def __setitem__(self, key: str, value: Any):
        encoded = value.model_dump_json() if self.model else json.dumps(value)
        with self.store.lock:
            self.store.connection.execute(
                "INSERT INTO oauth_state(namespace,key,value) VALUES(?,?,?) "
                "ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",
                (self.namespace, key, encoded))

    def __delitem__(self, key: str):
        with self.store.lock:
            cursor = self.store.connection.execute(
                "DELETE FROM oauth_state WHERE namespace=? AND key=?",
                (self.namespace, key))
        if cursor.rowcount == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self.store.lock:
            keys = self.store.connection.execute(
                "SELECT key FROM oauth_state WHERE namespace=?",
                (self.namespace,)).fetchall()
        return iter(row[0] for row in keys)

    def __len__(self) -> int:
        with self.store.lock:
            return self.store.connection.execute(
                "SELECT count(*) FROM oauth_state WHERE namespace=?",
                (self.namespace,)).fetchone()[0]
