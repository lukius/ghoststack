# Security Policy

GhostStack is a self-hosted application — there is no central service to attack — but vulnerabilities that affect users running their own instance are still serious. Please report them privately.

## Supported Versions

The latest minor release is supported with security fixes.

| Version | Supported |
|---|---|
| 2.0.x   | yes       |
| < 2.0   | no        |

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting:

1. Open the repository on GitHub.
2. Go to the **Security** tab and click **Report a vulnerability**.

Please include:

- A description of the issue and its potential impact.
- Steps to reproduce, or a proof-of-concept.
- The version (release tag or commit hash) you are running.
- Any relevant logs, with secrets redacted.

Acknowledgement target: within 7 days. Fix target for reproducible issues: within 30 days. Critical issues will be turned around faster.

## Scope

In scope:

- The GhostStack backend (`backend/`) and frontend (`frontend/`).
- Default configuration and the shipped Alembic migrations.

Out of scope:

- Vulnerabilities in third-party services (Mempool.space, Trezor Blockbook, api.kaspa.org, CoinGecko) — please report those upstream.
- Issues that require physical access to the machine running GhostStack.
- Self-inflicted misconfigurations: exposing the SQLite database file, running the dev server on the public internet without TLS in front of it, sharing your account password, etc.

## Disclosure

After a fix ships, we publish an advisory describing the issue, affected versions, and the resolution. Reporters are credited unless they ask to remain anonymous.
