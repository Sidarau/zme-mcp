"""Local project references and reviewed packets. No credentials, network or DB on import."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import pathlib
import re
import tempfile
from urllib.parse import urlsplit

from .share import scan_text
from .text import chunk

ID = re.compile(r"[a-z0-9][a-z0-9-]{1,40}\Z")
RESERVED = {"internal", "public", "zeug", "zeuglab", "all", "default"}
KINDS = {"note": "note", "context": "analysis", "research": "compiled_research"}
DEFAULTS = {"exclude_internal": True, "preview_required": True, "auto_publish": False}
FIELDS = {"version", "project", "actor", "kind", "title", "text", "url"}


class ValidationError(ValueError):
    """Only deliberately safe messages may be printed by the CLI."""


def handle(value: str) -> str:
    value = value.strip().lower().removeprefix("@")
    if not ID.fullmatch(value) or value in RESERVED:
        raise ValidationError("use an exact client project reference, such as @ecis")
    return value


def save_private(path: pathlib.Path, value: dict, *, replace: bool = True) -> None:
    """Private files; atomic replacement for registry edits, exclusive create for drafts."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    if not replace:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".zme-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Registry:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"version": 1, "projects": []}
        self._index()

    def _index(self) -> dict:
        if self.data.get("version") != 1 or not isinstance(self.data.get("projects"), list):
            raise ValidationError("unsupported project registry")
        index = {}
        ids = set()
        for p in self.data["projects"]:
            pid = handle(p["id"])
            if pid != p["id"] or pid in ids or not isinstance(p["title"], str):
                raise ValidationError("invalid or duplicate canonical project")
            ids.add(pid)
            for ref in [pid, *p["aliases"]]:
                if ref != handle(ref) or ref in index:
                    raise ValidationError(f"duplicate or invalid project reference: {ref}")
                index[ref] = p
        return index

    def resolve(self, reference: str) -> dict:
        p = self._index().get(handle(reference))
        if p is None:
            raise ValidationError("unknown project reference; run list, sync, or register the exact CMD project id")
        return p

    def register(self, pid: str, title: str) -> dict:
        pid = handle(pid)
        index = self._index()
        if pid in index and index[pid]["id"] != pid:
            raise ValidationError("project id collides with an existing alias")
        if not title.strip():
            raise ValidationError("project name is required")
        p = index.get(pid)
        if p is None:
            p = {"id": pid, "title": title.strip(), "aliases": []}
            self.data["projects"].append(p)
        else:
            p["title"] = title.strip()
        self._index()
        return p

    def alias(self, reference: str, alias: str) -> None:
        p, alias = self.resolve(reference), handle(alias)
        index = self._index()
        if alias in index:
            if index[alias] is p:
                return
            raise ValidationError("alias already belongs to another project")
        p["aliases"].append(alias)

    def save(self) -> None:
        self._index()
        save_private(self.path, self.data)


def references(p: dict) -> dict:
    pid = p["id"]
    return {"project": pid, "name": p["title"], "references": ["@" + x for x in [pid, *p["aliases"]]],
            "defaults": dict(DEFAULTS),
            "workspace": f"https://cmd.zeuglab.com/p/{pid}",
            "memory": f"https://cmd.zeuglab.com/p/{pid}/memory",
            "connector": f"https://mcp.zeuglab.com/w/{pid}",
            "prompt": f"Use @{pid}. Prepare a collaborator-facing note for this project only. "
                      "Keep internal strategy, finances, people assessments, other clients, personal data and access details out. "
                      "Show the complete draft before publishing. This reference does not grant access or authorize sharing."}


def initialize(registry: Registry, pid: str, title: str, aliases: list[str], target: pathlib.Path) -> dict:
    """Scaffold local owner routing for a future CMD project; never provision, grant or publish."""
    # Validate a proposed registry separately so failures leave the caller's registry unchanged.
    proposed = Registry(registry.path)
    project = proposed.register(pid, title)
    for alias in aliases:
        proposed.alias(project["id"], alias)
    if any(ord(c) < 32 for c in title) or "\n" in title:
        raise ValidationError("project name must be a single line")
    base = pathlib.Path(__file__).with_name("project_template")
    config = {"version": 1, "project": project["id"], "name": title, "aliases": project["aliases"],
              "defaults": dict(DEFAULTS), "memory": references(project)["memory"],
              "connector": references(project)["connector"], "role": "owner-routing-only"}
    files = {".zeug/memory-routing.json": json.dumps(config, indent=2) + "\n"}
    for name in ("MEMORY.md", "context.md", "research.md", "notes.md"):
        files[name] = (base / name).read_text().replace("{{PROJECT_REF}}", "@" + project["id"])
    # Preflight every destination, including dangling symlinks, before creating any files.
    if target.is_symlink() or (target / ".zeug").is_symlink():
        raise ValidationError("template target cannot be a symlink")
    for relative in files:
        path = target / relative
        if path.exists() or path.is_symlink():
            raise ValidationError("template would overwrite existing files; choose an empty target")
    for relative, text in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
    proposed.save()
    registry.data = proposed.data
    return {"project": project["id"], "target": str(target), "files": list(files), "defaults": dict(DEFAULTS),
            "provisioned": False, "published": False}


