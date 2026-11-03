"""Tests for HistoryService (T08)."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.clients.xpub import XpubTransaction
from backend.database import init_db
from backend.models.user import User
from backend.models.wallet import Wallet
from backend.repositories.snapshot import (
    BalanceSnapshotRepository,
    PriceSnapshotRepository,
)
from backend.repositories.transaction import TransactionRepository
from backend.services.history import HistoryImportResult, HistoryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user() -> User:
    return User(
        id=str(uuid4()),
        username="alice",
        password_hash="$2b$12$hashedvalue",
        created_at=datetime.now(timezone.utc),
    )


def make_wallet(user_id: str, network: str = "BTC") -> Wallet:
    return Wallet(
        id=str(uuid4()),
        user_id=user_id,
        network=network,
        address="bc1qtest" + "a" * 34,
        tag="Test Wallet",
        created_at=datetime.now(timezone.utc),
    )


def ts(year: int, month: int, day: int, hour: int = 12) -> int:
    """Return epoch seconds for the given UTC date."""
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(fresh_engine):
    factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False
    )
    await init_db(engine=fresh_engine, session_factory=factory)
    return factory


@pytest_asyncio.fixture
async def db(session_factory):
    """Provide a session for test setup/assertions (separate from service sessions)."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def user(db):
    u = make_user()
    db.add(u)
    await db.commit()
    return u


@pytest_asyncio.fixture
async def wallet(db, user):
    w = make_wallet(user.id, "BTC")
    db.add(w)
    await db.commit()
    return w


@pytest_asyncio.fixture
async def kas_wallet(db, user):
    w = make_wallet(user.id, "KAS")
    db.add(w)
    await db.commit()
    return w


def make_btc_tx(
    tx_hash: str, amount_sat: int, block_height: int, timestamp: int
) -> dict:
    return {
        "tx_hash": tx_hash,
        "amount_sat": amount_sat,
        "block_height": block_height,
        "timestamp": timestamp,
    }


def make_kas_tx(tx_hash: str, amount_sompi: int, timestamp: int) -> dict:
    return {
        "tx_hash": tx_hash,
        "amount_sompi": amount_sompi,
        "timestamp": timestamp,
    }


def make_mock_clients(btc_txs=None, kas_txs=None, price_history=None):
    btc_client = AsyncMock()
    kas_client = AsyncMock()
    coingecko_client = AsyncMock()
    ws_manager = AsyncMock()

    btc_client.get_all_transactions = AsyncMock(return_value=btc_txs or [])
    kas_client.get_all_transactions = AsyncMock(return_value=kas_txs or [])
    coingecko_client.get_price_history = AsyncMock(
        return_value=price_history or [(ts(2024, 1, 1) * 1000, Decimal("50000"))]
    )

    return btc_client, kas_client, coingecko_client, ws_manager


# ---------------------------------------------------------------------------
# test_full_import_stores_transactions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_stores_transactions(session_factory, wallet):
    txs = [
        make_btc_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1)),
        make_btc_tx("hash2", 50_000_000, 800_001, ts(2024, 1, 2)),
        make_btc_tx("hash3", -30_000_000, 800_002, ts(2024, 1, 3)),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=txs
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    result = await service.full_import(wallet)

    assert isinstance(result, HistoryImportResult)
    assert result.partial is False
    assert result.tx_count == 3

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(wallet.id)
    assert len(stored) == 3

    SATOSHI = Decimal("100000000")
    assert Decimal(stored[0].amount) == Decimal(100_000_000) / SATOSHI
    assert Decimal(stored[1].amount) == Decimal(50_000_000) / SATOSHI
    assert Decimal(stored[2].amount) == Decimal(-30_000_000) / SATOSHI

    # balance_after running sum
    assert Decimal(stored[0].balance_after) == Decimal("1")
    assert Decimal(stored[1].balance_after) == Decimal("1.5")
    assert Decimal(stored[2].balance_after) == Decimal("1.2")


