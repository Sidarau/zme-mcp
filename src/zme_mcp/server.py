"""zme-mcp — the Zeug Memory Engine as an MCP server.

Tools
-----
search, fetch   ChatGPT deep-research / company-knowledge compatible pair.
                `search` takes one query string and returns {results: [{id,
                title, url}]}; `fetch(id)` returns the full chunk text. `id`
                is a ZME chunk UUID.
recall          Zeug's own notes/analysis/research only (the corpus is 95.6%
                video transcripts — this is the scope that finds what Zeug
                wrote down).
why             Causal chain between two entities in the knowledge graph.
near            Entity neighbourhood, N hops, with evidence quotes.
remember        The only write path. Creates one note via zme_remember()
                (SECURITY DEFINER); searchable after the next ingest run.

Transports
----------
stdio (default)     zme-mcp
streamable HTTP     zme-mcp --http --port 8000        → POST /mcp
                    health check at GET /healthz

HTTP auth: set ZME_MCP_TOKEN and every request must carry it as
`Authorization: Bearer <token>` or `?token=<token>`. ChatGPT connectors can
carry it in the URL. Unset → open (fine for localhost, wrong for the internet).
"""
from __future__ import annotations

import json
import os
from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolAnnotations

from .backend import KNOWLEDGE_TYPES, NOTE_TYPES, ZME

INSTRUCTIONS = """\
ZME (Zeug Memory Engine) — hybrid retrieval over Zeug's collected knowledge
(articles, research, notes, transcripts) plus a bitemporal knowledge graph.

Use `search` before researching from scratch; use `recall` for things Zeug
itself wrote down; use `why` for "what caused what" questions. `remember`
records a durable note — pitfalls, decisions, incidents — so it survives the
session. Do not store secrets here."""

mcp = FastMCP(name="zme-mcp", instructions=INSTRUCTIONS)

_zme: ZME | None = None


def _client() -> ZME:
    global _zme
    if _zme is None:
        _zme = ZME()
    return _zme


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=True)
WRITE_NOTE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                             idempotentHint=False, openWorldHint=False)


def _fmt_row(r: dict) -> str:
    title = (r.get("source_title") or "(untitled)")[:80]
    url = r.get("source_url") or ""
    body = (r.get("content") or "").replace("\n", " ")[:400]
    head = f"[{r.get('rrf_score', 0):.5f}] {title}"
    if url:
        head += f" — {url}"
    return f"{head}\n    {body}\n    chunk:{r.get('chunk_id')}"


# --------------------------------------------------------------------------
# ChatGPT deep-research / company-knowledge compatible pair
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
async def search(query: str) -> dict[str, Any]:
    """Search Zeug's knowledge base. Returns {results: [{id, title, url}]} —
    call fetch(id) for full content. Natural-language questions work best."""
    rows = await anyio.to_thread.run_sync(
        lambda: _client().search(query, k=12, source_types=KNOWLEDGE_TYPES))
    # Results are documents, not chunks: one hit per source, best chunk wins
    # (rows arrive RRF-ordered). Otherwise three adjacent chunks of one file
    # crowd out everything else.
    seen: set[str] = set()
    results = []
    for r in rows:
        sid = str(r.get("source_id"))
        if sid in seen:
            continue
        seen.add(sid)
        results.append({"id": r["chunk_id"],
                        "title": r.get("source_title") or "(untitled)",
                        "url": r.get("source_url") or f"zme://sources/{sid}"})
        if len(results) >= 8:
            break
    return {"results": results}


@mcp.tool(annotations=READ_ONLY)
async def fetch(id: str) -> dict[str, Any]:
    """Fetch the full content of one search result by its id (a chunk UUID
    returned by search)."""
    row = await anyio.to_thread.run_sync(lambda: _client().fetch_chunk(id))
    if not row:
        return {"id": id, "title": "", "text": "", "url": "",
                "metadata": {"error": "not found"}}
    return {"id": row["id"],
            "title": row.get("source_title") or "(untitled)",
            "text": row.get("content") or "",
            "url": row.get("source_url") or f"zme://sources/{row.get('source_id')}",
            "metadata": {"source_type": row.get("source_type"),
                         "chunk_index": row.get("chunk_index")}}


