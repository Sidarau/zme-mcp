# zme-mcp

**Zeug Memory Engine as an MCP server.** Hybrid retrieval (vector + lexical, RRF-fused) and a bitemporal knowledge graph over Zeug's collected knowledge — 15k+ chunks of research, notes, articles and transcripts — exposed to any MCP client: Claude, Cursor, Codex, Hermes, and **ChatGPT** (deep research + company knowledge compatible).

Measured on the Zeug corpus: hybrid R@5 0.800 · MRR 0.562 · source-recall@10 0.900 — +18.1% MRR over vector-only.

## Tools

| Tool | What it does | Write? |
|---|---|---|
| `search` | Query plus optional publication/source/content filters → citable results with freshness metadata. Calling it with only one query string remains ChatGPT deep-research compatible. | no |
| `fetch` | `id` (from `search`) → full chunk text, citation URL, canonical source, publication/ingestion times, and transcript state. | no |
| `inventory` | Source-level indexed-content coverage: current/stale/unknown-date/not-present, newest timestamps, and counts. | no |
| `finance_report_evidence` | One source-grouped, citable evidence bundle for a 7- or 30-day market report, including explicit coverage gaps. | no |
| `recall` | Zeug's own notes/analysis/research only — "why did we decide X" | no |
| `why` | Causal chain between two knowledge-graph entities | no |
| `near` | Facts around one entity, with evidence quotes | no |
| `remember` | Record a durable note (pitfall, decision, incident). The **only** write path — backed by a `SECURITY DEFINER` function that can only create notes. | creates one note |

### Current-period and exact-source search

`search` accepts these optional structured fields:

- `published_after` / `published_before`: RFC 3339 timestamps, enforced against
  publication/event time (never ingestion time);
- `source_keys`: canonical source keys or registered aliases;
- `content_types`: ZME source types such as `article`, `social`, `podcast`, or
  `video`. Video remains opt-in for ordinary searches so the large transcript
  corpus does not swamp non-video research.
- `per_source_cap`: maximum results from one canonical publisher (default 3;
  automatically relaxed for an explicit single-source query).
- `exclude_short_form`: excludes YouTube Shorts by default; set false only when
  short-form video is intentionally part of the evidence set.

When a date or source filter is requested, ZME fails closed: a record without
enough metadata to prove eligibility is excluded. This prevents an old,
high-similarity item from appearing in a “last 30 days” report.

Example tool input:

```json
{
  "query": "inflation, growth, liquidity, positioning and market regime",
  "published_after": "2026-07-17T00:00:00Z",
  "published_before": "2026-08-16T23:59:59Z",
  "source_keys": ["kobeissi", "macrovoices", "forward_guidance"],
  "content_types": ["article", "newsletter", "podcast"]
}
```

The ingestion pipeline should persist canonical `source_key` and
`published_at` fields for every source. Legacy X daily bundles can be normalized
from their canonical `x://account/YYYY-MM-DD` URL; undated legacy sources are
intentionally omitted from filtered reports until backfilled.

Call `inventory` before preparing a current-period report. It is intentionally
honest about its boundary: a legacy channel check without a success/error ledger
is `outcome_not_exposed`; no observed check is `never_ingested`. A recent indexed
document proves content coverage; it does not prove the collector is healthy.
Current YouTube metadata without searchable captions is `metadata_only`; until an
authorized transcript provider is configured, inventory reports
`configuration_required` rather than implying that video text is available.

The additive migration in
[`sql/20260816_finance_freshness.sql`](./sql/20260816_finance_freshness.sql)
adds that heartbeat ledger, canonical publication/source metadata, and the native
`zme_source_inventory` RPC. During rollout, the MCP uses the native RPC when
available and falls back to the legacy source-table audit only when the RPC is
not yet deployed.

Apply [`sql/20260816_finance_source_seed.sql`](./sql/20260816_finance_source_seed.sql)
after the migration to register the initial required and optional finance sources.
The seed is idempotent and contains no credentials.

### Finance discovery workers

The Supabase functions under [`supabase/functions`](./supabase/functions) make
publisher ingestion observable and resumable:

- `zme-finance-discovery` polls the official MacroVoices RSS feed, resolves its
  publisher-hosted transcript PDF links, records exact publication timestamps,
  and writes a success/error heartbeat without treating metadata as full text;
- `zme-finance-transcripts` processes one pending transcript per invocation,
  extracts text with pinned `unpdf@1.8.1`, records `publisher_pdf` provenance,
  and invokes `zme-index` only after extraction succeeds;
- `zme-lyn-alden` polls the publisher's public WordPress RSS feed, extracts the
  public article body, and records an exact publication timestamp. This allows
  the inventory to distinguish a healthy but quiet publisher from a failed
  collector;
- `zme-index` writes long transcripts in bounded chunk batches so one large
  vector insert does not exceed the database statement timeout.

