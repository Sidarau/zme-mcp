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
assert redact("file at /tmp/oc_model_v2.py was used", P) == ""   # a word follows: the end is unclear, the line goes (round 3)
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

# --- Codex round 3 ---------------------------------------------------------------------------------------------
from zme_mcp.share import digest  # noqa: E402
# unknown chunker versions never cross
snap = Snapshot("s5", "T", "note", None, "text", [{"chunk_index": 0, "content": "c", "context_prefix": None, "heading": None,
       "token_count": 3, "embedding": None, "embed_variant": None, "chunker_version": "CustomSecret"}])
assert prepare(Scan(snap, []), P, "as-is")["chunks"][0]["chunker_version"] is None
# approval is bound to the exact payload
a = prepare(Scan(Snapshot("s6", "T", "note", None, "safe text here", []), []), P, "as-is")
b = prepare(Scan(Snapshot("s6", "T", "note", None, "safe text here, plus an acquisition ceiling", []), []), P, "as-is")
assert digest(P, "as-is", a) != digest(P, "as-is", b) and digest(P, "as-is", a) == digest(P, "as-is", dict(a))
assert digest(P, "as-is", a) != digest("ecis", "as-is", a) and digest(P, "as-is", a) != digest(P, "redacted", a)
# YAML blocks and escaped newlines inside quotes
out = redact("keep\npassword: |\n  synthetic-super-secret\n  second line\nafter", P)
assert "synthetic" not in out and "second line" not in out and out == "keep\nafter", out
out = redact("keep\napi_key: >\n  folded-secret-value\nafter", P)
assert "folded" not in out and out == "keep\nafter", out
out = redact('keep\npassword: "first \\\ncontinued secret"\nafter', P)
assert "continued" not in out and out == "keep\nafter", out
# paths: a dot is not a boundary; file URIs; apostrophes inside double quotes
out = redact("keep\nsee /Users/alex/Acme.Inc Bankruptcy Case/settlement.xlsx now\nafter", P)
assert "Bankruptcy" not in out and out == "keep\nafter", out
out = redact("keep\nfile:///Users/alex/Acme Bankruptcy Case/settlement.xlsx\nafter", P)
assert "Bankruptcy" not in out and out == "keep\nafter", out
out = redact('see "/Users/alex/Client\'s Private Files/settlement.xlsx" now', P)
assert "Private Files" not in out and "settlement" not in out, out
assert redact("file at /tmp/oc_model_v2.py.", P) == "file at [removed]"   # trailing punctuation goes with the path
# percent-encoded URLs
snap = Snapshot("s7", "T", "note", "https://example.org/Custom%53ecret", "text long enough to share", [])
assert any(f.where.startswith("url") for f in scan_snapshot(P, snap, ["CustomSecret"]))
assert prepare(Scan(snap, []), P, "redacted", ["CustomSecret"])["url"] is None
# replacement titles are checked and redacted like titles
try:
    prepare(Scan(Snapshot("s8", "our floor is 20%", "note", None, "text long enough here", []), []), P, "redacted", title="Our floor plan")
    raise AssertionError("a flagged replacement title was accepted")
except ValueError:
    pass
pay = prepare(Scan(Snapshot("s9", "x", "note", None, "text long enough here", []), []), P, "redacted", ["CustomSecret"], title="Client CustomSecret plan")
assert "CustomSecret" not in pay["title"] and all("CustomSecret" not in (c["context_prefix"] or "") for c in pay["chunks"]), pay
# more forms
for line in ["Authorization: Bearer abcdef1234567890", "Use access code bluebird", "Call (415)5551212 tomorrow"]:
    assert flagged(line), line
assert "abcdef1234567890" not in redact("Authorization: Bearer abcdef1234567890", P)
assert "bluebird" not in redact("Use access code bluebird", P)
assert "5551212" not in redact("Call (415)5551212 tomorrow", P)
# fewer destructive false positives
for ok in ["The HTML markup includes accessible labels", "Authentication is passwordless: true for this service",
           "token_count: 512 per chunk", "Tokens: 500 used"]:
    assert not flagged(ok), ok
print("share tests (round 3): all passed")

# --- Codex round 4 ---------------------------------------------------------------------------------------------
# copied vectors are bound to the approval
mk = lambda emb: Snapshot("s10", "T", "note", None, "text", [{"chunk_index": 0, "content": "c", "context_prefix": None, "heading": None,  # noqa: E731
                                                              "token_count": 1, "embedding": emb, "embed_variant": "arctic-l-v1", "chunker_version": "md-h+window-v1"}])
assert digest(P, "as-is", prepare(Scan(mk("[0.1,0.2]"), []), P, "as-is")) != digest(P, "as-is", prepare(Scan(mk("[0.1,0.3]"), []), P, "as-is"))
# credential continuations
for text, secret in [("keep\npassword: | # credential\n  synthetic-secret\nafter", "synthetic-secret"),
                     ("keep\npassword: |2\n  synthetic-secret\nafter", "synthetic-secret"),
                     ("keep\r\npassword: |\r\n  synthetic-secret\r\nafter", "synthetic-secret"),
                     ("keep\napi_key:\n  synthetic-secret\nafter", "synthetic-secret"),
                     ("keep\nexport TOKEN=abc\\\ncontinued-secret\nafter", "continued-secret")]:
    assert flagged(text), text
    out = redact(text, P)
    assert secret not in out and "keep" in out and "after" in out, (text, out)
# names with suffixes and compact code assignments
for line in ["API_KEY_PROD=synthetic-secret", "PASSWORD_PROD: synthetic-secret", "Gate code=482915", "Door code:482915",
             "authorization: bearer abcdef1234567890", "Authorization: Basic dXNlcjpwYXNzd29yZA=="]:
    assert flagged(line), line
    assert "synthetic-secret" not in redact(line, P) and "482915" not in redact(line, P), line
assert "abcdef1234567890" not in redact("authorization: bearer abcdef1234567890", P)
# ambiguous paths
for line in ["keep\n/Users/alex/Acme _Bankruptcy Case/settlement.xlsx\nafter", "keep\n/Users/alex/Client's Private Files/x.xlsx\nafter",
             'keep\nsee "/Users/alex/Acme Bankruptcy Case/settlement.xlsx\nafter']:
    out = redact(line, P)
    assert "Bankruptcy" not in out and "Private Files" not in out and out == "keep\nafter", (line, out)
# phone numbers never eat the next line
assert redact("Call +14155551212\n2026 roadmap approved", P) == "Call [removed]\n2026 roadmap approved"
# masks reach technical fields
pay = prepare(Scan(mk("[0.1]"), []), P, "as-is", ["md-h+window-v1", "note"])
assert pay["chunks"][0]["chunker_version"] is None and pay["source_type"] == "document", pay
print("share tests (round 4): all passed")
assert not flagged("an eligible connection and a basic national/regional application")
print("share tests (round 4 follow-up): all passed")
