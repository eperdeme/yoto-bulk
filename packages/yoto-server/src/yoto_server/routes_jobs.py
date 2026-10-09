"""User-scoped workflow status for the web UI."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from yoto_server.deps import get_store, get_user_id
from yoto_server.routes import push_card
from yoto_server.routes_sync import import_yoto_to_abs, pull_from_yoto, sync_from_audiobookshelf
from yoto_server.store import YotoStore

router = APIRouter()


class WorkflowRequest(BaseModel):
    action: Literal["push", "sync", "pull", "adopt"]
    card_id: str | None = Field(default=None, max_length=200)
    label: str = Field(default="", max_length=500)


@router.get("/jobs")
async def list_jobs(
    request: Request, user_id: Annotated[str, Depends(get_user_id)]
) -> list[dict]:
    return await request.app.state.workflows.list(user_id)


@router.post("/jobs", status_code=202)
async def start_job(
    spec: WorkflowRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[YotoStore, Depends(get_store)],
) -> dict:
    if spec.action in {"push", "adopt"}:
        if not spec.card_id:
            raise HTTPException(status_code=400, detail="A card id is required")
        if await store.load_card(user_id, spec.card_id) is None:
            raise HTTPException(status_code=404, detail="Card not found")

    async def operation():
        if spec.action == "push":
            assert spec.card_id is not None
            return await push_card(
                spec.card_id, request, store=store, user_id=user_id,
                settings=request.app.state.settings,
            )
        if spec.action == "adopt":
            assert spec.card_id is not None
            return await import_yoto_to_abs(spec.card_id, request, store=store, user_id=user_id)
        if spec.action == "sync":
            return await sync_from_audiobookshelf(request, store=store, user_id=user_id)
        return await pull_from_yoto(store=store, user_id=user_id)

    groups = {
        "push": "Pushing to Yoto", "adopt": "Importing to Audiobookshelf",
        "sync": "Syncing from Audiobookshelf", "pull": "Pulling from Yoto",
    }
    data = spec.model_dump(exclude_none=True)
    data["group"] = groups[spec.action]
    return await request.app.state.workflows.submit(user_id, data, operation)


@router.get("/jobs/{job_id}")
async def job_status(
    job_id: str, request: Request, user_id: Annotated[str, Depends(get_user_id)]
) -> dict:
    job = await request.app.state.workflows.get(user_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return job


@router.get("/sync/status")
async def sync_status(
    user_id: Annotated[str, Depends(get_user_id)],
    store: Annotated[YotoStore, Depends(get_store)],
) -> dict[str, str | None]:
    timestamps = await cast(
        Awaitable[dict[str, str]], store._r.hgetall(f"ui:sync:{user_id}")
    )
    return {
        key: timestamps.get(key)
        for key in (
            "last_abs_sync", "last_abs_attempt", "last_abs_sync_status", "last_yoto_push"
        )
    }