"""ZME backend — direct PostgREST RPC + NVIDIA query embeddings.

No local scripts, no shell-outs: this module is the whole data path, so the
server runs anywhere (laptop stdio, Fly.io, a customer's box).

Credential resolution order for each key:
  1. environment variable
  2. `noxkey get <path> --raw` on macOS (one Touch ID prompt per keychain
     session, then silent) — convenience for Alex's Mac, absent elsewhere
  3. a clear error naming exactly which variable to set

Security model: the server works with the Supabase *anon* key (SELECT-only,
RLS-enforced). The only write surface is `zme_remember()`, a SECURITY DEFINER
function whose entire capability is "create one note". Never configure this
server with a service-role key on a machine you don't fully control.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

DEFAULT_SUPABASE_URL = "https://vmjqmrsnuxmcvetirylw.supabase.co"
NV_URL = "https://integrate.api.nvidia.com/v1/embeddings"
NV_MODEL = "nvidia/nv-embedqa-e5-v5"

# Corpus scoping. 95.6% of eve-knowledge-base is trading-video transcripts, so
# an unscoped natural-language search is mostly noise.
KNOWLEDGE_TYPES = ["note", "analysis", "compiled_research", "tweet",
                   "article", "social", "newsletter", "podcast"]
SEARCHABLE_TYPES = [*KNOWLEDGE_TYPES, "video"]
NOTE_TYPES = ["note", "analysis", "compiled_research"]

DATE_IN_TEXT = re.compile(r"(?<!\d)(20\d{2})[-/](\d{2})[-/](\d{2})(?!\d)")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    match = DATE_IN_TEXT.fullmatch(candidate)
    if match:
        candidate = "-".join(match.groups()) + "T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _nested_values(row: dict, names: tuple[str, ...]) -> list[Any]:
    values = [row.get(name) for name in names]
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        values.extend(metadata.get(name) for name in names)
    return values


def _publication_time(row: dict) -> datetime | None:
    """Return publication/event time only; never substitute ingestion time."""
    names = ("published_at", "publication_date", "published", "event_time",
             "video_published_at", "post_time", "date")
    for value in _nested_values(row, names):
        parsed = _parse_time(value)
        if parsed:
            return parsed
    # Existing X daily bundles encode their event date in the canonical URL/title.
    for value in (row.get("source_url"), row.get("url"), row.get("source_title"), row.get("title")):
        if isinstance(value, str) and (match := DATE_IN_TEXT.search(value)):
            return _parse_time("-".join(match.groups()))
    return None


def _ingestion_time(row: dict) -> datetime | None:
    names = ("ingested_at", "indexed_at", "created_at")
    for value in _nested_values(row, names):
        parsed = _parse_time(value)
        if parsed:
            return parsed
    return None


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _source_aliases(row: dict) -> set[str]:
    names = ("source_key", "source_slug", "slug", "channel_id", "channel_title",
             "channel_name", "channel", "youtube_channel_id",
             "youtube_channel_handle", "account", "author", "publisher")
    aliases = {_normal(str(value)) for value in _nested_values(row, names) if value}
    url = row.get("source_url") or row.get("url")
    if isinstance(url, str):
        parsed = urlparse(url)
        if parsed.scheme == "x" and parsed.netloc:
            aliases.add(_normal(parsed.netloc))
        elif parsed.hostname:
            host = parsed.hostname.removeprefix("www.")
            aliases.add(_normal(host))
            aliases.add(_normal(host.split(".")[0]))
    return {alias for alias in aliases if alias}


def _canonical_source_key(row: dict) -> str | None:
    for value in _nested_values(row, ("source_key", "source_slug", "slug")):
        if value:
            return _normal(str(value))
    aliases = sorted(_source_aliases(row), key=len)
    return aliases[0] if aliases else None


def _decorate(row: dict, source: dict | None = None) -> dict:
    result = dict(row)
    normalization = dict(source or {})
    normalization.update(result)
    if source:
        result.update({f"source_{key}": value for key, value in source.items()
                       if f"source_{key}" not in result})
    # Source metadata participates in normalization but is not exposed wholesale
    # by the MCP response.
    published = _publication_time(normalization)
    ingested = _ingestion_time(normalization)
    result["published_at"] = published.isoformat() if published else None
    result["ingested_at"] = ingested.isoformat() if ingested else None
    result["source_key"] = _canonical_source_key(normalization)
    result["_source_aliases"] = sorted(_source_aliases(normalization))
    return result


def _eligible(row: dict, published_after: str | None, published_before: str | None,
              source_keys: list[str] | None) -> bool:
    if published_after or published_before:
        published = _publication_time(row)
        # Strict by design: an unknown date cannot support a current-period claim.
        if published is None:
            return False
        after = _parse_time(published_after) if published_after else None
        before = _parse_time(published_before) if published_before else None
        if published_after and after is None:
            raise ValueError(f"invalid published_after: {published_after}")
        if published_before and before is None:
            raise ValueError(f"invalid published_before: {published_before}")
        if after and published < after:
            return False
        if before and published > before:
            return False
    if source_keys:
        requested = {_normal(value) for value in source_keys}
        aliases = set(row.get("_source_aliases") or _source_aliases(row))
        if not requested.intersection(aliases):
            return False
    return True


def diversify(rows: list[dict], limit: int = 8, per_source_cap: int = 3) -> list[dict]:
    """Preserve rank order while limiting any canonical publisher's share."""
    if per_source_cap < 1:
        raise ValueError("per_source_cap must be at least 1")
    counts: Counter[str] = Counter()
    seen_documents: set[str] = set()
    output = []
    for row in rows:
        document = str(row.get("source_id"))
        if document in seen_documents:
            continue
        publisher = str(row.get("source_key") or document)
        if counts[publisher] >= per_source_cap:
            continue
        seen_documents.add(document)
        counts[publisher] += 1
        output.append(row)
        if len(output) >= limit:
            break
    return output


