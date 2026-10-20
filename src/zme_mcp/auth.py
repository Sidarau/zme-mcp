"""Zeuglab OAuth — the alpha of the Zeug ecosystem auth model.

Rings (release channels)
------------------------
alpha   zeuglab ecosystem (this server). OAuth + optional legacy token.
beta    ClawPanel. OAuth only; profiles move to managed identities.
prod    ClawPanel multi-tenant. OAuth + per-profile labels, RLS-backed.

Profile levels (identity → capability)
--------------------------------------
owner     zme:read + zme:write   (everything)
operator  zme:read + zme:write   (same scopes in alpha; labels differentiate in prod)
viewer    zme:read               (search/fetch/recall/why/near; no remember)

This module subclasses FastMCP's InMemoryOAuthProvider — a complete OAuth 2.1
authorization server (DCR, PKCE, authorization codes, refresh, revocation) —
and adds a durable SQLite state store and a real login gate. The /authorize GET
serves a passphrase form; the POST verifies (name, passphrase) against
ZME_OAUTH_PROFILES and forces the token's scopes to that profile's level —
requested scopes are never trusted.

Clients, grants, tokens and rotation links persist on ZME_OAUTH_DB. A single
instance owns that database. Access expiry preserves the refresh grant; token
rotation and revocation are atomic. The scope contract remains unchanged.
"""
from __future__ import annotations

import asyncio
import functools
import html
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .oauth_store import OAuthStore


def durable_operation(method):
    """Atomically commit local SDK map operations, including token rotation.

    The wrapped inherited methods have no network awaits. SQLite also serializes
    token consumption by separate processes sharing this database.
    """
    @functools.wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self._operation_lock:
            with self.store.transaction():
                return await method(self, *args, **kwargs)
    return wrapped

