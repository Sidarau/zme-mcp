"""ZME backend — direct PostgREST RPC + NVIDIA query embeddings.

The API data path is self-contained. An enrolled Zeug host may resolve missing
credentials through its installed cloud client; other deployments inject env values.

Credential resolution order: explicit environment injection, then the enrolled
cloud vault through a private pipe. No keychain, clipboard or secret files.

Security model: the server works with the Supabase *anon* key (SELECT-only,
RLS-enforced). The only write surface is `zme_remember()`, a SECURITY DEFINER
function whose entire capability is "create one note". Never configure this
server with a service-role key on a machine you don't fully control.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SUPABASE_URL = "https://vmjqmrsnuxmcvetirylw.supabase.co"
# Embedding providers (migrated 2026-09-02): corpus space is snowflake/arctic-embed-l
# @1024 (nv-embedqa-e5-v5 EOL'd 2026-08-25, 410 Gone). Argus ollama is primary,
# NVIDIA hosted is fallback — identical weights, one space. The query-side prefix
# is applied CLIENT-SIDE so both providers produce the same vector for the same text.
OLLAMA_URL = os.environ.get("ZME_EMBED_URL", "http://100.113.216.73:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("ZME_EMBED_MODEL", "snowflake-arctic-embed:335m")
NV_URL = "https://integrate.api.nvidia.com/v1/embeddings"
NV_MODEL = os.environ.get("ZME_NV_MODEL", "snowflake/arctic-embed-l")
EMBED_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Corpus scoping. 95.6% of eve-knowledge-base is trading-video transcripts, so
# an unscoped natural-language search is mostly noise.
KNOWLEDGE_TYPES = ["note", "analysis", "compiled_research", "tweet",
                   "article", "social", "newsletter", "podcast"]
NOTE_TYPES = ["note", "analysis", "compiled_research"]


from .zeug_credentials import resolve, CredentialError


def _resolve_key(env: str, cloud_reference: str | None, what: str,
                 aliases: tuple[str, ...] = (), required: bool = True) -> str | None:
    for name in (env, *aliases):
        if os.environ.get(name):
            return os.environ[name].strip()
    if cloud_reference:
        try:
            return resolve(cloud_reference, required=required)
        except CredentialError as error:
            raise RuntimeError(f'zme-mcp: {what} unavailable ({error}); check cloud enrollment or inject {env}.') from None
    if required:
        raise RuntimeError(f'zme-mcp: missing {what}; inject {env}.')
    return None


class ZME:
    """Thin async-unaware client; tools run it via anyio.to_thread."""

    def __init__(self) -> None:
        self.base = (os.environ.get("ZME_SUPABASE_URL") or DEFAULT_SUPABASE_URL).rstrip("/") + "/rest/v1"
        self.sb_key = _resolve_key(
            "ZME_SUPABASE_KEY", "zeuglab/claw-terminal/SUPABASE_SERVICE_ROLE_JWT_TOKEN",
            "Supabase key", aliases=("SB_KEY",))
        # Embeddings are optional: without a key, search degrades to the
        # lexical arm (which is what catches exact identifiers anyway).
        self.nv_key = _resolve_key(
            "NVIDIA_API_KEY", "zeuglab/eve/NVIDIA_API_KEY",
            "NVIDIA embeddings key", aliases=("NVIDIA_KEY",), required=False)
        self.headers = {"apikey": self.sb_key,
                        "Authorization": f"Bearer {self.sb_key}",
                        "Content-Type": "application/json"}

    # -- low level ---------------------------------------------------------

    def rpc(self, fn: str, args: dict) -> Any:
        body = {k: v for k, v in args.items() if v is not None}
        req = urllib.request.Request(f"{self.base}/rpc/{fn}",
                                     data=json.dumps(body).encode(),
                                     method="POST", headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"rpc {fn} -> {e.code} {e.read().decode()[:300]}") from e

    def get(self, path: str) -> Any:
        req = urllib.request.Request(f"{self.base}/{path}", headers=self.headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    # -- embeddings ----------------------------------------------------------

    def embed_query(self, text: str) -> str | None:
        """Returns a pgvector literal, or None to degrade to lexical-only.

        Provider chain: Argus ollama (no key needed) -> NVIDIA hosted (needs key).
        The query prefix is applied here, client-side; do NOT pass input_type to
        either provider or the space diverges from the local model.
        """
        q = EMBED_QUERY_PREFIX + text
        try:  # primary: argus ollama over the tailnet
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/embed",
                data=json.dumps({"model": OLLAMA_MODEL, "input": [q]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                vec = json.loads(resp.read())["embeddings"][0]
                return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
        except (urllib.error.HTTPError, OSError, KeyError, IndexError):
            pass
        if not self.nv_key:
            return None
        body = {"model": NV_MODEL, "input": [q], "truncate": "END"}
        req = urllib.request.Request(NV_URL, data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.nv_key}",
                                              "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    vec = json.loads(resp.read())["data"][0]["embedding"]
                    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
            except (urllib.error.HTTPError, OSError):
                time.sleep(2 ** attempt)
        return None  # degrade to lexical rather than fail the search

    # -- public surface ------------------------------------------------------

    def search(self, query: str, k: int = 8, mode: str = "hybrid",
               source_types: list[str] | None = None) -> list[dict]:
        vec = self.embed_query(query) if mode in ("hybrid", "vector") else None
        rows = self.rpc("zme_search_hybrid", {
            "q_embedding": vec,
            "q_text": query if mode in ("hybrid", "lexical") else None,
            "k": k,
            "p_source_types": source_types})
        return rows or []

    def fetch_chunk(self, chunk_id: str) -> dict | None:
        rows = self.get(f"chunks?id=eq.{chunk_id}"
                        "&select=id,content,chunk_index,source_id,sources(title,url,source_type)")
        if not rows:
            return None
        row = rows[0]
        src = row.pop("sources", None) or {}
        row.update({"source_title": src.get("title"), "source_url": src.get("url"),
                    "source_type": src.get("source_type")})
        return row

    def resolve(self, name: str) -> str | None:
        return self.rpc("zme_entity_resolve", {"p_name": name}) or None

    def path(self, from_name: str, to_name: str, max_hops: int = 5) -> list[dict]:
        fa, fb = self.resolve(from_name), self.resolve(to_name)
        if not fa or not fb:
            missing = from_name if not fa else to_name
            raise RuntimeError(f"unresolved entity: {missing!r} — try the exact canonical name")
        return self.rpc("zme_path", {"p_from": fa, "p_to": fb, "p_max_hops": max_hops}) or []

    def neighborhood(self, name: str, hops: int = 1) -> list[dict]:
        ent = self.resolve(name)
        if not ent:
            raise RuntimeError(f"entity not found: {name!r}")
        return self.rpc("zme_entity_neighborhood", {"p_entity": ent, "p_hops": hops}) or []

    def remember(self, title: str, text: str, agent: str = "zme-mcp") -> str:
        """The ONLY write path — zme_remember() is SECURITY DEFINER and can
        only create one note. Searchable after the next zme_ingest run."""
        return str(self.rpc("zme_remember",
                            {"p_title": title, "p_text": text, "p_agent": agent}))
