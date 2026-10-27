"""Chunking and embedding for workspace notes, in the same shapes as ZME's ingest. No database driver needed."""
from __future__ import annotations

import json
import os
import re
import urllib.request

EMBED_URL = os.environ.get("ZME_EMBED_URL", "http://100.113.216.73:11434").rstrip("/")   # Argus (M1)
EMBED_MODEL = os.environ.get("ZME_EMBED_MODEL", "snowflake-arctic-embed:335m")
EMBED_VARIANT = "arctic-l-v1"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

CHUNKER_VERSION = "md-h+window-v1"
MAX_CHARS = int(460 * 3.6)          # 460 tokens: the model truncates at 512
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


# -- chunking and embedding (same shapes as zme_ingest.py) ------------------------------------------------------------

def _sections(text: str) -> list[tuple[str, str]]:
    marks = [(m.start(), m.group(2).strip()) for m in HEADING_RE.finditer(text)]
    if not marks:
        return [("", text)]
    out = [("", text[:marks[0][0]].strip())] if marks[0][0] > 0 else []
    for i, (pos, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((title, text[pos:end].strip()))
    return [(h, b) for h, b in out if b]


def _window(text: str) -> list[str]:
    chunks, cur = [], ""
    for p in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not p:
            continue
        while len(p) > MAX_CHARS:                 # an oversized paragraph is cut at a space
            cut = p.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + len(p) + 2 > MAX_CHARS:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def chunk(title: str, text: str, source_type: str) -> list[dict]:
    rows = []
    for heading, body in _sections(text):
        for piece in _window(body):
            prefix = " — ".join(b for b in (title.strip(), heading, f"[{source_type}]") if b)[:600]
            rows.append({"chunk_index": len(rows), "content": piece, "context_prefix": prefix,
                         "heading": heading or None, "token_count": int(len(piece) / 3.6),
                         "chunker_version": CHUNKER_VERSION})
    return rows


def embed(texts: list[str], query: bool = False) -> list[list[float]] | None:
    """Arctic vectors from ollama, or None when the service is unreachable (callers degrade to lexical)."""
    if query:
        texts = [QUERY_PREFIX + t for t in texts]
    req = urllib.request.Request(f"{EMBED_URL}/api/embed", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode())
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            vectors = json.loads(resp.read())["embeddings"]
    except (OSError, ValueError, KeyError):
        return None
    return vectors if len(vectors) == len(texts) else None


def _vector(v: list[float] | None) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.7f}" for x in v) + "]"
