"""FastAPI application factory for GhostStack."""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from backend.clients.bitcoin import BitcoinClient
from backend.clients.coingecko import CoinGeckoClient
from backend.clients.kaspa import KaspaClient
from backend.clients.xpub import XpubClient
from backend.core.exceptions import (
    AccountExistsError,
    AccountNotFoundError,
    AddressValidationError,
    DuplicateWalletError,
    ExternalAPIError,
    InvalidCredentialsError,
    InvalidSessionError,
    RateLimitedError,
    TagValidationError,
    WalletLimitReachedError,
    WalletNotFoundError,
)
from backend.core.scheduler import Scheduler
from backend.core.websocket_manager import ConnectionManager
from backend.config import config as app_config
from backend.database import async_session, init_db
from backend.routers import auth, dashboard, settings, wallets, websocket
from backend.services.history import HistoryService
from backend.services.refresh import RefreshService

logger = logging.getLogger(__name__)


class SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html for unknown non-API paths so
    deep-link refreshes (e.g. /wallet/<id>) load the SPA instead of returning a
    JSON 404."""

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.startswith("api/"):
                return await super().get_response("index.html", scope)
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()

    from backend.services.maintenance import recompute_transaction_balances

    await recompute_transaction_balances(async_session)

    app.state.btc_client = BitcoinClient()
    app.state.kas_client = KaspaClient()
    app.state.coingecko_client = CoinGeckoClient()
    app.state.xpub_client = XpubClient()
    app.state.ws_manager = ConnectionManager()

    app.state.history_service = HistoryService(
        session_factory=async_session,
        btc_client=app.state.btc_client,
        kas_client=app.state.kas_client,
        coingecko_client=app.state.coingecko_client,
        ws_manager=app.state.ws_manager,
        xpub_client=app.state.xpub_client,
    )
    app.state.refresh_service = RefreshService(
        session_factory=async_session,
        btc_client=app.state.btc_client,
        kas_client=app.state.kas_client,
        coingecko_client=app.state.coingecko_client,
        ws_manager=app.state.ws_manager,
        history_service=app.state.history_service,
        xpub_client=app.state.xpub_client,
    )

    from backend.repositories.config import ConfigRepository
    from backend.repositories.session import SessionRepository

    async with async_session() as db:
        config_repo = ConfigRepository(db)
        app.state.scheduler = Scheduler(app.state.refresh_service, config_repo)
        await app.state.scheduler.start()

        session_repo = SessionRepository(db)
        expired_count = await session_repo.delete_expired(datetime.now(timezone.utc))
        await db.commit()
        if expired_count:
            logger.info("Cleaned up %d expired session(s)", expired_count)

    logger.info("GhostStack running at http://%s:%s", app_config.host, app_config.port)

    yield

    # Shutdown
    await app.state.scheduler.stop()
    await app.state.btc_client.close()
    await app.state.kas_client.close()
    await app.state.coingecko_client.close()
    await app.state.xpub_client.close()
    logger.info("GhostStack stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="GhostStack", version="2.0.1", lifespan=lifespan)

    # CORS — allow Vite dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(auth.router)
    app.include_router(wallets.router)
    app.include_router(dashboard.router)
    app.include_router(settings.router)
    app.include_router(websocket.router)

    # Exception handlers
    @app.exception_handler(AccountExistsError)
    async def handle_account_exists(request: Request, exc: AccountExistsError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidCredentialsError)
    async def handle_invalid_credentials(
        request: Request, exc: InvalidCredentialsError
    ):
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(RateLimitedError)
    async def handle_rate_limited(request: Request, exc: RateLimitedError):
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(InvalidSessionError)
    async def handle_invalid_session(request: Request, exc: InvalidSessionError):
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AddressValidationError)
    async def handle_address_validation(request: Request, exc: AddressValidationError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(DuplicateWalletError)
    async def handle_duplicate_wallet(request: Request, exc: DuplicateWalletError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(WalletLimitReachedError)
    async def handle_wallet_limit(request: Request, exc: WalletLimitReachedError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(TagValidationError)
    async def handle_tag_validation(request: Request, exc: TagValidationError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(WalletNotFoundError)
    async def handle_wallet_not_found(request: Request, exc: WalletNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(AccountNotFoundError)
    async def handle_account_not_found(request: Request, exc: AccountNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ExternalAPIError)
    async def handle_external_api(request: Request, exc: ExternalAPIError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # Static files — only if frontend/dist exists
    frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    if os.path.isdir(frontend_dist):
        app.mount(
            "/", SPAStaticFiles(directory=frontend_dist, html=True), name="static"
        )
    else:
        logger.warning(
            "frontend/dist not found — static file serving disabled. "
            "Run `cd frontend && npm run build` to enable it."
        )

    return app
