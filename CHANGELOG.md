# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Security

- **Stop trusting the distrusted emSign ECC Root CA - G3** ([eMudhra weak-key incident](https://thehackernews.com/2026/10/emudhra-to-exit-global-ssltls-business.html)). The API clients trust certifi's bundle, so published images up to and including 2.0.0 accept certificates chaining to this root. `requirements.txt` now requires `certifi>=2026.10.19`, and the image installs its dependencies against the OS CA store and no longer includes pip. Once this is released, pull the new image, or re-run `pip install -r requirements.txt` for a non-Docker install. Images under the old `ghcr.io/lukius/cryptodash` path will not get this fix.

---

## [2.0.0] — 2026-08-21

### Changed

- **Renamed the project from CryptoDash to GhostStack.** *Ghost* is a nod to Kaspa's GHOSTDAG consensus, *Stack* to Bitcoin's sats — and to the portfolio the app tracks. The wordmark and icon now colour the Kaspa half teal (`#49eacb`) and the Bitcoin half orange (`#f7931a`), in that reading order.

### Breaking

Nothing is migrated automatically. See [Upgrading from CryptoDash](README.md#upgrading-from-cryptodash) for the steps.

- Container image moved from `ghcr.io/lukius/cryptodash` to `ghcr.io/lukius/ghoststack`. Tags published under the old path stay there and will not be updated.
- Docker volume renamed `cryptodash-data` → `ghoststack-data`. A renamed volume is a new, empty volume: upgrading without copying the old one across starts the app with no wallets and no history.
- Default database file renamed `cryptodash.db` → `ghoststack.db`.
- Environment-variable prefix renamed `CRYPTODASH_*` → `GHOSTSTACK_*`. The old names are not accepted as a fallback; an unmigrated variable is ignored rather than reported.
- The `CryptoDashError` base exception is now `GhostStackError`.
- Outbound requests identify as `GhostStack/1.0` instead of `CryptoDash/1.0`.

---

## [1.0.1] — 2026-07-18

### Fixed

- **HD wallet transaction list**: same-block transactions could display a running balance inconsistent with their order. Transactions now sort deterministically by (timestamp, block height, tx hash) everywhere — in the running-balance computation and in the transaction list — and a one-time startup task repairs previously stored running balances.
- **Portfolio value chart**: the history endpoints issued one SQL query per wallet per data point and returned every stored snapshot, taking minutes to render on a database with months of live snapshots. Balance and price series are now merged in memory (a handful of queries total) and downsampled to at most 500 points.
- **Session persistence**: a transient failure of the auth status check (backend restarting, network blip) wiped the stored 30-day "remember me" token and forced a re-login. The token is now only cleared when the server authoritatively reports the session invalid.
- **Wallet table**: the bottom border of the tag column no longer drifts out of line with the rest of the row (flex layout moved off the `<td>`), and collapsed HD expand rows no longer paint a stray second separator line.
- **Logos**: the glow behind the circular logo used `box-shadow`, which lit everything around the image's square box and left a dark plate visible around the circle. Now uses `filter: drop-shadow`, which follows the logo's circular shape.

---

## [1.0.0] — 2026-04-27

Initial release, under the project's original name, CryptoDash.

### Added

**Backend**
- FastAPI async backend with SQLAlchemy + SQLite (WAL mode)
- Alembic migration infrastructure with initial schema (users, sessions, wallets, transactions, balance_snapshots, price_snapshots, configuration)
- Auth service: first-run account setup, login, logout, session management (7-day / 30-day "remember me"), bcrypt password hashing
- Wallet service: add/remove/tag wallets; regex-based address validation for Bitcoin (P2PKH, P2SH, Bech32, Taproot), Kaspa, and extended public keys (xpub / ypub / zpub)
- HD wallet support: track an entire BIP84/BIP44 wallet from a single extended public key; balance, per-address breakdown, and transaction history fetched via Trezor Blockbook
- History service: retroactive transaction import from blockchain + incremental sync on refresh
- Refresh service: orchestrates full balance + price refresh cycle with asyncio lock to prevent concurrent runs
- Price service: current and historical BTC/USD and KAS/USD prices via CoinGecko
- Background scheduler: configurable auto-refresh interval, starts with app lifecycle
- WebSocket manager: real-time broadcast of refresh events to connected clients
- External API clients for Mempool.space (Bitcoin), api.kaspa.org (Kaspa), CoinGecko — with retry and timeout handling
- Configuration persistence via key-value table (refresh interval and future settings)
- `python run.py reset-password` CLI command
- Environment variable configuration (host, port, DB path, log level)
- CORS for frontend dev server

**Frontend**
- Vue 3 + TypeScript + Pinia SPA
- Auth flow: setup page (first-run), login page with "Remember me" checkbox, guarded routes
- Dashboard with eight widgets:
  - W1: Total portfolio value in USD
  - W2: Total BTC balance
  - W3: Total KAS balance
  - W4: Wallet table with per-wallet balances and inline tag editing
  - W5: Portfolio composition pie chart
  - W6: Portfolio value over time (line chart)
  - W7: Per-wallet balance history (line chart on wallet detail page)
  - W8: BTC/USD and KAS/USD price charts
- Time range selector (7d / 30d / 90d / 1y / All) for history charts
- Wallet management: add wallet dialog (supports individual addresses and xpub/ypub/zpub), inline tag editor, remove confirmation dialog, address format validation
- HD badge on wallet cards; derived address list on wallet detail view
- Wallet detail view: balance history chart, transaction timeline, delete wallet
- Settings page: configurable refresh interval
- Header: manual refresh button, settings link, logout
- WebSocket composable with auto-reconnect; real-time portfolio updates without page reload
- Responsive Tailwind CSS design

**Tests**
- Backend: 20 pytest test modules covering auth, wallets, dashboard, settings, refresh, history, scheduler, security, repositories, models, database, exception handlers, and all three external API clients
- Frontend: Vitest component and store tests (WalletTable, AddWalletDialog, TimeRangeSelector, auth store, wallets store)

[Unreleased]: https://github.com/lukius/ghoststack/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/lukius/ghoststack/compare/v1.0.1...v2.0.0
[1.0.1]: https://github.com/lukius/ghoststack/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/lukius/ghoststack/releases/tag/v1.0.0
