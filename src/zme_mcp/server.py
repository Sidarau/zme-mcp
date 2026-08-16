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

Auth (see auth.py — the alpha of the zeuglab → ClawPanel auth model)
--------------------------------------------------------------------
ZME_AUTH=oauth   Full OAuth 2.1 (DCR + PKCE) with profile levels
                 (owner/operator/viewer → zme:read / zme:write scopes).
                 This is what ChatGPT connects to. Required from ring beta.
ZME_AUTH=token   Legacy shared bearer (ZME_MCP_TOKEN), alpha ring only.
unset            oauth when ZME_OAUTH_PROFILES is set, else token.

Transports
----------
stdio (default)     zme-mcp
streamable HTTP     zme-mcp --http --port 8080        → POST /mcp
                    health check at GET /healthz
"""
from __future__ import annotations

import os
from typing import Any

import anyio
from fastmcp import FastMCP
from fastmcp.server.auth import require_scopes
from fastmcp.tools.tool import ToolAnnotations

from .auth import build_auth
from .backend import (KNOWLEDGE_TYPES, NOTE_TYPES, SEARCHABLE_TYPES, ZME,
                      coverage_gaps, diversify, finance_evidence_bundle)

INSTRUCTIONS = """\
ZME (Zeug Memory Engine) — hybrid retrieval over Zeug's collected knowledge
(articles, research, notes, transcripts) plus a bitemporal knowledge graph.

