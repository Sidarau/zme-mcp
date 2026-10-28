# Project references and collaborator context

Use ZME's internal memory for your research, working notes and delivery knowledge. Use a CMD project's memory for the material its collaborators and their connected agents should see. Linear owns task status; Git owns code.

`@ecis` means the exact CMD project id `ecis`. `@oc` can be a local alias for `opencollective`. These are explicit instructions for this tool and a convention for agent prompts. Typing an alias into arbitrary ChatGPT or Claude chats does not select a connector or grant access.

## Owner setup

From a checkout of `Sidarau/zme-mcp`:

```sh
uv venv .venv
uv pip install --python .venv/bin/python 'psycopg[binary]>=3.2'
chmod +x scripts/zme-project
scripts/zme-project sync
scripts/zme-project alias oc opencollective
scripts/zme-project list
scripts/zme-project ref @oc
```

`sync` reads the provisioned workspace registry and saves local references at `~/.config/zme/projects.json`, preserving aliases. It does not create workspaces or publish notes. `list` and `ref` are offline and may reflect older registry data. References provide the CMD project link, memory link, connector URL and a prompt you can copy.

For a CMD project whose memory is not enabled yet, copy its exact id from the CMD URL, then use `register <id> --name 'Project name'`. This only adds a local reference. Turn on memory through the project's CMD page before using it. The alias registry is a convenience, not an authorization boundary; server-side permissions still apply. No fuzzy matching, implicit project, or internal-memory destination exists in this tool.

Only the owner launcher retrieves the existing DB credential through Zeug Keys, once per live command. Offline register, list, alias, ref, draft and preview need no credentials. Keep the registry and drafts outside Git. For another Python environment, set `ZME_PROJECT_PYTHON`. The launcher needs an enrolled owner host and tailnet access; collaborators must use their own CMD connection instead.

## Daily commands

For a named project, excluding internal context and showing a collaborator draft are defaults. You can say “Prepare research for @project” without repeating those instructions. Preparing a draft never authorizes publication.

```sh
scripts/zme-project search @ecis 'What did we agree about exports?'
scripts/zme-project sources @oc
scripts/zme-project ref @ecis
```

Useful agent prompts:

> Use @ecis. Find the current export requirements in project memory and cite the sources.

> Prepare research for @oc. Summarize the relevant findings, public sources, limitations and project implications. Keep the draft internal until I review it.

> Prepare a collaborator note for @ecis from this discussion. Include decisions and open questions. Show the exact outgoing text before publishing.

An agent using this convention must resolve the reference first, then use the named project connection or tool. It must not search all of Zeug's internal memory on a collaborator's behalf. The general ZME connector is the owner's internal corpus; do not distribute it to clients. If it needs reauthentication, reconnect it separately; project links do not repair its OAuth state.

## New context, research and notes

Write a selected summary for the audience first. Do not feed a transcript, session log or internal handoff straight into project memory. The file should contain only the text collaborators should receive:

| Kind | Include |
| --- | --- |
| `context` | Agreed goals, constraints, terminology, requirements and decisions |
| `research` | Findings, dated public citations, uncertainty and relevance to the project |
| `note` | Meeting conclusions, open questions and agreed next steps |

Leave internal strategy, negotiating positions, margins, studio finances, ownership strategy, assessments of people, other clients, personal data, credentials and local paths out. Write a fresh summary when the source mixes these with useful content. Retain qualifications: a hypothesis must not become a confirmed fact through summarization.

```sh
scripts/zme-project draft @ecis --kind research \
  --file selected-summary.md --title 'Export research' \
  --url https://example.org/public-research \
  --as alex@zeuglab.com --out /tmp/ecis-review.json
scripts/zme-project preview /tmp/ecis-review.json
# Read the full preview, then paste its approve digest explicitly:
scripts/zme-project publish @ecis --draft /tmp/ecis-review.json \
  --as alex@zeuglab.com --approve DIGEST_FROM_PREVIEW
scripts/zme-project sources @ecis
```

Draft creation and preview are local. Flagged material is held for rewriting. This flow does not automatically redact a fresh note or certify it safe. Public citation URLs cannot contain credentials, queries, fragments, local addresses or known internal service hosts. The selected title and text cross with the visible note kind and route metadata. A public citation is appended to the outgoing text as `Source: URL`, as shown in the preview. It is not used as a unique note identity, so separate findings may cite the same source. Raw source metadata and internal provenance are not copied.

The preview shows the destination, author, complete text, URL, source type, generated chunks and metadata. Its SHA-256 digest covers those fields. A changed project, alias target, author, text or generated payload requires a new preview and approval. Draft files have owner-only permissions and are not overwritten. Publication is an explicit add through the existing workspace API; it does not enable memory, edit previous notes or mirror internal updates. Identical notes follow that API's deduplication rules. If a result is uncertain, inspect sources before retrying. Embeddings are derived on publication; without the service, lexical search still works.

## Existing internal ZME sources

Use the existing share tool, which supports redaction and records a share audit. The alias wrapper forwards the exact canonical project:

```sh
scripts/zme-project share @oc SOURCE_ID --as alex@zeuglab.com
scripts/zme-project share @oc SOURCE_ID --redact SOURCE_ID --preview --as alex@zeuglab.com
# Read the preview before providing SOURCE_ID=DIGEST:
scripts/zme-project share @oc SOURCE_ID --redact SOURCE_ID --publish \
  --approve SOURCE_ID=DIGEST --as alex@zeuglab.com
```

The scanner is a tripwire. ZEUG-808 tracks known redaction gaps; review every field, including title and source URL. Prefer a newly authored summary for mixed internal material. Existing `share` behavior and its audit are unchanged by this wrapper. No content moves simply because you mentioned a project, created an alias or ran a preview.

## Collaborator connection

Open the project's CMD memory page and use **Connect** for that person's tools. Browser apps use the project OAuth connector where available; terminal tools use the project's installer and a per-person access key. Verify availability and the access level on that page. Never paste keys into chat or project memory. Each collaborator should receive only their project's connection. The owner's local aliases and database tooling are not a client setup package.

This branch adds local owner tooling. It does not add autocomplete to CMD, a ChatGPT slash command, an automatic sync job, or a new authentication path. A future CMD project picker can use the same exact-id convention after review.

## Reuse for a future project

```sh
zme-project init sample-project --name 'Sample project' --alias sample --target ./sample-work
```

Use the exact CMD project id. The command creates owner-only routing metadata in `.zeug/memory-routing.json`, a `MEMORY.md` policy and three local draft starting points. It does not create a server workspace, change permissions, add credentials or publish content. It refuses existing target files and preserves the collaborator profile `.zeug/project.json`. The template carries the same draft defaults to each future project.

Fleet agents receive the portable runtime and this default policy through the shared `zme` skill. Existing running chats must re-read the updated skill, or start a new chat. A browser refresh does not update an agent's loaded instructions. Connector reauthentication is a separate action.