# --------------------------------------------------------------------------
# Native ZME tools
# --------------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
async def recall(query: str, k: int = 8, wide: bool = False) -> str:
    """Recall what Zeug itself wrote down — notes, analysis, compiled research.
    Use this (not search) for 'why did we decide X' questions. Set wide=true
    to also include articles and social posts."""
    scope = KNOWLEDGE_TYPES if wide else NOTE_TYPES
    rows = await anyio.to_thread.run_sync(
        lambda: _client().search(query, k=k, source_types=scope))
    if not rows:
        return (f"Nothing found for {query!r}. Try the `search` tool — recall "
                f"only looks at Zeug's own notes.")
    header = f"recall ({'knowledge' if wide else 'notes'}) · {len(rows)} results for: {query}"
    return header + "\n" + "=" * 72 + "\n\n" + "\n\n".join(_fmt_row(r) for r in rows)


@mcp.tool(annotations=READ_ONLY)
async def why(from_entity: str, to_entity: str, max_hops: int = 5) -> str:
    """Causal chain between two entities in the knowledge graph, e.g.
    why('kimi CLI 0.27.0', 'loop.ts CLI_COMMANDS fix'). Use canonical names."""
    paths = await anyio.to_thread.run_sync(
        lambda: _client().path(from_entity, to_entity, max_hops))
    if not paths:
        return f"No path between {from_entity!r} and {to_entity!r} within {max_hops} hops."
    out = []
    for p in paths:
        names, preds = p["node_names"], p["predicates"]
        chain = names[0]
        for pr, nx in zip(preds, names[1:]):
            chain += f"\n   --[{pr}]--> {nx}"
        out.append(f"{p['hops']} hops:\n   {chain}")
    return "\n\n".join(out)


@mcp.tool(annotations=READ_ONLY)
async def near(entity: str, hops: int = 1) -> str:
    """Facts around one entity in the knowledge graph, with evidence quotes."""
    rows = await anyio.to_thread.run_sync(
        lambda: _client().neighborhood(entity, hops))
    if not rows:
        return f"No facts recorded around {entity!r}."
    lines = []
    for r in rows:
        line = (f"[{r['depth']}] {r['subject']} --{r['predicate']}--> {r['object']}"
                f"  ({r['confidence']})")
        if r.get("evidence_quote"):
            line += f"\n      “{r['evidence_quote'][:160]}”"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool(annotations=WRITE_NOTE)
async def remember(text: str, title: str | None = None,
                   agent: str = "zme-mcp") -> str:
    """Record a durable fact — a pitfall, decision, or incident — so it
    survives the session. Becomes searchable after the next ingest run.
    Never store secrets."""
    title = title or text[:60]
    new_id = await anyio.to_thread.run_sync(
        lambda: _client().remember(title, text, agent))
    return f"remembered {new_id}\n  {title}\n  (searchable after the next ingest run)"


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

def _http_app():
    """Streamable-HTTP ASGI app with optional bearer/query-token auth."""
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    token = os.environ.get("ZME_MCP_TOKEN")

    class TokenAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            if token:
                auth = request.headers.get("authorization", "")
                given = (auth[7:] if auth.startswith("Bearer ")
                         else request.query_params.get("token"))
                if given != token:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request):
        return PlainTextResponse("ok")

    if not token:
        import warnings
        warnings.warn("ZME_MCP_TOKEN unset — HTTP endpoint is UNAUTHENTICATED. "
                      "Fine for localhost; set a token before exposing it.")
    return mcp.http_app(path="/mcp", middleware=[Middleware(TokenAuth)])


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="zme-mcp",
                                description="Zeug Memory Engine as an MCP server")
    p.add_argument("--http", action="store_true",
                   help="serve streamable HTTP at /mcp instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    if args.http:
        import uvicorn
        uvicorn.run(_http_app(), host=args.host, port=args.port,
                    log_level="info")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
