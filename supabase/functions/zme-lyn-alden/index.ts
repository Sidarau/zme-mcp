import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SB = `${SB_URL}/rest/v1`;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const INDEX_SECRET = Deno.env.get("ZME_HOOK_SECRET")!;
const FEED = "https://www.lynalden.com/feed/";
const SOURCE_KEY = "lyn_alden";
const NS = "6f9619ff-8b86-d011-b42d-00c04fc964ff";
const USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 ZME/1.0";

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
    ndash: "–", mdash: "—", rsquo: "’", lsquo: "‘", rdquo: "”", ldquo: "“",
  };
  return value
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&([a-z]+);/gi, (whole, name) => named[name.toLowerCase()] ?? whole);
}

function plainText(html: string): string {
  return decodeEntities(html
    .replace(/<script\b[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[\s\S]*?<\/style>/gi, " ")
    .replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/(?:p|h[1-6]|li|blockquote|div)>/gi, "\n\n")
    .replace(/<li(?:\s[^>]*)?>/gi, "- ")
    .replace(/<[^>]+>/g, " "))
    .replace(/\r/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
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

type Article = { title: string; url: string; publishedAt: string; summary: string; content: string };

async function discover(): Promise<Article[]> {
  const response = await fetch(FEED, { headers: { "User-Agent": USER_AGENT } });
  if (!response.ok) throw new Error(`Lyn Alden RSS: ${response.status}`);
  const xml = await response.text();
  const cutoff = Date.now() - 45 * 24 * 60 * 60 * 1000;
  const articles: Article[] = [];
  for (const match of xml.matchAll(/<item(?:\s[^>]*)?>([\s\S]*?)<\/item>/gi)) {
    const item = match[1];
    const published = new Date(tag(item, "pubDate"));
    const url = plainText(tag(item, "link"));
    if (!url || Number.isNaN(published.valueOf()) || published.valueOf() < cutoff) continue;
    const page = await fetch(url, { redirect: "follow", headers: { "User-Agent": USER_AGENT } });
    if (!page.ok) throw new Error(`Lyn Alden article ${page.status}: ${url}`);
    const html = await page.text();
    const body = html.match(/<div[^>]+class=["'][^"']*entry-content[^"']*["'][^>]*>([\s\S]*?)<footer[^>]+class=["'][^"']*entry-footer/i)?.[1];
    const content = plainText(body || "");
    if (content.length < 500) throw new Error(`Lyn Alden article extraction returned ${content.length} characters: ${url}`);
    articles.push({
      title: plainText(tag(item, "title")), url,
      publishedAt: published.toISOString(),
      summary: plainText(tag(item, "description")), content,
    });
  }
  return articles;
}

async function run() {
  const startedAt = new Date().toISOString();
  try {
    const channels = await sb("GET", `/channels?select=id&source_key=eq.${SOURCE_KEY}&limit=1`);
    if (!channels.length) throw new Error("Lyn Alden channel is not configured");
    const channelId = channels[0].id;
    const existing = await sb("GET", `/sources?select=id,url,text_status&source_key=eq.${SOURCE_KEY}`);
    const known = new Map(existing.map((row: { id: string; url: string; text_status: string }) => [row.url, row]));
    const articles = await discover();
    const now = new Date().toISOString();
    let inserted = 0;
    let indexed = 0;

    for (const article of articles) {
      const metadata = { channel_name: "Lyn Alden", feed_url: FEED, published_at: article.publishedAt };
      const prior = known.get(article.url) as { id: string; text_status: string } | undefined;
      const id = prior?.id || await uuidv5(`zme-source:${article.url}`);
      if (prior) {
        await sb("PATCH", `/sources?id=eq.${id}`, {
          title: article.title, summary: article.summary, raw_content: article.content,
          published_at: article.publishedAt, metadata, updated_at: now,
          text_status: prior.text_status === "ready" ? "ready" : "indexing",
        }, "return=minimal");
      } else {
        await sb("POST", "/sources", [{
          id, url: article.url, title: article.title, source_type: "article",
          summary: article.summary, raw_content: article.content, metadata,
          tags: ["finance", "macro", "article"], channel_id: channelId,
          source_key: SOURCE_KEY, published_at: article.publishedAt, ingested_at: now,
          platform_item_id: article.url, text_status: "indexing",
          transcript_provenance: "publisher_html",
        }], "return=minimal");
        inserted++;
      }
      if (!prior || prior.text_status !== "ready") {
        await indexSource(id);
        await sb("PATCH", `/sources?id=eq.${id}`, { text_status: "ready", updated_at: now }, "return=minimal");
        indexed++;
      }
    }
    await sb("PATCH", `/channels?id=eq.${channelId}`, { last_checked_at: now }, "return=minimal");
    await sb("POST", "/zme_ingestion_runs", [{
      source_key: SOURCE_KEY, started_at: startedAt, finished_at: now,
      status: "success", items_seen: articles.length, items_inserted: inserted,
      newest_published_at: articles[0]?.publishedAt || null,
      metadata: { feed_url: FEED, indexed },
    }], "return=minimal");
    return { source_key: SOURCE_KEY, items_seen: articles.length, items_inserted: inserted, indexed };
  } catch (error) {
    const finishedAt = new Date().toISOString();
    try {
      await sb("POST", "/zme_ingestion_runs", [{
        source_key: SOURCE_KEY, started_at: startedAt, finished_at: finishedAt,
        status: "error", error_class: error instanceof Error ? error.name : "Error",
        error_message: String(error).slice(0, 500), metadata: { feed_url: FEED },
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
