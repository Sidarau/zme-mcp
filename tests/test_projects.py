"""Offline routing and publication guard tests; no provider, embeddings or live DB."""
import io
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zme_mcp.projects import DEFAULTS, Registry, approval, initialize, packet, payload, preview, publish, references, save_private

spec = importlib.util.spec_from_file_location("project_cli", ROOT / "scripts/zme_project.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "projects.json"
        self.registry = Registry(self.path)
        self.registry.register("ecis", "ECIS")
        self.registry.register("opencollective", "Open Collective")
        self.registry.alias("opencollective", "oc")
        self.registry.save()
        self.doc = packet("ecis", "alex@example.org", "context", "Export decisions",
                          "The team agreed to test the export before the next review.")

    def test_exact_references_and_no_default(self):
        self.assertEqual(self.registry.resolve("@OC")["id"], "opencollective")
        self.assertEqual(self.registry.resolve("ecis")["id"], "ecis")
        for bad in ("", "internal", "public", "zeug", "@all", "default", "eci", "Open Collective", "../ecis"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.registry.resolve(bad)

    def test_future_project_template_is_offline_and_defaults_to_draft(self):
        target = pathlib.Path(self.temp.name) / "future-project"
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            result = initialize(self.registry, "sample-project", "Sample project", ["sample"], target)
        self.assertFalse(result["provisioned"])
        self.assertFalse(result["published"])
        config = json.loads((target / ".zeug/memory-routing.json").read_text())
        self.assertEqual(config["defaults"], DEFAULTS)
        self.assertTrue(config["defaults"]["exclude_internal"])
        self.assertTrue(config["defaults"]["preview_required"])
        self.assertFalse(config["defaults"]["auto_publish"])
        self.assertEqual(Registry(self.path).resolve("@sample")["id"], "sample-project")
        for name in result["files"]:
            self.assertEqual((target / name).stat().st_mode & 0o777, 0o600)
        self.assertFalse((target / ".zeug/project.json").exists())
        self.assertEqual(references(self.registry.resolve("@sample"))["defaults"], DEFAULTS)

    def test_template_collision_preserves_all_files_and_registry(self):
        target = pathlib.Path(self.temp.name) / "existing"
        target.mkdir()
        (target / "research.md").write_text("Keep my work")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            initialize(self.registry, "sample-project", "Sample project", ["sample"], target)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((target / "research.md").read_text(), "Keep my work")
        self.assertFalse((target / "MEMORY.md").exists())

    def test_alias_collision_does_not_reassign(self):
        with self.assertRaises(ValueError):
            self.registry.alias("ecis", "oc")
        with self.assertRaises(ValueError):
            self.registry.alias("ecis", "opencollective")
        with self.assertRaises(ValueError):
            self.registry.register("oc", "Unrelated")
        self.assertEqual(Registry(self.path).resolve("oc")["id"], "opencollective")

    def test_registry_rejects_tampered_duplicate(self):
        self.registry.data["projects"][0]["aliases"].append("oc")
        with self.assertRaises(ValueError):
            self.registry.save()

    def test_private_file_no_accidental_overwrite(self):
        target = pathlib.Path(self.temp.name) / "draft.json"
        save_private(target, self.doc, replace=False)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            save_private(target, {"wrong": True}, replace=False)
        self.assertEqual(json.loads(target.read_text()), self.doc)

    def test_offline_preview_shows_every_outgoing_field(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
            p = preview(self.doc)
        self.assertEqual(p["outgoing"], payload(self.doc))
        self.assertEqual(p["actor"], self.doc["actor"])
        self.assertEqual(p["project"], "ecis")
        self.assertTrue(p["outgoing"]["chunks"])
        self.assertEqual(p["metadata"], {"route": "reviewed-project-packet-v1", "kind": "context"})

    def test_flagged_text_is_held_without_leaking_input(self):
        for text in ("Internal only: keep this account proposal inside the studio.",
                     "Our floor is 5000 and our margin is 80 percent on this deal.",
                     "Try Day Club needs this from the other customer.",
                     "Authorization: Bearer abcdefghijklmnop",
                     'See "/Users/alex/Client\\\"s Private/settlement.xlsx" now.',
                     "API_KEY: # credential\n  synthetic-value-not-for-collaborators"):
            with self.subTest(text=text), self.assertRaises(ValueError) as ctx:
                packet("ecis", "alex@example.org", "note", "Meeting", text)
            self.assertNotIn(text, str(ctx.exception))

    def test_url_metadata_and_unknown_fields_held(self):
        for url in ("https://user:pass@example.org/a", "https://example.org?a=b", "https://example.org/#code",
                    "file:///tmp/test", "https://example.org/our%20floor", "https://localhost/test",
                    "http://100.118.214.24/test", "https://graph.zeuglab.com/g/private"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                packet("ecis", "alex@example.org", "research", "Sources", self.doc["text"], url)
        altered = {**self.doc, "metadata": {"raw_internal_source": "hidden"}}
        with self.assertRaises(ValueError):
            payload(altered)

    def test_approval_is_bound_before_workspace_access(self):
        original = approval(self.doc)
        factory = Mock(side_effect=AssertionError("must not connect"))
        for key, value in (("text", self.doc["text"] + " A later edit."), ("project", "opencollective"),
                           ("actor", "editor@example.org"), ("title", "Changed"), ("kind", "research"),
                           ("url", "https://example.org/evidence")):
            changed = {**self.doc, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish(changed, "ecis", self.doc["actor"], original, factory)
        for project, actor, digest in (("opencollective", self.doc["actor"], original),
                                       ("ecis", "editor@example.org", original), ("ecis", self.doc["actor"], "")):
            with self.assertRaises(ValueError):
                publish(self.doc, project, actor, digest, factory)
        factory.assert_not_called()

    def test_publish_only_exact_workspace_payload(self):
        ws = Mock()
        ws.remember.return_value = {"id": "synthetic-id", "chunks": 1, "embedded": False}
        result = publish(self.doc, "ecis", self.doc["actor"], approval(self.doc), lambda: ws)
        self.assertEqual(result["project"], "ecis")
        ws.remember.assert_called_once_with("ecis", self.doc["title"], self.doc["text"], source_type="analysis",
                                          url=None, actor=self.doc["actor"],
                                          metadata={"route": "reviewed-project-packet-v1", "kind": "context"})

    def test_citations_are_not_a_unique_note_identity(self):
        first = {**self.doc, "url": "https://example.org/research"}
        second = {**first, "text": "A different finding about the same public source must be retained."}
        for doc in (first, second):
            ws = Mock()
            ws.remember.return_value = {"id": "test-id"}
            publish(doc, "ecis", doc["actor"], approval(doc), lambda: ws)
            call = ws.remember.call_args
            self.assertIsNone(call.kwargs["url"])
            self.assertEqual(call.args[2], doc["text"] + "\n\nSource: https://example.org/research")
            self.assertEqual(preview(doc)["outgoing"]["text"], call.args[2])
        self.assertNotEqual(payload(first)["text"], payload(second)["text"])

    def test_malformed_url_error_never_echoes_rejected_content(self):
        source = pathlib.Path(self.temp.name) / "selected.md"
        source.write_text(self.doc["text"])
        secret = "sk-abcdefghijklmnopqrstuv"
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), patch.object(cli, "workspace", side_effect=AssertionError("DB forbidden")):
            result = cli.main(["--registry", str(self.path), "draft", "@ecis", "--file", str(source),
                               "--title", "Research", "--url", f"https://[{secret}]/", "--as", self.doc["actor"],
                               "--out", str(pathlib.Path(self.temp.name) / "draft.json")])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("Refused:", stderr.getvalue())

    def test_cli_draft_never_connects_and_preserves_input(self):
        source = pathlib.Path(self.temp.name) / "selected.md"
        out = pathlib.Path(self.temp.name) / "packet.json"
        source.write_text(self.doc["text"])
        with patch.object(cli, "workspace", side_effect=AssertionError("DB forbidden")), patch("builtins.print"):
            code = cli.main(["--registry", str(self.path), "draft", "@ecis", "--file", str(source),
                             "--title", "Export decisions", "--as", self.doc["actor"], "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(source.read_text(), self.doc["text"])
        self.assertEqual(json.loads(out.read_text())["project"], "ecis")

    def test_cli_share_resolves_alias_without_shell(self):
        with patch.object(cli.subprocess, "call", return_value=0) as call:
            code = cli.main(["--registry", str(self.path), "share", "@oc", "source-id", "--preview", "--as", "alex@example.org"])
        self.assertEqual(code, 0)
        forwarded = call.call_args.args[0]
        self.assertEqual(forwarded[2:], ["share", "opencollective", "source-id", "--preview", "--as", "alex@example.org"])


if __name__ == "__main__":
    unittest.main()
