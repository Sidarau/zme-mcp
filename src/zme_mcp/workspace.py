"""Client workspaces (ZEUG-795): one Postgres schema per cmd project on omarchy ZME.

The database is the security boundary (see migrations/001_workspaces.sql). Every call runs inside one transaction
that switches to exactly one workspace role and declares that workspace; row-level security checks both. The
connecting role holds no privileges of its own: `zme_ws_gateway` in production (ZEUG-796), `postgres` for
administration and tests.

Writes are chunked and embedded here, before the insert, so a note is searchable the moment `remember` returns.
Embeddings use the same arctic-l space as Zeug's memory (ollama `snowflake-arctic-embed:335m`), so the owner can
copy a Zeug source into a workspace with its vectors. When no embedding service answers, search falls back to the
lexical arm and notes are stored without vectors.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")   # cmd PROJECT_RE
LEVELS = ("read", "write")

EMBED_URL = os.environ.get("ZME_EMBED_URL", "http://100.113.216.73:11434").rstrip("/")   # Argus (M1)
EMBED_MODEL = os.environ.get("ZME_EMBED_MODEL", "snowflake-arctic-embed:335m")
EMBED_VARIANT = "arctic-l-v1"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

CHUNKER_VERSION = "md-h+window-v1"
MAX_CHARS = int(460 * 3.6)          # 460 tokens: the model truncates at 512
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


def check_id(workspace: str) -> str:
    if not ID_RE.match(workspace or "") or workspace == "internal":
        raise ValueError(f"not a client project id: {workspace!r}")
    return workspace


def role_for(workspace: str, level: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    return f"zme_ws_{check_id(workspace).replace('-', '_')}_{level}"


# -- chunking and embedding (same shapes as zme_ingest.py) ------------------------------------------------------------

def _sections(text: str) -> list[tuple[str, str]]:
    marks = [(m.start(), m.group(2).strip()) for m in HEADING_RE.finditer(text)]
    if not marks:
        return [("", text)]
    out = [("", text[:marks[0][0]].strip())] if marks[0][0] > 0 else []
    for i, (pos, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((title, text[pos:end].strip()))
    return [(h, b) for h, b in out if b]


def _window(text: str) -> list[str]:
    chunks, cur = [], ""
    for p in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not p:
            continue
        while len(p) > MAX_CHARS:                 # an oversized paragraph is cut at a space
            cut = p.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + len(p) + 2 > MAX_CHARS:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def chunk(title: str, text: str, source_type: str) -> list[dict]:
    rows = []
    for heading, body in _sections(text):
        for piece in _window(body):
            prefix = " — ".join(b for b in (title.strip(), heading, f"[{source_type}]") if b)[:600]
            rows.append({"chunk_index": len(rows), "content": piece, "context_prefix": prefix,
                         "heading": heading or None, "token_count": int(len(piece) / 3.6),
                         "chunker_version": CHUNKER_VERSION})
    return rows


def embed(texts: list[str], query: bool = False) -> list[list[float]] | None:
    """Arctic vectors from ollama, or None when the service is unreachable (callers degrade to lexical)."""
    if query:
        texts = [QUERY_PREFIX + t for t in texts]
    req = urllib.request.Request(f"{EMBED_URL}/api/embed", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode())
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            vectors = json.loads(resp.read())["embeddings"]
    except (OSError, ValueError, KeyError):
        return None
    return vectors if len(vectors) == len(texts) else None


def _vector(v: list[float] | None) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.7f}" for x in v) + "]"


# -- database ------------------------------------------------------------------------------------------------------

class Workspaces:
    """`dsn` connects as the gateway role (or postgres for admin). One connection per call."""

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
