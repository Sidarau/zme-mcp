"""Sharing from Zeug's memory into a client workspace: explicit, checked, approved per source, recorded.

Zeug's memory is internal by default. Nothing reaches a client workspace unless someone runs `share` for named
sources and a named project, previews them, and approves each one by id: `--accept` (publish as is, vectors
included) or `--redact` (publish a redacted copy). A clean scan never publishes on its own.

Each source is read once into a snapshot, and the snapshot is what gets scanned, previewed and published: URL,
type, title, text, and for an as-is copy every chunk's content, prefix and heading. The scan flags lines that look
internal (another client, a negotiating position, ownership or Zeug-finance talk, an assessment of a person,
personal data, credentials and access codes, keys, contact details, local paths, and any literal the operator asks
to mask). Redaction replaces spans with a known extent in place and drops every other flagged line, including a
credential whose value runs across lines and a path whose extent is unclear; then it re-chunks and re-embeds.
URLs cross only as plain http(s) without credentials, query or fragment, and only when nothing in them is flagged.
Technical chunk fields are rebuilt or checked against an allowlist. The insert and its audit row (zme_ws.shares)
commit together under a per-source lock, and a source already shared into a workspace is skipped.

The scan is a tripwire, not a judge. Preview everything and read it before approving.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from .text import EMBED_VARIANT, _vector, chunk, embed

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")   # cmd PROJECT_RE
# Names a project may mention about itself. Everything else in CLIENTS is another client and gets flagged.
OWN_NAMES: dict[str, list[str]] = {
    "opencollective": ["Open Collective", "My Open Collective", "Collective", "Collecta", "OC"],
    "ecis": ["ECIS", "Embercrest", "Total Loss"],
}
CLIENTS = ["Try Day Club", "TDC", "ChartClose", "Clearwell", "Mitchel", "Mitchie", "Carbid", "Ambula", "Mastery Estate",
           "Evangelos", "Wattfinity", "Qcells", "PRIMA", "Open Collective", "Collecta", "ECIS", "Embercrest", "Total Loss",
           "Nexwave", "Sketchy"]
TYPES = {"note", "article", "analysis", "document", "transcript", "research", "compiled_research", "social"}
VARIANTS = {"arctic-l-v1", "arctic-335m-v1"}            # vectors in the arctic-l space; any other variant is dropped
TECH_RE = re.compile(r"^[A-Za-z0-9.+_-]{1,40}$")

MASK, DROP = "mask", "drop"   # MASK: the match is replaced in place. DROP: every line the match touches goes.
_ = re.compile
_CRED_NAME = r"""["']?\b[\w-]*(?:password|passwd|passcode|secret|token|api[_ -]?key|access[_ -]?(?:code|key)|private[_ -]?key|client[_ -]?secret|credentials?)[\w-]*\b["']?"""
RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("key or token", MASK, _(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
                             r"|sk-[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abposr]-[A-Za-z0-9-]+"
                             r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")),
    ("access code", MASK, _(r"\((?:access\s+)?code:?\s+[^)\n]+\)", re.I)),
    # A credential assignment: a quoted value may run across lines (unterminated: to the end), so every line it touches goes.
    ("credential", DROP, _(_CRED_NAME + r"""\s*(?:[:=]|\bis\b)\s*(?:"(?:[^"\\]|\\.)*(?:"|\Z)|'(?:[^'\\]|\\.)*(?:'|\Z)|\S+)""", re.I)),
    ("access code", DROP, _(r"\b(?:access code|passcode|pin code|door code|gate code|wi-?fi password)\b\W{0,3}(?:[A-Za-z]+\W{1,3}){0,3}?[A-Za-z0-9-]*\d[A-Za-z0-9-]*", re.I)),
    ("email", MASK, _(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", MASK, _(r"(?<![\w+])\+\d{1,3}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?\d[\d\s.-]{6,}\d|\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b"
                      r"|(?<![\w.,+-])\d{10,11}(?![\w.,])")),
    ("local path", MASK, _(r"""file://\S+|(["'])(?:~[/\\]|/(?:Users|home|tmp|private|var|opt|etc|Volumes|mnt|root)/|[A-Za-z]:\\|\\\\)[^"'\n]*\1""")),
    ("marked internal", DROP, _(r"\b(internal only|do not share|don'?t share|confidential|private note|zeug[- ]only)\b", re.I)),
    ("negotiating position", DROP, _(r"\b(walk[- ]?away|our floor|floor of|bottom line|leverage over|negotiat\w*|bluff|our margin|markup)\b", re.I)),
    ("ownership or equity", DROP, _(r"\b(equity|cap table|vesting|dilution|founder parameters?)\b|\b\d+(?:\.\d+)?\s*%\s*(?:equity|ownership|stake|of the company)\b"
                                    r"|\b(?:ownership|equity)\s+(?:split|stake|percentage|share)\b", re.I)),
    ("Zeug finances", DROP, _(r"\b(our revenue|zeug(?: lab)?'?s? (?:revenue|runway|cash|pricing)|runway|burn rate|\$10k/mo)\b", re.I)),
    ("assessment of a person", DROP, _(r"\b(background check|evidence comparison|contribution audit|trust posture)\b", re.I)),
    ("personal data", DROP, _(r"\b(birthday|date of birth|DOB|born on|passport|home address|SSN)\b", re.I)),
]
# Unquoted local paths: masked when their end is clear, otherwise the line goes (a path with spaces can't be bounded).
UNQUOTED_PATH = _(r"""(?<![\w/.:"'])(?:~[/\\]|/(?:Users|home|tmp|private|var|opt|etc|Volumes|mnt|root)/|\b[A-Za-z]:\\|\\\\[^\s"'<>\\]+\\)[^\s"'<>)\]]*""")


@dataclass
class Flag:
    where: str     # url, source type, title, text, or chunk N <field>
    line: int
    reason: str
    action: str
    text: str


@dataclass
class Snapshot:
    source_id: str
    title: str
    source_type: str
    url: str | None
    text: str
    chunks: list[dict]


@dataclass
class Scan:
    snap: Snapshot
    flags: list[Flag] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.snap.source_id


def _spans(text: str, project: str, extra: list[str]) -> list[tuple[int, int, str, str]]:
    """Every flagged span in `text`: (start, end, reason, action), computed on the original text."""
    out: list[tuple[int, int, str, str]] = []
    for reason, action, rx in RULES:
        out += [(*m.span(), reason, action) for m in rx.finditer(text)]
    for m in UNQUOTED_PATH.finditer(text):
        last = re.split(r"[/\\]", m.group(0).rstrip("/\\"))[-1]
        ends_clearly = "." in last or not re.match(r" +[A-Za-z0-9]", text[m.end():])
        out.append((*m.span(), "local path", MASK if ends_clearly else DROP))
    own = {n.lower() for n in OWN_NAMES.get(project, [])}
    others = [c for c in CLIENTS if c.lower() not in own]
    if others:
        out += [(*m.span(), f"another client ({m.group(1)})", DROP)
                for m in re.finditer(r"\b(" + "|".join(re.escape(c) for c in others) + r")\b", text, re.I)]
    for word in extra:
        out += [(*m.span(), "operator mask", MASK) for m in re.finditer(re.escape(word), text, re.I)]
    return out


def scan_text(project: str, text: str, where: str = "text", extra: list[str] | None = None) -> list[Flag]:
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    flags = []
    for start, _end, reason, action in sorted(_spans(text, project, extra or [])):
        n = max(i for i, s in enumerate(starts) if s <= start)
        flags.append(Flag(where, n + 1, reason, action, lines[n].strip()[:160]))
    return flags


def redact(text: str, project: str, extra: list[str] | None = None) -> str:
    """Drops every line touched by a DROP span; replaces merged MASK spans in the rest with [removed]."""
    spans = _spans(text, project, extra or [])
    drops = [(s, e) for s, e, _r, a in spans if a == DROP]
    merged: list[list[int]] = []
    for s, e in sorted((s, e) for s, e, _r, a in spans if a == MASK):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, pos = [], 0
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        if any(s < max(end, start + 1) and e > start for s, e in drops):
            continue
        parts, cursor = [], start
        for s, e in merged:
            if e <= start or s >= end:
                continue
            parts += [text[cursor:max(s, start)], "[removed]"]
            cursor = min(e, end)
        parts.append(text[cursor:end])
        out.append("".join(parts))
    return "\n".join(out)


def clean_url(url: str | None) -> str | None:
    """Plain http(s) only, without credentials, query or fragment; anything else stays behind."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    return urlunsplit((parts.scheme, parts.hostname + (f":{port}" if port else ""), parts.path, "", ""))


def source_type(kind: str | None) -> str:
    return kind if kind in TYPES else "document"


def snapshot(conn, source_id: str) -> Snapshot:
    row = conn.execute("select title, source_type, url, raw_content from public.sources where id = %s", (source_id,)).fetchone()
    if not row:
        raise ValueError(f"no Zeug source {source_id}")
    chunks = conn.execute("select chunk_index, content, context_prefix, heading, token_count, embedding_v::text as embedding,"
                          " embed_variant, chunker_version from public.chunks where source_id = %s and content is not null"
                          " order by chunk_index", (source_id,)).fetchall()
    text = row["raw_content"] or ""
    if not text.strip():   # some sources keep their text only in chunks
        text = "\n\n".join(c["content"] for c in chunks)
    return Snapshot(source_id, row["title"] or "Untitled", row["source_type"] or "document", row["url"], text, chunks)


def scan_snapshot(project: str, snap: Snapshot, extra: list[str] | None = None) -> list[Flag]:
    """Flags for everything that could leave: URL, type, title, text, and every chunk field an as-is copy carries."""
    flags = scan_text(project, snap.url or "", "url", extra) + scan_text(project, snap.source_type, "source type", extra)
    flags += scan_text(project, snap.title, "title", extra) + scan_text(project, snap.text, "text", extra)
    for c in snap.chunks:
        for fld in ("content", "context_prefix", "heading"):
            if c[fld]:
                flags += scan_text(project, c[fld], f"chunk {c['chunk_index']} {fld}", extra)
    return flags


def scan(ws, project: str, source_ids: list[str], extra: list[str] | None = None) -> list[Scan]:
    """Reads every source once, in one consistent snapshot, and flags the whole outgoing payload."""
    import psycopg
    from psycopg.rows import dict_row
    if not ID_RE.match(project) or project == "internal":
        raise ValueError(f"not a client project id: {project!r}")
    with psycopg.connect(ws.dsn, row_factory=dict_row) as conn:
        conn.execute("set transaction isolation level repeatable read")
        out = []
        for sid in source_ids:
            snap = snapshot(conn, sid)
            out.append(Scan(snap, scan_snapshot(project, snap, extra)))
        return out


def prepare(s: Scan, project: str, mode: str, extra: list[str] | None = None, title: str | None = None) -> dict:
    """The exact payload that would be published (what --preview prints)."""
    snap = s.snap
    kind = source_type(snap.source_type)
    url = clean_url(snap.url)
    if url and scan_text(project, url, "url", extra):
        url = None
    if mode == "as-is":
        new_title, text = snap.title, snap.text
        chunks = []
        for c in snap.chunks:
            ok_vector = c.get("embedding") and c.get("embed_variant") in VARIANTS
            chunks.append({
                "chunk_index": int(c["chunk_index"]), "content": c["content"], "context_prefix": c.get("context_prefix"),
                "heading": c.get("heading"), "token_count": int(c["token_count"]) if c.get("token_count") is not None else None,
                "embedding": c["embedding"] if ok_vector else None, "embed_variant": c["embed_variant"] if ok_vector else None,
                "chunker_version": c["chunker_version"] if c.get("chunker_version") and TECH_RE.match(c["chunker_version"]) else None,
            })
    elif mode == "redacted":
        title_dropped = any(f.where == "title" and f.action == DROP for f in s.flags)
        new_title = (title or ("" if title_dropped else redact(snap.title, project, extra))).strip()
        if not new_title:
            raise ValueError(f"the title is flagged; give a replacement with --title {snap.source_id}=...")
        text = redact(snap.text, project, extra)
        chunks = chunk(new_title, text, kind)        # the type is normalized before it reaches any prefix
        vectors = embed([c["content"] for c in chunks]) if chunks else None
        for c, v in zip(chunks, vectors or [None] * len(chunks)):
            c["embedding"] = _vector(v)
            c["embed_variant"] = EMBED_VARIANT if v else None
    else:
        raise ValueError("mode must be as-is or redacted")
    return {"title": new_title[:300], "text": text, "url": url, "source_type": kind, "chunks": chunks}


def preview(payload: dict) -> str:
    """Every outgoing text field, as it would be written."""
    out = [f"title: {payload['title']}", f"url: {payload['url']}", f"type: {payload['source_type']}", "text:"]
    out += ["  | " + line for line in payload["text"].splitlines()]
    for c in payload["chunks"]:
        out.append(f"chunk {c['chunk_index']}: heading={c.get('heading')!r} prefix={c.get('context_prefix')!r} "
                   f"vector={'yes' if c.get('embedding') else 'no'}")
        out += ["  | " + line for line in c["content"].splitlines()]
    return "\n".join(out)


def publish(ws, project: str, s: Scan, mode: str, actor: str, payload: dict) -> tuple[str, str]:
    """Inserts the prepared payload and its audit row in one transaction, under a per-source lock."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    text = payload["text"]
    with psycopg.connect(ws.dsn, row_factory=dict_row) as conn:
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"zme_share:{project}:{s.source_id}",))
        done = conn.execute("select workspace_source_id from zme_ws.shares where workspace = %s and zeug_source_id = %s",
                            (project, s.source_id)).fetchone()
        if done:
            return "already shared", str(done["workspace_source_id"])
        schema = conn.execute("select schema_name from zme_ws.workspaces where id = %s", (project,)).fetchone()
        if not schema:
            raise ValueError(f"no workspace {project}")
        sources = sql.Identifier(schema["schema_name"], "sources")
        chunks = sql.Identifier(schema["schema_name"], "chunks")
        row = conn.execute(sql.SQL("insert into {} (url, title, source_type, summary, raw_content, content_hash, metadata, origin,"
                                   " created_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing returning id").format(sources),
                           (payload["url"], payload["title"], payload["source_type"], text[:400], text,
                            hashlib.sha256(text.encode()).hexdigest(), json.dumps({"shared_from": "zeug", "mode": mode}),
                            f"zeug:{s.source_id}", actor)).fetchone()
        if not row:
            conn.rollback()
            return "identical text or URL already in the workspace; nothing written", ""
        for c in payload["chunks"]:
            conn.execute(sql.SQL("insert into {} (source_id, chunk_index, content, context_prefix, heading, token_count, embedding_v,"
                                 " embed_variant, chunker_version) values (%s, %s, %s, %s, %s, %s, %s::extensions.vector, %s, %s)").format(chunks),
                         (row["id"], c["chunk_index"], c["content"], c.get("context_prefix"), c.get("heading"), c.get("token_count"),
                          c.get("embedding"), c.get("embed_variant"), c.get("chunker_version")))
        conn.execute("insert into zme_ws.shares (workspace, zeug_source_id, workspace_source_id, mode, flags, shared_by)"
                     " values (%s, %s, %s, %s, %s, %s)", (project, s.source_id, row["id"], mode, len(s.flags), actor))
    return "published", str(row["id"])
