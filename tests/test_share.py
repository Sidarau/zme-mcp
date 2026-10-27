"""Regression cases for the share scanner and redaction (no database). Run: python3 tests/test_share.py"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from zme_mcp.share import clean_url, redact, scan_text  # noqa: E402

P = "opencollective"
def flagged(text, extra=None): return bool(scan_text(P, text, extra=extra))
def gone(secret, text, extra=None):
    out = redact(text, P, extra)
    assert secret not in out, f"{secret!r} survived in {out!r}"
    return out

# Common secret forms
for s in ["sk-proj-AbCdEfGhIjKlMnOpQrStUv123456", "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuv",
          "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "AKIAABCDEFGHIJKLMNOP"]:
    assert flagged(f"use {s} here"), s
    gone(s, f"use {s} here")
for line, secret in [("API_KEY=abc123def", "abc123def"), ('{"password": "two words here"}', "two words"),
                     ("access code = 482915", "482915"), ("The access code is 482915", "482915"),
                     ("DB_PASSWORD: hunter2", "hunter2")]:
    assert flagged(line), line
    gone(secret, f"keep this line\n{line}\nand this one")
assert "keep this line" in redact("keep this line\nAPI_KEY=abc123def\nand this one", P)
gone("innerstandard", "deck at example.app (code innerstandard). Base case breaks even.")
assert "Base case breaks even." in redact("deck at example.app (code innerstandard). Base case breaks even.", P)
# Phones, emails, paths
for s in ["(415)555-1212", "+44 (20) 7946 0958", "+4915566194922", "415.555.1212"]:
    assert flagged(f"call {s} today"), s
    gone(s, f"call {s} today")
for s in ["~/client/private.txt", "/Users/alex/My Drive/client notes.md", r"C:\Users\alex\secret.txt", "file:///tmp/don-intake.md"]:
    assert flagged(f"see {s} for details"), s
    out = gone(s.split(" ")[0], f"see {s} for details")
    assert "Drive" not in out and "notes.md" not in out, out
# No false alarms on ordinary content
for ok in ["EBITDA +49k in year one", "Meeting on 2026-10-15 at 10am", "https://example.org/home/alex/help",
           "revenue of 1,068,000 in Y1", "We will create a password for the client portal later"]:
    assert not flagged(ok), ok
    assert redact(ok, P) == ok, ok
# Other clients, case-insensitive; the project's own names are fine
assert flagged("we also run try day club ads") and not flagged("Open Collective deck v2")
# An explicit mask removes the whole literal even when it overlaps an email
out = gone("ConfidentialValue", "token: ConfidentialValue john@example.com", extra=["ConfidentialValue john@example.com"])
assert "john@" not in out
out = redact("x ConfidentialValue john@example.com y", P, ["ConfidentialValue john@example.com"])
assert out == "x [removed] y", out
# Line drops follow the text's own lines
assert redact("a\nour floor is 20%\nb", P) == "a\nb"
# URLs
assert clean_url("https://user:pw@example.com/x?token=1#f") is None
assert clean_url("https://example.com/a/b?token=1#f") == "https://example.com/a/b"
assert clean_url("file:///Users/alex/x.md") is None and clean_url("internal://note/abc") is None
print("share tests: all passed")

# --- Codex round 2 ---------------------------------------------------------------------------------------------
from zme_mcp.share import Scan, Snapshot, prepare, scan_snapshot  # noqa: E402
# URLs are scanned (masks included) and withheld when flagged
snap = Snapshot("s1", "Title", "note", "https://example.org/CustomSecret", "Plain text long enough to share.", [])
assert any(f.where == "url" for f in scan_snapshot(P, snap, ["CustomSecret"]))
# a flagged source type never reaches a prefix; it is normalized first
snap = Snapshot("s2", "Title", "password: synthetic-secret", None, "Plain text long enough to share here.", [])
assert any(f.where == "source type" for f in scan_snapshot(P, snap))
import zme_mcp.share as share_mod  # noqa: E402
share_mod.embed = lambda texts, query=False: None   # no network in tests
pay = prepare(Scan(snap, scan_snapshot(P, snap)), P, "redacted")
assert pay["source_type"] == "document" and all("synthetic" not in (c["context_prefix"] or "") for c in pay["chunks"]), pay
pay = prepare(Scan(Snapshot("s3", "T", "note", "https://example.org/CustomSecret", "text", []), []), P, "as-is", ["CustomSecret"])
assert pay["url"] is None
# as-is technical fields are checked
snap = Snapshot("s4", "T", "note", None, "text", [{"chunk_index": 0, "content": "c", "context_prefix": None, "heading": None,
       "token_count": 3, "embedding": "[0.1]", "embed_variant": "weird variant!", "chunker_version": "bad version with spaces"}])
c = prepare(Scan(snap, []), P, "as-is")["chunks"][0]
assert c["embedding"] is None and c["embed_variant"] is None and c["chunker_version"] is None, c
# multiline secrets
pem = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----\nafter"
out = redact(pem, P)
assert "MIIEow" not in out and "AAAA" not in out and "before" in out and "after" in out, out
out = redact('keep\npassword: "first\nsecond third"\nalso keep', P)
assert "second third" not in out and "first" not in out and "keep" in out and "also keep" in out, out
out = redact('keep\npassword: "never closed\nrest of the file', P)
assert "never closed" not in out and "rest of the file" not in out, out
# paths with spaces: quoted paths are masked whole; unquoted ones with an unclear end drop the line
out = redact('see "/Users/alex/Acme Bankruptcy Case/settlement.xlsx" now', P)
assert "Bankruptcy" not in out and "settlement" not in out and out.startswith("see"), out
out = redact("keep\nopen /Users/alex/Acme Bankruptcy Case/settlement.xlsx now\nkeep too", P)
assert "Bankruptcy" not in out and out == "keep\nkeep too", out
assert redact("file at /tmp/oc_model_v2.py was used", P) == "file at [removed] was used"
# forms that used to pass as clean
for line in ["AWS_SECRET_ACCESS_KEY=abcd1234efgh", "Use access code 482915", "Call 4155551212 tomorrow",
             r"\\server\client\private.txt", "client_secret: xyz789"]:
    assert flagged(line), line
assert "482915" not in redact("Use access code 482915", P)
assert "4155551212" not in redact("Call 4155551212 tomorrow", P)
assert "private.txt" not in redact(r"see \\server\client\private.txt here", P)
# fewer destructive false positives
for ok in ["The client retains ownership of the deliverables.", "Display red flags beside overdue tasks.",
           "Revenue grew to 1068000 in Y1", "Stakeholders meet on Monday", "Order 12345678 shipped"]:
    assert not flagged(ok), ok
print("share tests (round 2): all passed")