def packet(project: str, actor: str, kind: str, title: str, text: str, url: str | None = None) -> dict:
    doc = {"version": 1, "project": handle(project), "actor": actor, "kind": kind,
           "title": title, "text": text, "url": url}
    payload(doc)  # validate before saving anything
    return doc


def payload(doc: dict) -> dict:
    if not isinstance(doc, dict) or set(doc) != FIELDS or doc["version"] != 1:
        raise ValidationError("unsupported draft fields or version")
    pid = handle(doc["project"])
    if pid != doc["project"] or doc["kind"] not in KINDS:
        raise ValidationError("invalid project or note kind")
    if not isinstance(doc["actor"], str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", doc["actor"]):
        raise ValidationError("name the publishing actor with --as EMAIL")
    title, text, url = doc["title"], doc["text"], doc["url"]
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300 or title != title.strip():
        raise ValidationError("title must be 1–300 characters without outer whitespace")
    if not isinstance(text, str) or not 20 <= len(text.strip()) <= 100_000 or text != text.strip():
        raise ValidationError("text must be 20–100000 characters without outer whitespace")
    if url is not None:
        if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValidationError("source URL must be plain public http(s)")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValidationError("invalid public source URL") from None
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValidationError("source URL must be http(s), without credentials, query or fragment")
        host = parsed.hostname.lower().rstrip(".")
        if (host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".ts.net"))
                or (host.endswith(".zeuglab.com") and host != "www.zeuglab.com")):
            raise ValidationError("source URL must point to public evidence, not an internal service")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host):
                raise ValidationError("ambiguous numeric source address") from None
        else:
            if not address.is_global:
                raise ValidationError("source URL must point to public evidence, not a private address")
    # Citation is evidence in the note, not its unique DB identity. Multiple notes may cite one source.
    if url:
        text += f"\n\nSource: {url}"
    if len(text) > 100_000:
        raise ValidationError("note including its citation exceeds 100000 characters")
    rows = chunk(title, text, KINDS[doc["kind"]])
    outgoing = {"title": title, "text": text, "url": None, "source_type": KINDS[doc["kind"]], "chunks": rows}
    # Inspect every outgoing textual field, including generated headings/prefixes. No flag contents in errors/logs.
    flags = []
    for field in ("title", "text", "url", "source_type"):
        flags += scan_text(pid, outgoing[field] or "", field)
    if url:
        from urllib.parse import unquote
        flags += scan_text(pid, unquote(url), "decoded URL")
    for row in rows:
        for field in ("content", "heading", "context_prefix"):
            flags += scan_text(pid, row[field] or "", field)
    # Conservative hold for known scanner gaps (ZEUG-808). Drafts containing these require a fresh authored summary.
    for value in (title, text, url or ""):
        if re.search(r"(?im)authorization\s*:|\b(?:api[_ -]?key|password|secret|token)\s*[:=]|/Users/|[A-Z]:\\", value):
            raise ValidationError("draft contains access details or local paths; write a collaborator summary first")
    if flags:
        reasons = ", ".join(sorted({f.reason for f in flags}))
        raise ValidationError(f"draft held ({reasons}); rewrite the selected text for this audience and preview again")
    return outgoing


def approval(doc: dict) -> str:
    body = {"draft": doc, "outgoing": payload(doc), "metadata": {"route": "reviewed-project-packet-v1", "kind": doc["kind"]}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def preview(doc: dict) -> dict:
    return {"project": doc["project"], "actor": doc["actor"], "audience": "project collaborators and connected agents",
            "outgoing": payload(doc), "metadata": {"route": "reviewed-project-packet-v1", "kind": doc["kind"]},
            "approve": approval(doc), "review": "Read every field. A clean scan does not establish that this is suitable to share."}


def publish(doc: dict, project: str, actor: str, approved: str, workspace_factory) -> dict:
    """Validate before credential/DB access; an alias cannot retarget a previously reviewed draft."""
    outgoing = payload(doc)
    if handle(project) != doc["project"] or actor != doc["actor"] or approved != approval(doc):
        raise ValidationError("project, actor or content changed; preview again and approve the new digest")
    project = doc["project"]
    ws = workspace_factory()
    result = ws.remember(project, outgoing["title"], outgoing["text"], source_type=outgoing["source_type"],
                         url=outgoing["url"], actor=actor,
                         metadata={"route": "reviewed-project-packet-v1", "kind": doc["kind"]})
    return {"project": project, **result}
