import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { extractText, getDocumentProxy } from "unpdf";

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SB = `${SB_URL}/rest/v1`;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const INDEX_SECRET = Deno.env.get("ZME_HOOK_SECRET")!;
const SOURCE_KEY = "macrovoices";
const USER_AGENT = "ZME authorized publisher transcript worker/1.0";

async function sb(method: string, path: string, payload?: unknown,
                  prefer = "return=representation") {
  const response = await fetch(`${SB}${path}`, {
    method,
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      "Content-Type": "application/json",
      Prefer: prefer,
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`Supabase ${method} ${path}: ${response.status} ${(await response.text()).slice(0, 300)}`);
  }
  const text = await response.text();
  return text.trim() ? JSON.parse(text) : [];
}

async function indexSource(id: string) {
  if (!INDEX_SECRET) throw new Error("ZME_HOOK_SECRET is unavailable");
  const response = await fetch(`${SB_URL}/functions/v1/zme-index`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-hook-secret": INDEX_SECRET },
    body: JSON.stringify({ type: "INSERT", table: "sources", record: { id } }),
  });
  const body = await response.text();
  if (!response.ok) throw new Error(`zme-index ${response.status}: ${body.slice(0, 300)}`);
  const parsed = JSON.parse(body);
  const result = parsed.results?.[0];
  if (!parsed.ok || !result || !["ok", "already-ok"].includes(result.status)) {
    throw new Error(`zme-index did not complete: ${body.slice(0, 300)}`);
  }
  return result;
}

async function extract(url: string): Promise<string> {
  const response = await fetch(url, { redirect: "follow", headers: { "User-Agent": USER_AGENT } });
  if (!response.ok) throw new Error(`transcript PDF ${response.status}`);
  const type = response.headers.get("content-type") || "";
  if (!type.toLowerCase().includes("pdf")) throw new Error(`unexpected transcript type: ${type}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const pdf = await getDocumentProxy(bytes);
  const result = await extractText(pdf, { mergePages: true });
  const text = String(result.text || "").replace(/\u0000/g, "").trim();
  if (text.length < 1000) throw new Error(`transcript extraction returned only ${text.length} characters`);
  return text;
}

async function run() {
  const startedAt = new Date().toISOString();
  let transcriptAvailable = false;
  const rows = await sb("GET",
    `/sources?select=id,title,raw_content,text_status,metadata&source_key=eq.${SOURCE_KEY}` +
    `&text_status=in.(pending,index_failed,extraction_failed)&order=published_at.desc&limit=1`);
  if (!rows.length) return { source_key: SOURCE_KEY, status: "idle" };
  const source = rows[0];
  try {
    let transcript = source.raw_content;
    transcriptAvailable = Boolean(transcript);
    if (!transcript) {
      const url = source.metadata?.transcript_url;
      if (!url) throw new Error("publisher transcript URL is missing");
      transcript = await extract(url);
      transcriptAvailable = true;
      await sb("PATCH", `/sources?id=eq.${source.id}`, {
        raw_content: transcript,
        transcript_status: "fetched",
        transcript_provenance: "publisher_pdf",
        text_status: "indexing",
        updated_at: new Date().toISOString(),
      }, "return=minimal");
    }
    const indexed = await indexSource(source.id);
    const finishedAt = new Date().toISOString();
    await sb("PATCH", `/sources?id=eq.${source.id}`, {
      text_status: "ready", updated_at: finishedAt,
    }, "return=minimal");
    await sb("POST", "/zme_ingestion_runs", [{
      source_key: SOURCE_KEY, started_at: startedAt, finished_at: finishedAt,
      status: "success", items_seen: 1, items_inserted: 0,
      metadata: { stage: "transcript", source_id: source.id,
                  characters: transcript.length, chunks: indexed.chunks ?? null },
    }], "return=minimal");
    return { source_key: SOURCE_KEY, status: "ready", source_id: source.id,
             characters: transcript.length, chunks: indexed.chunks ?? null };
  } catch (error) {
    const finishedAt = new Date().toISOString();
    await sb("PATCH", `/sources?id=eq.${source.id}`, {
      text_status: transcriptAvailable ? "index_failed" : "extraction_failed",
      updated_at: finishedAt,
    }, "return=minimal");
    await sb("POST", "/zme_ingestion_runs", [{
      source_key: SOURCE_KEY, started_at: startedAt, finished_at: finishedAt,
      status: "error", items_seen: 1,
      error_class: error instanceof Error ? error.name : "Error",
      error_message: String(error).slice(0, 500),
      metadata: { stage: "transcript", source_id: source.id },
    }], "return=minimal");
    throw error;
  }
}

Deno.serve(async (request) => {
  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
  try {
    return Response.json({ ok: true, result: await run() });
  } catch (error) {
    return Response.json({ ok: false, error: String(error).slice(0, 500) }, { status: 500 });
  }
});
