"""Client workspaces (ZEUG-795): one Postgres schema per cmd project on omarchy ZME.

The database is the security boundary (see migrations/001_workspaces.sql). Every call runs inside one transaction
that switches to exactly one workspace role and declares that workspace; row-level security checks both. This
library is for administration and tooling on the tailnet, connected as `postgres`. Services reach workspaces through
the narrow API in migrations/003_cmd_gateway.sql instead; no service login can switch into every workspace.

Writes are chunked and embedded here, before the insert, so a note is searchable the moment `remember` returns.
Embeddings use the same arctic-l space as Zeug's memory (ollama `snowflake-arctic-embed:335m`), so the owner can
copy a Zeug source into a workspace with its vectors. When no embedding service answers, search falls back to the
lexical arm and notes are stored without vectors.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .text import EMBED_URL, EMBED_VARIANT, _vector, chunk, embed  # noqa: F401  (re-exported)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")   # cmd PROJECT_RE
LEVELS = ("read", "write")


def check_id(workspace: str) -> str:
    if not ID_RE.match(workspace or "") or workspace == "internal":
        raise ValueError(f"not a client project id: {workspace!r}")
    return workspace


def role_for(workspace: str, level: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    return f"zme_ws_{check_id(workspace).replace('-', '_')}_{level}"

# -- database ------------------------------------------------------------------------------------------------------

class Workspaces:
    """`dsn` connects as postgres (admin and tooling). One connection per call."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("ZME_WS_DSN") or ""
        if not self.dsn:
            raise RuntimeError("zme workspaces: set ZME_WS_DSN (libpq connection string; password via PGPASSWORD)")

    @contextmanager
    def _session(self, workspace: str, level: str, actor: str | None) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:   # commits on success, rolls back on error
            conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role_for(workspace, level))))
            conn.execute("select set_config('zme.workspace', %s, true), set_config('zme.actor', %s, true)",
                         (workspace, actor or ""))
            yield conn

    def search(self, workspace: str, query: str, k: int = 8, mode: str = "hybrid",
               source_types: list[str] | None = None, actor: str | None = None) -> list[dict]:
        vec = embed([query], query=True) if mode in ("hybrid", "vector") else None
        with self._session(workspace, "read", actor) as conn:
            return conn.execute(
                "select * from zme_ws.search(%s, %s::extensions.vector, %s, %s, p_source_types => %s)",
                (workspace, _vector(vec[0]) if vec else None, query if mode in ("hybrid", "lexical") else None,
                 k, source_types)).fetchall()

    def remember(self, workspace: str, title: str, text: str, source_type: str = "note", url: str | None = None,
                 metadata: dict | None = None, actor: str | None = None) -> dict:
        rows = chunk(title, text, source_type)
        if not rows:
            raise ValueError("nothing to store")
        vectors = embed([r["content"] for r in rows])
        for r, v in zip(rows, vectors or [None] * len(rows)):
            r["embedding"] = v
            r["embed_variant"] = EMBED_VARIANT if v else None
        with self._session(workspace, "write", actor) as conn:
            sid = conn.execute("select zme_ws.add_source(%s, %s, %s, %s::jsonb, %s, %s, %s::jsonb) as id",
                               (workspace, title, text, json.dumps(rows), url, source_type,
                                json.dumps(metadata or {}))).fetchone()["id"]
        return {"id": str(sid), "chunks": len(rows), "embedded": bool(vectors)}

    def fetch(self, workspace: str, chunk_id: str, actor: str | None = None) -> dict | None:
        with self._session(workspace, "read", actor) as conn:
            return conn.execute("select * from zme_ws.fetch(%s, %s)", (workspace, chunk_id)).fetchone()

    def sources(self, workspace: str, limit: int = 50, actor: str | None = None) -> list[dict]:
        with self._session(workspace, "read", actor) as conn:
            return conn.execute("select * from zme_ws.sources(%s, %s)", (workspace, limit)).fetchall()

    # -- administration (postgres) ----------------------------------------------------------------------------------

    def _admin(self, query: str, params: tuple = ()) -> list[dict]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    def create(self, workspace: str, title: str) -> str:
        return self._admin("select zme_ws.create_workspace(%s, %s) as s", (check_id(workspace), title))[0]["s"]

    def workspaces(self) -> list[dict]:
        return self._admin("""select w.id, w.title, w.schema_name, w.created_at,
                                     (select count(*) from pg_class c where c.relnamespace = w.schema_name::regnamespace) as relations
                              from zme_ws.workspaces w order by w.id""")

    def copy_from_zeug(self, workspace: str, source_id: str, actor: str | None = None) -> str:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            conn.execute("select set_config('zme.actor', %s, true)", (actor or "",))
            return str(conn.execute("select zme_ws.copy_from_zeug(%s, %s) as id",
                                    (check_id(workspace), source_id)).fetchone()["id"])

    def embed_pending(self, limit: int = 500) -> dict[str, int]:
        """Adds vectors to chunks stored without one (notes added from cmd, which cannot reach the embedder)."""
        done: dict[str, int] = {}
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            for w in conn.execute("select id, schema_name from zme_ws.workspaces order by id").fetchall():
                table = sql.Identifier(w["schema_name"], "chunks")
                rows = conn.execute(sql.SQL("select id, content from {} where embedding_v is null order by created_at limit %s").format(table),
                                    (limit,)).fetchall()
                for i in range(0, len(rows), 32):
                    batch = rows[i:i + 32]
                    vectors = embed([r["content"] for r in batch])
                    if vectors is None:
                        raise RuntimeError(f"embedding service unreachable at {EMBED_URL}")
                    for r, v in zip(batch, vectors):
                        conn.execute(sql.SQL("update {} set embedding_v = %s::extensions.vector, embed_variant = %s where id = %s").format(table),
                                     (_vector(v), EMBED_VARIANT, r["id"]))
                    conn.commit()
                if rows:
                    done[w["id"]] = len(rows)
        return done

    def drop(self, workspace: str, confirm: str) -> None:
        self._admin("select zme_ws.drop_workspace(%s, %s)", (check_id(workspace), confirm))
