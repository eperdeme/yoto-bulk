from __future__ import annotations

import os
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for path in (
    ROOT / "src",
    ROOT / "packages" / "borrowbox" / "src",
    ROOT / "packages" / "borrowbox-server" / "src",
    ROOT / "packages" / "yoto-server" / "src",
):
    sys.path.insert(0, str(path))


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()


if "redis" not in sys.modules:
    redis_mod = types.ModuleType("redis")
    redis_asyncio_mod = types.ModuleType("redis.asyncio")
    redis_asyncio_mod.Redis = object
    redis_mod.asyncio = redis_asyncio_mod
    sys.modules["redis"] = redis_mod
    sys.modules["redis.asyncio"] = redis_asyncio_mod


if "fastapi" not in sys.modules:
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_responses_mod = types.ModuleType("fastapi.responses")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def post(self, *args, **kwargs):
            return lambda fn: fn

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def delete(self, *args, **kwargs):
            return lambda fn: fn

        def patch(self, *args, **kwargs):
            return lambda fn: fn

    def Depends(dep):  # noqa: N802
        return dep

    class Request:  # pragma: no cover - marker type only
        pass

    class Response:
        def __init__(self, content=b"", media_type=None, headers=None, status_code=200):
            self.body = content.encode() if isinstance(content, str) else content
            self.media_type = media_type
            self.status_code = status_code
            self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    fastapi_mod.APIRouter = APIRouter
    fastapi_mod.Depends = Depends
    fastapi_mod.HTTPException = HTTPException
    fastapi_mod.Request = Request
    fastapi_responses_mod.Response = Response
    sys.modules["fastapi"] = fastapi_mod
    sys.modules["fastapi.responses"] = fastapi_responses_mod
