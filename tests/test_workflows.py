from __future__ import annotations

import asyncio
import fnmatch
import json

from tests.test_yoto_store import _FakeRedis
from yoto_bulk.workflows import WorkflowManager, report_workflow


class WorkflowRedis(_FakeRedis):
    async def eval(self, script, key_count, *args):
        if key_count == 1:
            key, job_id = args
            if self.values.get(key) == job_id:
                return self.values.pop(key)
            return 0
        active_key, job_key, worker_key = args[:3]
        job_prefix, worker_prefix, job_id, record, retention = args[3:]
        current_id = self.values.get(active_key)
        previous = self.values.get(job_prefix + current_id) if current_id else None
        if previous:
            job = json.loads(previous)
            active = job["status"] in {"queued", "running"}
            if active and worker_prefix + job["worker_id"] in self.values:
                return previous
        self.values.update({active_key: job_id, job_key: record, worker_key: "active"})
        return record

    async def set(self, key, value, nx=False, xx=False, ex=None):
        if (nx and key in self.values) or (xx and key not in self.values):
            return None
        self.values[key] = value
        return True

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def delete(self, key):
        self.values.pop(key, None)

    async def expire(self, key, seconds):
        return key in self.values

    async def scan_iter(self, match):
        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key


def test_jobs_persist_progress_and_coalesce_duplicates() -> None:
    async def run():
        redis = WorkflowRedis()
        manager = WorkflowManager(redis, "yoto")
        gate = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            await report_workflow("Uploading audio", tracks_done=2, tracks_total=5)
            started.set()
            await gate.wait()
            return {"status": "pushed"}

        spec = {"action": "push", "card_id": "abs-1", "label": "Test Book"}
        job = await manager.submit("user-1", spec, operation)
        await started.wait()
        duplicate = await manager.submit("user-1", spec, operation)
        restored = await manager.get("user-1", job["id"])
        assert duplicate["id"] == job["id"]
        assert restored["stage"] == "Uploading audio"
        assert restored["progress"]["tracks_done"] == 2
        assert await manager.get("user-2", job["id"]) is None
        gate.set()
        await asyncio.gather(*list(manager._tasks.values()))
        finished = await manager.get("user-1", job["id"])
        assert finished["status"] == "done"
        assert calls == 1
        assert len(await manager.list("user-1")) == 1
        await manager.close()

    asyncio.run(run())


def test_jobs_preserve_failures_and_allow_explicit_retry() -> None:
    async def run():
        manager = WorkflowManager(WorkflowRedis(), "borrowbox")

        async def failed():
            raise RuntimeError("Audiobookshelf TLS certificate has expired")

        async def success():
            return {"status": "imported"}

        spec = {"action": "import", "library": "test", "loan_id": "loan-1"}
        job = await manager.submit("default", spec, failed)
        await asyncio.gather(*list(manager._tasks.values()))
        failure = await manager.get("default", job["id"])
        assert failure["error"] == "Audiobookshelf TLS certificate has expired"
        retried = await manager.submit("default", spec, success)
        assert retried["id"] != job["id"]
        await asyncio.gather(*list(manager._tasks.values()))
        assert (await manager.get("default", retried["id"]))["status"] == "done"
        await manager.close()

    asyncio.run(run())


def test_shutdown_marks_inflight_jobs_interrupted() -> None:
    async def run():
        manager = WorkflowManager(WorkflowRedis(), "yoto")
        started = asyncio.Event()

        async def operation():
            started.set()
            await asyncio.Event().wait()

        job = await manager.submit("user-1", {"action": "push", "card_id": "abs-1"}, operation)
        await started.wait()
        await manager.close()
        restored = await manager.get("user-1", job["id"])
        assert restored["status"] == "error"
        assert restored["stage"] == "Interrupted"

    asyncio.run(run())


def test_concurrent_workers_claim_only_one_copy_of_the_same_task() -> None:
    async def run():
        redis = WorkflowRedis()
        first = WorkflowManager(redis, "yoto")
        second = WorkflowManager(redis, "yoto")
        gate = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            await gate.wait()

        spec = {"action": "push", "card_id": "abs-1"}
        first_job, second_job = await asyncio.gather(
            first.submit("user-1", spec, operation), second.submit("user-1", spec, operation),
        )
        assert first_job["id"] == second_job["id"]
        gate.set()
        await asyncio.gather(*list(first._tasks.values()), *list(second._tasks.values()))
        assert calls == 1
        await first.close()
        await second.close()

    asyncio.run(run())