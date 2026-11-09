# GhostStack

## Project Structure

```
ghoststack/
├── .claude/
│   ├── agents/            # Agent team definitions (PM, developer, tech lead, QA)
│   └── skills/            # Claude Code skills (invoke with /skill-name)
│       ├── develop-feature/       # /develop-feature — agent team: plan → TDD → review → QA
│       ├── generate-func-spec/   # /generate-func-spec — functional spec from a brief
│       └── generate-tech-spec/   # /generate-tech-spec — tech spec from a func spec
├── specs/                 # All design artifacts (specs + mockups)
│   ├── FUNC_SPEC.md       # What the system does
│   ├── TECH_SPEC.md       # How to build it
│   ├── TECH_NOTES.md      # Research notes and decision points
│   └── mockups/           # Interactive HTML mockups (visual source of truth for UI)
├── backend/               # Python 3.11+ / FastAPI backend
│   ├── app.py             # FastAPI app factory (create_app), lifespan, exception handlers
│   ├── routers/           # HTTP + WebSocket handlers (auth, wallets, dashboard, settings, ws)
│   ├── services/          # Business logic (auth, wallet, history, refresh)
│   ├── repositories/      # SQLAlchemy async DB access
│   ├── clients/           # External API clients (Bitcoin, Kaspa, CoinGecko, Blockbook/xpub)
│   ├── models/            # SQLAlchemy ORM models
│   ├── schemas/           # Pydantic request/response models
│   └── core/              # Cross-cutting: scheduler, WebSocket manager, dependencies, exceptions
├── tests/backend/         # pytest test suite (asyncio_mode=auto)
├── tools/                 # check-image.sh — asserts on a built image, run by the CI Docker job
├── run.py                 # Entry point: `python run.py`
├── alembic.ini            # Alembic migrations config
├── requirements.txt       # Production Python deps
└── requirements-dev.txt   # Test + lint deps (includes requirements.txt)
```

Read `specs/` for detailed functional and technical specs before implementing features. UI mockups in `specs/mockups/` are the visual source of truth — open them in a browser to see the target look-and-feel.

## Build & Run

- Install deps: `pip install -r requirements-dev.txt` (uses `.venv` — activate first: `source .venv/bin/activate`)
- Run: `python run.py`
- Reset password: `python run.py reset-password`
- Test (backend): `pytest tests/backend/ -v`
- Lint (backend): `ruff check backend/ tests/`
- Format (backend): `ruff format backend/ tests/`
- Build (frontend): `cd frontend && npm run build`
- Test (frontend): `cd frontend && npm run test`
- Lint (frontend): `cd frontend && npm run lint`
- Dev server (frontend): `cd frontend && npm run dev`

## Git & Remote

- **Hosted on GitHub**: `github.com:lukius/ghoststack`
- Use `gh` for all remote operations: PRs, issues, CI, etc.
- Example: `gh pr create`, `gh issue list`, `gh run list`

## Development Rules

### Testing — non-negotiable
- **Always run the full test suite after making changes.** Do not accept code that makes tests fail.
- Add new unit and/or functional tests for every new feature or bug fix.

### Commits
- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`
- One-liner messages, no co-author info.

### After touching code
- Run tests.
- Update this CLAUDE.md if the change affects project structure, build commands, key dependencies, or development workflows.

### About this file
- **Keep it under 200 lines.** If it's growing past that, something belongs elsewhere or can be removed.
- **Only non-obvious information.** If the code, tests, or commit history already describe it, it should not be here. This file is for context that a reader *cannot* derive by reading the source: the *why* behind decisions, implicit conventions, trust boundaries, known traps, and anything that would otherwise live only in someone's head.

## Key Design Decisions

- **bcrypt directly, not via passlib** — `passlib==1.7.4` is incompatible with `bcrypt>=4.x`: passlib's internal wrap-bug detection hashes a 200-byte string, but newer bcrypt rejects passwords over 72 bytes. `requirements.txt` uses `bcrypt==5.0.0` directly. The spec's "bcrypt via passlib" recommendation cannot be followed; the `bcrypt` library directly produces equivalent `$2b$12$...` hashes.
- **HD wallets use Trezor Blockbook (`btc2.trezor.io`), individual BTC wallets use mempool.space** — Blockbook accepts xpub/ypub/zpub natively and returns balance + per-address breakdown + tx history for the entire wallet in one call, eliminating the rate-limit pressure that broke the per-address mempool.space approach. The spec's original blockchain.info choice does not work for BIP84 zpubs (it derives only legacy P2PKH addresses from an xpub). The User-Agent `GhostStack/1.0` (set in `BaseClient`) is required — generic UAs like `Mozilla/5.0` are blocked by Cloudflare on Blockbook xpub paths.
- **certifi is a runtime dependency with a floor, and the image has no packaging tools** — httpx trusts certifi's bundle rather than the OS store, so a stale certifi leaves the API clients trusting distrusted roots even when the base image is clean, and Python's `ssl` doesn't check revocation. `certifi>=2026.10.19` excludes emSign ECC Root CA - G3 (eMudhra incident); it's deliberately not pinned, so a fresh install gets later root removals. The release workflow rebuilds the runtime stage uncached (`no-cache-filters: runtime`), so every release re-resolves pip deps and runs `apt-get upgrade`; without that, the cached layers would keep stale packages regardless of what Debian or PyPI published. The Dockerfile installs with `--cert` (pip 24.0 keeps honouring its vendored bundle under `--use-feature=truststore`) and then uninstalls pip, setuptools and wheel. `tools/check-image.sh <image>` asserts all of this and that the server boots.
- **Dependabot doesn't see Python transitive dependencies; the image scan does** — `requirements.txt` lists only direct dependencies, so GitHub's dependency graph has no `starlette` (it comes through `fastapi`) and raises no alert for it. `image-scan.yml` covers that gap, plus Debian packages, by scanning the published image weekly. Merging an update doesn't reach users until a release is cut. GitHub disables scheduled workflows after 60 days without repo activity, so re-enable the scan if it goes quiet.
