"""Read-only live OAuth restart canary. Secrets remain in memory.

Inject VERIFY_OWNER_PASS and FLY_API_TOKEN through zeug-key. This script creates
its own OAuth client, restarts the named existing Fly machine, checks access and
refresh persistence, reads search/fetch, and revokes its grant. No remember calls.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://zme-mcp.fly.dev"
CLEANUP = {}


def require(ok, label):
    if not ok:
        raise RuntimeError(label)
    print("PASS", label, flush=True)


def payload(response):
    response.raise_for_status()
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return json.loads(next(line[5:] for line in response.text.splitlines()
                           if line.startswith("data:")))


def main():
    machine = sys.argv[1]
    with httpx.Client(timeout=90) as client:
        redirect = "http://localhost:9999/callback"
        reg = client.post(BASE + "/register", json={
            "client_name": "restart-canary", "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
            "scope": "zme:read"})
        require(reg.status_code in (200, 201), "client registration")
        cid = reg.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = client.post(BASE + "/authorize", data={
            "client_id": cid, "redirect_uri": redirect,
            "response_type": "code", "state": "restart-canary",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "scope": "zme:read", "name": "alex",
            "passphrase": os.environ["VERIFY_OWNER_PASS"]})
        require(auth.status_code == 303, "profile authorization")
        code = parse_qs(urlparse(auth.headers["location"]).query)["code"][0]
        response = client.post(BASE + "/token", data={
            "grant_type": "authorization_code", "client_id": cid,
            "code": code, "code_verifier": verifier, "redirect_uri": redirect})
        require(response.status_code == 200, "PKCE token issuance")
        tokens = response.json()
        CLEANUP.update(client_id=cid, token=tokens["refresh_token"])

        def call(token, method, params=None, sid=None):
            headers = {"Authorization": "Bearer " + token,
                       "Accept": "application/json, text/event-stream"}
            if sid:
                headers["mcp-session-id"] = sid
            return client.post(BASE + "/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": method,
                "params": params or {}})

        init_params = {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "restart-canary", "version": "1"}}
        require(call(tokens["access_token"], "initialize", init_params).status_code == 200,
                "access before restart")
        restarted = subprocess.run(["fly", "machine", "restart", machine,
                                    "--app", "zme-mcp"], capture_output=True)
        require(restarted.returncode == 0, "Fly machine restarted")
        for attempt in range(30):
            try:
                if client.get(BASE + "/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        init = call(tokens["access_token"], "initialize", init_params)
        require(init.status_code == 200, "same access token survives restart")
        rotated = client.post(BASE + "/token", data={
            "grant_type": "refresh_token", "client_id": cid,
            "refresh_token": tokens["refresh_token"], "scope": "zme:read"})
        require(rotated.status_code == 200, "same refresh grant survives restart")
        new = rotated.json()
        CLEANUP["token"] = new["refresh_token"]
        replay = client.post(BASE + "/token", data={
            "grant_type": "refresh_token", "client_id": cid,
            "refresh_token": tokens["refresh_token"]})
        require(replay.status_code in (400, 401), "rotated refresh replay denied")
        require(call(tokens["access_token"], "initialize", init_params).status_code == 401,
                "rotated access token denied")
        init = call(new["access_token"], "initialize", init_params)
        require(init.status_code == 200, "new access token accepted")
        sid = init.headers.get("mcp-session-id")
        search = payload(call(new["access_token"], "tools/call", {
            "name": "search", "arguments": {"query": "BMW dashboard"}}, sid))
        require("result" in search and not search["result"].get("isError"), "live search")
        content = search["result"].get("structuredContent")
        if content is None:
            content = json.loads(search["result"]["content"][0]["text"])
        results = content.get("results", [])
        if results:
            fetched = payload(call(new["access_token"], "tools/call", {
                "name": "fetch", "arguments": {"id": results[0]["id"]}}, sid))
            require("result" in fetched and not fetched["result"].get("isError"), "live fetch")
        else:
            print("INFO no BMW match in corpus; fetch not exercised", flush=True)
        revoked = client.post(BASE + "/revoke", data={
            "client_id": cid, "client_secret": "", "token": new["refresh_token"],
            "token_type_hint": "refresh_token"})
        require(revoked.status_code == 200, "canary grant revoked")
        CLEANUP.clear()
        require(call(new["access_token"], "initialize", init_params).status_code == 401,
                "revoked access denied")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # HTTP response bodies and OAuth objects may contain credentials.
        print("FAIL", type(exc).__name__, flush=True)
        sys.exit(1)
    finally:
        if CLEANUP:
            try:
                with httpx.Client(timeout=15) as cleanup_client:
                    result = cleanup_client.post(BASE + "/revoke", data={
                        **CLEANUP, "client_secret": "",
                        "token_type_hint": "refresh_token"})
                    print("PASS cleanup" if result.status_code == 200
                          else "WARN cleanup failed", flush=True)
            except Exception:
                print("WARN cleanup unavailable", flush=True)
