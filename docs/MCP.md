# SimpleAI Proxy — secure remote MCP connection

## OAuth 2.1 browser login (recommended for ChatGPT)

The same HTTPS `/mcp` endpoint now supports both the original manually
issued bearer tokens and OAuth 2.1. ChatGPT clients should select **OAuth**
when adding the custom MCP server: no API key needs to be pasted.

- Resource metadata (RFC 9728):
  `https://aiproxy.cekluka.com/.well-known/oauth-protected-resource`
- Authorization server metadata (RFC 8414):
  `https://aiproxy.cekluka.com/.well-known/oauth-authorization-server`
- OAuth endpoints:
  `/oauth/register` (RFC 7591 dynamic client registration),
  `/oauth/authorize` (admin login and explicit consent),
  `/oauth/token` (S256 PKCE code exchange/refresh rotation),
  `/oauth/revoke` (token revocation)

The first unauthenticated MCP request now advertises both
`simpleai:read` and `simpleai:operate` in its OAuth
`WWW-Authenticate` scope challenge. MCP clients prioritize this challenge
over the metadata's `scopes_supported` list. This makes the consent page
offer both permissions, with **read-only selected by default** and explicit
admin approval required for operate. An existing read-only OAuth connection
must be disconnected and reconnected to grant a stronger scope; previously
issued access tokens are never silently elevated.

**ChatGPT plan limitation:** As of November 2026, full custom MCP write
capabilities in ChatGPT require a Business or Enterprise/Edu workspace.
Personal plans may connect for read/fetch only; changing the OAuth scope
does not override ChatGPT's account or workspace permissions. Full write
operations still work in other MCP clients that allow them and in eligible
ChatGPT workspaces.

ChatGPT's supported OAuth registration paths include **DCR**, so this
server advertises a public-client `registration_endpoint`. Its redirect
URI is registered with an exact match and the code verifier must prove
possession of the corresponding PKCE S256 challenge. The consent page uses
the same SimpleAI administrator login (HTTP Basic authentication over
HTTPS), a short-lived one-time request and a CSRF nonce. The user may
approve **read-only** access or **read + operate** if the client requested
the stronger scope. Approval is never automatic.

Access tokens are scoped to `https://aiproxy.cekluka.com/mcp`, expire in
one hour, and are stored only as SHA-256 hashes in `app.db`. Clients
requesting `offline_access` receive a 30-day rotating refresh token.
Access tokens cannot be used as SimpleAI regular API keys or admin login
credentials. Errors include a protected-resource `WWW-Authenticate`
challenge, and tools advertise their OAuth scopes individually.

For a ChatGPT connection:

1. Go to ChatGPT Plugins and add a custom remote MCP server with URL
   `https://aiproxy.cekluka.com/mcp`; choose **OAuth**.
2. ChatGPT performs public-client registration and opens SimpleAI's
   browser login and consent page.
3. Sign in using your SimpleAI administrator credentials and choose the
   minimum scope that your workflow requires (read-only by default).
4. Return to ChatGPT and test the `system_status` MCP tool. If you chose
   read+operate, model and worker action tools are also available.

To disconnect, open `https://aiproxy.cekluka.com/admin/mcp` and select
**Disconnect** for the authorized OAuth client. This revokes all access and
refresh tokens issued to that client. Legacy manually issued MCP keys
remain managed separately on the same page.

**Notes:** HTTPS must terminate at Nginx Proxy Manager with a valid public
certificate, and NPM must forward `/.well-known/` and `/oauth/` routes to
the same SimpleAI proxy. No new ports or additional containers are needed.
OAuth consent works for one existing SimpleAI admin identity; this is not
a multi-tenant identity provider. Do not use OAuth client credentials or
disable authentication to work around a linking issue.



SimpleAI Proxy includes an **opt-in Streamable HTTP MCP server** at
`https://aiproxy.cekluka.com/mcp`, on the same FastAPI/Nginx Proxy Manager
HTTPS hostname and port as the admin. No Docker socket, second container
or new public port is required.

## What it does

**Read/diagnostic tools**:
`system_status`, `ui_state`, `list_providers`, `list_groups`,
`list_models`, `list_workers`, `get_worker`, `worker_events`,
`request_logs`, `provider_attempts`, `proxy_runtime_logs`,
`error_overview`, `codex_status`, `get_operation`, `inspect_gguf`,
`playground_targets`.

**Operate tools** (separate scoped connection):
`add_gguf_model`, `set_model_policy`, `set_placement`,
`worker_model_action`, `save_group`, `run_playground`.