# ---------------------------------------------------------------------------
# test_full_import_same_timestamp_balance_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_same_timestamp_balance_order(session_factory, wallet):
    """Two txs sharing a timestamp+block get running balances assigned in
    tx_hash order, matching the deterministic display order."""
    t = ts(2024, 1, 1)
    txs = [
        make_btc_tx("bbb", 33_032_118, 800_000, t),
        make_btc_tx("aaa", 5_717_685, 800_000, t),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=txs
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(wallet.id)

    by_hash = {tx.tx_hash: tx.balance_after for tx in stored}
    assert Decimal(by_hash["aaa"]) == Decimal("0.05717685")
    assert Decimal(by_hash["bbb"]) == Decimal("0.38749803")


# ---------------------------------------------------------------------------
# test_full_import_deduplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_deduplicates(session_factory, wallet):
    txs = [
        make_btc_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1)),
        make_btc_tx("hash2", 50_000_000, 800_001, ts(2024, 1, 2)),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=txs
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    # Run again with same transactions — should not duplicate
    await service.full_import(wallet)

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(wallet.id)
    assert len(stored) == 2


# ---------------------------------------------------------------------------
# test_full_import_computes_daily_balances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_computes_daily_balances(session_factory, wallet):
    # 5 transactions across 3 different days
    txs = [
        make_btc_tx("h1", 100_000_000, 800_000, ts(2024, 1, 1, 8)),
        make_btc_tx("h2", 50_000_000, 800_001, ts(2024, 1, 1, 14)),
        make_btc_tx("h3", -20_000_000, 800_002, ts(2024, 1, 2, 10)),
        make_btc_tx("h4", 30_000_000, 800_003, ts(2024, 1, 3, 9)),
        make_btc_tx("h5", 10_000_000, 800_004, ts(2024, 1, 3, 18)),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=txs
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    async with session_factory() as s:
        snap_repo = BalanceSnapshotRepository(s)
        snaps = await snap_repo.get_range(
            wallet.id,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 4, tzinfo=timezone.utc),
        )

    # Filter only historical snapshots
    historical = [s for s in snaps if s.source == "historical"]
    assert len(historical) == 3

    SATOSHI = Decimal("100000000")
    # Day 1: h1 + h2 = 1.5 BTC
    assert Decimal(historical[0].balance) == Decimal(150_000_000) / SATOSHI
    # Day 2: 1.5 - 0.2 = 1.3 BTC
    assert Decimal(historical[1].balance) == Decimal(130_000_000) / SATOSHI
    # Day 3: 1.3 + 0.3 + 0.1 = 1.7 BTC
    assert Decimal(historical[2].balance) == Decimal(170_000_000) / SATOSHI


# ---------------------------------------------------------------------------
# test_full_import_fetches_prices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_fetches_prices(session_factory, wallet):
    txs = [make_btc_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1))]
    price_data = [
        (ts(2024, 1, 1) * 1000, Decimal("42000")),
        (ts(2024, 1, 2) * 1000, Decimal("43000")),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=txs, price_history=price_data
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    # CoinGecko was called
    coingecko_client.get_price_history.assert_called_once()

    async with session_factory() as s:
        price_repo = PriceSnapshotRepository(s)
        all_snaps = await price_repo.get_range(
            "BTC",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 3, tzinfo=timezone.utc),
        )
    assert len(all_snaps) == 2


# ---------------------------------------------------------------------------
# test_full_import_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_timeout(session_factory, wallet):
    async def slow_get_all_transactions(address):
        await asyncio.sleep(10)
        return []

    btc_client = AsyncMock()
    btc_client.get_all_transactions = slow_get_all_transactions
    kas_client = AsyncMock()
    coingecko_client = AsyncMock()
    coingecko_client.get_price_history = AsyncMock(return_value=[])
    ws_manager = AsyncMock()

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    service.IMPORT_TIMEOUT = 0.05  # 50ms

    result = await service.full_import(wallet)

    assert result.partial is True
    assert result.message is not None

    # Verify partial broadcast was sent
    broadcast_calls = ws_manager.broadcast.call_args_list
    partial_calls = [
        c
        for c in broadcast_calls
        if c.args[0] == "wallet:history:completed" and c.args[1].get("partial") is True
    ]
    assert len(partial_calls) == 1


# ---------------------------------------------------------------------------
# test_full_import_broadcasts_progress_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_broadcasts_progress_events(session_factory, wallet):
    txs = [make_btc_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1))]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=txs
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    calls = ws_manager.broadcast.call_args_list
    assert len(calls) == 2

    # First call: started
    assert calls[0].args[0] == "wallet:history:progress"
    assert calls[0].args[1]["status"] == "started"
    assert calls[0].args[1]["wallet_id"] == wallet.id

    # Second call: completed
    assert calls[1].args[0] == "wallet:history:completed"
    assert calls[1].args[1]["wallet_id"] == wallet.id
    assert calls[1].args[1]["partial"] is False


