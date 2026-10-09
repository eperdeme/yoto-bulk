"""FastAPI application for BorrowBox server."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import redis.asyncio as redis
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from borrowbox_server import libraries as registry
from borrowbox_server.config import Settings
from borrowbox_server.credentials import CredentialVault
from borrowbox_server.routes_catalog import router as catalog_router
from borrowbox_server.routes_auth import router as auth_router
from borrowbox_server.routes_credentials import router as credentials_router
from borrowbox_server.routes_libraries import router as libraries_router
from borrowbox_server.store import Store
from borrowbox_server.worker import AvailabilityRefresher
from borrowbox_server.routes_jobs import router as jobs_router
from yoto_bulk.workflows import WorkflowManager

logger = logging.getLogger(__name__)

settings = Settings()

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage Redis connection and background worker lifecycle."""
    # Connect to Redis
    pool = redis.ConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    r = redis.Redis(connection_pool=pool)
    await r.ping()
    logger.info("Connected to Redis at %s", settings.redis_url)

    store = Store(
        r,
        ttl_availability=settings.ttl_availability,
        ttl_credits=settings.ttl_credits,
        ttl_discovery=settings.ttl_discovery,
        ttl_product=settings.ttl_product,
        ttl_session=settings.ttl_session,
    )

    # Credentials live encrypted in Redis, seeded from the environment on a
    # fresh deployment. Constructing this early surfaces a bad secret key at
    # startup rather than on the first sign-in attempt.
    vault = CredentialVault(
        store,
        secret_key=settings.secret_key,
        env_credentials=settings.credentials,
    )

    # Stash in app state for dependency injection
    app.state.redis = r
    app.state.store = store
    app.state.settings = settings
    app.state.vault = vault
    app.state.workflows = WorkflowManager(r, "borrowbox")

    # The library registry lives in Redis and is managed from the UI.
    # BORROWBOX_LIBRARIES is only a first-boot seed; after that the store wins,
    # so a library removed in the UI stays removed across restarts.
    known = await registry.refresh(store, settings)
    logger.info(
        "Library registry: %d configured (%s)",
        len(known),
        ", ".join(sorted(known)) or "none",
    )

    # Start background worker if configured
    worker_task: asyncio.Task | None = None
    # Started whenever refreshing is enabled, even with no libraries yet: they
    # can now be added from the UI at runtime, and the worker no-ops on an empty
    # registry. Previously this was decided once at startup, so a fresh install
    # never refreshed until it was restarted.
    if settings.refresh_interval > 0:
        refresher = AvailabilityRefresher(store, settings)
        worker_task = asyncio.create_task(refresher.run())
        logger.info(
            "Background refresh started (interval=%ds, libraries=%s)",
            settings.refresh_interval,
            list(settings.libraries.keys()),
        )

    yield

    # Shutdown
    await app.state.workflows.close()
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    await r.aclose()
    await pool.aclose()
    logger.info("Redis connection closed")


app = FastAPI(
    title="BorrowBox Server",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(catalog_router, prefix="/api/catalog", tags=["catalog"])
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(credentials_router, prefix="/api/credentials", tags=["credentials"])
app.include_router(libraries_router, prefix="/api/libraries", tags=["libraries"])
app.include_router(jobs_router, prefix="/api/jobs", tags=["jobs"])


# API key authentication middleware
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Require API key for /api/ routes when configured."""
    api_key = settings.api_key
    if api_key and request.url.path.startswith("/api/"):
        provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if provided != api_key:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
    return await call_next(request)

# Serve frontend static files
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Prevent browser from serving stale static assets."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _render_shell(index_file: Path) -> HTMLResponse | dict:
    """Serve a frontend shell, injecting runtime config before </head>.

    The Yoto API lives on a different origin, so its base URL has to reach the
    browser somehow; it is injected here rather than hard-coded in the asset.
    """
    if not index_file.is_file():
        return {"message": "BorrowBox Server", "docs": "/docs"}
    html = index_file.read_text(encoding="utf-8")
    injection = f"<script>window.YOTO_API_URL = {settings.yoto_api_url!r};</script>"
    return HTMLResponse(html.replace("</head>", f"  {injection}\n</head>"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the site icon.

    Browsers request /favicon.ico unconditionally, which was logging a 404 on
    every page load. An SVG is returned regardless of the .ico path, which all
    current browsers accept.
    """
    icon = STATIC_DIR / "favicon.svg"
    if not icon.is_file():
        return Response(status_code=404)
    return Response(
        content=icon.read_bytes(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/")
async def index():
    """Serve the frontend."""
    return _render_shell(STATIC_DIR / "index.html")


@app.get("/v2", include_in_schema=False)
async def index_v2_alias():
    """The UI briefly lived at /v2 while it ran alongside the original."""
    return RedirectResponse(url="/", status_code=308)


@app.get("/v1", include_in_schema=False)
async def index_v1_gone():
    """The original frontend has been removed.

    Every flow it had is now in the current UI, so a bookmarked /v1 is sent
    there rather than 404ing.
    """
    return RedirectResponse(url="/", status_code=308)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    try:
        await app.state.redis.ping()
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded", "detail": "Redis unreachable"}


def run() -> None:
    """Entry point for ``borrowbox-server`` console script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "borrowbox_server.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
