# Security policy

PG-Limiter controls user access and handles panel API keys, Telegram credentials and connection history. Use the latest published release and review its migration notes before upgrading. Development branches and private test bundles are not published security releases.

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** option when available:

https://github.com/JavadMoqaddam/PG-Limiter/security/advisories/new

If that option is unavailable, ask [the maintainer](https://github.com/JavadMoqaddam) for a private reporting channel before sharing technical details. Do not post exploit details, credentials, personal data or production logs in a public issue. A public request for a private contact should contain no vulnerability details.

Include the affected version or commit, relevant deployment conditions, impact, and reproducible steps using synthetic data. Never send a working panel key, bot token, database backup or `.env` file. Coordinate disclosure with the maintainer while a fix is prepared.

## Deployment boundaries

- Keep the optional management API on loopback or behind an authenticated TLS reverse proxy. HTTP Basic credentials need transport encryption for remote access. Limit trusted proxy headers to your actual proxy addresses.
- Give the panel key only the permissions required by the configured features. Rotate exposed credentials; deleting a file or commit does not revoke them.
- Verify panel TLS certificates. For a private CA, configure a trusted certificate bundle as described in the [panel connection guide](docs/PANEL_API_KEY.md).
- Restrict access to persistent data, backups and operator logs. They can contain credentials and user connection history.
- Automated scanners and optional AI review supplement code review and tests. A skipped or unavailable scanner is not a clean security result.
