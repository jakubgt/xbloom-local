# Security policy

## Supported versions

| Version | Security updates |
| --- | --- |
| 1.0.x | Supported; use the latest 1.0 patch release. |
| Earlier releases and unversioned snapshots | Not supported; upgrade to the current supported release. |

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/jakubgt/xbloom-local/security/advisories/new) for suspected security issues. Please do not disclose exploit details, credentials, or personal card data in a public issue or pull request.

Include the affected app/bridge version or commit, operating system and Python version, access mode, expected and observed behavior, impact, and concise reproduction steps. Prefer synthetic profiles and simulated or mocked card data. Redact tokens, passwords, cookies, private hostnames and addresses, usernames, authentication headers, and real card dumps from logs and screenshots. A minimal synthetic example is more useful than a complete personal library or backup.

There is no guaranteed response time or bounty program. Keep the report private while the issue is assessed and a fix or mitigation is coordinated. Ordinary feature requests and non-security bugs can use public issues after removing personal data.

## Intended security boundaries

- The application is designed for one computer or a restricted LAN. Use HTTPS for LAN access, explicit allowed hosts, and firewall rules; do not expose it directly to the public internet.
- Password mode requires a local account. Trusted-LAN mode intentionally lets permitted LAN clients access and change the library, download backups, and issue tokens. Treat access to that network/service as access to the application.
- Bridge and API tokens are separate scopes. Do not share them in screenshots, release attachments, support examples, or public configuration files.
- Product lookup accepts public HTTPS pages and rejects private/local destinations. Treat product content as data and review imported facts before saving.
- Physical card writes and initialization require explicit per-operation approval and the foreground bridge's local confirmation. Reproduce security reports in simulation or with mocked calls whenever possible; reporting a problem does not authorize a hardware write.

Authentication or token-scope bypasses, private-network access through product lookup, unsafe file/archive handling, exposure of private library data, and bypasses of the card approval/backup protections are relevant reports. Initialization's disclosed hidden-setting limitations remain described in the [NFC documentation](docs/nfc-format.md#separate-blank-card-initialization).

## Protect local recovery material

Application exports can contain account/token hashes, photos, and card data. Keep exports, runtime databases, bridge snapshots, credentials, and certificates private and outside release archives. Use the [backup and recovery procedure](docs/deployment.md#backups) before upgrades or security remediation.
