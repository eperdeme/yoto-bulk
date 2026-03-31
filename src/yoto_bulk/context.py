"""Shared application context."""

from __future__ import annotations

from functools import cached_property

from rich.console import Console

from yoto_bulk.auth import YotoAuthService
from yoto_bulk.config import Settings
from yoto_bulk.library import LocalLibrary
from yoto_bulk.llm import LlmService
from yoto_bulk.yoto_client import YotoClient


class AppContext:
    """Lazily-constructed service bag shared across CLI commands."""

    __slots__ = ("settings", "console", "dry_run", "__dict__")

    def __init__(self, settings: Settings, console: Console, dry_run: bool = True) -> None:
        self.settings = settings
        self.console = console
        self.dry_run = dry_run

    @cached_property
    def auth(self) -> YotoAuthService:
        return YotoAuthService(self.settings)

    @cached_property
    def yoto(self) -> YotoClient:
        return YotoClient(settings=self.settings, auth_service=self.auth)

    @cached_property
    def llm(self) -> LlmService:
        return LlmService(self.settings)

    @cached_property
    def library(self) -> LocalLibrary:
        return LocalLibrary(self.settings.library_dir)

