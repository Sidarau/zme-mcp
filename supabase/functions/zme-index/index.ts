// zme-index — auto-index new ZME sources (agent notes) without the Mac.
//
// Triggered by a Supabase database webhook on sources INSERT, and by a pg_cron
// drain ({"drain":true}) every 5 min as a safety net. Replicates
// skills/zme/scripts/zme_ingest.py exactly: same chunker (md-h+window-v1),
// same uuid5 chunk ids, same NVIDIA embed call, same idempotent write
// (delete source's chunks -> insert -> upsert zme_ingest_runs).
//
// Env (auto-injected by Supabase): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
// Secrets (supabase secrets set): NVIDIA_API_KEY, ZME_HOOK_SECRET.

const SB = `${Deno.env.get("SUPABASE_URL")}/rest/v1`;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const NV_KEY = Deno.env.get("NVIDIA_API_KEY")!;
const HOOK_SECRET = Deno.env.get("ZME_HOOK_SECRET")!;
const NV_URL = "https://integrate.api.nvidia.com/v1/embeddings";
const NV_MODEL = "nvidia/nv-embedqa-e5-v5";

const NS = "6f9619ff-8b86-d011-b42d-00c04fc964ff"; // chunk-id namespace (matches zme_ingest.py)
const PIPELINE_VERSION = "v1";
const CHUNKER_VERSION = "md-h+window-v1";
const MAX_CHARS = Math.floor(460 * 3.6); // 1656
const OVERLAP_CHARS = Math.floor(MAX_CHARS * 0.15);
const EMBED_BATCH = 32;
const WRITE_BATCH = 12; // keep long transcript inserts below Postgres statement_timeout
const DRAIN_LIMIT = 10; // max sources per drain invocation

