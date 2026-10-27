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