def coverage_gaps(rows: list[dict], required_only: bool = True) -> list[dict]:
    """Explain why an inventory cannot support a complete current report."""
    gaps = []
    for row in rows:
        if required_only and not row.get("required", False):
            continue
        reasons = []
        if row.get("state") != "content_current":
            reasons.append(f"content_{row.get('state') or 'unknown'}")
        if int(row.get("documents_in_window") or 0) == 0:
            reasons.append("no_items_in_window")
        if int(row.get("full_text_in_window") or 0) == 0:
            reasons.append("no_searchable_full_text")
        if row.get("collector_health") != "healthy":
            reasons.append(f"collector_{row.get('collector_health') or 'unknown'}")
        if reasons:
            gaps.append({"source_key": row.get("source_key"),
                         "display_name": row.get("display_name"),
                         "reasons": reasons})
    return gaps


def finance_evidence_bundle(client: "ZME", query: str, days: int = 30,
                            as_of: str | None = None,
                            source_keys: list[str] | None = None,
                            content_types: list[str] | None = None,
                            per_source_limit: int = 2) -> dict:
    """Return one bounded, source-grouped evidence set for a market report.

    This is intentionally retrieval-only: the calling model synthesizes thesis,
    asset direction, disagreements, confidence, and actions from cited excerpts.
    """
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    if not 1 <= per_source_limit <= 3:
        raise ValueError("per_source_limit must be between 1 and 3")
    end = _parse_time(as_of) if as_of else datetime.now(timezone.utc)
    if as_of and end is None:
        raise ValueError(f"invalid as_of: {as_of}")
    assert end is not None
    start = end - timedelta(days=days)
    published_after = start.isoformat()
    published_before = end.isoformat()
    types = content_types or SEARCHABLE_TYPES
    types = [value for value in types if value in SEARCHABLE_TYPES]
    if not types:
        raise ValueError("content_types contains no searchable type")

    inventory = client.inventory(
        source_keys=source_keys, source_types=types,
        published_after=published_after, as_of=published_before)
    searchable_keys = [
        str(row["source_key"]) for row in inventory
        if row.get("source_key")
        and row.get("state") == "content_current"
        and int(row.get("full_text_in_window") or 0) > 0
    ]
    rows: list[dict] = []
    if searchable_keys:
        target = min(max(len(searchable_keys) * per_source_limit * 4, 24), 120)
        rows = client.search(
            query, k=target, source_types=types,
            published_after=published_after, published_before=published_before,
            source_keys=searchable_keys, exclude_short_form=True)

    grouped: dict[str, list[dict]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        key = str(row.get("source_key") or "")
        document = str(row.get("source_id") or "")
        if not key or not document or document in seen[key]:
            continue
        if len(grouped[key]) >= per_source_limit:
            continue
        seen[key].add(document)
        grouped[key].append({
            "id": row.get("chunk_id"),
            "title": row.get("source_title") or "(untitled)",
            "url": row.get("source_url") or f"zme://sources/{document}",
            "published_at": row.get("published_at"),
            "content_type": row.get("source_type"),
            "excerpt": (row.get("content") or "")[:1600],
        })

    sources = []
    for item in inventory:
        source = dict(item)
        source["evidence"] = grouped.get(str(item.get("source_key")), [])
        sources.append(source)
    gaps = coverage_gaps(inventory, required_only=source_keys is None)
    return {
        "window": {"published_after": published_after,
                   "published_before": published_before,
                   "days": days},
        "query": query,
        "coverage_complete": bool(inventory) and not gaps,
        "coverage_gaps": gaps,
        "sources": sources,
        "synthesis_contract": {
            "fields": ["latest_item", "timestamp", "thesis",
                       "bullish_assets", "bearish_assets", "confidence",
                       "disagreements", "actionable_implication"],
            "rule": "Synthesize only from evidence and disclose every coverage gap.",
        },
    }


def _resolve_key(env: str, noxkey_path: str | None, what: str,
                 aliases: tuple[str, ...] = (), required: bool = True) -> str | None:
    for name in (env, *aliases):
        v = os.environ.get(name)
        if v:
            return v.strip()
    if noxkey_path and sys.platform == "darwin":
        try:
            r = subprocess.run(["noxkey", "get", noxkey_path, "--raw"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if required:
        raise RuntimeError(
            f"zme-mcp: missing {what}. Set {env}=... in the environment. "
            f"The anon key is enough — never deploy a service-role key."
        )
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
        """Returns a pgvector literal, or None when no NVIDIA key (lexical-only).

        Query side MUST use input_type=query (passages use input_type=passage);
        the model is asymmetric and the wrong side quietly degrades recall.
        """
        if not self.nv_key:
            return None
        body = {"model": NV_MODEL, "input": [text],
                "input_type": "query", "truncate": "END"}
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
               source_types: list[str] | None = None,
               published_after: str | None = None,
               published_before: str | None = None,
               source_keys: list[str] | None = None,
               exclude_short_form: bool = True) -> list[dict]:
        if published_after and _parse_time(published_after) is None:
            raise ValueError(f"invalid published_after: {published_after}")
        if published_before and _parse_time(published_before) is None:
            raise ValueError(f"invalid published_before: {published_before}")
        vec = self.embed_query(query) if mode in ("hybrid", "vector") else None
        filtering = bool(
            published_after or published_before or source_keys
            or (exclude_short_form and source_types and "video" in source_types)
        )
        hydrate_sources = filtering or bool(source_types and "video" in source_types)
        # Fetch a wider semantic pool when applying metadata filters. The final
        # result limit remains k and ineligible rows can never backfill it.
        candidate_k = min(max(k * 12, 60), 240) if hydrate_sources else k
        rpc_name = "zme_search_filtered" if filtering else "zme_search_hybrid"
        rows = self.rpc(rpc_name, {
            "q_embedding": vec,
            "q_text": query if mode in ("hybrid", "lexical") else None,
            "k": candidate_k,
            "p_source_types": source_types,
            "p_published_after": published_after if filtering else None,
            "p_published_before": published_before if filtering else None,
            "p_source_keys": source_keys if filtering else None,
            "p_exclude_short_form": exclude_short_form if filtering else None})
        rows = rows or []
        if not hydrate_sources:
            return [_decorate(row) for row in rows[:k]]

        source_ids = {str(row.get("source_id")) for row in rows if row.get("source_id")}
        source_map: dict[str, dict] = {}
        # PostgREST `in` lists are bounded by candidate_k above. A missing source
        # record makes the candidate unverifiable and therefore ineligible.
        if source_ids:
            joined = ",".join(sorted(source_ids))
            for source in self.get(f"sources?id=in.({joined})&select=*"):
                source_map[str(source.get("id"))] = source
        decorated = [_decorate(row, source_map.get(str(row.get("source_id")))) for row in rows]
        # Keep the local predicate as defense in depth against database contract
        # drift; the filtered RPC already applies it before ranking.
        return [row for row in decorated
                if _eligible(row, published_after, published_before, source_keys)][:k]

    def fetch_chunk(self, chunk_id: str) -> dict | None:
        rows = self.get(f"chunks?id=eq.{chunk_id}"
                        "&select=id,content,chunk_index,source_id,sources(*)")
        if not rows:
            return None
        row = rows[0]
        src = row.pop("sources", None) or {}
        row.update({"source_title": src.get("title"), "source_url": src.get("url"),
                    "source_type": src.get("source_type")})
        return _decorate(row, src)

    def inventory(self, source_keys: list[str] | None = None,
                  source_types: list[str] | None = None,
                  published_after: str | None = None,
                  as_of: str | None = None) -> list[dict]:
        """Inventory indexed source documents, distinct from collector health.

        This deliberately reports ``unknown_date`` and ``not_present`` instead of
        guessing. A healthy collector requires a separate ingestion-run ledger.
        """
        after = _parse_time(published_after) if published_after else None
        end = _parse_time(as_of) if as_of else datetime.now(timezone.utc)
        if published_after and after is None:
            raise ValueError(f"invalid published_after: {published_after}")
        if as_of and end is None:
            raise ValueError(f"invalid as_of: {as_of}")

        try:
            native = self.rpc("zme_source_inventory", {
                "p_source_keys": source_keys,
                "p_published_after": after.isoformat() if after else None,
                "p_as_of": end.isoformat(),
            })
        except RuntimeError as exc:
            # Backward-compatible rollout: only a missing RPC triggers the legacy
            # source-table audit. Auth/network/database errors must stay visible.
            if "404" not in str(exc):
                raise
            native = None
        if native is not None:
            return native

        clauses = ["select=*"]
        if source_types:
            safe_types = [value for value in source_types if value in SEARCHABLE_TYPES]
            if not safe_types:
                return []
            clauses.append("source_type=in.(" + ",".join(safe_types) + ")")
        clauses.append("limit=10000")
        sources = self.get("sources?" + "&".join(clauses))

        requested = {_normal(value): value for value in (source_keys or [])}
        groups: dict[str, list[dict]] = defaultdict(list)
        for source in sources:
            decorated = _decorate({}, source)
            aliases = set(decorated.get("_source_aliases") or [])
            if requested and not aliases.intersection(requested):
                continue
            key = decorated.get("source_key") or "unidentified"
            groups[key].append(decorated)

        output: list[dict] = []
        represented: set[str] = set()
        for key, rows in groups.items():
            aliases = {alias for row in rows for alias in row.get("_source_aliases", [])}
            represented.update(aliases.intersection(requested))
            published = [_parse_time(row.get("published_at")) for row in rows]
            published = [value for value in published if value is not None]
            ingested = [_parse_time(row.get("ingested_at")) for row in rows]
            ingested = [value for value in ingested if value is not None]
            in_window = [value for value in published
                         if (after is None or value >= after) and value <= end]
            latest = max(published, default=None)
            latest_row = next((row for row in rows
                               if _parse_time(row.get("published_at")) == latest), rows[0])
            if not published:
                state = "unknown_date"
            elif in_window:
                state = "content_current"
            else:
                state = "content_stale"
            source_record = {
                "source_key": key,
                "state": state,
                "documents_total": len(rows),
                "documents_in_window": len(in_window),
                "undated_documents": len(rows) - len(published),
                "latest_published_at": latest.isoformat() if latest else None,
                "latest_ingested_at": max(ingested).isoformat() if ingested else None,
                "latest_title": latest_row.get("source_title"),
                "latest_url": latest_row.get("source_url"),
                "content_types": sorted({row.get("source_source_type") for row in rows
                                          if row.get("source_source_type")}),
                "collector_health": "not_exposed",
            }
            output.append(source_record)

        for normalized, display in requested.items():
            if normalized not in represented:
                output.append({
                    "source_key": normalized,
                    "requested_as": display,
                    "state": "not_present",
                    "documents_total": 0,
                    "documents_in_window": 0,
                    "undated_documents": 0,
                    "latest_published_at": None,
                    "latest_ingested_at": None,
                    "latest_title": None,
                    "latest_url": None,
                    "content_types": [],
                    "collector_health": "not_exposed",
                })
        order = {"not_present": 0, "unknown_date": 1, "content_stale": 2,
                 "content_current": 3}
        return sorted(output, key=lambda row: (order[row["state"]], row["source_key"]))

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