// ---------------------------------------------------------------- supabase
async function sb(method: string, path: string, payload?: unknown, extraHeaders: Record<string, string> = {}) {
  const res = await fetch(`${SB}${path}`, {
    method,
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
      ...extraHeaders,
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`supabase ${method} ${path} -> ${res.status} ${(await res.text()).slice(0, 300)}`);
  const text = await res.text();
  return text.trim() ? JSON.parse(text) : [];
}

// ---------------------------------------------------------------- nvidia
async function embed(texts: string[], inputType = "passage"): Promise<number[][]> {
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(NV_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${NV_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: NV_MODEL, input: texts, input_type: inputType, truncate: "END" }),
    });
    if (res.ok) {
      const d = await res.json();
      return (d.data as { index: number; embedding: number[] }[])
        .sort((a, b) => a.index - b.index)
        .map((o) => o.embedding);
    }
    if (res.status === 429 || res.status >= 500) {
      await new Promise((r) => setTimeout(r, Math.min(2 ** attempt * 2000, 30000)));
      continue;
    }
    throw new Error(`nvidia ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  throw new Error("nvidia embed failed after retries");
}

// ---------------------------------------------------------------- uuid v5 (SHA-1), matches Python uuid.uuid5
function nsBytes(u: string): Uint8Array {
  const h = u.replace(/-/g, "");
  const b = new Uint8Array(16);
  for (let i = 0; i < 16; i++) b[i] = parseInt(h.substring(i * 2, i * 2 + 2), 16);
  return b;
}

async function shaHex(algo: string, data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest(algo, data as BufferSource));
}

async function uuidv5(name: string): Promise<string> {
  const enc = new TextEncoder().encode(name);
  const data = new Uint8Array(16 + enc.length);
  data.set(nsBytes(NS), 0);
  data.set(enc, 16);
  const hash = (await shaHex("SHA-1", data)).slice(0, 16);
  hash[6] = (hash[6] & 0x0f) | 0x50;
  hash[8] = (hash[8] & 0x3f) | 0x80;
  const hex = [...hash].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function sha256Hex(s: string): Promise<string> {
  const h = await shaHex("SHA-256", new TextEncoder().encode(s));
  return [...h].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------- chunker (port of zme_ingest.py)
type Source = { id: string; title?: string; summary?: string; source_type?: string; raw_content?: string };

const HEADING_RE = /^(#{1,6})\s+(.*)$/gm;
const TS_RE = /\[\d+(?:\.\d+)?s\]\s*/g;

function splitSections(text: string): [string, string][] {
  const marks: { pos: number; title: string }[] = [];
  for (const m of text.matchAll(HEADING_RE)) marks.push({ pos: m.index!, title: m[2].trim() });
  if (!marks.length) return [["", text]];
  const out: [string, string][] = [];
  if (marks[0].pos > 0) {
    const head = text.slice(0, marks[0].pos).trim();
    if (head) out.push(["", head]);
  }
  for (let i = 0; i < marks.length; i++) {
    const end = i + 1 < marks.length ? marks[i + 1].pos : text.length;
    const body = text.slice(marks[i].pos, end).trim();
    if (body) out.push([marks[i].title, body]);
  }
  return out;
}

function window_(text: string, maxChars = MAX_CHARS, overlap = OVERLAP_CHARS): string[] {
  const paras = text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  const chunks: string[] = [];
  let cur = "";
  for (let p of paras) {
    while (p.length > maxChars) {
      if (cur) { chunks.push(cur); cur = ""; }
      chunks.push(p.slice(0, maxChars));
      p = p.slice(maxChars - overlap);
    }
    if (cur.length + p.length + 2 <= maxChars) {
      cur = cur ? `${cur}\n\n${p}` : p;
    } else {
      if (cur) chunks.push(cur);
      cur = p;
    }
  }
  if (cur) chunks.push(cur);
  return chunks.length ? chunks : (text.trim() ? [text.slice(0, maxChars)] : []);
}

function contextPrefix(src: Source, heading: string): string {
  const bits: string[] = [];
  if (src.title) bits.push(src.title.trim());
  if (heading) bits.push(heading.trim());
  const summ = (src.summary || "").trim();
  if (summ) bits.push(summ.split(/(?<=[.!?])\s/)[0].slice(0, 220));
  if (src.source_type) bits.push(`[${src.source_type}]`);
  return bits.filter(Boolean).join(" — ").slice(0, 600);
}

function extractText(raw: string): string {
  const s = (raw || "").trim();
  if (s.startsWith("{") || s.startsWith("[")) {
    try {
      const d = JSON.parse(s);
      if (d && typeof d === "object" && !Array.isArray(d)) {
        for (const key of ["transcript", "text", "content", "body", "markdown"]) {
          const v = d[key];
          if (typeof v === "string" && v.length > 100) {
            return v.replace(TS_RE, " ").replace(/[ \t]{2,}/g, " ").replace(/(?<=[.!?])\s+/g, "\n\n").trim();
          }
        }
        const parts = Object.values(d).filter((x): x is string => typeof x === "string" && x.length > 200);
        if (parts.length) return parts.reduce((a, b) => (b.length > a.length ? b : a));
      }
    } catch { /* not JSON — chunk raw */ }
  }
  return raw;
}

async function chunkSource(src: Source) {
  const raw = extractText(src.raw_content || "");
  const rows = [];
  let idx = 0;
  for (const [heading, body] of splitSections(raw)) {
    for (const piece of window_(body)) {
      if (piece.trim().length < 40) continue;
      const pre = contextPrefix(src, heading);
      rows.push({
        id: await uuidv5(`${src.id}:${CHUNKER_VERSION}:${idx}`),
        source_id: src.id,
        chunk_index: idx,
        content: piece,
        context_prefix: pre,
        heading: heading || null,
        token_count: Math.floor(piece.length / 3.6),
        content_hash: await sha256Hex(pre + piece),
        chunker_version: CHUNKER_VERSION,
      });
      idx++;
    }
  }
  return rows;
}

// ---------------------------------------------------------------- indexing
async function indexSource(src: Source) {
  const raw = (src.raw_content || "").trim();
  const floor = ["note", "analysis"].includes(src.source_type || "") ? 20 : 200;
  if (raw.length < floor) {
    await sb("POST", "/zme_ingest_runs", [{
      source_id: src.id, pipeline_version: PIPELINE_VERSION,
      status: "skipped", error: `raw_content under ${floor} chars`,
    }], { Prefer: "resolution=merge-duplicates" });
    return { id: src.id, status: "skipped" };
  }

  const rows = await chunkSource(src);
  if (!rows.length) return { id: src.id, status: "no-chunks" };

  const vectors: number[][] = [];
  for (let i = 0; i < rows.length; i += EMBED_BATCH) {
    const batch = rows.slice(i, i + EMBED_BATCH);
    vectors.push(...await embed(batch.map((r) => `${r.context_prefix}\n\n${r.content}`), "passage"));
  }
  const now = new Date().toISOString();
  const out = rows.map((r, i) => ({
    ...r,
    embedding_v: "[" + vectors[i].map((x) => x.toFixed(7)).join(",") + "]",
    embedded_at: now,
  }));

  await sb("DELETE", `/chunks?source_id=eq.${src.id}`, undefined, { Prefer: "return=minimal" });
  for (let i = 0; i < out.length; i += WRITE_BATCH) {
    await sb("POST", "/chunks", out.slice(i, i + WRITE_BATCH), { Prefer: "return=minimal" });
  }
  await sb("POST", "/zme_ingest_runs", [{
    source_id: src.id, pipeline_version: PIPELINE_VERSION,
    chunks_written: out.length, chunks_embedded: vectors.length, status: "ok",
    content_hash: await sha256Hex(raw),
  }], { Prefer: "resolution=merge-duplicates" });
  return { id: src.id, status: "ok", chunks: out.length };
}

async function alreadyDone(ids: string[]): Promise<Set<string>> {
  if (!ids.length) return new Set();
  const rows = await sb("GET",
    `/zme_ingest_runs?select=source_id&pipeline_version=eq.${PIPELINE_VERSION}&status=eq.ok&source_id=in.(${ids.join(",")})`);
  return new Set(rows.map((r: { source_id: string }) => r.source_id));
}

async function processIds(ids: string[]) {
  const done = await alreadyDone(ids);
  const results = [];
  for (const id of ids) {
    if (done.has(id)) { results.push({ id, status: "already-ok" }); continue; }
    const rows = await sb("GET", `/sources?select=id,title,summary,source_type,raw_content&id=eq.${id}`);
    if (!rows.length) { results.push({ id, status: "missing" }); continue; }
    try {
      results.push(await indexSource(rows[0]));
    } catch (e) {
      results.push({ id, status: "failed", error: String(e).slice(0, 300) });
    }
  }
  return results;
}

// Drain: recent notes/analyses without an ok ingest run (safety net behind the webhook).
async function drain() {
  const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  const rows = await sb("GET",
    `/sources?select=id&raw_content=not.is.null&source_type=in.(note,analysis)&created_at=gte.${since}&order=created_at.desc&limit=50`);
  const done = await alreadyDone(rows.map((r: { id: string }) => r.id));
  const pending = rows.filter((r: { id: string }) => !done.has(r.id)).slice(0, DRAIN_LIMIT);
  return processIds(pending.map((r: { id: string }) => r.id));
}

// ---------------------------------------------------------------- entry
Deno.serve(async (req) => {
  if (req.headers.get("x-hook-secret") !== HOOK_SECRET) {
    return new Response("unauthorized", { status: 401 });
  }
  try {
    const body = await req.json();
    let results;
    if (body.drain) {
      results = await drain();
    } else if (body.type === "INSERT" && body.table === "sources" && body.record?.id) {
      results = await processIds([body.record.id]);
    } else {
      return new Response(JSON.stringify({ ignored: true }), { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true, results }), {
      headers: { "Content-Type": "application/json" },
    });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: String(e).slice(0, 500) }), { status: 500 });
  }
});
