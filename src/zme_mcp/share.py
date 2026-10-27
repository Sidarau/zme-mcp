"""Sharing from Zeug's memory into a client workspace: explicit, checked, recorded.

Zeug's memory is internal by default. Nothing reaches a client workspace unless someone runs `share` for named
sources and a named project. Each source is first scanned for lines that look internal: another client's name, a
negotiating position, ownership or money talk about Zeug itself, secrets, contact details. A source with no flags can
be published as is (its vectors come along); a flagged one is published only after a person accepts it, or with the
flagged lines removed. Every publish is recorded in zme_ws.shares.

The scan is a tripwire, not a judge: a clean scan does not prove a source is shareable. Read what you publish.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .workspace import Workspaces, check_id

# Names a project may mention about itself. Everything else in CLIENTS is another client and gets flagged.
OWN_NAMES: dict[str, list[str]] = {
    "opencollective": ["Open Collective", "My Open Collective", "Collective", "Collecta", "OC"],
    "ecis": ["ECIS", "Embercrest", "Total Loss"],
}
CLIENTS = ["Try Day Club", "TDC", "ChartClose", "Chartclose", "Clearwell", "Mitchel", "Mitchie", "Carbid", "Ambula",
           "Mastery Estate", "Evangelos", "Wattfinity", "Qcells", "PRIMA", "Open Collective", "Collecta", "ECIS",
           "Embercrest", "Total Loss", "Nexwave", "Sketchy"]

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("marked internal", re.compile(r"\b(internal only|do not share|don'?t share|confidential|private note|zeug[- ]only)\b", re.I)),
    ("negotiating position", re.compile(r"\b(walk[- ]?away|our floor|floor of|bottom line|leverage over|negotiat\w*|bluff|our margin|markup)\b", re.I)),
    ("ownership or equity", re.compile(r"\b(equity|stake|ownership|cap table|vesting|dilution|founder parameters?)\b", re.I)),
    ("Zeug finances", re.compile(r"\b(our revenue|zeug(?: lab)?'?s? (?:revenue|runway|cash|pricing)|runway|burn rate|\$10k/mo)\b", re.I)),
    ("assessment of a person", re.compile(r"\b(track record|background check|evidence comparison|contribution audit|red flags?|trust posture)\b", re.I)),
    ("secret", re.compile(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-\S+|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.\S*|password\s*[:=]\s*\S+|access code[:\s]+[^\s,;)]+|\(code [^\s,;)]+)", re.I)),
    ("personal data", re.compile(r"\b(birthday|date of birth|DOB|born on|passport|home address|SSN)\b", re.I)),
    ("local path", re.compile(r"(/(?:home|Users)/[^\s)\]]+)")),
    # Emails, and phone numbers written as phone numbers (not dates or amounts).
    ("contact detail", re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+\d{1,3}[ .-]?\d[\d .-]{7,}\d|\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b)")),
]


# Flags for these are fixed in place (the match is replaced); the rest drop the whole line when redacting.
MASKED = {"secret", "contact detail", "local path"}


def mask(text: str, extra: list[str] | None = None) -> str:
    """Replaces secrets, contact details and local paths, plus any literal in `extra` (e.g. a deck password the
    patterns cannot recognize), with [removed]."""
    for name, rx in RULES:
        if name in MASKED:
            text = rx.sub("[removed]", text)
    for word in extra or []:
        text = re.sub(re.escape(word), "[removed]", text, flags=re.I)
    return text


@dataclass
class Flag:
    line: int
    reason: str
    text: str


@dataclass
class Scan:
    source_id: str
    title: str
    source_type: str
    text: str
    flags: list[Flag] = field(default_factory=list)


def scan_text(project: str, text: str) -> list[Flag]:
    own = {n.lower() for n in OWN_NAMES.get(project, [])}
    others = [c for c in CLIENTS if c.lower() not in own]
    other_re = re.compile(r"\b(" + "|".join(re.escape(c) for c in others) + r")\b") if others else None
    flags: list[Flag] = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        reasons = [name for name, rx in RULES if rx.search(line)]
        if other_re and (m := other_re.search(line)):
            reasons.append(f"another client ({m.group(1)})")
        for r in reasons:
            flags.append(Flag(i, r, line.strip()[:160]))
    return flags


def load(conn: psycopg.Connection, source_id: str) -> tuple[str, str, str]:
    row = conn.execute("select title, source_type, raw_content from public.sources where id = %s", (source_id,)).fetchone()
    if not row:
        raise ValueError(f"no Zeug source {source_id}")
    text = row["raw_content"] or ""
    if not text.strip():   # some sources keep their text only in chunks
        text = "\n\n".join(r["content"] for r in conn.execute(
            "select content from public.chunks where source_id = %s order by chunk_index", (source_id,)).fetchall())
    return row["title"] or "Untitled", row["source_type"] or "document", text


def scan(ws: Workspaces, project: str, source_ids: list[str]) -> list[Scan]:
    check_id(project)
    with psycopg.connect(ws.dsn, row_factory=dict_row) as conn:
        out = []
        for sid in source_ids:
            title, kind, text = load(conn, sid)
            s = Scan(sid, title, kind, text)
            s.flags = scan_text(project, title + "\n" + text)
            out.append(s)
        return out


def publish(ws: Workspaces, project: str, s: Scan, mode: str, actor: str, extra_mask: list[str] | None = None) -> str:
    """mode: 'as-is' (copy with vectors) or 'redacted' (secrets, contact details and local paths masked, other flagged
    lines removed; re-chunked and embedded)."""
    if mode == "as-is":
        new_id = ws.copy_from_zeug(project, s.source_id, actor=actor)
    elif mode == "redacted":
        drop = {f.line for f in s.flags if f.line > 1 and f.reason not in MASKED}   # line 1 of the scan is the title
        kept = mask("\n".join(l for i, l in enumerate(s.text.splitlines(), 2) if i not in drop), extra_mask)
        new_id = ws.remember(project, s.title, kept, source_type=s.source_type, actor=actor,
                             metadata={"shared_from": "zeug", "redacted_lines": len(drop)})["id"]
    else:
        raise ValueError("mode must be as-is or redacted")
    with psycopg.connect(ws.dsn) as conn:
        schema = conn.execute("select schema_name from zme_ws.workspaces where id = %s", (project,)).fetchone()[0]
        # Zeug's own file URLs (file://, local paths) say where things live on Alex's machines; they stay behind.
        conn.execute(sql.SQL("update {} set origin = %s, url = case when url ~* '^(file:|/)' then null else url end"
                             " where id = %s").format(sql.Identifier(schema, "sources")), (f"zeug:{s.source_id}", new_id))
        conn.execute("insert into zme_ws.shares (workspace, zeug_source_id, workspace_source_id, mode, flags, shared_by)"
                     " values (%s, %s, %s, %s, %s, %s)", (project, s.source_id, new_id, mode, len(s.flags), actor))
    return new_id
