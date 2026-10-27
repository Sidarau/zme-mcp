#!/usr/bin/env python3
"""Client workspace administration and smoke tests (ZEUG-795).

Connects with ZME_WS_DSN (libpq string); the password comes from PGPASSWORD, injected per call:

  export ZME_WS_DSN="host=100.118.214.24 port=54322 dbname=postgres user=postgres sslmode=disable"
  ~/.local/bin/zeug-key run PGPASSWORD=zeuglab/zme/DB_PASSWORD -- python3 scripts/zme_ws.py list

Commands:
  create <id> --title T                    provision a workspace for cmd project <id>
  list                                     workspaces and their schemas
  add <id> (--text T | --file F) [--title T] [--type note] [--as EMAIL]
  search <id> "query" [--mode hybrid|lexical|vector] [-k 8]
  sources <id>                             newest sources in the workspace
  copy <id> <zeug-source-id> [--as EMAIL]  copy one source from Zeug's memory (owner only)
  drop <id> --confirm <id>                 delete the workspace and everything in it
  embed-pending                            add vectors to chunks stored without one (run on the tailnet)
  share <id> <zeug-source-id>... [--preview] [--publish] [--accept SRC ...] [--redact SRC ...] [--mask WORD ...]
        [--title SRC=TITLE ...] [--approve SRC=DIGEST ...] --as EMAIL
                                           scan; --preview prints every outgoing field and a digest; --publish writes
                                           only named sources whose payload still matches the approved digest
  shares <id>                              what was shared into the workspace, by whom and how
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from zme_mcp.workspace import Workspaces  # noqa: E402


def show(rows) -> None:
    print(json.dumps(rows, indent=1, default=str, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("id"); c.add_argument("--title", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("id"); a.add_argument("--text"); a.add_argument("--file")
    a.add_argument("--title", default=""); a.add_argument("--type", default="note"); a.add_argument("--url")
    a.add_argument("--as", dest="actor")
    s = sub.add_parser("search"); s.add_argument("id"); s.add_argument("query")
    s.add_argument("--mode", default="hybrid", choices=("hybrid", "lexical", "vector")); s.add_argument("-k", type=int, default=8)
    so = sub.add_parser("sources"); so.add_argument("id")
    cp = sub.add_parser("copy"); cp.add_argument("id"); cp.add_argument("source_id"); cp.add_argument("--as", dest="actor")
    d = sub.add_parser("drop"); d.add_argument("id"); d.add_argument("--confirm", required=True)
    sub.add_parser("embed-pending")
    sh = sub.add_parser("share"); sh.add_argument("id"); sh.add_argument("sources", nargs="+")
    sh.add_argument("--publish", action="store_true"); sh.add_argument("--accept", nargs="*", default=[])
    sh.add_argument("--redact", nargs="*", default=[]); sh.add_argument("--as", dest="actor", required=True)
    sh.add_argument("--mask", nargs="*", default=[], help="extra literals to flag and remove (never committed)")
    sh.add_argument("--title", nargs="*", default=[], help="SRC=replacement title, when the title itself is flagged")
    sh.add_argument("--preview", action="store_true", help="print the exact payload that would be published")
    sh.add_argument("--approve", nargs="*", default=[], help="SRC=DIGEST printed by --preview; publication needs it")
    shs = sub.add_parser("shares"); shs.add_argument("id")
    args = ap.parse_args()

    ws = Workspaces()
    if args.cmd == "create":
        print(ws.create(args.id, args.title))
    elif args.cmd == "list":
        show(ws.workspaces())
    elif args.cmd == "add":
        if bool(args.text) == bool(args.file):
            ap.error("give exactly one of --text or --file")
        text = args.text or pathlib.Path(args.file).read_text()
        title = args.title or (pathlib.Path(args.file).stem if args.file else text[:60])
        show(ws.remember(args.id, title, text, source_type=args.type, url=args.url, actor=args.actor))
    elif args.cmd == "search":
        for r in ws.search(args.id, args.query, k=args.k, mode=args.mode):
            print(f"[{r['rrf_score']:.5f}] vec#{r['vec_rank']} lex#{r['lex_rank']}  {r['source_title']}  ({r['chunk_id']})")
            print("   " + " ".join(r["content"].split())[:200])
    elif args.cmd == "sources":
        show(ws.sources(args.id))
    elif args.cmd == "copy":
        print(ws.copy_from_zeug(args.id, args.source_id, actor=args.actor))
    elif args.cmd == "share":
        # Nothing publishes unless it is named in --accept (as is) or --redact; a clean scan is not approval.
        from zme_mcp.share import digest, prepare, preview, publish, scan
        titles = dict(x.split("=", 1) for x in args.title)
        approvals = dict(x.split("=", 1) for x in args.approve)
        for s in scan(ws, args.id, args.sources, args.mask):
            mode = "redacted" if s.source_id in args.redact else "as-is" if s.source_id in args.accept else None
            state = mode or ("not approved" if not s.flags else "held")
            print(f"\n{s.source_id}  {s.snap.title}  [{s.snap.source_type}, {len(s.snap.text)} chars]  flags: {len(s.flags)}  -> {state}")
            for f in s.flags[:15]:
                print(f"   {f.where} line {f.line}: {f.reason} ({f.action}): {f.text}")
            if len(s.flags) > 15:
                print(f"   ... {len(s.flags) - 15} more")
            if not mode or not (args.publish or args.preview):
                continue
            try:
                payload = prepare(s, args.id, mode, args.mask, titles.get(s.source_id))
            except ValueError as e:
                print(f"   held: {e}")
                continue
            if args.preview:
                print(f"   --- {mode} payload ---")
                print("\n".join("   " + line for line in preview(payload).splitlines()))
                print(f"   to publish exactly this: --approve {s.source_id}={digest(args.id, mode, payload)}")
            if args.publish:
                status, new_id = publish(ws, args.id, s, mode, args.actor, payload, approvals.get(s.source_id))
                print(f"   {status}{' as ' + new_id if new_id else ''}")
    elif args.cmd == "shares":
        with __import__("psycopg").connect(ws.dsn) as conn:
            show([dict(zip(("source", "copy", "mode", "flags", "by", "at"), r)) for r in conn.execute(
                "select zeug_source_id, workspace_source_id, mode, flags, shared_by, shared_at from zme_ws.shares where workspace = %s order by shared_at",
                (args.id,)).fetchall()])
    elif args.cmd == "embed-pending":
        show(ws.embed_pending())
    elif args.cmd == "drop":
        ws.drop(args.id, args.confirm)
        print(f"dropped {args.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
