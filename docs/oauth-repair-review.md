# ZME ChatGPT OAuth repair

Owner request: restore ZME in ChatGPT. The old provider kept clients and grants
in process memory, and access expiry revoked the associated refresh grant.
ChatGPT calls returned UNAUTHORIZED while the service health endpoint was healthy.

The repair stores all six OAuth maps on a private SQLite volume, serializes code
and refresh consumption, retains refresh grants on access expiry, and enables
client-authenticated revocation. Registered redirects are validated and login
HTML is escaped. FastMCP is pinned to the reviewed 3.4.5 implementation.

## Evidence on October 4 2026

- Seven local persistence, replay, scope, expiry, rollback and HTTP tests pass.
- Independent reviewer passed the patch and verified two-provider refresh races.
- Fly release 11 uses one machine, 874699b0719978, with one encrypted 1 GB volume
  mounted at /data. OAuth database files are excluded from Git and build context.
- Live canary passed registration, profile authorization, PKCE, an actual machine
  restart, access and refresh persistence, rotation replay denial, live search and
  fetch, grant revocation and denial of the revoked access token.
- ChatGPT UI shows Alex's ZME account connected. Its registration and active grant
  survived the subsequent credential rotation. Existing tool bindings in this
  conversation still reference the old disconnected account; fresh consumer
  retrieval remains to be checked after the ChatGPT connection binding refreshes.
- A vault accessibility read exposed the previous profile passphrase. The cloud
  value and live Fly profile were replaced through private process memory/stdin.
  Roles were unchanged. A full live canary passed with the replacement; the
  already-connected ChatGPT grant was retained. No replacement value is logged.

## Operational limits and rollback

This is a singleton volume deployment, not high availability. Do not add
independent machines with separate OAuth state. Restoring an old database snapshot
can resurrect revoked grants; invalidate restored grants before opening traffic.

The original image is
registry.fly.io/zme-mcp:deployment-01KZM0BH9C6PQJ1KD9HSJKVPR8. An emergency code
rollback to that image restores the former behavior but invalidates connections
on restart and does not fix persistence. Retain the encrypted volume and arrange
a fresh sign-in if such a rollback is required. Do not print or export database
rows into logs or repository files.

Source publication is prepared on codex/zme-oauth-persistence. The bounded GitHub
session requires native approval in Zeug Keys before publishing a PR.
