"""Tests for Dashboard Router (T09 ST8)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.dependencies import get_db
from backend.database import init_db
from backend.models.balance_snapshot import BalanceSnapshot
from backend.models.price_snapshot import PriceSnapshot
from backend.models.wallet import Wallet


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def dashboard_app(fresh_engine):
    """FastAPI app wired to auth + dashboard routers with fresh in-memory DB."""
    session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    await init_db(engine=fresh_engine, session_factory=session_factory)

    from backend.routers.auth import router as auth_router
    from backend.routers.dashboard import router as dashboard_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(dashboard_router)

    # Wire mock refresh service on app.state
    mock_refresh = AsyncMock()
    mock_refresh.run_full_refresh = AsyncMock()

    from backend.services.refresh import RefreshResult

    mock_refresh.run_full_refresh.return_value = RefreshResult(
        success_count=2,
        failure_count=0,
        skipped=False,
        errors=[],
        timestamp=datetime.now(timezone.utc),
    )
    app.state.refresh_service = mock_refresh

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest_asyncio.fixture
async def dashboard_client(dashboard_app):
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://test"
    ) as client:
        client.app = dashboard_app
        yield client


@pytest_asyncio.fixture
async def auth_token(dashboard_client):
    resp = await dashboard_client.post(
        "/api/auth/setup",
        json={
            "username": "alice",
            "password": "password1",
            "password_confirm": "password1",
        },
    )
    assert resp.status_code == 201
    return resp.json()["token"]


@pytest_asyncio.fixture
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


@pytest_asyncio.fixture
async def seeded_db(fresh_engine, auth_token, dashboard_client):
    """Seed the DB with wallets and snapshots for history/composition tests."""
    session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        # Get the user from auth token (by looking up users table)
        from sqlalchemy import select

        from backend.models.session import Session as UserSession

        result = await db.execute(
            select(UserSession).where(UserSession.token == auth_token)
        )
        session_row = result.scalar_one_or_none()
        assert session_row is not None
        user_id = session_row.user_id

        btc_wallet = Wallet(
            id="wallet-btc-1",
            user_id=user_id,
            network="BTC",
            address="1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf",
            tag="BTC Wallet",
            created_at=now,
        )
        kas_wallet = Wallet(
            id="wallet-kas-1",
            user_id=user_id,
            network="KAS",
            address="kaspa:" + "a" * 61,
            tag="KAS Wallet",
            created_at=now,
        )
        db.add(btc_wallet)
        db.add(kas_wallet)

        # Balance snapshots
        for days_ago in [2, 1, 0]:
            ts = now - timedelta(days=days_ago, hours=1)
            db.add(
                BalanceSnapshot(
                    id=str(uuid4()),
                    wallet_id="wallet-btc-1",
                    balance="1.0",
                    timestamp=ts,
                    source="live",
                )
            )
            db.add(
                BalanceSnapshot(
                    id=str(uuid4()),
                    wallet_id="wallet-kas-1",
                    balance="1000.0",
                    timestamp=ts,
                    source="live",
                )
            )

        # Price snapshots
        for days_ago in [2, 1, 0]:
            ts = now - timedelta(days=days_ago, hours=1)
            db.add(
                PriceSnapshot(
                    id=str(uuid4()),
                    coin="BTC",
                    price_usd="50000",
                    timestamp=ts,
                )
            )
            db.add(
                PriceSnapshot(
                    id=str(uuid4()),
                    coin="KAS",
                    price_usd="0.05",
                    timestamp=ts,
                )
            )

        await db.commit()
        return user_id


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_requires_auth(dashboard_client):
    resp = await dashboard_client.get("/api/dashboard/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_portfolio_history_requires_auth(dashboard_client):
    resp = await dashboard_client.get("/api/dashboard/portfolio-history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wallet_history_requires_auth(dashboard_client):
    resp = await dashboard_client.get("/api/dashboard/wallet-history/some-id")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_price_history_requires_auth(dashboard_client):
    resp = await dashboard_client.get("/api/dashboard/price-history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_composition_requires_auth(dashboard_client):
    resp = await dashboard_client.get("/api/dashboard/composition")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_endpoint_requires_auth(dashboard_client):
    resp = await dashboard_client.post("/api/dashboard/refresh")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /summary — empty portfolio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_empty_portfolio(dashboard_client, auth_headers):
    """With no wallets, summary should return zeros/nulls, not 500."""
    resp = await dashboard_client.get("/api/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_btc"] == "0"
    assert data["total_kas"] == "0"
    assert data["total_value_usd"] is None or data["total_value_usd"] == "0"
    assert data["change_24h_usd"] is None
    assert data["change_24h_pct"] is None


# ---------------------------------------------------------------------------
# GET /summary — with wallets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_with_wallets(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get("/api/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    # Should have non-zero totals
    assert Decimal(data["total_btc"]) == Decimal("1.0")
    assert Decimal(data["total_kas"]) == Decimal("1000.0")

    # USD values
    btc_val = Decimal(data["btc_value_usd"])
    kas_val = Decimal(data["kas_value_usd"])
    assert btc_val == Decimal("50000")
    assert kas_val == Decimal("50")

    total = Decimal(data["total_value_usd"])
    assert total == btc_val + kas_val

    # last_updated should be a UTC ISO string so browsers parse it correctly
    assert data["last_updated"] is not None
    assert data["last_updated"].endswith("Z")


@pytest.mark.asyncio
async def test_summary_has_prices(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get("/api/dashboard/summary", headers=auth_headers)
    data = resp.json()
    assert Decimal(data["btc_price_usd"]) == Decimal("50000")
    assert Decimal(data["kas_price_usd"]) == Decimal("0.05")


@pytest.mark.asyncio
async def test_summary_24h_change_uses_historical_prices(
    dashboard_client, auth_headers, auth_token, fresh_engine
):
    """24h change should use historical prices at ~24h ago, not current price.

    Scenario: BTC balance unchanged (1.0 BTC), but price rose from 40000 to 50000.
    Expected change: (1.0 * 50000) - (1.0 * 40000) = +10000 USD.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.models.session import Session as UserSession

    session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        result = await db.execute(
            select(UserSession).where(UserSession.token == auth_token)
        )
        session_row = result.scalar_one_or_none()
        assert session_row is not None
        user_id = session_row.user_id

        btc_wallet = Wallet(
            id="wallet-btc-hist",
            user_id=user_id,
            network="BTC",
            address="1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf",
            tag="BTC Hist",
            created_at=now,
        )
        db.add(btc_wallet)

        # Balance: same 1.0 BTC now and 25h ago
        db.add(
            BalanceSnapshot(
                id=str(uuid4()),
                wallet_id="wallet-btc-hist",
                balance="1.0",
                timestamp=now,
                source="live",
            )
        )
        db.add(
            BalanceSnapshot(
                id=str(uuid4()),
                wallet_id="wallet-btc-hist",
                balance="1.0",
                timestamp=now - timedelta(hours=25),
                source="live",
            )
        )

        # Prices: 40000 at 25h ago, 50000 now
        db.add(
            PriceSnapshot(
                id=str(uuid4()),
                coin="BTC",
                price_usd="50000",
                timestamp=now,
            )
        )
        db.add(
            PriceSnapshot(
                id=str(uuid4()),
                coin="BTC",
                price_usd="40000",
                timestamp=now - timedelta(hours=25),
            )
        )
        await db.commit()

    resp = await dashboard_client.get("/api/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["change_24h_usd"] is not None
    # 1.0 BTC * 50000 (now) - 1.0 BTC * 40000 (24h ago) = +10000
    assert Decimal(data["change_24h_usd"]) == Decimal("10000")


# ---------------------------------------------------------------------------
# GET /portfolio-history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portfolio_history_returns_data_points(
    dashboard_client, auth_headers, seeded_db
):
    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=7d", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "7d"
    assert data["unit"] == "usd"
    assert isinstance(data["data_points"], list)
    assert len(data["data_points"]) > 0

    # Each data point has a parseable UTC timestamp and a value
    dp = data["data_points"][0]
    assert "timestamp" in dp
    assert "value" in dp
    # Must be "YYYY-MM-DDTHH:MM:SSZ" — not the double-encoded "+00:00Z" form
    ts = dp["timestamp"]
    assert ts.endswith("Z") and not ts.endswith("+00:00Z"), (
        f"timestamp has invalid format: {ts!r}"
    )
    datetime.fromisoformat(ts.replace("Z", "+00:00"))  # must be parseable


