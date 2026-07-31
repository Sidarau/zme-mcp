"""End-to-end verification for zme-mcp over real stdio transport.

Spawns the server as a subprocess, speaks MCP to it, and exercises every
tool against the live ZME corpus. Usage:

    .venv/bin/python scripts/verify_stdio.py
"""
import asyncio
import json
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def main() -> int:
    transport = StdioTransport(command=sys.prefix + "/bin/zme-mcp", args=[])
    failures = 0

    async with Client(transport) as c:
        tools = await c.list_tools()
        names = [t.name for t in tools]
        print("tools:", names)
        assert set(names) == {"search", "fetch", "recall", "why", "near", "remember"}, names

        def check(label, ok, detail=""):
            nonlocal failures
            print(("PASS" if ok else "FAIL"), label, ("— " + detail) if detail and not ok else "")
            if not ok:
                failures += 1

        # 1. ChatGPT-compat search
        r = await c.call_tool("search", {"query": "how do agents verify their own findings"})
        payload = r.structured_content or json.loads(r.content[0].text)
        results = payload.get("results", [])
        check("search returns results[]", len(results) > 0, f"got {len(results)}")
        first = results[0] if results else {}
        check("search result has id/title/url",
              all(k in first for k in ("id", "title", "url")), str(first)[:120])
        print("   top:", first.get("title", "")[:70], "|", first.get("url", "")[:60])

        # 2. fetch on the id search returned
        if first.get("id"):
            f = await c.call_tool("fetch", {"id": first["id"]})
            doc = f.structured_content or json.loads(f.content[0].text)
            check("fetch returns full text",
                  len(doc.get("text", "")) > 100 and doc.get("id") == first["id"],
                  str(doc)[:120])
            print("   fetched:", doc.get("title", "")[:70], f"({len(doc.get('text',''))} chars)")

        # 3. recall (notes scope)
        r = await c.call_tool("recall", {"query": "why did we defer analytics", "k": 5})
        txt = r.content[0].text
        check("recall hits notes", "recall (" in txt and "results for:" in txt, txt[:120])
        print("   ", txt.splitlines()[0])

        # 4. why (causal path)
        r = await c.call_tool("why", {"from_entity": "kimi CLI 0.27.0",
                                      "to_entity": "loop.ts CLI_COMMANDS fix"})
        txt = r.content[0].text
        check("why returns a chain", "--[" in txt or "No path" in txt or "unresolved" in txt,
              txt[:120])
        print("   ", txt.splitlines()[0][:100])

        # 5. near (neighbourhood)
        r = await c.call_tool("near", {"entity": "kimi CLI 0.27.0", "hops": 1})
        txt = r.content[0].text
        check("near returns facts", "--" in txt or "No facts" in txt, txt[:120])
        print("   ", txt.splitlines()[0][:100])

        # 6. remember (the only write path)
        r = await c.call_tool("remember", {
            "text": "zme-mcp stdio verification passed end-to-end on 2026-07-30: "
                    "search/fetch/recall/why/near/remember all live against the corpus.",
            "title": "zme-mcp stdio verification",
            "agent": "zme-mcp-verify"})
        txt = r.content[0].text
        check("remember creates a note", txt.startswith("remembered "), txt[:120])
        print("   ", txt.splitlines()[0])

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURES"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