Write tools require `confirm=true`. Deleting cached worker model weights
also requires `confirm_delete=true`. Nothing may edit keys, OAuth
credentials, arbitrary files, raw Docker config, remote shell commands,
host Docker sockets or provider auth settings.

Tools call the **same in-process** group, GGUF catalog, routing and worker
action implementations used by the admin. `run_playground` exercises the
real `/v1/chat/completions` route with a one-time API key deleted afterward,
a short test prompt, max 256 output tokens, a private-data routing policy
and a maximum ~100-second timeout. It may use paid provider credits.

`ui_state` compares backing state for admin page cards and reports broken
group/model references; it cannot see the rendered user browser or inspect
Javascript from a connected user's desktop. `proxy_runtime_logs` collects
bounded **structured** Python error categories in the mounted `cache/`
directory, not arbitrary host Docker/journald/stdout log contents. Raw
logs could contain prompts, passwords or other private data. Worker events
are the privacy-safe WSS diagnostics introduced in PR #48 and depend on
worker image upgrades. The request log and ProviderAttempts tools expose
routing decisions, status codes, timings and **error categories**, not raw
request prompt/output or unredacted exception messages.

## Legacy manual bearer credentials

1. Open `https://aiproxy.cekluka.com/admin/mcp` and sign in with the existing
   SimpleAI proxy admin account.
2. Click **Generate private MCP token**. Choose a label and `read`
   (recommended for diagnosis) or `operate` (required for actions).
   Expiry is 7, 30 or 90 days. The token is displayed **once** and stored in
   SQLite *only as a SHA256 hash*.
3. Put the token in your trusted client authentication setting, never in
   a ChatGPT message, commit, issue, terminal command history or screenshot.
4. Revoke it from the same page at any time. No existing SimpleAI API keys or
   OAuth tokens are usable as MCP bearer tokens.

The MCP endpoint requires `Authorization: Bearer simpleai_mcp_...` on every
request. It accepts MCP versions `2025-03-26`, `2025-11-25`, and
`2026-07-28` through stateless JSON-RPC POST, with
`Accept: application/json, text/event-stream`. It responds to notifications
with HTTP 202 and GET with HTTP 405. Cross-origin browser requests are
denied unless explicitly allowed by `SIMPLEAI_MCP_ALLOWED_ORIGINS`.
It returns no secrets, even to an `operate` token. Connections are rate
limited, scoped and restricted to a finite tool allowlist.

## Connecting from clients

For a portable/private plugin, `mcp.json` should specify the remote server
using `streamable-http` and the OpenAI extension API-key **bearer** scheme.
The manifest must **not contain the actual credential**:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "simpleai": {
      "type": "streamable-http",
      "url": "https://aiproxy.cekluka.com/mcp",
      "extensions": {
        "com.openai": {
          "auth": {"type": "api_key", "headerScheme": "bearer"}
        }
      }
    }
  }
}
```

In ChatGPT, install/connect the private plugin if the account and workspace
support MCP custom plugins and their bearer-credential setup. **The ordinary
"Add custom MCP server" flow may offer OAuth/no-auth only**; do not select
No authentication and do not disable MCP bearer validation to work around
that. If the custom plugin cannot support a configured API-key credential,
use an MCP client that supports Bearer tokens, or add a proper OAuth 2.1
authorization server separately. Never publish a no-auth admin MCP endpoint.

Only users can authorize and install the connector in their ChatGPT account;
pushing a Docker image does **not** install a ChatGPT plugin.

## Simple transport smoke check (optional)

In a trusted terminal, read the token from a protected prompt and issue an
MCP initialization request. Do **not** paste the token into shell history:

```bash
read -rs -p 'MCP token: ' SIMPLEAI_MCP_TOKEN; echo
curl -fsS 'https://aiproxy.cekluka.com/mcp' \
  -H "Authorization: Bearer $SIMPLEAI_MCP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
unset SIMPLEAI_MCP_TOKEN
```

The result should identify `serverInfo.name: simpleai-proxy-mcp`. A missing
token returns HTTP 401. No credentials are logged or echoed.

## Rollout

Production deploys through the existing GitHub Actions workflow on merging
to `main`. Start with a read-only token and inspect `system_status`/
`list_workers` before granting `operate` to an assistant.

MCP access remains **off by default** until a dedicated token is generated.
Use distinct tokens per client and short expirations. Do not reuse API tokens
in any other service. Rotate by revoking and creating another token.