async def _seed_dense_snapshots(fresh_engine, n_snapshots: int, days: int = 30):
    """Add n_snapshots live balance snapshots (and hourly prices) for the BTC
    wallet spread over the past `days` days."""
    session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    now = datetime.now(timezone.utc)
    step = timedelta(days=days) / n_snapshots
    async with session_factory() as db:
        for i in range(n_snapshots):
            ts = now - timedelta(days=days) + step * (i + 1)
            db.add(
                BalanceSnapshot(
                    id=str(uuid4()),
                    wallet_id="wallet-btc-1",
                    balance=f"{1.0 + i * 0.0001:.8f}",
                    timestamp=ts,
                    source="live",
                )
            )
        for i in range(days * 24):
            db.add(
                PriceSnapshot(
                    id=str(uuid4()),
                    coin="BTC",
                    price_usd="50000",
                    timestamp=now - timedelta(days=days) + timedelta(hours=i),
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_portfolio_history_downsamples_dense_data(
    dashboard_client, auth_headers, seeded_db, fresh_engine
):
    """Months of frequent live snapshots must not produce thousands of data
    points — the series is downsampled to a bounded size, keeping the newest
    point so the chart ends at the current value."""
    from backend.routers.dashboard import MAX_HISTORY_POINTS

    await _seed_dense_snapshots(fresh_engine, n_snapshots=2000)

    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=30d", headers=auth_headers
    )
    assert resp.status_code == 200
    points = resp.json()["data_points"]
    assert 0 < len(points) <= MAX_HISTORY_POINTS

    # Newest snapshot must survive downsampling
    timestamps = [p["timestamp"] for p in points]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_portfolio_history_bounded_query_count(
    dashboard_client, auth_headers, seeded_db, fresh_engine
):
    """The endpoint must not issue per-data-point SQL queries (the old
    behaviour was O(timestamps × wallets) queries, taking minutes on a
    months-old database)."""
    from sqlalchemy import event

    await _seed_dense_snapshots(fresh_engine, n_snapshots=600)

    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(fresh_engine.sync_engine, "before_cursor_execute", _count)
    try:
        resp = await dashboard_client.get(
            "/api/dashboard/portfolio-history?range=30d", headers=auth_headers
        )
    finally:
        event.remove(fresh_engine.sync_engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    assert len(resp.json()["data_points"]) > 0
    # auth (~3) + wallets + per-wallet snapshots + price ranges/baselines
    assert len(statements) < 25, f"{len(statements)} queries issued"


@pytest.mark.asyncio
async def test_wallet_history_downsamples_dense_data(
    dashboard_client, auth_headers, seeded_db, fresh_engine
):
    from backend.routers.dashboard import MAX_HISTORY_POINTS

    await _seed_dense_snapshots(fresh_engine, n_snapshots=2000)

    resp = await dashboard_client.get(
        "/api/dashboard/wallet-history/wallet-btc-1?range=30d&unit=usd",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    points = resp.json()["data_points"]
    assert 0 < len(points) <= MAX_HISTORY_POINTS


@pytest.mark.asyncio
async def test_wallet_history_bounded_query_count(
    dashboard_client, auth_headers, seeded_db, fresh_engine
):
    from sqlalchemy import event

    await _seed_dense_snapshots(fresh_engine, n_snapshots=600)

    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(fresh_engine.sync_engine, "before_cursor_execute", _count)
    try:
        resp = await dashboard_client.get(
            "/api/dashboard/wallet-history/wallet-btc-1?range=30d&unit=usd",
            headers=auth_headers,
        )
    finally:
        event.remove(fresh_engine.sync_engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    assert len(statements) < 25, f"{len(statements)} queries issued"


@pytest.mark.asyncio
async def test_price_history_downsamples_dense_data(
    dashboard_client, auth_headers, seeded_db, fresh_engine
):
    from backend.routers.dashboard import MAX_HISTORY_POINTS

    session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        for i in range(1500):
            db.add(
                PriceSnapshot(
                    id=str(uuid4()),
                    coin="BTC",
                    price_usd="50000",
                    timestamp=now - timedelta(days=30) + timedelta(minutes=i * 28),
                )
            )
        await db.commit()

    resp = await dashboard_client.get(
        "/api/dashboard/price-history?range=30d", headers=auth_headers
    )
    assert resp.status_code == 200
    assert 0 < len(resp.json()["btc"]) <= MAX_HISTORY_POINTS


@pytest.mark.asyncio
async def test_portfolio_history_all_range(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=all", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "all"


@pytest.mark.asyncio
async def test_portfolio_history_unit_btc(dashboard_client, auth_headers, seeded_db):
    # seeded: 1.0 BTC @ $50000 + 1000 KAS @ $0.05 = $50050 total
    # BTC price = $50000, so total in BTC = 50050 / 50000 = 1.001
    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=7d&unit=btc", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["unit"] == "btc"
    assert len(data["data_points"]) > 0
    value = Decimal(data["data_points"][0]["value"])
    assert value == Decimal("50050") / Decimal("50000")


@pytest.mark.asyncio
async def test_portfolio_history_unit_kas(dashboard_client, auth_headers, seeded_db):
    # seeded: $50050 total, KAS price = $0.05 → total in KAS = 50050 / 0.05 = 1001000
    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=7d&unit=kas", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["unit"] == "kas"
    assert len(data["data_points"]) > 0
    value = Decimal(data["data_points"][0]["value"])
    assert value == Decimal("50050") / Decimal("0.05")


@pytest.mark.asyncio
async def test_portfolio_history_invalid_unit(
    dashboard_client, auth_headers, seeded_db
):
    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=7d&unit=eur", headers=auth_headers
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /wallet-history/{wallet_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wallet_history_native(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get(
        "/api/dashboard/wallet-history/wallet-btc-1?range=7d&unit=native",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["wallet_id"] == "wallet-btc-1"
    assert data["unit"] == "native"
    assert len(data["data_points"]) > 0
    # Native value should be 1.0 BTC
    assert Decimal(data["data_points"][0]["value"]) == Decimal("1.0")
    # Timestamp must be "...Z", not the double-encoded "+00:00Z" form
    ts = data["data_points"][0]["timestamp"]
    assert ts.endswith("Z") and not ts.endswith("+00:00Z"), (
        f"wallet history timestamp has invalid format: {ts!r}"
    )
    datetime.fromisoformat(ts.replace("Z", "+00:00"))  # must be parseable


@pytest.mark.asyncio
async def test_wallet_history_usd(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get(
        "/api/dashboard/wallet-history/wallet-btc-1?range=7d&unit=usd",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["unit"] == "usd"
    assert len(data["data_points"]) > 0
    # 1.0 BTC * 50000 USD = 50000 USD
    assert Decimal(data["data_points"][0]["value"]) == Decimal("50000")


@pytest.mark.asyncio
async def test_wallet_history_not_found(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get(
        "/api/dashboard/wallet-history/nonexistent-wallet?range=7d",
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /price-history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_price_history(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get(
        "/api/dashboard/price-history?range=7d", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "7d"
    assert isinstance(data["btc"], list)
    assert isinstance(data["kas"], list)
    assert len(data["btc"]) > 0
    assert len(data["kas"]) > 0
    assert Decimal(data["btc"][0]["value"]) == Decimal("50000")
    assert Decimal(data["kas"][0]["value"]) == Decimal("0.05")


# ---------------------------------------------------------------------------
# GET /composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composition(dashboard_client, auth_headers, seeded_db):
    resp = await dashboard_client.get(
        "/api/dashboard/composition", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "segments" in data
    segments = data["segments"]
    assert len(segments) >= 1

    networks = {s["network"] for s in segments}
    assert "BTC" in networks
    assert "KAS" in networks

    total_pct = sum(Decimal(s["percentage"]) for s in segments)
    assert abs(total_pct - Decimal("100")) < Decimal("0.01")


@pytest.mark.asyncio
async def test_composition_empty_portfolio(dashboard_client, auth_headers):
    resp = await dashboard_client.get(
        "/api/dashboard/composition", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["segments"] == []


# ---------------------------------------------------------------------------
# POST /refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_endpoint_calls_service(dashboard_client, auth_headers):
    resp = await dashboard_client.post("/api/dashboard/refresh", headers=auth_headers)
    assert resp.status_code in (200, 202)
    data = resp.json()
    assert "skipped" in data


# ---------------------------------------------------------------------------
# End-of-day snapshot inclusion — regression for "no data for 7d/30d" bug
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_db_eod_snapshot(fresh_engine, auth_token, dashboard_client):
    """Seed a wallet that only has a historical end-of-day (23:59:59) snapshot for
    today, plus price data.  Simulates a sparse HD wallet where the most recent
    transaction occurred earlier today but the daily snapshot is timestamped at
    23:59:59 — in the future relative to datetime.now() at query time."""
    session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    now = datetime.now(timezone.utc)

    async with session_factory() as db:
        from sqlalchemy import select
        from backend.models.session import Session as UserSession

        result = await db.execute(
            select(UserSession).where(UserSession.token == auth_token)
        )
        session_row = result.scalar_one_or_none()
        user_id = session_row.user_id

        btc_wallet = Wallet(
            id="wallet-btc-eod",
            user_id=user_id,
            network="BTC",
            address="1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf",
            tag="BTC EOD",
            created_at=now,
        )
        db.add(btc_wallet)

        # Only snapshot: today's end-of-day (23:59:59) — this is in the future if
        # datetime.now() < 23:59:59 UTC, which is always true before midnight.
        eod_ts = now.replace(hour=23, minute=59, second=59, microsecond=0)
        db.add(
            BalanceSnapshot(
                id=str(uuid4()),
                wallet_id="wallet-btc-eod",
                balance="1.5",
                timestamp=eod_ts,
                source="historical",
            )
        )

        # Price snapshot from early today (before EOD)
        db.add(
            PriceSnapshot(
                id=str(uuid4()),
                coin="BTC",
                price_usd="80000",
                timestamp=now - timedelta(hours=1),
            )
        )

        await db.commit()


@pytest.mark.asyncio
async def test_wallet_history_includes_eod_snapshot_in_7d(
    dashboard_client, auth_headers, seeded_db_eod_snapshot
):
    """wallet-history for 7d must include today's 23:59:59 historical snapshot.

    Regression: when end was set to datetime.now(), end-of-day snapshots were
    excluded because their timestamp (23:59:59) is in the future.
    """
    resp = await dashboard_client.get(
        "/api/dashboard/wallet-history/wallet-btc-eod?range=7d&unit=native",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["data_points"]) >= 1, (
        "End-of-day snapshot (23:59:59) must be included in 7d range"
    )
    assert Decimal(data["data_points"][0]["value"]) == Decimal("1.5")


@pytest.mark.asyncio
async def test_portfolio_history_includes_eod_snapshot_in_30d(
    dashboard_client, auth_headers, seeded_db_eod_snapshot
):
    """portfolio-history for 30d must include today's 23:59:59 historical snapshot."""
    resp = await dashboard_client.get(
        "/api/dashboard/portfolio-history?range=30d", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["data_points"]) >= 1, (
        "End-of-day snapshot (23:59:59) must be included in 30d portfolio history"
    )


@pytest.mark.asyncio
async def test_refresh_endpoint_returns_202_when_skipped(
    dashboard_client, auth_headers
):
    from backend.services.refresh import RefreshResult

    dashboard_client.app.state.refresh_service.run_full_refresh.return_value = (
        RefreshResult(
            success_count=0,
            failure_count=0,
            skipped=True,
            errors=[],
            timestamp=datetime.now(timezone.utc),
        )
    )

    resp = await dashboard_client.post("/api/dashboard/refresh", headers=auth_headers)
    assert resp.status_code == 202
    assert resp.json()["skipped"] is True
