"""Redis-backed status and lifecycle management for service workflows."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

ProgressReporter = Callable[[str, dict[str, Any]], Awaitable[None]]
Workflow = Callable[[], Awaitable[Any]]
_progress: ContextVar[ProgressReporter | None] = ContextVar("workflow_progress", default=None)
_RETENTION_SECONDS = 172800
_ACTIVE = {"queued", "running"}
_CLAIM = """
local existing = redis.call('GET', KEYS[1])
if existing then
    local record = redis.call('GET', ARGV[1] .. existing)
    if record then
        local job = cjson.decode(record)
        if (job.status == 'queued' or job.status == 'running') and
           redis.call('EXISTS', ARGV[2] .. job.worker_id) == 1 then
            return record
        end
    end
end
redis.call('SET', KEYS[1], ARGV[3], 'EX', 7200)
redis.call('SETEX', KEYS[2], ARGV[5], ARGV[4])
redis.call('SETEX', KEYS[3], 90, 'active')
return ARGV[4]
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def report_workflow(stage: str, **details: Any) -> None:
    reporter = _progress.get()
    if reporter is not None:
        await reporter(stage, details)


class WorkflowManager:
    def __init__(self, redis_client: Any, namespace: str) -> None:
        self.redis = redis_client
        self.namespace = namespace
        self.worker_id = uuid4().hex
        self._tasks: dict[str, asyncio.Task] = {}
        self._leases: dict[str, str] = {}
        self._serial = asyncio.Lock()
        self._heartbeat: asyncio.Task | None = None

    def _key(self, owner: str, job_id: str) -> str:
        return f"workflow:{self.namespace}:{owner}:job:{job_id}"

    def _worker_key(self, worker_id: str) -> str:
        return f"workflow:{self.namespace}:worker:{worker_id}"

    async def _save(self, owner: str, job: dict[str, Any]) -> None:
        job["updated_at"] = datetime.now(UTC).isoformat()
        await self.redis.setex(
            self._key(owner, job["id"]), _RETENTION_SECONDS, json.dumps(job)
        )

    async def get(self, owner: str, job_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._key(owner, job_id))
        if raw is None:
            return None
        job = json.loads(raw)
        if job["status"] in _ACTIVE and not await self.redis.get(
            self._worker_key(job["worker_id"])
        ):
            job.update(
                status="error", stage="Interrupted", error="Service interrupted. Retry to resume."
            )
            await self._save(owner, job)
        return job

    async def list(self, owner: str) -> list[dict[str, Any]]:
        result = []
        async for key in self.redis.scan_iter(match=self._key(owner, "*")):
            job = await self.get(owner, key.rsplit(":", 1)[-1])
            if job is not None:
                result.append(job)
        return sorted(result, key=lambda job: job["created_at"])[-100:]

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await self.redis.setex(self._worker_key(self.worker_id), 90, "active")
                for job_id, active_key in list(self._leases.items()):
                    if await self.redis.get(active_key) == job_id:
                        await self.redis.expire(active_key, 7200)
                await asyncio.sleep(15)
        finally:
            await self.redis.delete(self._worker_key(self.worker_id))

    async def submit(
        self, owner: str, spec: dict[str, Any], operation: Workflow
    ) -> dict[str, Any]:
        identity = {key: value for key, value in spec.items() if key not in {"label", "group"}}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        active_key = f"workflow:{self.namespace}:{owner}:active:{digest}"
        job_id = uuid4().hex
        job = {
            "id": job_id, "spec": spec, "label": spec.get("label") or spec["action"],
            "group": spec.get("group") or "Working", "status": "queued", "stage": "Queued",
            "created_at": datetime.now(UTC).isoformat(), "worker_id": self.worker_id,
            "progress": {}, "error": None,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        raw = await self.redis.eval(
            _CLAIM, 3, active_key, self._key(owner, job_id),
            self._worker_key(self.worker_id), self._key(owner, ""),
            f"workflow:{self.namespace}:worker:", job_id, json.dumps(job), _RETENTION_SECONDS,
        )
        claimed = json.loads(raw)
        if claimed["id"] != job_id:
            return claimed

        async def progress(stage: str, details: dict[str, Any]) -> None:
            job["stage"] = stage
            job["progress"].update(details)
            await self._save(owner, job)

        async def run() -> None:
            token = _progress.set(progress)
            try:
                async with self._serial:
                    job.update(status="running", stage="Starting")
                    await self._save(owner, job)
                    result = await operation()
                    job["result"] = result.model_dump() if hasattr(result, "model_dump") else result
                    if isinstance(job["result"], dict) and job["result"].get("status") in {
                        "error", "partial"
                    }:
                        raise RuntimeError(
                            job["result"].get("message")
                            or "Operation completed partially. Retry to resume."
                        )
                    job.update(status="done", stage="Done")
            except asyncio.CancelledError:
                job.update(
                    status="error",
                    stage="Interrupted",
                    error="Service interrupted. Retry to resume.",
                )
                raise
            except Exception as exc:
                job.update(status="error", stage="Failed", error=str(exc)[:4000])
            finally:
                _progress.reset(token)
                await self._save(owner, job)
                await self.redis.eval(_RELEASE, 1, active_key, job_id)
                self._tasks.pop(job_id, None)
                self._leases.pop(job_id, None)

            self._leases[job_id] = active_key
        self._tasks[job_id] = asyncio.create_task(run())
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._heartbeat_loop())
        return job

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self.redis.delete(self._worker_key(self.worker_id))