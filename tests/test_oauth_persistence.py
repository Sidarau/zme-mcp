import asyncio
import base64
import hashlib
from pathlib import Path
import stat
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import httpx
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from zme_mcp.auth import ZeuglabOAuthProvider
from zme_mcp.oauth_store import SQLiteMapping


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private" / "oauth.sqlite3"
        self.providers = []
        self.p = self.open()
        self.client = OAuthClientInformationFull(
            client_id="client-a", redirect_uris=["http://localhost:9999/callback"],
            token_endpoint_auth_method="none", scope="zme:read zme:write")
        await self.p.register_client(self.client)

    def open(self):
        p = ZeuglabOAuthProvider("http://localhost:8000", {}, self.path)
        self.providers.append(p)
        return p

    async def asyncTearDown(self):
        for p in self.providers:
            p.store.close()
        self.tmp.cleanup()

    async def code(self, scopes=None):
        redirect = await self.p.authorize(self.client, AuthorizationParams(
            redirect_uri=AnyUrl("http://localhost:9999/callback"),
            redirect_uri_provided_explicitly=True, state="state", scopes=scopes or ["zme:read"],
            code_challenge="pkce-challenge"))
        value = parse_qs(urlparse(redirect).query)["code"][0]
        return await self.p.load_authorization_code(self.client, value)

    async def tokens(self, scopes=None):
        return await self.p.exchange_authorization_code(self.client, await self.code(scopes))

    async def test_registration_code_tokens_and_refresh_survive_restart(self):
        code = await self.code(["zme:read", "zme:write"])
        p2 = self.open()
        client = await p2.get_client("client-a")
        code = await p2.load_authorization_code(client, code.code)
        tokens = await p2.exchange_authorization_code(client, code)
        p3 = self.open()
        access = await p3.load_access_token(tokens.access_token)
        self.assertEqual(access.scopes, ["zme:read", "zme:write"])
        refresh = await p3.load_refresh_token(client, tokens.refresh_token)
        rotated = await p3.exchange_refresh_token(client, refresh, ["zme:read"])
        p4 = self.open()
        self.assertIsNone(await p4.load_access_token(tokens.access_token))
        self.assertIsNone(await p4.load_refresh_token(client, tokens.refresh_token))
        self.assertEqual((await p4.load_access_token(rotated.access_token)).scopes, ["zme:read"])

    async def test_revocation_survives_restart(self):
        tokens = await self.tokens()
        p2 = self.open()
        await p2.revoke_token(await p2.load_access_token(tokens.access_token))
        p3 = self.open()
        self.assertIsNone(await p3.load_access_token(tokens.access_token))
        self.assertIsNone(await p3.load_refresh_token(self.client, tokens.refresh_token))

    async def test_stale_code_and_refresh_cannot_be_replayed_by_second_provider(self):
        code = await self.code()
        p2 = self.open()
        stale = await p2.load_authorization_code(self.client, code.code)
        tokens = await self.p.exchange_authorization_code(self.client, code)
        with self.assertRaises(TokenError):
            await p2.exchange_authorization_code(self.client, stale)
        refresh = await self.p.load_refresh_token(self.client, tokens.refresh_token)
        stale_refresh = await p2.load_refresh_token(self.client, tokens.refresh_token)
        await self.p.exchange_refresh_token(self.client, refresh, ["zme:read"])
        with self.assertRaises(TokenError):
            await p2.exchange_refresh_token(self.client, stale_refresh, ["zme:read"])

    async def test_client_binding_expiry_and_scope_escalation(self):
        tokens = await self.tokens()
        other = self.client.model_copy(update={"client_id": "other"})
        self.assertIsNone(await self.p.load_refresh_token(other, tokens.refresh_token))
        refresh = await self.p.load_refresh_token(self.client, tokens.refresh_token)
        with self.assertRaises(TokenError):
            await self.p.exchange_refresh_token(self.client, refresh, ["zme:read", "zme:write"])
        self.assertIsNotNone(await self.p.load_refresh_token(self.client, tokens.refresh_token))
        self.p.access_tokens[tokens.access_token] = (
            await self.p.load_access_token(tokens.access_token)).model_copy(
                update={"expires_at": int(time.time()) - 1})
        self.assertIsNone(await self.open().load_access_token(tokens.access_token))
        after_expiry = self.open()
        refresh = await after_expiry.load_refresh_token(self.client, tokens.refresh_token)
        self.assertIsNotNone(refresh)
        self.assertIsNotNone(await after_expiry.exchange_refresh_token(self.client, refresh, ["zme:read"]))

    async def test_failed_exchange_rolls_back_code_consumption_and_partial_tokens(self):
        code = await self.code()
        original = SQLiteMapping.__setitem__
        def failing_set(mapping, key, value):
            if mapping.namespace == "refresh_to_access":
                raise RuntimeError("simulated disk failure")
            original(mapping, key, value)
        with patch.object(SQLiteMapping, "__setitem__", failing_set):
            with self.assertRaises(RuntimeError):
                await self.p.exchange_authorization_code(self.client, code)
        p2 = self.open()
        self.assertIsNotNone(await p2.load_authorization_code(self.client, code.code))
        self.assertEqual(len(p2.access_tokens), 0)
        self.assertEqual(len(p2.refresh_tokens), 0)
        self.assertIsNotNone(await p2.exchange_authorization_code(self.client, code))

    async def test_private_file_and_unregistered_redirect(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        with self.assertRaises(ValueError):
            await self.p.authorize(self.client, AuthorizationParams(
                redirect_uri=AnyUrl("https://attacker.example/callback"),
                redirect_uri_provided_explicitly=True, scopes=["zme:read"],
                code_challenge="challenge"))


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_pkce_profile_scopes_and_restarted_token(self):
        from zme_mcp.server import build_server, _http_app
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "ZME_AUTH": "oauth", "ZME_OAUTH_DB": str(Path(tmp)/"oauth.sqlite3"),
            "ZME_OAUTH_PROFILES": '{"guest":{"secret":"test-only","level":"viewer"}}',
            "ZME_MCP_TOKEN": "",
        }):
            mcp = build_server(http=True, base_url="http://localhost:8000")
            app = _http_app(mcp)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://localhost:8000") as c:
                reg = await c.post("/register", json={
                    "redirect_uris": ["http://localhost:9999/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none",
                    "scope": "zme:read zme:write"})
                self.assertIn(reg.status_code, (200, 201))
                client_id = reg.json()["client_id"]
                verifier = "v" * 48
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                q = {"client_id": client_id, "redirect_uri": "http://localhost:9999/callback",
                     "response_type": "code", "scope": "zme:read zme:write",
                     "code_challenge": challenge, "code_challenge_method": "S256"}
                bad = await c.post("/authorize", data={**q, "name": "guest", "passphrase": "wrong"})
                self.assertEqual(bad.status_code, 200)
                good = await c.post("/authorize", data={**q, "name": "guest", "passphrase": "test-only"})
                self.assertEqual(good.status_code, 303)
                code = parse_qs(urlparse(good.headers["location"]).query)["code"][0]
                exchange = {"grant_type": "authorization_code", "client_id": client_id,
                            "code": code, "redirect_uri": q["redirect_uri"], "code_verifier": "wrong"}
                self.assertIn((await c.post("/token", data=exchange)).status_code, (400, 401))
                exchange["code_verifier"] = verifier
                token = await c.post("/token", data=exchange)
                self.assertEqual(token.status_code, 200)
                self.assertEqual(token.json()["scope"], "zme:read")
                self.assertIn((await c.post("/token", data=exchange)).status_code, (400, 401))
                restored = ZeuglabOAuthProvider("http://localhost:8000", {}, Path(tmp)/"oauth.sqlite3")
                access = await restored.verify_token(token.json()["access_token"])
                self.assertEqual(access.scopes, ["zme:read"])
                restored.store.close()
            mcp.auth.store.close()


if __name__ == "__main__":
    unittest.main()