# ---------------------------------------------------------------------------
# Helpers for paginated BTC tx objects (used by incremental sync)
# ---------------------------------------------------------------------------


def make_btc_paginated_tx(
    txid: str,
    amount_sat: int,
    block_height: int,
    block_time: int,
    address: str = "bc1qtest" + "a" * 34,
) -> dict:
    """Build a full BTC tx object as returned by get_transactions_paginated."""
    if amount_sat >= 0:
        # incoming — put in vout
        return {
            "txid": txid,
            "vout": [{"value": amount_sat, "scriptpubkey_address": address}],
            "vin": [],
            "status": {"block_height": block_height, "block_time": block_time},
        }
    else:
        # outgoing — put in vin
        return {
            "txid": txid,
            "vout": [],
            "vin": [
                {
                    "prevout": {
                        "value": -amount_sat,
                        "scriptpubkey_address": address,
                    }
                }
            ],
            "status": {"block_height": block_height, "block_time": block_time},
        }


# ---------------------------------------------------------------------------
# test_incremental_sync_fetches_only_new
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_sync_fetches_only_new(session_factory, wallet):
    # Pre-populate 5 existing transactions via full_import
    existing_txs = [
        make_btc_tx(f"old_{i}", 10_000_000 * i, 800_000 + i, ts(2024, 1, i + 1))
        for i in range(1, 6)
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=existing_txs
    )
    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    # Incremental sync: single page with 2 new + 1 old (stop-early)
    addr = wallet.address
    page = [
        make_btc_paginated_tx("new_2", 15_000_000, 800_007, ts(2024, 1, 8), addr),
        make_btc_paginated_tx("new_1", 20_000_000, 800_006, ts(2024, 1, 7), addr),
        make_btc_paginated_tx("old_5", 50_000_000, 800_005, ts(2024, 1, 6), addr),
    ]
    btc_client.get_transactions_paginated = AsyncMock(return_value=page)

    count = await service.incremental_sync(wallet)

    assert count == 2

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(wallet.id)
    assert len(stored) == 7


# ---------------------------------------------------------------------------
# test_incremental_sync_no_new_returns_zero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_sync_no_new_returns_zero(session_factory, wallet):
    existing_txs = [
        make_btc_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1)),
        make_btc_tx("hash2", 50_000_000, 800_001, ts(2024, 1, 2)),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=existing_txs
    )
    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    addr = wallet.address
    # Paginated page immediately hits a known txid
    page = [
        make_btc_paginated_tx("hash2", 50_000_000, 800_001, ts(2024, 1, 2), addr),
    ]
    btc_client.get_transactions_paginated = AsyncMock(return_value=page)
    count = await service.incremental_sync(wallet)

    assert count == 0


