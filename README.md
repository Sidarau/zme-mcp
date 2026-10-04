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

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"0"}}}' | .venv/bin/zme-mcp
```

## Durable OAuth operations

OAuth clients, authorization codes, access and refresh tokens, and rotation links
live in SQLite at `ZME_OAUTH_DB` (local default:
`~/.local/state/zme-mcp/oauth.sqlite3`). The database contains credentials: keep
it private, never commit it, and never print its rows. Access token expiry leaves
its refresh grant available. Code exchange, rotation and revocation commit
atomically. `/revoke` uses the SDK's client-authenticated revocation handler.

Fly runs exactly one machine with the encrypted `zme_oauth` volume mounted at
`/data`. Create the volume in `sjc` before first deployment and deploy with
`fly deploy --ha=false`; verify machine count and mount afterwards. Do not scale
independent replicas with separate OAuth databases. Volume snapshots are enabled;
restoring an old snapshot can resurrect revoked tokens, so invalidate restored
grants before reopening service. A persistent volume does not make this instance
highly available. Existing credentials lost by the former memory-only process
require one fresh ChatGPT sign-in.

Validation:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

`scripts/verify_restart.py MACHINE_ID` performs a live, read-only search/fetch
canary and an actual Fly restart, then revokes its own grant. Inject
`VERIFY_OWNER_PASS` and `FLY_API_TOKEN` with `zeug-key`, never on command arguments.
OAuth secrets remain in the canary process's memory and are not logged.
