"""End-to-end OAuth verification — the exact dance ChatGPT performs.

Against a live server (default http://127.0.0.1:8378):

  1. protected-resource + authorization-server metadata discovery
  2. dynamic client registration (DCR)
  3. GET /authorize          → login form, not an auto-approve
  4. POST /authorize         → bad passphrase rejected, good one redirects with code
  5. POST /token (PKCE S256) → access token carries the profile's scopes
  6. MCP initialize with the token      → 200
  7. MCP initialize without a token     → 401
  8. viewer token calls `remember`      → 403 insufficient_scope
  9. owner token calls `remember`       → works

Usage:
  ZME_AUTH=oauth ZME_OAUTH_PROFILES='{"alex":{"secret":"s3cret","level":"owner"},
  "guest":{"secret":"viewonly","level":"viewer"}}' \
    .venv/bin/zme-mcp --http --port 8378 &
  .venv/bin/python scripts/verify_oauth.py
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8378"
OWNER_PASS = os.environ.get("VERIFY_OWNER_PASS", "s3cret")
VIEWER_PASS = os.environ.get("VERIFY_VIEWER_PASS", "viewonly")
failures = 0


def check(label, ok, detail=""):
    global failures
    print(("PASS" if ok else "FAIL"), label, (f"— {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


def oauth_dance(client: httpx.Client, name: str, passphrase: str) -> tuple[str, list[str]]:
    """DCR + authorize + PKCE exchange. Returns (access_token, scopes)."""
    # 1. discovery
    meta = client.get(f"{BASE}/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].startswith(BASE), meta

    # 2. dynamic client registration
    reg = client.post(meta["registration_endpoint"], json={
        "client_name": "verify-oauth",
        "redirect_uris": ["http://localhost:9999/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "zme:read zme:write",
    })
    assert reg.status_code in (200, 201), reg.text
    cid = reg.json()["client_id"]

    # 3. authorize: GET shows a login form
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": cid,
         "redirect_uri": "http://localhost:9999/callback",
         "code_challenge": challenge, "code_challenge_method": "S256",
         "state": "xyz", "scope": "zme:read zme:write",
         "resource": f"{BASE}/mcp"}
    form_page = client.get(f"{BASE}/authorize", params=q)
    assert "passphrase" in form_page.text, form_page.text[:200]

    # 4a. bad passphrase is rejected, no code leaks
    bad = client.post(f"{BASE}/authorize", data={**q, "name": name,
                                                 "passphrase": "wrong"},
                      follow_redirects=False)
    assert bad.status_code == 200 and "bad passphrase" in bad.text, bad.status_code
    assert "code=" not in bad.text

    # 4b. good passphrase → 303 redirect with ?code=
    good = client.post(f"{BASE}/authorize", data={**q, "name": name,
                                                  "passphrase": passphrase},
                       follow_redirects=False)
    assert good.status_code == 303, (good.status_code, good.text[:300])
    loc = good.headers["location"]
    code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]

    # 5. PKCE token exchange
    tok = client.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "http://localhost:9999/callback",
        "client_id": cid, "code_verifier": verifier,
    })
    assert tok.status_code == 200, tok.text
    body = tok.json()
    return body["access_token"], body.get("scope", "").split()


def mcp_call(client, token, method, params=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = client.post(f"{BASE}/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return r


def main() -> int:
    with httpx.Client(timeout=30) as client:
        # 1. metadata endpoints (RFC 9728: resource path is appended)
        prm = client.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
        check("protected-resource metadata", prm.status_code == 200)
        asm = client.get(f"{BASE}/.well-known/oauth-authorization-server")
        check("authorization-server metadata", asm.status_code == 200)

        # 2-5. full dance for owner and viewer
        owner_token, owner_scopes = oauth_dance(client, "alex", OWNER_PASS)
        check("owner dance", bool(owner_token))
        check("owner scopes include write", "zme:write" in owner_scopes, str(owner_scopes))
        viewer_token, viewer_scopes = oauth_dance(client, "guest", VIEWER_PASS)
        check("viewer dance", bool(viewer_token))
        check("viewer scopes are read-only",
              viewer_scopes == ["zme:read"], str(viewer_scopes))

        # 6/7. MCP endpoint auth
        r = mcp_call(client, owner_token, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "verify", "version": "0"}})
        check("MCP with token", r.status_code == 200, r.text[:120])
        r = mcp_call(client, None, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "verify", "version": "0"}})
        check("MCP without token → 401", r.status_code == 401, str(r.status_code))

        # 8/9. scope enforcement on the write tool (need a session first)
        for label, token, want in (("viewer", viewer_token, (401, 403)),
                                   ("owner", owner_token, (200,))):
            h = {"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {token}"}
            init = client.post(f"{BASE}/mcp", headers=h, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "verify", "version": "0"}}})
            sid = init.headers.get("mcp-session-id")
            client.post(f"{BASE}/mcp", headers={**h, "mcp-session-id": sid}, json={
                "jsonrpc": "2.0", "method": "notifications/initialized"})
            call = client.post(f"{BASE}/mcp", headers={**h, "mcp-session-id": sid}, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "remember",
                           "arguments": {"text": "oauth verify note — scope enforcement check",
                                         "title": "oauth verify"}}})
            body = call.json() if call.headers.get("content-type", "").startswith(
                "application/json") else None
            if body is None:  # SSE envelope
                line = [l for l in call.text.splitlines() if l.startswith("data:")][0]
                body = json.loads(line[5:])
            denied = body.get("error", {}).get("code") in (-32001, 403) or \
                "scope" in json.dumps(body.get("error", {})).lower() or \
                "unknown tool" in json.dumps(body.get("result", {})).lower() or \
                (body.get("result", {}).get("isError") and
                 "scope" in json.dumps(body.get("result", {})).lower())
            if label == "viewer":
                check("viewer remember → scope-denied", denied, json.dumps(body)[:300])
            else:
                ok = "result" in body
                check("owner remember works", ok, call.text[:200])

    print("\n" + ("ALL PASS" if failures == 0 else f"{failures} FAILURES"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