Use `search` before researching from scratch; use `recall` for things Zeug
itself wrote down; use `why` for "what caused what" questions. `remember`
records a durable note — pitfalls, decisions, incidents — so it survives the
session. Do not store secrets here."""

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


def build_server(*, http: bool = False, base_url: str = "") -> FastMCP:
    provider = build_auth(base_url) if http else None
    mcp = FastMCP(name="zme-mcp", instructions=INSTRUCTIONS, auth=provider)
    # In OAuth mode, remember additionally requires the zme:write scope
    # (provider-wide requirement is zme:read).
    write_auth = [require_scopes("zme:write")] if provider else None

    # ------------------------------------------------------------------
    # ChatGPT deep-research / company-knowledge compatible pair
    # ------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def search(query: str, published_after: str | None = None,
                     published_before: str | None = None,
                     source_keys: list[str] | None = None,
                     content_types: list[str] | None = None,
                     per_source_cap: int = 3,
                     exclude_short_form: bool = True) -> dict[str, Any]:
        """Search Zeug's knowledge base. Returns {results: [{id, title, url}]} —
        call fetch(id) for full content. Use RFC 3339 published_after / published_before
        and canonical source_keys for current-period or exact-source requests. These are
        enforced filters; records with unprovable dates/source identity are excluded."""
        source_types = (KNOWLEDGE_TYPES if content_types is None else
                        [value for value in content_types if value in SEARCHABLE_TYPES])
        if not source_types:
            return {"results": []}
        rows = await anyio.to_thread.run_sync(
            lambda: _client().search(
                query, k=24 if (published_after or published_before or source_keys) else 12,
                source_types=source_types,
                published_after=published_after, published_before=published_before,
                source_keys=source_keys,
                exclude_short_form=exclude_short_form))
        # Results are documents, not chunks: one hit per source, best chunk
        # wins (rows arrive RRF-ordered). Otherwise three adjacent chunks of
        # one file crowd out everything else.
        results = []
        cap = 8 if source_keys and len(source_keys) == 1 else per_source_cap
        for r in diversify(rows, limit=8, per_source_cap=cap):
            sid = str(r.get("source_id"))
            results.append({"id": r["chunk_id"],
                            "title": r.get("source_title") or "(untitled)",
                            "url": r.get("source_url") or f"zme://sources/{sid}",
                            "source_key": r.get("source_key"),
                            "published_at": r.get("published_at"),
                            "ingested_at": r.get("ingested_at"),
                            "content_type": r.get("source_type")})
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
                             "source_key": row.get("source_key"),
                             "published_at": row.get("published_at"),
                             "ingested_at": row.get("ingested_at"),
                             "transcript_provenance": row.get("source_transcript_provenance"),
                             "text_status": row.get("source_text_status"),
                             "chunk_index": row.get("chunk_index")}}

    @mcp.tool(annotations=READ_ONLY)
    async def inventory(source_keys: list[str] | None = None,
                        published_after: str | None = None,
                        as_of: str | None = None,
                        content_types: list[str] | None = None) -> dict[str, Any]:
        """Audit indexed source coverage before a current-period report.

        Returns current/stale/unknown-date/not-present states, publication and poll
        timestamps, searchable-text counts, and explicit coverage gaps. A report is
        complete only when every required source is current, searchable, and has a
        successful collector heartbeat.
        """
        rows = await anyio.to_thread.run_sync(
            lambda: _client().inventory(
                source_keys=source_keys, source_types=content_types,
                published_after=published_after, as_of=as_of))
        gaps = coverage_gaps(rows)
        health_available = any(
            row.get("collector_health") not in (
                None, "not_exposed", "outcome_not_exposed", "never_ingested"
            ) for row in rows
        )
        return {"sources": rows,
                "collector_health_available": health_available,
                "coverage_complete": bool(rows) and not gaps,
                "coverage_gaps": gaps}

    @mcp.tool(annotations=READ_ONLY)
    async def finance_report_evidence(
            query: str = "market regime, growth, inflation, liquidity, positioning and risk",
            days: int = 30,
            as_of: str | None = None,
            source_keys: list[str] | None = None,
            content_types: list[str] | None = None,
            per_source_limit: int = 2) -> dict[str, Any]:
        """Build a citable evidence bundle for a last-week or last-30-day report.

        Use days=7 for last week or days=30 for the monthly view. Results are
        grouped by canonical source and include inventory health, exact UTC
        timestamps, recent excerpts, and explicit gaps. Synthesize each source's
        thesis, bullish/bearish assets, confidence, disagreements, and actionable
        implication only from the returned evidence; never hide a coverage gap.
        """
        return await anyio.to_thread.run_sync(
            lambda: finance_evidence_bundle(
                _client(), query=query, days=days, as_of=as_of,
                source_keys=source_keys, content_types=content_types,
                per_source_limit=per_source_limit))

    # ------------------------------------------------------------------
    # Native ZME tools
    # ------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def recall(query: str, k: int = 8, wide: bool = False) -> str:
        """Recall what Zeug itself wrote down — notes, analysis, compiled
        research. Use this (not search) for 'why did we decide X' questions.
        Set wide=true to also include articles and social posts."""
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

    @mcp.tool(annotations=WRITE_NOTE, auth=write_auth)
    async def remember(text: str, title: str | None = None,
                       agent: str = "zme-mcp") -> str:
        """Record a durable fact — a pitfall, decision, or incident — so it
        survives the session. Becomes searchable after the next ingest run.
        Never store secrets. Requires the zme:write scope in OAuth mode."""
        title = title or text[:60]
        new_id = await anyio.to_thread.run_sync(
            lambda: _client().remember(title, text, agent))
        return f"remembered {new_id}\n  {title}\n  (searchable after the next ingest run)"

    return mcp


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

def _http_app(mcp: FastMCP):
    """Streamable-HTTP ASGI app. In token mode, guard /mcp with
    ZME_MCP_TOKEN (Bearer header or ?token=). In OAuth mode the provider's
    own Bearer middleware and routes handle everything."""
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    token = os.environ.get("ZME_MCP_TOKEN")
    middleware = []

    if token:
        class TokenAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                path = request.url.path
                if path == "/healthz" or path.startswith("/.well-known") \
                        or path in ("/authorize", "/token", "/register"):
                    return await call_next(request)
                if token:
                    auth = request.headers.get("authorization", "")
                    given = (auth[7:] if auth.startswith("Bearer ")
                             else request.query_params.get("token"))
                    if given != token:
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        middleware.append(Middleware(TokenAuth))
    else:
        import warnings
        if not mcp.auth:
            warnings.warn("No ZME_MCP_TOKEN and no OAuth — HTTP endpoint is "
                          "UNAUTHENTICATED. Fine for localhost only.")

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request):
        return PlainTextResponse("ok")

    return mcp.http_app(path="/mcp", middleware=middleware)


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
        base_url = os.environ.get("ZME_BASE_URL") or f"http://{args.host}:{args.port}"
        mcp = build_server(http=True, base_url=base_url)
        uvicorn.run(_http_app(mcp), host=args.host, port=args.port, log_level="info")
    else:
        build_server().run()  # stdio


if __name__ == "__main__":
    main()