# ---------------------------------------------------------------------------
# test_incremental_sync_creates_balance_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_sync_creates_balance_snapshot(session_factory, wallet):
    existing_txs = [
        make_btc_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1)),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=existing_txs
    )
    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    addr = wallet.address
    # One new tx, then hits known hash
    page = [
        make_btc_paginated_tx("hash2", 50_000_000, 800_001, ts(2024, 1, 2), addr),
        make_btc_paginated_tx("hash1", 100_000_000, 800_000, ts(2024, 1, 1), addr),
    ]
    btc_client.get_transactions_paginated = AsyncMock(return_value=page)
    count = await service.incremental_sync(wallet)
    assert count == 1

    async with session_factory() as s:
        snap_repo = BalanceSnapshotRepository(s)
        after_snaps = await snap_repo.get_range(
            wallet.id,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
    # A new "live" snapshot should have been created
    live_snaps = [s for s in after_snaps if s.source == "live"]
    assert len(live_snaps) == 1
    assert Decimal(live_snaps[0].balance) == Decimal("1.5")


# ---------------------------------------------------------------------------
# test_incremental_sync_btc_stops_at_known_txid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_sync_btc_stops_at_known_txid(session_factory, wallet):
    """Page 1 has 1 new + 1 old — service stops and never requests page 2."""
    existing_txs = [
        make_btc_tx("old_tx", 100_000_000, 800_000, ts(2024, 1, 1)),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        btc_txs=existing_txs
    )
    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    await service.full_import(wallet)

    addr = wallet.address
    # Page 1: 1 new tx followed by the known "old_tx" — 25 entries so pagination
    # would continue if we didn't stop early.
    page1 = [make_btc_paginated_tx("new_tx", 50_000_000, 800_001, ts(2024, 1, 2), addr)]
    page1 += [
        make_btc_paginated_tx("old_tx", 100_000_000, 800_000, ts(2024, 1, 1), addr)
    ]
    # Pad to 25 so the loop would normally request page 2
    page1 += [
        make_btc_paginated_tx(
            f"filler_{i}", 1_000_000, 799_999 - i, ts(2023, 12, 31), addr
        )
        for i in range(23)
    ]

    # page2 would only be returned if the service incorrectly continues
    page2 = [
        make_btc_paginated_tx("old_tx", 100_000_000, 800_000, ts(2024, 1, 1), addr)
    ]

    btc_client.get_transactions_paginated = AsyncMock(side_effect=[page1, page2])

    count = await service.incremental_sync(wallet)

    # Only "new_tx" should be inserted
    assert count == 1
    # get_transactions_paginated called exactly once (page 2 never fetched)
    assert btc_client.get_transactions_paginated.call_count == 1

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(wallet.id)
    assert len(stored) == 2


# ---------------------------------------------------------------------------
# test_full_import_kaspa_wallet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_import_kaspa_wallet(session_factory, kas_wallet):
    txs = [
        make_kas_tx("kash1", 100_000_000_00, ts(2024, 1, 1) * 1000),
        make_kas_tx("kash2", -50_000_000_00, ts(2024, 1, 2) * 1000),
    ]
    btc_client, kas_client, coingecko_client, ws_manager = make_mock_clients(
        kas_txs=txs
    )

    service = HistoryService(
        session_factory, btc_client, kas_client, coingecko_client, ws_manager
    )
    result = await service.full_import(kas_wallet)

    assert result.partial is False
    assert result.tx_count == 2

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(kas_wallet.id)
    assert len(stored) == 2

    SOMPI = Decimal("100000000")
    assert Decimal(stored[0].amount) == Decimal(100_000_000_00) / SOMPI
    assert Decimal(stored[1].amount) == Decimal(-50_000_000_00) / SOMPI


# ---------------------------------------------------------------------------
# Helpers for HD wallet tests
# ---------------------------------------------------------------------------

# A valid-looking 111-char xpub key (does not need real Base58Check for these tests)
FAKE_XPUB = "xpub" + "A" * 107


def make_hd_wallet(user_id: str) -> Wallet:
    return Wallet(
        id=str(uuid4()),
        user_id=user_id,
        network="BTC",
        address=FAKE_XPUB,
        tag="HD Test Wallet",
        wallet_type="hd",
        extended_key_type="xpub",
        created_at=datetime.now(timezone.utc),
    )


def make_xpub_tx(
    tx_hash: str,
    amount_sat: int,
    timestamp: int | None,
    block_height: int | None = None,
) -> XpubTransaction:
    return XpubTransaction(
        tx_hash=tx_hash,
        timestamp=timestamp,
        block_height=block_height,
        amount_sat=amount_sat,
    )


def make_mock_clients_hd(xpub_txs=None, price_history=None):
    """Build mock clients + xpub_client for HD wallet tests."""
    btc_client = AsyncMock()
    kas_client = AsyncMock()
    coingecko_client = AsyncMock()
    ws_manager = AsyncMock()
    xpub_client = AsyncMock()

    xpub_client.get_xpub_transactions_all = AsyncMock(return_value=xpub_txs or [])
    xpub_client.get_xpub_transactions_since = AsyncMock(return_value=[])
    coingecko_client.get_price_history = AsyncMock(
        return_value=price_history or [(ts(2024, 1, 1) * 1000, Decimal("50000"))]
    )

    return btc_client, kas_client, coingecko_client, ws_manager, xpub_client


@pytest_asyncio.fixture
async def hd_wallet(db, user):
    w = make_hd_wallet(user.id)
    db.add(w)
    await db.commit()
    return w


# ---------------------------------------------------------------------------
# test_hd_wallet_history_import
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hd_wallet_history_import(session_factory, hd_wallet):
    """full_import_hd stores transactions and daily snapshots."""
    txs = [
        make_xpub_tx("htx1", 100_000_000, ts(2024, 1, 1), block_height=800_000),
        make_xpub_tx("htx2", 50_000_000, ts(2024, 1, 2), block_height=800_001),
        make_xpub_tx("htx3", -30_000_000, ts(2024, 1, 3), block_height=800_002),
    ]
    btc_client, kas_client, coingecko_client, ws_manager, xpub_client = (
        make_mock_clients_hd(xpub_txs=txs)
    )

    service = HistoryService(
        session_factory,
        btc_client,
        kas_client,
        coingecko_client,
        ws_manager,
        xpub_client=xpub_client,
    )
    result = await service.full_import_hd(hd_wallet)

    assert isinstance(result, HistoryImportResult)
    assert result.partial is False

    SATOSHI = Decimal("100000000")
    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(hd_wallet.id)

    assert len(stored) == 3
    assert Decimal(stored[0].amount) == Decimal(100_000_000) / SATOSHI
    assert Decimal(stored[1].amount) == Decimal(50_000_000) / SATOSHI
    assert Decimal(stored[2].amount) == Decimal(-30_000_000) / SATOSHI

    # Running-balance reconstruction (FR-H23)
    assert Decimal(stored[0].balance_after) == Decimal("1")
    assert Decimal(stored[1].balance_after) == Decimal("1.5")
    assert Decimal(stored[2].balance_after) == Decimal("1.2")

    # Daily snapshots exist
    async with session_factory() as s:
        snap_repo = BalanceSnapshotRepository(s)
        snaps = await snap_repo.get_range(
            hd_wallet.id,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 4, tzinfo=timezone.utc),
        )
    historical = [sn for sn in snaps if sn.source == "historical"]
    assert len(historical) == 3


