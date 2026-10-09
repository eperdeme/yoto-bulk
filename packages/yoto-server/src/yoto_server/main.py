"""FastAPI application for Yoto card library server."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis.asyncio as redis
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from yoto_server.config import Settings
from yoto_server.routes import router as cards_router
from yoto_server.routes_duplicates import router as duplicates_router
from yoto_server.routes_jobs import router as jobs_router
from yoto_server.routes_sync import router as sync_router
from yoto_server.store import YotoStore
from yoto_bulk.workflows import WorkflowManager

logger = logging.getLogger(__name__)

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Set up Redis store."""
    # Redis
    redis_client = redis.from_url(
        settings.redis_url, decode_responses=True
    )
    store = YotoStore(redis_client)
    app.state.store = store
    app.state.settings = settings
    app.state.workflows = WorkflowManager(redis_client, "yoto")

    logger.info("Connected to Redis at %s", settings.redis_url)
    yield

    await app.state.workflows.close()
    await redis_client.aclose()


app = FastAPI(
    title="Yoto Server",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow the borrowbox-server frontend (port 8000) to call us
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cards_router, prefix="/api/yoto", tags=["yoto"])
app.include_router(sync_router, prefix="/api/yoto", tags=["yoto"])
app.include_router(duplicates_router, prefix="/api/yoto", tags=["duplicates"])
app.include_router(jobs_router, prefix="/api/yoto", tags=["jobs"])


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    response: Response = await call_next(request)
    cover_request = (
        request.url.path.startswith("/api/yoto/cards/") and request.url.path.endswith("/cover")
    )
    if request.url.path.startswith("/api/") and not cover_request:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    store: YotoStore = app.state.store
    redis_ok = await store.ping()
    return {
        "status": "ok" if redis_ok else "degraded",
        "service": "yoto-server",
        "redis": "connected" if redis_ok else "disconnected",
    }


def run() -> None:
    """Entry point for ``yoto-server`` console script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "yoto_server.main:app",
        host=settings.yoto_server_host,
        port=settings.yoto_server_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