The corresponding cron scripts use a publishable key stored in Supabase Vault;
no API key is committed. Discovery runs hourly at minute 12 and one transcript
job runs at minute 18. The first production backfill indexed five MacroVoices
transcripts (264 chunks), including four documents in the 30-day window.
The initial Lyn Alden run indexed the latest public article into seven chunks;
because it was published July 15, a July 17–August 16 inventory correctly marks
the content stale while keeping collector health healthy. Known missing paid
feeds are bootstrapped as `not_entitled` by
[`sql/20260816_finance_entitlement_status.sql`](./sql/20260816_finance_entitlement_status.sql),
instead of being conflated with collector failure.

## Authentication — the zeuglab → ClawPanel auth model (alpha)

This server is the **alpha ring** of the ecosystem auth design:

| Ring | Where | Auth |
|---|---|---|
| **alpha** | zeuglab ecosystem (this server) | OAuth 2.1 + optional legacy token |
| **beta** | ClawPanel | OAuth only; stores move from memory to Postgres |
| **prod** | ClawPanel multi-tenant | OAuth + per-profile labels, RLS-backed |

**OAuth 2.1, self-contained** (no external IdP): DCR, PKCE, authorization codes,
refresh tokens, revocation — the full flow ChatGPT performs. `/authorize` shows a
zeuglab login form; profiles are configured server-side:

```bash
ZME_AUTH=oauth
ZME_BASE_URL=https://zme-mcp.fly.dev
ZME_OAUTH_PROFILES='{"alex":{"secret":"…","level":"owner"},
                     "guest":{"secret":"…","level":"viewer"}}'
```

**Profile levels → scopes:** `owner`/`operator` get `zme:read` + `zme:write`;
`viewer` gets `zme:read`. The server **forces scopes from the profile** — client
requests are never trusted. A viewer's `tools/list` doesn't even contain
`remember`. In beta the in-memory stores become tables and passphrases become
Supabase sessions; the scope contract stays identical, so alpha clients keep working.

Legacy token mode (`ZME_AUTH=token` + `ZME_MCP_TOKEN`) still works in the alpha
ring for stdio-style clients that can't do OAuth; it is a startup error from
ring `beta` onwards.

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `ZME_SUPABASE_KEY` | yes | Supabase key for the ZME project. **The anon key is enough** (SELECT-only + `zme_remember`). Never deploy a service-role key on a shared machine. |
| `ZME_SUPABASE_URL` | no | Defaults to Zeug's own memory project. Point at another ZME deployment (e.g. a ClawPanel tenant DB) to serve that instead. |
| `NVIDIA_API_KEY` | no | Enables the vector arm of hybrid search. Without it, everything still works in lexical mode (which is the arm that catches exact identifiers). |
| `ZME_AUTH` | no | `oauth` (default when profiles are set) or `token` (alpha ring only). |
| `ZME_OAUTH_PROFILES` | oauth | JSON map of profile name → `{secret, level}`. |
| `ZME_BASE_URL` | oauth | Public URL of the server (for OAuth metadata + redirects). |
| `ZME_RING` | no | `alpha` (default) / `beta` / `prod` — release channel gates. |
| `ZME_MCP_TOKEN` | token mode | Shared bearer for the HTTP endpoint. |

On macOS, keys are also auto-resolved from the NoxKey keychain (one Touch ID prompt per session) when the env vars are absent.

## Install & run — local agents (stdio)

No install needed with uv (any machine with Python):

```bash
uvx --from git+https://github.com/Sidarau/zme-mcp zme-mcp
```

or pipx: `pipx install git+https://github.com/Sidarau/zme-mcp` ·
PyPI (`uvx zme-mcp`) once the package is published there.

**Claude Code** — `claude mcp add zme -- uvx --from git+https://github.com/Sidarau/zme-mcp zme-mcp`

**Claude Desktop / Cursor** (`mcpServers` JSON):

```json
{
  "zme": {
    "command": "uvx",
    "args": ["--from", "git+https://github.com/Sidarau/zme-mcp", "zme-mcp"],
    "env": { "ZME_SUPABASE_KEY": "…", "NVIDIA_API_KEY": "…" }
  }
}
```

## Run as a service (HTTP) — for ChatGPT

```bash
zme-mcp --http --host 0.0.0.0 --port 8000
# streamable HTTP endpoint: POST /mcp · health: GET /healthz
```

### Connect from ChatGPT (OAuth)

1. Server is live at `https://zme-mcp.fly.dev` (this repo ships `Dockerfile` + `fly.toml`: `flyctl deploy`).
2. In ChatGPT: **Settings → Security and login → Developer mode** → on.
3. Go to **chatgpt.com/plugins** → **+** → enter `https://zme-mcp.fly.dev/mcp`.
4. ChatGPT discovers the OAuth metadata, registers itself via DCR, and opens
   the zeuglab login form — sign in with your profile (`alex` + passphrase).
   The token it gets back carries your level's scopes.
5. Use it in chat — and in **deep research**, where the `search`/`fetch` pair makes ZME a citable knowledge source.

## Security model

- Read path is plain `SELECT` through PostgREST with whatever key you configure — use the **anon** key anywhere shared.
- The only write is `remember`, which calls `zme_remember()`: a `SECURITY DEFINER` function whose entire capability is "create one note". No UPDATE, no DELETE, no schema access.
- The HTTP layer never logs keys; the token is compared, not echoed.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"0"}}}' | .venv/bin/zme-mcp
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
