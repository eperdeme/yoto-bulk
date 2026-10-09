"""Durable BorrowBox import status for the web UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from borrowbox_server.routes_auth import _validate_library, import_to_yoto

router = APIRouter()


class ImportRequest(BaseModel):
    library: str = Field(min_length=1, max_length=120)
    loan_id: str = Field(min_length=1, max_length=200)
    product_id: str | None = Field(default=None, max_length=200)
    label: str = Field(default="", max_length=500)


@router.get("")
async def list_jobs(request: Request) -> list[dict]:
    return await request.app.state.workflows.list("default")


@router.post("", status_code=202)
async def start_import(spec: ImportRequest, request: Request) -> dict:
    settings = request.app.state.settings
    store = request.app.state.store
    _validate_library(spec.library, settings)

    async def operation():
        return await import_to_yoto(
            spec.library, spec.loan_id, product_id=spec.product_id,
            force=False, store=store, settings=settings,
        )

    data = spec.model_dump(exclude_none=True)
    data.update(action="import", group="Importing to Audiobookshelf")
    return await request.app.state.workflows.submit("default", data, operation)


@router.get("/{job_id}")
async def job_status(job_id: str, request: Request) -> dict:
    job = await request.app.state.workflows.get("default", job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return job