# ---------------------------------------------------------------------------
# test_hd_wallet_history_import_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hd_wallet_history_import_timeout(session_factory, hd_wallet):
    """Timeout → partial result returned and wallet:history:completed broadcast sent."""

    async def slow_get_xpub_transactions_all(xpub):
        await asyncio.sleep(10)
        return []

    btc_client = AsyncMock()
    kas_client = AsyncMock()
    coingecko_client = AsyncMock()
    coingecko_client.get_price_history = AsyncMock(return_value=[])
    ws_manager = AsyncMock()
    xpub_client = AsyncMock()
    xpub_client.get_xpub_transactions_all = slow_get_xpub_transactions_all

    service = HistoryService(
        session_factory,
        btc_client,
        kas_client,
        coingecko_client,
        ws_manager,
        xpub_client=xpub_client,
    )
    service.IMPORT_TIMEOUT = 0.05  # 50ms

    result = await service.full_import_hd(hd_wallet)

    assert result.partial is True

    broadcast_calls = ws_manager.broadcast.call_args_list
    partial_calls = [
        c
        for c in broadcast_calls
        if c.args[0] == "wallet:history:completed" and c.args[1].get("partial") is True
    ]
    assert len(partial_calls) == 1


# ---------------------------------------------------------------------------
# test_hd_wallet_incremental_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hd_wallet_incremental_sync(session_factory, hd_wallet):
    """incremental_sync_hd fetches only txs after the last known timestamp."""
    # Seed one transaction via full_import_hd
    seed_txs = [
        make_xpub_tx("htx1", 100_000_000, ts(2024, 1, 1), block_height=800_000),
    ]
    btc_client, kas_client, coingecko_client, ws_manager, xpub_client = (
        make_mock_clients_hd(xpub_txs=seed_txs)
    )
    service = HistoryService(
        session_factory,
        btc_client,
        kas_client,
        coingecko_client,
        ws_manager,
        xpub_client=xpub_client,
    )
    await service.full_import_hd(hd_wallet)

    # Now simulate incremental sync finding 2 new transactions
    new_txs = [
        make_xpub_tx("htx2", 50_000_000, ts(2024, 1, 2), block_height=800_001),
        make_xpub_tx("htx3", 20_000_000, ts(2024, 1, 3), block_height=800_002),
    ]
    xpub_client.get_xpub_transactions_since = AsyncMock(return_value=new_txs)

    count = await service.incremental_sync_hd(hd_wallet)

    assert count == 2

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(hd_wallet.id)

    assert len(stored) == 3

    SATOSHI = Decimal("100000000")
    # Running balance should continue from seed tx's balance_after (1.0 BTC)
    assert Decimal(stored[1].balance_after) == Decimal(150_000_000) / SATOSHI
    assert Decimal(stored[2].balance_after) == Decimal(170_000_000) / SATOSHI

    # Verify get_xpub_transactions_since was called with the seed tx's timestamp
    xpub_client.get_xpub_transactions_since.assert_called_once()
    call_args = xpub_client.get_xpub_transactions_since.call_args
    assert call_args.args[1] == ts(2024, 1, 1)

    # incremental_sync_hd deliberately creates NO live snapshot — RefreshService
    # already stores source="live" before calling this method, so none should exist.
    async with session_factory() as s:
        snap_repo = BalanceSnapshotRepository(s)
        all_snaps = await snap_repo.get_range(
            hd_wallet.id,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
    live_snaps = [sn for sn in all_snaps if sn.source == "live"]
    assert len(live_snaps) == 0


# ---------------------------------------------------------------------------
# test_hd_wallet_incremental_sync_no_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hd_wallet_incremental_sync_no_history(session_factory, hd_wallet):
    """No stored transactions → incremental_sync_hd returns 0 without calling API."""
    btc_client, kas_client, coingecko_client, ws_manager, xpub_client = (
        make_mock_clients_hd()
    )

    service = HistoryService(
        session_factory,
        btc_client,
        kas_client,
        coingecko_client,
        ws_manager,
        xpub_client=xpub_client,
    )
    count = await service.incremental_sync_hd(hd_wallet)

    assert count == 0
    xpub_client.get_xpub_transactions_since.assert_not_called()


# ---------------------------------------------------------------------------
# test_hd_wallet_history_skips_unconfirmed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hd_wallet_history_skips_unconfirmed(session_factory, hd_wallet):
    """Transactions with timestamp=None are excluded from balance reconstruction.

    XpubClient.get_xpub_transactions_all only returns confirmed transactions now
    (confirmed=True check in _get_all_txs_for_address), but the service layer
    also has a defensive guard: timestamp=None → skip. This test verifies that
    the service-layer guard works correctly when XpubTransaction(timestamp=None)
    is present in the list.
    """
    # Directly construct XpubTransaction objects — the new XpubClient no longer
    # has a _parse_txs method; the service layer is what we're testing here.
    parsed_txs = [
        XpubTransaction(
            tx_hash="htx1",
            timestamp=ts(2024, 1, 1),
            block_height=800_000,
            amount_sat=100_000_000,
        ),
        XpubTransaction(
            tx_hash="htx_unconfirmed",
            timestamp=None,  # unconfirmed — must be skipped by service
            block_height=None,
            amount_sat=50_000_000,
        ),
        XpubTransaction(
            tx_hash="htx2",
            timestamp=ts(2024, 1, 2),
            block_height=800_001,
            amount_sat=20_000_000,
        ),
    ]

    # Sort oldest-first (XpubClient.get_xpub_transactions_all does this)
    parsed_txs.sort(key=lambda t: t.timestamp if t.timestamp is not None else 0)

    btc_client, kas_client, coingecko_client, ws_manager, xpub_client = (
        make_mock_clients_hd(xpub_txs=parsed_txs)
    )

    service = HistoryService(
        session_factory,
        btc_client,
        kas_client,
        coingecko_client,
        ws_manager,
        xpub_client=xpub_client,
    )
    result = await service.full_import_hd(hd_wallet)

    assert result.partial is False

    async with session_factory() as s:
        tx_repo = TransactionRepository(s)
        stored = await tx_repo.list_by_wallet(hd_wallet.id)

    # Only 2 confirmed transactions stored — unconfirmed one skipped
    assert len(stored) == 2
    stored_hashes = {tx.tx_hash for tx in stored}
    assert "htx_unconfirmed" not in stored_hashes
    assert "htx1" in stored_hashes
    assert "htx2" in stored_hashes

    SATOSHI = Decimal("100000000")
    # Running balance excludes the unconfirmed tx
    assert Decimal(stored[0].balance_after) == Decimal(100_000_000) / SATOSHI
    assert Decimal(stored[1].balance_after) == Decimal(120_000_000) / SATOSHI
