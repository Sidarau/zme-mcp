import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SB = `${Deno.env.get("SUPABASE_URL")}/rest/v1`;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const FEED = "https://feeds.feedburner.com/MacroVoices";
const SOURCE_KEY = "macrovoices";
const NS = "6f9619ff-8b86-d011-b42d-00c04fc964ff";
const USER_AGENT = "ZME finance source discovery/1.0";

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

function tag(xml: string, name: string): string {
  const match = xml.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "i"));
  return (match?.[1] || "").replace(/^<!\[CDATA\[|\]\]>$/g, "").trim();
}

function decodeEntities(value: string): string {
  const named: Record<string, string> = {
    amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ",
  };
  return value
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&([a-z]+);/gi, (whole, name) => named[name.toLowerCase()] ?? whole);
}

function plainText(html: string): string {
  return decodeEntities(html.replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<[^>]+>/g, " "))
    .replace(/[ \t]+/g, " ").replace(/\n\s+/g, "\n").trim();
}

function hrefs(html: string): string[] {
  return [...html.matchAll(/href=['"]([^'"]+)['"]/gi)].map((match) => decodeEntities(match[1]));
}

function nsBytes(uuid: string): Uint8Array {
  const hex = uuid.replace(/-/g, "");
  return new Uint8Array(Array.from({ length: 16 }, (_, i) => parseInt(hex.slice(i * 2, i * 2 + 2), 16)));
}

async function uuidv5(name: string): Promise<string> {
  const encoded = new TextEncoder().encode(name);
  const bytes = new Uint8Array(16 + encoded.length);
  bytes.set(nsBytes(NS));
  bytes.set(encoded, 16);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-1", bytes)).slice(0, 16);
  hash[6] = (hash[6] & 0x0f) | 0x50;
  hash[8] = (hash[8] & 0x3f) | 0x80;
  const hex = [...hash].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function transcriptUrl(descriptionHtml: string): Promise<{ landing?: string; transcript?: string }> {
  const candidate = hrefs(descriptionHtml).find((url) => /bit\.ly|macrovoices\.com/i.test(url));
  if (!candidate) return {};
  try {
    const page = await fetch(candidate, { redirect: "follow", headers: { "User-Agent": USER_AGENT } });
    if (!page.ok) return {};
    const html = await page.text();
    const match = html.match(/href=['"]([^'"]*guest-transcripts[^'"]*\/file)['"]/i);
    return {
      landing: page.url,
      transcript: match ? new URL(decodeEntities(match[1]), page.url).href : undefined,
    };
  } catch {
    return {};
  }
}

type Episode = {
  title: string;
  url: string;
  publishedAt: string;
  summary: string;
  landing?: string;
  transcript?: string;
};

async function discover(): Promise<Episode[]> {
  const response = await fetch(FEED, { headers: { "User-Agent": USER_AGENT } });
  if (!response.ok) throw new Error(`MacroVoices RSS: ${response.status}`);
  const xml = await response.text();
  const cutoff = Date.now() - 35 * 24 * 60 * 60 * 1000;
  const episodes: Episode[] = [];
  for (const match of xml.matchAll(/<item(?:\s[^>]*)?>([\s\S]*?)<\/item>/gi)) {
    const item = match[1];
    const published = new Date(tag(item, "pubDate"));
    const url = plainText(tag(item, "link"));
    if (!url || Number.isNaN(published.valueOf()) || published.valueOf() < cutoff) continue;
    const description = tag(item, "description");
    const transcript = await transcriptUrl(description);
    episodes.push({
      title: plainText(tag(item, "title")),
      url,
      publishedAt: published.toISOString(),
      summary: plainText(description),
      ...transcript,
    });
  }
  return episodes;
}

async function run() {
  const startedAt = new Date().toISOString();
  try {
    const channels = await sb("GET", `/channels?select=id&source_key=eq.${SOURCE_KEY}&limit=1`);
    if (!channels.length) throw new Error("MacroVoices channel is not configured");
    const channelId = channels[0].id;
    const existing = await sb("GET", `/sources?select=id,url&source_key=eq.${SOURCE_KEY}`);
    const known = new Set(existing.map((row: { url: string }) => row.url));
    const episodes = await discover();
    const now = new Date().toISOString();
    const inserted = [];

    for (const episode of episodes) {
      const metadata = {
        channel_name: "MacroVoices",
        feed_url: FEED,
        published_at: episode.publishedAt,
        transcript_landing_url: episode.landing,
        transcript_url: episode.transcript,
      };
      if (known.has(episode.url)) {
        await sb("PATCH", `/sources?url=eq.${encodeURIComponent(episode.url)}`, {
          title: episode.title,
          summary: episode.summary,
          published_at: episode.publishedAt,
          metadata,
          updated_at: now,
        }, "return=minimal");
        continue;
      }
      inserted.push({
        id: await uuidv5(`zme-source:${episode.url}`),
        url: episode.url,
        title: episode.title,
        source_type: "podcast",
        summary: episode.summary,
        raw_content: null,
        metadata,
        tags: ["finance", "macro", "podcast"],
        channel_id: channelId,
        transcript_status: "pending",
        source_key: SOURCE_KEY,
        published_at: episode.publishedAt,
        ingested_at: now,
        platform_item_id: episode.url,
        text_status: "pending",
        transcript_provenance: "publisher_transcript_pending",
      });
    }
    if (inserted.length) await sb("POST", "/sources", inserted, "return=representation");
    await sb("PATCH", `/channels?id=eq.${channelId}`, { last_checked_at: now }, "return=minimal");
    await sb("POST", "/zme_ingestion_runs", [{
      source_key: SOURCE_KEY,
      started_at: startedAt,
      finished_at: now,
      status: "success",
      items_seen: episodes.length,
      items_inserted: inserted.length,
      newest_published_at: episodes[0]?.publishedAt || null,
      metadata: { feed_url: FEED, transcript_links: episodes.filter((e) => e.transcript).length },
    }], "return=minimal");
    return { source_key: SOURCE_KEY, items_seen: episodes.length,
             items_inserted: inserted.length,
             transcript_links: episodes.filter((episode) => episode.transcript).length };
  } catch (error) {
    const finishedAt = new Date().toISOString();
    try {
      await sb("POST", "/zme_ingestion_runs", [{
        source_key: SOURCE_KEY, started_at: startedAt, finished_at: finishedAt,
        status: "error", error_class: error instanceof Error ? error.name : "Error",
        error_message: String(error).slice(0, 500),
      }], "return=minimal");
    } catch { /* retain the original failure */ }
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
