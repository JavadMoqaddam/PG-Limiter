# Panel API-key authentication

PG-Limiter uses `PANEL_API_KEY` as its sole panel credential. REST requests,
SSE node-log streams and panel availability/startup probes send
`X-Api-Key: <key>`. There is no panel username/password login or token-refresh
fallback. These instructions apply to version 1.7.1 and later;
the `v1.7.0` installer and image predate API-key-only authentication.

## Configure the key

Create an active API key in the PasarGuard panel for the administrator whose
users the limiter should manage. PasarGuard 5.5.1 produces keys in the form
`pg_key_<UUIDv4>`. Copy the full key into the limiter's environment file:

```dotenv
PANEL_DOMAIN=panel.example.com:8443
PANEL_API_KEY=your_panel_api_key
PANEL_VERIFY_SSL=true
```

The standard installation stores this file at `/etc/opt/pg-limiter/.env`.
Keep it readable only by its owner (`chmod 600 /etc/opt/pg-limiter/.env`).
Interactive installation accepts the key through a hidden prompt. Use
`pg-limiter config` to edit the environment file in your editor. Telegram
panel settings explain this setup without collecting the key. CLI configuration
output reports whether the key is set; the management API configuration
response masks it.

A domain without a scheme uses HTTPS. Explicit `http://` selects HTTP; HTTPS
failures do not trigger an automatic HTTP retry. `PANEL_VERIFY_SSL` controls
certificate verification for both REST and SSE. Its existing default is
`false`; set it to `true` when the panel has a certificate trusted by the
limiter. Supply an HTTPS domain for an encrypted connection.

## Permissions and user scope

Grant only the operations you use. The table names the panel's permission
resource and action as `resource.action`.

| Limiter operation | Required panel permission |
| --- | --- |
| List and refresh the node fleet | `nodes.read` |
| Collect online IPs through the panel API and resolve per-user node scope | `nodes.stats`, `groups.read`, `cores.read` |
| Stream node logs through SSE, including automatic fallback | `nodes.logs` |
| Synchronize users and check current user facts | `users.read` |
| Disable/re-enable users or change their groups/proxy settings | `users.update` |
| Revoke a user's subscription when the punishment policy requests it | `users.revoke_sub` |
| Read groups for filters, group limits and group-based disable settings | `groups.read` |
| List administrators for administrator filters | `admins.read` |

Panel user permissions have a scope: Own (`1`) covers the key's administrator's
users; All (`2`) covers users across administrators. Choose scopes for
`users.read`, `users.update` and `users.revoke_sub` that cover the intended
monitoring and enforcement population. The limiter's group/administrator
filters further narrow that population. An API key cannot enlarge the
permissions of its administrator's current role. A custom key permission map
is capped by that role; an inheriting key follows its administrator's current
permissions. The key and its administrator must remain active, and the key
must be unexpired and unrevoked.

## Upgrade or rotate credentials

1. Prepare the version 1.7.1 upgrade and create a panel API key
   with the necessary permissions and scopes.
2. Set `PANEL_API_KEY` in `.env`, retaining the intended `PANEL_DOMAIN` and
   `PANEL_VERIFY_SSL`. Remove `PANEL_USERNAME` and `PANEL_PASSWORD`; they are no
   longer read and cannot substitute for a missing key.
3. Restart a local Python process. For Docker, recreate the container so it
   receives the edited environment, for example `docker compose up -d
   --force-recreate` from the directory containing its Compose file.
4. Check startup, user synchronization and the selected IP source. After
   confirming a rotated key works, revoke the old key in the panel.

An empty or invalid header value prevents key-only startup. An HTTP 401 or 403
does not trigger password login, token refresh or a scheme change. Check the
key's status, administrator status and permissions; a 403 can indicate a
missing action or scope. An availability probe establishes panel reachability,
not authorization to perform every limiter operation.

Panel credentials are separate from the optional limiter management API's
`API_USERNAME`/`API_PASSWORD`, the Telegram bot token and IPinfo credentials.
Their authentication settings continue to apply independently.

## OpenAPI reference

The downloaded PasarGuard 5.5.1 source includes `export_openapi.py`. It builds
the route table and exports `app.openapi()` without running application
startup hooks. From the panel checkout with its Python 3.14 dependencies
installed, choose an output path:

```bash
uv run python export_openapi.py /path/to/PasarGuard-openapi-5.5.1.json
```

The verified local `panel-code/PasarGuard-openapi.json` is version 5.5.1 with
184 paths and 271 schemas. Its SHA256 is
`60f2e002673ef6334115e1d77d201938f5057d624904a2628b14dee69f26cb71`.
The exported schema declares only `OAuth2PasswordBearer`, because API-key
authentication is extracted directly from the request in the panel source.
Runtime accepts `X-Api-Key`; it also supports `Authorization: APIKey <key>`,
while the limiter consistently uses `X-Api-Key`.

API-key authentication does not establish complete IP evidence. Missing node
maps and stale, invalid or future timestamps retain the protections described
in [Panel API IP source](API_IP_SOURCE.md) and [Logs IP source](LOG_SOURCE.md).