LEVEL_SCOPES: dict[str, list[str]] = {
    "owner": ["zme:read", "zme:write"],
    "operator": ["zme:read", "zme:write"],
    "viewer": ["zme:read"],
}
ALL_SCOPES = sorted({s for scopes in LEVEL_SCOPES.values() for s in scopes})

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>zeuglab · zme</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:ui-sans-serif,system-ui;background:#0e1512;color:#efe9dc;
display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#16211c;border:1px solid #2a3a32;border-radius:12px;padding:2rem;
width:320px;display:grid;gap:.9rem}}
h1{{font-size:1.05rem;margin:0;font-weight:600}}p{{font-size:.8rem;color:#9fb0a6;margin:0}}
input{{background:#0e1512;border:1px solid #2a3a32;border-radius:8px;color:#efe9dc;
padding:.6rem .7rem;font-size:.9rem}}
button{{background:#c9a959;border:0;border-radius:8px;padding:.65rem;font-weight:600;
cursor:pointer}}.err{{color:#e08585;font-size:.8rem}}</style></head>
<body><form method="post">
<h1>Zeug Memory Engine</h1>
<p>{client} is requesting access. Sign in with your zeuglab profile.</p>
{error}
<input type="text" name="name" placeholder="profile name" autocomplete="username" required>
<input type="password" name="passphrase" placeholder="passphrase"
 autocomplete="current-password" required>
{hidden}
<button type="submit">Authorize</button></form></body></html>"""


class ZeuglabOAuthProvider(InMemoryOAuthProvider):
    """OAuth 2.1 AS with durable state and a profile-scoped login gate."""

    def __init__(self, base_url: str, profiles: dict[str, dict[str, str]],
                 state_path: str | Path | None = None):
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=ALL_SCOPES,
                default_scopes=["zme:read"]),
            required_scopes=["zme:read"],
            revocation_options=RevocationOptions(enabled=True),
        )
        # name -> {"secret": ..., "level": owner|operator|viewer}
        self.profiles = profiles
        self.store = OAuthStore(state_path or os.environ.get("ZME_OAUTH_DB")
                                or Path.home() / ".local/state/zme-mcp/oauth.sqlite3")
        self._operation_lock = asyncio.Lock()
        self.clients = self.store.mapping("clients", OAuthClientInformationFull)
        self.auth_codes = self.store.mapping("auth_codes", AuthorizationCode)
        self.access_tokens = self.store.mapping("access_tokens", AccessToken)
        self.refresh_tokens = self.store.mapping("refresh_tokens", RefreshToken)
        self._access_to_refresh_map = self.store.mapping("access_to_refresh")
        self._refresh_to_access_map = self.store.mapping("refresh_to_access")

    @durable_operation
    async def register_client(self, client_info):
        return await super().register_client(client_info)

    @durable_operation
    async def authorize(self, client, params):
        client.validate_redirect_uri(AnyUrl(str(params.redirect_uri)))
        return await super().authorize(client, params)

    @durable_operation
    async def load_authorization_code(self, client, authorization_code):
        return await super().load_authorization_code(client, authorization_code)

    @durable_operation
    async def exchange_authorization_code(self, client, authorization_code):
        current = await super().load_authorization_code(client, authorization_code.code)
        if current is None or current != authorization_code:
            raise TokenError("invalid_grant", "Authorization code is invalid or expired.")
        return await super().exchange_authorization_code(client, current)

    @durable_operation
    async def load_refresh_token(self, client, refresh_token):
        return await super().load_refresh_token(client, refresh_token)

    @durable_operation
    async def exchange_refresh_token(self, client, refresh_token, scopes):
        current = await super().load_refresh_token(client, refresh_token.token)
        if current is None or current != refresh_token:
            raise TokenError("invalid_grant", "Refresh token is invalid or expired.")
        return await super().exchange_refresh_token(client, current, scopes)

    @durable_operation
    async def load_access_token(self, token):
        current = self.access_tokens.get(token)
        if current is not None and current.expires_at is not None and current.expires_at < time.time():
            # Access expiry is not grant revocation. Keep the refresh token so
            # ChatGPT can renew its one-hour access token without signing in.
            del self.access_tokens[token]
            return None
        return current

    @durable_operation
    async def revoke_token(self, token):
        return await super().revoke_token(token)

    # -- routes ------------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        # Replace the SDK's /authorize (which would auto-approve) with the
        # login gate. Keep every other route (metadata, /token, /register…).
        return [r for r in routes if not (getattr(r, "path", None) == "/authorize")] + [
            Route("/authorize", self._authorize_get, methods=["GET"]),
            Route("/authorize", self._authorize_post, methods=["POST"]),
        ]

    async def _authorize_get(self, request: Request) -> HTMLResponse:
        q = dict(request.query_params)
        return self._page(q, error="")

    async def _authorize_post(self, request: Request) -> Any:
        form = await request.form()
        q = {k: str(v) for k, v in form.items()
             if k not in ("name", "passphrase", "csrf") and isinstance(v, str)}
        name = str(form.get("name", "")).strip().lower()
        secret = str(form.get("passphrase", ""))
        profile = self.profiles.get(name)

        if not profile or profile["secret"] != secret:
            return self._page(q, error='<span class="err">Unknown profile or bad passphrase.</span>')

        try:
            client_id = str(q["client_id"])
            client = await self.get_client(client_id)
            if client is None:
                client = await self._restore_chatgpt_client(client_id, q)
            if client is None:
                return self._page(q, error='<span class="err">Unregistered client.</span>')
            from mcp.server.auth.provider import AuthorizationParams
            params = AuthorizationParams(
                redirect_uri=str(q["redirect_uri"]),
                redirect_uri_provided_explicitly=True,
                state=q.get("state") or None,
                # Profile level decides scopes — never the client's request.
                scopes=LEVEL_SCOPES[profile["level"]],
                code_challenge=str(q["code_challenge"]),
                resource=q.get("resource") or None,
            )
            redirect_url = await self.authorize(client, params)
            return RedirectResponse(redirect_url, status_code=303)
        except Exception as e:  # noqa: BLE001 — surface as a login-page error
            return self._page(q, error=f'<span class="err">{html.escape(type(e).__name__ + ": " + str(e))}</span>')

    async def _restore_chatgpt_client(
        self, client_id: str, params: dict[str, str]
    ) -> OAuthClientInformationFull | None:
        """Restore ChatGPT's public client registration after a Fly restart.

        FastMCP keeps DCR clients in process memory. ChatGPT retains its client
        ID, so an ordinary Fly autostop otherwise breaks an existing connector.
        This permits only ChatGPT's fixed HTTPS connector callback—the same
        public-client registration DCR already permits ChatGPT to create.
        """
        redirect_uri = params.get("redirect_uri", "")
        parsed = urlparse(redirect_uri)
        is_chatgpt_callback = (
            parsed.scheme == "https"
            and parsed.hostname == "chatgpt.com"
            and parsed.path.startswith("/connector/oauth/")
            and bool(parsed.path.removeprefix("/connector/oauth/"))
        )
        if not client_id or not is_chatgpt_callback:
            return None

        client = OAuthClientInformationFull(
            client_id=client_id,
            client_name="ChatGPT",
            redirect_uris=[redirect_uri],
            token_endpoint_auth_method="none",
            scope="zme:read",
        )
        await self.register_client(client)
        return client

    def _page(self, q: dict[str, Any], error: str) -> HTMLResponse:
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(str(k), quote=True)}" '
            f'value="{html.escape(str(v), quote=True)}">' for k, v in q.items())
        client = html.escape(str(q.get("client_id", "An MCP client")), quote=True)
        return HTMLResponse(LOGIN_PAGE.format(
            client=client, error=error, hidden=hidden))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

RINGS = ("alpha", "beta", "prod")


def load_ring() -> str:
    ring = (os.environ.get("ZME_RING") or "alpha").strip().lower()
    if ring not in RINGS:
        raise RuntimeError(f"ZME_RING must be one of {RINGS}, got {ring!r}")
    return ring


def build_auth(base_url: str) -> ZeuglabOAuthProvider | None:
    """Returns the OAuth provider, or None when auth is token/none.

    Ring rules:
      alpha  — ZME_AUTH=oauth|token (default: oauth if profiles configured)
      beta   — oauth only (token mode is a startup error)
      prod   — oauth only, profiles must come from ClawPanel (not implemented)
    """
    ring = load_ring()
    mode = (os.environ.get("ZME_AUTH") or "").strip().lower()
    profiles_json = os.environ.get("ZME_OAUTH_PROFILES", "")

    if not mode:
        mode = "oauth" if profiles_json else "token"

    if ring in ("beta", "prod") and mode != "oauth":
        raise RuntimeError(
            f"ZME_AUTH={mode} is not allowed in ring {ring!r} — "
            f"token auth is alpha-only; use oauth.")
    if mode != "oauth":
        return None

    if not profiles_json:
        raise RuntimeError(
            "ZME_AUTH=oauth needs ZME_OAUTH_PROFILES — JSON like "
            '{"alex": {"secret": "…", "level": "owner"}, '
            '"guest": {"secret": "…", "level": "viewer"}}')
    profiles = {k.strip().lower(): v for k, v in json.loads(profiles_json).items()}
    for name, p in profiles.items():
        if p.get("level") not in LEVEL_SCOPES:
            raise RuntimeError(f"profile {name!r}: level must be one of {list(LEVEL_SCOPES)}")
    return ZeuglabOAuthProvider(base_url=base_url, profiles=profiles)
