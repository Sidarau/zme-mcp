# zme-mcp

**Zeug Memory Engine as an MCP server.** Hybrid retrieval (vector + lexical, RRF-fused) and a bitemporal knowledge graph over Zeug's collected knowledge — 15k+ chunks of research, notes, articles and transcripts — exposed to any MCP client: Claude, Cursor, Codex, Hermes, and **ChatGPT** (deep research + company knowledge compatible).

Measured on the Zeug corpus: hybrid R@5 0.800 · MRR 0.562 · source-recall@10 0.900 — +18.1% MRR over vector-only.

## Tools

| Tool | What it does | Write? |
|---|---|---|
| `search` | One query string → `{results: [{id, title, url}]}`. ChatGPT deep-research compatible. | no |
| `fetch` | `id` (from `search`) → full chunk text + citation URL. ChatGPT compatible. | no |
| `recall` | Zeug's own notes/analysis/research only — "why did we decide X" | no |
| `why` | Causal chain between two knowledge-graph entities | no |
| `near` | Facts around one entity, with evidence quotes | no |
| `remember` | Record a durable note (pitfall, decision, incident). The **only** write path — backed by a `SECURITY DEFINER` function that can only create notes. | creates one note |

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

## Client workspaces (ZEUG-795)

Each cmd.zeuglab.com project can get its own memory on omarchy ZME, separate from Zeug's memory and from every other client. The MCP endpoint that serves them is ZEUG-796; this is the database layer and an admin CLI.

**Layout.** Project `ecis` lives in schema `ws_ecis` (hyphens become underscores) with `sources` and `chunks` tables, the same hybrid search as ZME (HNSW cosine + RUM lexical, fused with RRF), and two roles: `zme_ws_ecis_read` and `zme_ws_ecis_write` (add-only: no update, no delete). Control functions live in schema `zme_ws`, which PostgREST does not expose.

**Two locks.**
1. *Privileges.* Only a workspace's own roles can use its schema. No workspace role holds any privilege on Zeug's memory in `public`, on pg_net, or on another workspace.
2. *Row-level security, forced,* keyed to the workspace the session declares (`zme.workspace`). A wrongly granted role still sees no rows unless the session also declares that workspace.

Services do not log in as workspace roles. cmd (and, later, the MCP server in ZEUG-796) call the narrow API in `migrations/003_cmd_gateway.sql`: one call names one workspace and one level, and the database keeps it inside that workspace. `zme_ws_gateway` is reserved and holds no memberships. The CLI and `Workspaces` in `src/zme_mcp/workspace.py` connect as postgres for administration: per call they switch to one workspace role (`set local role`) and declare that workspace (`set_config('zme.workspace', …, true)`).

**Trust boundary.** cmd is the authority on who is on which project. A leaked cmd key or a compromised cmd reaches client workspaces at the levels cmd could use, never Zeug's memory.

**Setup and checks** (as `postgres` unless noted):

```bash
psql "$ZME_DB" -f migrations/001_workspaces.sql            # idempotent; re-run after changing provision()
psql "$ZME_DB_SUPERUSER" -f migrations/002_net_hardening.sql  # once, as supabase_admin
psql "$ZME_DB" -f migrations/003_cmd_gateway.sql           # cmd gateway (zme_ws_caller); idempotent
psql "$ZME_DB" -X -v ON_ERROR_STOP=1 -f tests/workspace_isolation.sql   # 59 checks, rolls back; exits non-zero on any failure
```

**CLI** (`ZME_WS_DSN` is a libpq string; the password is injected per call):

```bash
export ZME_WS_DSN="host=100.118.214.24 port=54322 dbname=postgres user=postgres sslmode=disable"
zk() { ~/.local/bin/zeug-key run PGPASSWORD=zeuglab/zme/DB_PASSWORD -- python3 scripts/zme_ws.py "$@"; }
zk create ecis --title "ECIS"
zk add ecis --file kickoff.md --as alex@zeuglab.com
zk search ecis "who owns the Xactimate export"
zk copy ecis <zeug-source-id>          # owner copies one Zeug source in, vectors included
zk drop ecis --confirm ecis            # deletes the workspace and everything in it
```

Notes are chunked and embedded on write (ollama `snowflake-arctic-embed:335m`, the arctic-l space Zeug's memory uses), so they are searchable as soon as `add` returns. Without an embedding service, notes are stored without vectors and search uses the lexical arm.

**cmd.zeuglab.com** reaches workspaces through the public ZME front door (PostgREST) with one JWT for role `zme_ws_caller` (`migrations/003_cmd_gateway.sql`, Zeug Keys `zeuglab/zeug-cmd/ZME_WS_JWT`, minted with the PostgREST secret, valid a year). That role can execute `public.zme_ws_call` (status, search, sources, fetch, add) and `public.zme_ws_setup` and nothing else; cmd passes the project, the access level and the person's email. Notes added from cmd have no vectors until `zme_ws.py embed-pending` runs on the tailnet; until then they are found by the lexical arm.

Every role can read the catalog (table names, function bodies), so never put a secret in a function body.

**Sharing from Zeug's memory (explicit only).** Zeug's memory is internal by default; nothing reaches a client workspace unless someone runs `share` for named sources and a named project:

```bash
zk share ecis <zeug-source-id> ...                         # dry run: flags per source, nothing written
zk share ecis <ids> --preview --redact ID ...                # the exact text that would be published
zk share ecis <ids> --publish [--accept ID ...] [--redact ID ...] [--mask WORD ...] [--title ID=New title] --as alex@zeuglab.com
zk shares ecis                                             # audit: what was shared, how, by whom
```

Each source is read once into a snapshot; that snapshot is what is scanned (title, text, and every chunk's content, prefix and heading) and what is published, in one transaction with its audit row. A source already shared into the workspace is skipped. The scan (`src/zme_mcp/share.py`, regression cases in `tests/test_share.py`) flags other clients' names, negotiating positions, ownership and Zeug-finance talk, assessments of people, personal data, secrets and access codes, contact details and local paths. Nothing publishes on a clean scan: each source must be named in `--accept` (copied as is, vectors included, after you have read the preview) or `--redact`. Redacting replaces spans with a known extent (tokens, emails, phone numbers, paths, masked literals, parenthesised access codes) in place and drops every other flagged line, including credential assignments, then re-chunks and re-embeds. A flagged title needs `--title`. URLs cross only as plain http(s) without credentials, query or fragment. Every publish is recorded in `zme_ws.shares`. `--preview` prints every outgoing field: URL, type, title, text, and each chunk's heading, prefix and content. The scan is a tripwire, not a judge: read what you publish.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"0"}}}' | .venv/bin/zme-mcp
```
