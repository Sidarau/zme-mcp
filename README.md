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

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `ZME_SUPABASE_KEY` | yes | Supabase key for the ZME project. **The anon key is enough** (SELECT-only + `zme_remember`). Never deploy a service-role key on a shared machine. |
| `ZME_SUPABASE_URL` | no | Defaults to Zeug's own memory project. Point at another ZME deployment (e.g. a ClawPanel tenant DB) to serve that instead. |
| `NVIDIA_API_KEY` | no | Enables the vector arm of hybrid search. Without it, everything still works in lexical mode (which is the arm that catches exact identifiers). |
| `ZME_MCP_TOKEN` | HTTP only | Bearer token for the HTTP endpoint. ChatGPT carries it in the connector URL. Unset = unauthenticated (localhost only). |

On macOS, keys are also auto-resolved from the NoxKey keychain (one Touch ID prompt per session) when the env vars are absent.

## Install & run — local agents (stdio)

No install needed with uv:

```bash
uvx --from git+https://github.com/Sidarau/zme-mcp zme-mcp
```

or pipx: `pipx install git+https://github.com/Sidarau/zme-mcp`

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

### Connect from ChatGPT

1. Deploy somewhere with HTTPS (this repo ships a `Dockerfile` + `fly.toml`: `flyctl deploy` → `https://zme-mcp.fly.dev`).
2. In ChatGPT: **Settings → Security and login → Developer mode** → on.
3. Go to **chatgpt.com/plugins** → **+** → enter your server URL with the token:
   `https://zme-mcp.fly.dev/mcp?token=<ZME_MCP_TOKEN>`
4. Use it in chat — and in **deep research**, where the `search`/`fetch` pair makes ZME a citable knowledge source.

## Security model

- Read path is plain `SELECT` through PostgREST with whatever key you configure — use the **anon** key anywhere shared.
- The only write is `remember`, which calls `zme_remember()`: a `SECURITY DEFINER` function whose entire capability is "create one note". No UPDATE, no DELETE, no schema access.
- The HTTP layer never logs keys; the token is compared, not echoed.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"0"}}}' | .venv/bin/zme-mcp
```
