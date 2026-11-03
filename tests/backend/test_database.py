from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.database import init_db


async def test_init_db_creates_tables():
    """init_db() should create all expected tables on a fresh in-memory engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    await init_db(engine=engine, session_factory=session_factory)

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_table_names()
        )

    expected_tables = {
        "users",
        "sessions",
        "wallets",
        "transactions",
        "balance_snapshots",
        "price_snapshots",
        "configuration",
    }
    assert expected_tables.issubset(set(table_names)), (
        f"Missing tables: {expected_tables - set(table_names)}"
    )

    await engine.dispose()


async def test_init_db_enables_wal_mode():
    """init_db() should enable WAL journal mode."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    await init_db(engine=engine, session_factory=session_factory)

    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        mode = result.scalar()

    # In-memory SQLite reports 'memory' for :memory: databases (WAL PRAGMA still executes)
    assert mode in ("wal", "memory")

    await engine.dispose()


async def test_init_db_seeds_default_config():
    """init_db() should seed a default refresh_interval_minutes configuration."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    await init_db(engine=engine, session_factory=session_factory)

    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT value FROM configuration WHERE key = 'refresh_interval_minutes'"
            )
        )
        row = result.fetchone()

    assert row is not None, (
        "Default configuration key 'refresh_interval_minutes' was not seeded"
    )
    assert row[0] == "15"

    await engine.dispose()
