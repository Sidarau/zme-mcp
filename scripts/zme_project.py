#!/usr/bin/env python3
"""Exact project references; selected collaborator notes require preview and digest approval.

Offline: list, register, alias, ref, draft, preview. Live: sync, search, sources, publish, share.
This is owner tooling. Collaborators use their project's CMD connection, never the owner's DB credentials.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from zme_mcp.projects import ValidationError, Registry, packet, preview, publish, references, save_private


def workspace():
    from zme_mcp.workspace import Workspaces
    return Workspaces()


def show(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", type=pathlib.Path, default=pathlib.Path.home() / ".config/zme/projects.json")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("sync", help="import provisioned workspace ids, preserving aliases; read-only on the server")
    reg = sub.add_parser("register", help="register an exact CMD id locally; does not create a workspace or grant access")
    reg.add_argument("id"); reg.add_argument("--name", required=True)
    alias = sub.add_parser("alias"); alias.add_argument("alias"); alias.add_argument("project")
    for cmd in ("ref", "sources"):
        sub.add_parser(cmd).add_argument("project")
    search = sub.add_parser("search"); search.add_argument("project"); search.add_argument("query")
    draft = sub.add_parser("draft", help="prepare selected, already rewritten collaborator text; never publish")
    draft.add_argument("project"); draft.add_argument("--file", type=pathlib.Path, required=True)
    draft.add_argument("--title", required=True); draft.add_argument("--kind", choices=("context", "research", "note"), default="note")
    draft.add_argument("--url"); draft.add_argument("--as", dest="actor", required=True)
    draft.add_argument("--out", type=pathlib.Path, required=True)
    pv = sub.add_parser("preview"); pv.add_argument("file", type=pathlib.Path)
    pub = sub.add_parser("publish"); pub.add_argument("project"); pub.add_argument("--draft", type=pathlib.Path, required=True)
    pub.add_argument("--approve", required=True); pub.add_argument("--as", dest="actor", required=True)
    share = sub.add_parser("share", help="forward selected existing sources to the existing reviewed share tool")
    share.add_argument("project"); share.add_argument("args", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    try:
        registry = Registry(args.registry)
        if args.command == "list":
            show([references(p) for p in registry.data["projects"]])
        elif args.command == "sync":
            for row in workspace().workspaces():
                if row["id"] == "internal":
                    continue
                registry.register(row["id"], row["title"])
            registry.save()
            show([references(p) for p in registry.data["projects"]])
        elif args.command == "register":
            registry.register(args.id, args.name); registry.save()
            show(references(registry.resolve(args.id)))
        elif args.command == "alias":
            registry.alias(args.project, args.alias); registry.save()
            show(references(registry.resolve(args.project)))
        elif args.command == "preview":
            show(preview(json.loads(args.file.read_text())))
        else:
            pid = registry.resolve(args.project)["id"]
            if args.command == "ref":
                show(references(registry.resolve(args.project)))
            elif args.command == "sources":
                show(workspace().sources(pid))
            elif args.command == "search":
                show(workspace().search(pid, args.query))
            elif args.command == "draft":
                doc = packet(pid, args.actor, args.kind, args.title.strip(), args.file.read_text().strip(), args.url)
                save_private(args.out, doc, replace=False)
                show(preview(doc))
            elif args.command == "publish":
                doc = json.loads(args.draft.read_text())
                show(publish(doc, pid, args.actor, args.approve, workspace))
            elif args.command == "share":
                return subprocess.call([sys.executable, str(pathlib.Path(__file__).with_name("zme_ws.py")), "share", pid, *args.args])
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        # Third-party exceptions may contain connection strings or input text; expose only our validation errors.
        if isinstance(error, ValidationError):
            print(f"Refused: {error}", file=sys.stderr)
        else:
            print("Refused: invalid or unreadable registry/draft, or unavailable operation.", file=sys.stderr)
        return 1
    except Exception:
        print("Operation unavailable. Check the workspace connection and local dependencies; no credentials are shown.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
