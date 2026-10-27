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
    elif args.cmd == "embed-pending":
        show(ws.embed_pending())
    elif args.cmd == "drop":
        ws.drop(args.id, args.confirm)
        print(f"dropped {args.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
