"""CLI entrypoint for yoto-bulk."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import json
import logging
from pathlib import Path
import shutil
from typing import Annotated, Any, TypeVar
from urllib.parse import parse_qs, urlparse

import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from yoto_bulk.card_utils import normalize_title, resolve_author, resolve_card_id, resolve_title
from yoto_bulk.config import Settings
from yoto_bulk.context import AppContext
from yoto_bulk.library import LocalLibrary
from yoto_bulk.logging import configure_logging
from yoto_bulk.reconcile import LocalLibraryScanner, ReconcileService
from yoto_bulk.yoto_client import YotoApiError

T = TypeVar("T")

app = typer.Typer(help="Bulk operations for Yoto libraries.")
auth_app = typer.Typer(help="Authenticate with the Yoto API.")
reconcile_app = typer.Typer(help="Compare local audiobooks with the Yoto library.")
library_app = typer.Typer(help="Inspect Yoto library metadata.")
series_app = typer.Typer(help="Series detection and grouping workflows.")
metadata_app = typer.Typer(help="Enrich and repair Yoto metadata.")
console = Console()
logger = logging.getLogger(__name__)


def run_async_command(coro: Coroutine[Any, Any, T], app_console: Console) -> T:
    import asyncio

    try:
        return asyncio.run(coro)
    except ValueError as exc:
        app_console.print({"error": str(exc)})
        raise typer.Exit(code=1) from exc
    except YotoApiError as exc:
        app_console.print({"error": str(exc)})
        raise typer.Exit(code=1) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        payload: str | dict[str, object]
        try:
            payload = exc.response.json()
        except ValueError:
            payload = exc.response.text
        app_console.print(
            {
                "error": f"Yoto API request failed with HTTP {status}",
                "status_code": status,
                "response": payload,
            }
        )
        raise typer.Exit(code=1) from exc
    except httpx.HTTPError as exc:
        app_console.print({"error": f"Network request failed: {exc}"})
        raise typer.Exit(code=1) from exc


def _capture_oauth_callback(redirect_uri: str, timeout_seconds: float = 180.0) -> dict[str, str] | None:
    import http.server
    import socketserver
    import threading
    import time

    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        logger.debug(
            "OAuth callback listener skipped because redirect URI is not a localhost http endpoint",
            extra={"redirect_uri": redirect_uri},
        )
        return None

    port = parsed.port or 80
    callback_path = parsed.path or "/"
    result: dict[str, str] = {}
    finished = threading.Event()

    logger.info(
        "Starting OAuth callback listener",
        extra={
            "redirect_uri": redirect_uri,
            "hostname": parsed.hostname,
            "port": port,
            "callback_path": callback_path,
            "timeout_seconds": timeout_seconds,
        },
    )

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            logger.info(
                "Received OAuth callback request",
                extra={"request_path": request.path, "callback_path": callback_path},
            )
            if request.path != callback_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            params = parse_qs(request.query)
            result["code"] = params.get("code", [""])[0]
            result["state"] = params.get("state", [""])[0]
            result["error"] = params.get("error", [""])[0]
            logger.info(
                "Captured OAuth callback payload",
                extra={
                    "has_code": bool(result["code"]),
                    "has_state": bool(result["state"]),
                    "has_error": bool(result["error"]),
                },
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authentication complete</h1><p>You can close this window and return to the terminal.</p></body></html>"
            )
            finished.set()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        with ReusableTCPServer((parsed.hostname, port), CallbackHandler) as server:
            server.timeout = 0.5
            deadline = time.monotonic() + timeout_seconds
            logger.debug(
                "OAuth callback listener bound successfully",
                extra={"hostname": parsed.hostname, "port": port},
            )
            while time.monotonic() < deadline and not finished.is_set():
                server.handle_request()
    except OSError as exc:
        logger.exception(
            "OAuth callback listener failed to bind",
            extra={"hostname": parsed.hostname, "port": port},
        )
        raise typer.BadParameter(
            f"Could not listen for OAuth callback on {parsed.hostname}:{port}: {exc}"
        ) from exc

    logger.info(
        "OAuth callback listener stopped",
        extra={"received_callback": bool(result), "redirect_uri": redirect_uri},
    )
    return result if result else None

app.add_typer(auth_app, name="auth")
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(library_app, name="library")
app.add_typer(series_app, name="series")
app.add_typer(metadata_app, name="metadata")


def get_context(ctx: typer.Context) -> AppContext:
    return ctx.obj


def _resolve_card_id(card: dict[str, Any]) -> str:
    return resolve_card_id(card)


def _resolve_card_title(card: dict[str, Any]) -> str:
    return resolve_title(card)


def _extract_metadata_author(card: dict[str, Any]) -> str | None:
    return resolve_author(card)


def _normalize_title(value: str) -> str:
    return normalize_title(value)


def _resolve_family_library_group(
    groups: list[dict[str, Any]],
    series_name: str,
    matched_book_ids: set[str],
) -> dict[str, Any]:
    normalized_series_name = _normalize_title(series_name)
    exact_name_matches = [
        group
        for group in groups
        if _normalize_title(str(group.get("name") or "")) == normalized_series_name
    ]
    if len(exact_name_matches) == 1:
        return {"status": "matched_exact_name", "group": exact_name_matches[0]}
    if len(exact_name_matches) > 1:
        return {"status": "ambiguous_name", "groups": exact_name_matches}

    overlap_matches = [
        group
        for group in groups
        if matched_book_ids
        & ({
            str(item.get("contentId") or item.get("cardId") or item.get("id") or "").strip()
            for item in group.get("items", [])
            if isinstance(item, dict)
        } or {
            _resolve_card_id(card)
            for card in group.get("cards", [])
            if isinstance(card, dict) and _resolve_card_id(card)
        })
    ]
    if len(overlap_matches) == 1:
        return {"status": "matched_existing_membership", "group": overlap_matches[0]}
    if len(overlap_matches) > 1:
        return {"status": "ambiguous_membership", "groups": overlap_matches}

    return {"status": "missing"}


def _match_titles_to_books(
    titles: list[Any],
    series_name: str,
    books_by_normalized_title: dict[str, list[Any]],
    summary: dict[str, Any],
    progress: Progress,
    task_id: Any,
) -> tuple[list[tuple[Any, dict[str, Any]]], list[dict[str, Any]]]:
    """Match suggested series titles to local books. Returns (matched, unmatched) result lists."""
    matched_entries: list[tuple[Any, dict[str, Any]]] = []
    missing_or_ambiguous: list[dict[str, Any]] = []

    for sequence_number, raw_title in enumerate(titles, start=1):
        title = str(raw_title).strip()
        result: dict[str, Any] = {
            "series": series_name,
            "sequence_number": sequence_number,
            "title": title,
        }
        progress.update(task_id, description=f"Processing {title}")
        summary["processed"] += 1

        matches = books_by_normalized_title.get(_normalize_title(title), [])
        if not matches:
            logger.debug("Skipped '%s' for series '%s': not found in selection", title, series_name)
            summary["skipped"] += 1
            result["status"] = "missing_from_selection"
            missing_or_ambiguous.append(result)
            progress.advance(task_id)
            continue

        if len(matches) > 1:
            summary["skipped"] += 1
            result.update({"status": "ambiguous_title", "matching_card_ids": [b.id for b in matches]})
            missing_or_ambiguous.append(result)
            progress.advance(task_id)
            continue

        book = matches[0]
        result["card_id"] = book.id
        matched_entries.append((book, result))
        progress.advance(task_id)

    return matched_entries, missing_or_ambiguous


def _update_local_card_metadata(
    lib: LocalLibrary,
    matched_entries: list[tuple[Any, dict[str, Any]]],
    series_name: str,
    group_resolution: dict[str, Any],
    target_group: dict[str, Any] | None,
    summary: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Stamp series/sequence into local card metadata. Returns cards ready for group sync."""
    successful: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for book, result in matched_entries:
        result["group_validation_status"] = group_resolution["status"]
        if target_group is not None:
            result["group_id"] = target_group.get("id")
            result["group_name"] = target_group.get("name")

        if group_resolution["status"] in {"ambiguous_name", "ambiguous_membership"}:
            summary["skipped"] += 1
            result["status"] = "ambiguous_group_match"
            result["candidate_group_ids"] = [
                g.get("id") for g in group_resolution.get("groups", []) if isinstance(g, dict)
            ]
            summary["results"].append(result)
            continue

        try:
            full_card_payload = lib.load_card(book.id)
            if full_card_payload is None:
                summary["skipped"] += 1
                result["status"] = "missing_from_local_library"
                summary["results"].append(result)
                continue

            metadata = full_card_payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                full_card_payload["metadata"] = metadata

            metadata["series"] = series_name
            metadata["sequence_number"] = result["sequence_number"]

            series_tag = f"Series: {series_name} | Book: {result['sequence_number']}"
            existing_description = metadata.get("description") or ""
            if series_tag not in existing_description:
                separator = "\n\n" if existing_description else ""
                new_desc = f"{existing_description}{separator}{series_tag}"
                from yoto_bulk.card_utils import MAX_DESCRIPTION_LENGTH
                metadata["description"] = new_desc[:MAX_DESCRIPTION_LENGTH]

            lib.save_card(book.id, full_card_payload)
            summary["updated"] += 1
            result["metadata_status"] = "updated_locally"
            successful.append((full_card_payload, result))
        except Exception as exc:
            logger.warning(
                "Skipping series update because local card update failed",
                extra={"card_id": book.id, "title": result["title"], "series": series_name, "error": str(exc)},
            )
            summary["failed"] += 1
            result.update({"status": "api_failed", "error": str(exc), "metadata_status": "api_failed"})
            summary["results"].append(result)

    return successful


async def _sync_group_membership(
    yoto_client: Any,
    app_context: AppContext,
    successful_group_cards: list[tuple[dict[str, Any], dict[str, Any]]],
    series_name: str,
    target_group: dict[str, Any] | None,
    target_group_payload: dict[str, Any] | None,
    target_group_content_ids: set[str],
    existing_groups: list[dict[str, Any]],
    group_budget: int,
    summary: dict[str, Any],
) -> int:
    """Add cards to an existing group or create a new one.  Returns the updated group budget."""
    is_new_group = target_group is None

    if is_new_group and len(successful_group_cards) <= 1:
        summary["skipped_singleton_series"] += 1
        for _, result in successful_group_cards:
            result["group_membership_status"] = "skipped_singleton_series"
        return group_budget

    if is_new_group and group_budget <= 0:
        summary["skipped_group_limit"] += 1
        for _, result in successful_group_cards:
            result["group_membership_status"] = "skipped_group_limit"
        return group_budget

    cards_to_add: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for card_payload, result in successful_group_cards:
        card_id = _resolve_card_id(card_payload)
        if target_group is not None and card_id in target_group_content_ids:
            result["group_membership_status"] = "already_in_group"
        else:
            cards_to_add.append((card_payload, result))

    if app_context.dry_run:
        if target_group is not None:
            for _, result in cards_to_add:
                result["group_membership_status"] = "would_add_to_existing_group"
        elif cards_to_add:
            for _, result in cards_to_add:
                result["group_creation_status"] = "would_create_group"
                result["group_membership_status"] = "would_add_to_new_group"
    elif target_group is not None and cards_to_add and target_group_payload is not None:
        try:
            existing_items = [
                {"contentId": str(item.get("contentId") or "").strip()}
                for item in target_group_payload.get("items", [])
                if isinstance(item, dict) and item.get("contentId")
            ]
            new_items = [{"contentId": _resolve_card_id(cp)} for cp, _ in cards_to_add]
            updated_group = await yoto_client.update_family_library_group(
                group_id=str(target_group_payload.get("id")),
                name=str(target_group_payload.get("name") or series_name),
                image_id=str(target_group_payload.get("imageId") or "fp-cards"),
                items=existing_items + new_items,
            )
            summary["group_cards_added"] += len(cards_to_add)
            for _, result in cards_to_add:
                result["group_membership_status"] = "added_to_existing_group"
        except Exception as exc:
            logger.warning(
                "Skipping family library group update",
                extra={"series": series_name, "group_id": target_group_payload.get("id"), "error": str(exc)},
            )
            summary["failed"] += len(cards_to_add)
            for _, result in cards_to_add:
                result["status"] = "group_api_failed"
                result["group_membership_status"] = "api_failed"
                result["error"] = str(exc)
    elif target_group is None and cards_to_add:
        try:
            created_group = await yoto_client.create_family_library_group(
                name=series_name,
                items=[{"contentId": _resolve_card_id(cp)} for cp, _ in cards_to_add],
            )
            existing_groups.append(created_group)
            summary["groups_created"] += 1
            summary["group_cards_added"] += len(cards_to_add)
            group_budget -= 1
            for _, result in cards_to_add:
                result["group_id"] = created_group.get("id")
                result["group_name"] = created_group.get("name")
                result["group_creation_status"] = "created"
                result["group_membership_status"] = "added_to_new_group"
        except Exception as exc:
            logger.warning(
                "Skipping family library group creation",
                extra={"series": series_name, "error": str(exc)},
            )
            summary["failed"] += len(cards_to_add)
            for _, result in cards_to_add:
                result["status"] = "group_api_failed"
                result["group_creation_status"] = "api_failed"
                result["group_membership_status"] = "api_failed"
                result["error"] = str(exc)

    return group_budget


async def _apply_series_suggestions(
    app_context: AppContext,
    selected_books: list[Any],
    suggestions: dict[str, Any],
) -> dict[str, Any]:
    lib = app_context.library
    yoto_client = app_context.yoto
    suggested_series = suggestions.get("series", {})
    series_mapping = suggested_series if isinstance(suggested_series, dict) else {}
    existing_groups = await yoto_client.list_family_library_groups()

    books_by_normalized_title: dict[str, list[Any]] = {}
    for book in selected_books:
        normalized_title = _normalize_title(book.title)
        books_by_normalized_title.setdefault(normalized_title, []).append(book)

    summary: dict[str, Any] = {
        "dry_run": app_context.dry_run,
        "candidate_count": len(selected_books),
        "series_count": len(series_mapping),
        "max_family_groups": app_context.settings.max_family_groups,
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "skipped_singleton_series": 0,
        "skipped_group_limit": 0,
        "failed": 0,
        "groups_matched": 0,
        "groups_created": 0,
        "group_cards_added": 0,
        "results": [],
    }

    if not series_mapping:
        return summary

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=app_context.console,
    )

    total_titles = sum(len(titles) for titles in series_mapping.values() if isinstance(titles, list))
    sorted_series = sorted(
        series_mapping.items(),
        key=lambda kv: len(kv[1]) if isinstance(kv[1], list) else 0,
        reverse=True,
    )
    group_budget = max(0, app_context.settings.max_family_groups - len(existing_groups))

    with progress:
        task_id = progress.add_task("Applying series suggestions", total=total_titles)
        for series_name, titles in sorted_series:
            if not isinstance(titles, list):
                continue

            # Phase 1: match titles to books
            matched_entries, missing_or_ambiguous = _match_titles_to_books(
                titles, series_name, books_by_normalized_title, summary, progress, task_id,
            )

            # Phase 2: resolve target group
            matched_book_ids = {book.id for book, _ in matched_entries}
            group_resolution = _resolve_family_library_group(existing_groups, series_name, matched_book_ids)
            target_group = group_resolution.get("group") if isinstance(group_resolution.get("group"), dict) else None
            target_group_payload: dict[str, Any] | None = None
            target_group_content_ids: set[str] = set()

            if target_group is not None:
                summary["groups_matched"] += 1
                target_group_content_ids = yoto_client.family_group_content_ids(target_group)
                if not app_context.dry_run:
                    target_group_payload = await yoto_client.get_family_library_group(str(target_group.get("id")))
                    target_group_content_ids = yoto_client.family_group_content_ids(target_group_payload)

            # Phase 3: update local card metadata
            successful_group_cards = _update_local_card_metadata(
                lib, matched_entries, series_name, group_resolution, target_group, summary,
            )

            # Phase 4: sync group membership
            if successful_group_cards and group_resolution["status"] not in {"ambiguous_name", "ambiguous_membership"}:
                group_budget = await _sync_group_membership(
                    yoto_client, app_context, successful_group_cards, series_name,
                    target_group, target_group_payload, target_group_content_ids,
                    existing_groups, group_budget, summary,
                )

            # Flush remaining results
            summary["results"].extend(missing_or_ambiguous)
            for _, result in matched_entries:
                if "status" not in result:
                    result["status"] = "dry_run" if app_context.dry_run else "updated"
                summary["results"].append(result)

    return summary


async def _enrich_metadata_authors(app_context: AppContext) -> dict[str, Any]:
    lib = _require_local_library(app_context)
    llm_service = app_context.llm
    all_cards = lib.load_all_cards()

    cards_with_ids = [(cid, payload) for cid, payload in all_cards if cid]
    candidates = [(cid, payload) for cid, payload in cards_with_ids if not _extract_metadata_author(payload)]

    summary: dict[str, Any] = {
        "library_count": len(cards_with_ids),
        "missing_author_count": len(candidates),
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "results": [],
    }

    if not candidates:
        return summary

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=app_context.console,
    )

    with progress:
        task_id = progress.add_task("Enriching missing authors", total=len(candidates))
        for card_id, payload in candidates:
            title = _resolve_card_title(payload)
            result: dict[str, Any] = {"card_id": card_id, "title": title}
            progress.update(task_id, description=f"Processing {title}")

            try:
                guessed_author = await asyncio.to_thread(llm_service.guess_author, title)
            except Exception as exc:
                logger.warning("Skipping author enrichment because the LLM call failed", extra={"card_id": card_id, "title": title, "error": str(exc)})
                summary["failed"] += 1
                result.update({"status": "llm_failed", "error": str(exc)})
                summary["results"].append(result)
                summary["processed"] += 1
                progress.advance(task_id)
                continue

            try:
                metadata = payload.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    payload["metadata"] = metadata
                metadata["author"] = guessed_author

                lib.save_card(card_id, payload)
                summary["updated"] += 1
                result["author"] = guessed_author
                result["status"] = "updated_locally"
            except Exception as exc:
                logger.warning("Skipping author enrichment because local save failed", extra={"card_id": card_id, "title": title, "error": str(exc)})
                summary["failed"] += 1
                result.update({"status": "save_failed", "error": str(exc)})
            finally:
                summary["results"].append(result)
                summary["processed"] += 1
                progress.advance(task_id)

    return summary


@app.callback()
def main(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply",
            help="Preview changes by default. Use --apply to allow mutations.",
        ),
    ] = True,
    log_level: Annotated[
        str | None,
        typer.Option(help="Override the configured log level for this invocation."),
    ] = None,
) -> None:
    """Run the Yoto bulk CLI."""
    settings = Settings()
    if log_level:
        settings.log_level = log_level
    settings.ensure_directories()
    configure_logging(settings.log_level)
    ctx.obj = AppContext(settings=settings, console=console, dry_run=dry_run)


@app.command()
def version() -> None:
    """Show the current application version."""
    console.print("yoto-bulk 0.1.0")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Show current scaffold status."""
    app_context = get_context(ctx)
    app_context.console.print(
        {
            "status": "ok",
            "data_dir": str(app_context.settings.data_dir),
            "cache_dir": str(app_context.settings.cache_dir),
            "library_dir": str(app_context.settings.library_dir),
            "library_cards": len(app_context.library.list_card_ids()),
            "token_path": str(app_context.settings.token_path),
            "dry_run": app_context.dry_run,
            "audiobook_dir": (
                str(app_context.settings.audiobook_dir)
                if app_context.settings.audiobook_dir
                else None
            ),
        }
    )


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Inspect the stored token state."""
    app_context = get_context(ctx)
    token_set = app_context.auth.token_store.load()
    app_context.console.print(
        {
            "authenticated": token_set is not None,
            "token_path": str(app_context.settings.token_path),
            "has_refresh_token": bool(token_set.refresh_token) if token_set else False,
        }
    )


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    redirect_uri: Annotated[
        str,
        typer.Option(help="OAuth redirect URI registered with your Yoto app."),
    ] = "http://localhost:8787/callback",
    scope: Annotated[str, typer.Option(help="OAuth scope string.")] = "profile offline_access",
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open-browser/--no-open-browser",
            help="Open the authorization URL in a browser after printing it.",
        ),
    ] = False,
    listen: Annotated[
        bool,
        typer.Option(
            "--listen/--no-listen",
            help="Start a temporary localhost callback server for browser-based login when the redirect URI points to localhost.",
        ),
    ] = True,
) -> None:
    """Print and optionally open a browser login URL for the headless OAuth flow."""
    app_context = get_context(ctx)
    try:
        bootstrap = app_context.auth.build_authorize_url(redirect_uri=redirect_uri, scope=scope)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    logger.info(
        "Prepared OAuth login flow",
        extra={
            "redirect_uri": redirect_uri,
            "scope": scope,
            "open_browser": open_browser,
            "listen": listen,
        },
    )
    should_listen = listen and open_browser
    callback_payload: dict[str, str] | None = None

    if should_listen:
        import threading
        import time
        import webbrowser

        listener_ready = threading.Event()
        listener_result: dict[str, str] = {}
        listener_error: list[BaseException] = []

        def listen_for_callback() -> None:
            parsed_redirect = urlparse(redirect_uri)
            if parsed_redirect.scheme != "http" or parsed_redirect.hostname not in {"localhost", "127.0.0.1"}:
                listener_ready.set()
                return

            import http.server
            import socketserver

            port = parsed_redirect.port or 80
            callback_path = parsed_redirect.path or "/"
            finished = threading.Event()

            class CallbackHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    request = urlparse(self.path)
                    logger.info(
                        "Received OAuth callback request",
                        extra={"request_path": request.path, "callback_path": callback_path},
                    )
                    if request.path != callback_path:
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(b"Not found")
                        return

                    params = parse_qs(request.query)
                    listener_result["code"] = params.get("code", [""])[0]
                    listener_result["state"] = params.get("state", [""])[0]
                    listener_result["error"] = params.get("error", [""])[0]
                    logger.info(
                        "Captured OAuth callback payload",
                        extra={
                            "has_code": bool(listener_result.get("code")),
                            "has_state": bool(listener_result.get("state")),
                            "has_error": bool(listener_result.get("error")),
                        },
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h1>Authentication complete</h1><p>You can close this window and return to the terminal.</p></body></html>"
                    )
                    finished.set()

                def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                    return

            class ReusableTCPServer(socketserver.TCPServer):
                allow_reuse_address = True

            logger.info(
                "Starting OAuth callback listener",
                extra={
                    "redirect_uri": redirect_uri,
                    "hostname": parsed_redirect.hostname,
                    "port": port,
                    "callback_path": callback_path,
                    "timeout_seconds": 180.0,
                },
            )
            try:
                with ReusableTCPServer((parsed_redirect.hostname, port), CallbackHandler) as server:
                    server.timeout = 0.5
                    listener_ready.set()
                    logger.debug(
                        "OAuth callback listener bound successfully",
                        extra={"hostname": parsed_redirect.hostname, "port": port},
                    )
                    deadline = time.monotonic() + 180.0
                    while time.monotonic() < deadline and not finished.is_set():
                        server.handle_request()
            except OSError as exc:
                listener_error.append(exc)
                listener_ready.set()
                logger.exception(
                    "OAuth callback listener failed to bind",
                    extra={"hostname": parsed_redirect.hostname, "port": port},
                )
                return
            finally:
                logger.info(
                    "OAuth callback listener stopped",
                    extra={"received_callback": bool(listener_result), "redirect_uri": redirect_uri},
                )

        listener_thread = threading.Thread(target=listen_for_callback, daemon=True)
        listener_thread.start()
        listener_ready.wait(timeout=5.0)
        if listener_error:
            raise typer.BadParameter(
                f"Could not listen for OAuth callback on {urlparse(redirect_uri).hostname}:{urlparse(redirect_uri).port or 80}: {listener_error[0]}"
            ) from listener_error[0]

        logger.info("Opening browser for OAuth login", extra={"redirect_uri": redirect_uri})
        webbrowser.open(bootstrap.authorize_url)
        listener_thread.join()
        callback_payload = listener_result or None
    elif open_browser:
        import webbrowser

        logger.info("Opening browser for OAuth login", extra={"redirect_uri": redirect_uri})
        webbrowser.open(bootstrap.authorize_url)

    logger.info(
        "OAuth login callback wait finished",
        extra={"received_callback": bool(callback_payload), "redirect_uri": redirect_uri},
    )
    if callback_payload and callback_payload.get("error"):
        raise typer.BadParameter(f"OAuth provider returned an error: {callback_payload['error']}")
    if callback_payload and callback_payload.get("code") and callback_payload.get("state"):
        if not app_context.auth.validate_state(callback_payload["state"]):
            raise typer.BadParameter("OAuth state mismatch. Run auth login again and use the returned state.")
        tokens = run_async_command(
            app_context.auth.exchange_code(code=callback_payload["code"], redirect_uri=redirect_uri),
            app_context.console,
        )
        app_context.console.print(
            {
                "saved": True,
                "opened_browser": open_browser,
                "redirect_uri": redirect_uri,
                "token_type": tokens.token_type,
                "has_refresh_token": bool(tokens.refresh_token),
            }
        )
        return

    app_context.console.print(
        {
            "authorize_url": bootstrap.authorize_url,
            "state": bootstrap.state,
            "audience": app_context.settings.yoto_api_audience,
            "scope": scope,
            "opened_browser": open_browser,
            "listening_for_callback": bool(listen and open_browser),
            "next_step": "If automatic capture does not complete, copy the returned code and state from your redirect URL and run auth exchange.",
        }
    )


@auth_app.command("exchange")
def auth_exchange(
    ctx: typer.Context,
    code: Annotated[
        str | None,
        typer.Option(help="Authorization code returned by Yoto OAuth. If omitted, prompt securely."),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option(help="OAuth state returned alongside the authorization code. If omitted, prompt interactively."),
    ] = None,
    redirect_uri: Annotated[
        str,
        typer.Option(help="OAuth redirect URI used during login."),
    ] = "http://localhost:8787/callback",
) -> None:
    """Exchange an OAuth code for access and refresh tokens."""
    app_context = get_context(ctx)
    resolved_code = code if code is not None else typer.prompt("Authorization code", hide_input=True)
    resolved_state = state if state is not None else typer.prompt("OAuth state")
    if not app_context.auth.validate_state(resolved_state):
        raise typer.BadParameter("OAuth state mismatch. Run auth login again and use the returned state.")
    tokens = run_async_command(
        app_context.auth.exchange_code(code=resolved_code, redirect_uri=redirect_uri),
        app_context.console,
    )
    app_context.console.print(
        {
            "saved": True,
            "token_type": tokens.token_type,
            "has_refresh_token": bool(tokens.refresh_token),
        }
    )


@auth_app.command("refresh")
def auth_refresh(ctx: typer.Context) -> None:
    """Refresh the saved OAuth access token."""
    app_context = get_context(ctx)
    tokens = run_async_command(app_context.auth.refresh_access_token(), app_context.console)
    app_context.console.print(
        {
            "refreshed": True,
            "token_type": tokens.token_type,
            "has_refresh_token": bool(tokens.refresh_token),
        }
    )


@reconcile_app.command("scan")
def reconcile_scan(ctx: typer.Context) -> None:
    """Scan the configured audiobook folder for zip files."""
    app_context = get_context(ctx)
    if app_context.settings.audiobook_dir is None:
        raise typer.BadParameter("Set YOTO_BULK_AUDIOBOOK_DIR or provide it in .env first.")
    scanner = LocalLibraryScanner()
    items = scanner.scan(app_context.settings.audiobook_dir)
    app_context.console.print(
        {
            "count": len(items),
            "audiobook_dir": str(app_context.settings.audiobook_dir),
            "sample": [str(item.path) for item in items[:5]],
        }
    )


@reconcile_app.command("compare")
def reconcile_compare(ctx: typer.Context) -> None:
    """Compare local zip titles against Yoto library titles."""
    app_context = get_context(ctx)
    if app_context.settings.audiobook_dir is None:
        raise typer.BadParameter("Set YOTO_BULK_AUDIOBOOK_DIR or provide it in .env first.")

    scanner = LocalLibraryScanner()
    reconciler = ReconcileService()
    local_items = scanner.scan(app_context.settings.audiobook_dir)
    yoto_books = run_async_command(app_context.yoto.list_library_books(), app_context.console)
    matches = reconciler.reconcile(local_items=local_items, yoto_books=yoto_books)
    missing = [match for match in matches if match.yoto_book is None]
    app_context.console.print(
        {
            "local_count": len(local_items),
            "yoto_count": len(yoto_books),
            "matched_count": len(matches) - len(missing),
            "missing_on_yoto": [match.local_item.stem for match in missing[:20]],
        }
    )


@library_app.command("list")
def library_list(ctx: typer.Context) -> None:
    """Fetch and print a sample of books from the Yoto library."""
    app_context = get_context(ctx)
    books = run_async_command(app_context.yoto.list_library_books(), app_context.console)
    app_context.console.print(
        {
            "count": len(books),
            "sample": [book.model_dump() for book in books[:10]],
        }
    )


async def _pull_library(app_context: AppContext, *, download_media: bool = True) -> dict[str, Any]:
    """Download all card payloads from Yoto to the local library store."""
    lib = app_context.library
    lib.ensure_directories()
    yoto_client = app_context.yoto

    library_payload = await yoto_client.get_content_mine()
    raw_cards = library_payload.get("cards", [])
    cards = [card for card in raw_cards if isinstance(card, dict)]

    summary: dict[str, Any] = {
        "library_dir": str(lib.library_dir),
        "remote_count": len(cards),
        "pulled": 0,
        "failed": 0,
        "media": {"cover": 0, "audio": 0, "icons": 0, "errors": []},
        "results": [],
    }

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=app_context.console,
    )

    sem = asyncio.Semaphore(6)

    async def _pull_one(card: dict[str, Any]) -> None:
        card_id = _resolve_card_id(card)
        title = _resolve_card_title(card)

        if not card_id:
            progress.advance(task_id)
            return

        async with sem:
            progress.update(task_id, description=f"Pulling {title}")
            try:
                full_payload = await yoto_client.get_content_card(card_id)
                lib.save_card(card_id, full_payload, update_pull_hash=True)
                summary["pulled"] += 1

                if download_media:
                    resolved_payload = await yoto_client.get_content_card(
                        card_id, playable=True
                    )
                    media_stats = await lib.download_media(
                        card_id, resolved_payload, media_client
                    )
                    if media_stats["cover"]:
                        summary["media"]["cover"] += 1
                    summary["media"]["audio"] += media_stats["audio"]
                    summary["media"]["icons"] += media_stats["icons"]
                    summary["media"]["errors"].extend(media_stats["errors"])

                summary["results"].append({"card_id": card_id, "title": title, "status": "pulled"})
            except Exception as exc:
                logger.warning(
                    "Failed to pull card",
                    extra={"card_id": card_id, "title": title, "error": str(exc)},
                )
                summary["failed"] += 1
                summary["results"].append({"card_id": card_id, "title": title, "status": "failed", "error": str(exc)})

            progress.advance(task_id)

    async with httpx.AsyncClient(timeout=120) as media_client:
        with progress:
            task_id = progress.add_task("Pulling library", total=len(cards))
            await asyncio.gather(*[_pull_one(card) for card in cards])

    return summary


async def _upload_audio_for_card(
    yoto_client: Any,
    lib: LocalLibrary,
    card_id: str,
    payload: dict[str, Any],
    progress: Progress,
    task_id: Any,
    title: str,
) -> dict[str, Any]:
    """Upload local audio files and fill in trackUrl/duration/fileSize/channels/format.

    Mutates *payload* in-place. Returns the payload with all tracks populated.
    """
    audio_files = lib.audio_files_for_card(card_id)
    chapters = payload.get("content", {}).get("chapters", [])

    if len(audio_files) != len(chapters):
        raise ValueError(
            f"Audio file count ({len(audio_files)}) != chapter count ({len(chapters)}) for {title}"
        )

    mime_map = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
    }

    for idx, (audio_path, chapter) in enumerate(zip(audio_files, chapters)):
        track = chapter.get("tracks", [{}])[0]
        # Skip if trackUrl already populated (re-push scenario)
        if track.get("trackUrl") and track["trackUrl"] != "":
            continue

        progress.update(task_id, description=f"Uploading {title} ({idx + 1}/{len(audio_files)})")

        content_type = mime_map.get(audio_path.suffix.lower(), "audio/mpeg")
        file_data = audio_path.read_bytes()

        transcode = await yoto_client.upload_and_transcode(file_data, content_type)
        info = transcode.get("transcodedInfo", {})

        track["trackUrl"] = f"yoto:#{transcode['transcodedSha256']}"
        track["duration"] = info.get("duration", 0)
        track["fileSize"] = info.get("fileSize", 0)
        track["channels"] = info.get("channels", "stereo")
        track["format"] = info.get("format", audio_path.suffix.lstrip("."))

    return payload


async def _upload_cover_for_card(
    yoto_client: Any,
    lib: LocalLibrary,
    card_id: str,
    payload: dict[str, Any],
    progress: Progress,
    task_id: Any,
    title: str,
) -> None:
    """Upload local cover image and set metadata.cover.imageL if missing."""
    cover = payload.get("metadata", {}).get("cover", {}).get("imageL")
    if cover:
        return  # Already has a cover URL

    cover_path = lib.cover_file_for_card(card_id)
    if not cover_path:
        return

    progress.update(task_id, description=f"Uploading cover for {title}")

    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}
    content_type = mime_map.get(cover_path.suffix.lower(), "image/jpeg")
    file_data = cover_path.read_bytes()

    media_url = await yoto_client.upload_cover_image(file_data, content_type)
    payload.setdefault("metadata", {}).setdefault("cover", {})["imageL"] = media_url


async def _push_library(app_context: AppContext) -> dict[str, Any]:
    """Push locally modified and imported cards back to the Yoto API."""
    lib = app_context.library
    yoto_client = app_context.yoto
    modified = lib.modified_cards()
    imported = lib.imported_cards()

    # Combine both lists — modified cards get updated, imported cards get created
    all_cards = [(cid, payload, title, "modified") for cid, payload, title in modified]
    all_cards += [(cid, payload, title, "imported") for cid, payload, title in imported]

    summary: dict[str, Any] = {
        "dry_run": app_context.dry_run,
        "modified_count": len(modified),
        "imported_count": len(imported),
        "pushed": 0,
        "failed": 0,
        "results": [],
    }

    if not all_cards:
        return summary

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=app_context.console,
    )

    with progress:
        task_id = progress.add_task("Pushing changes", total=len(all_cards))
        for card_id, payload, title, source in all_cards:
            progress.update(task_id, description=f"Pushing {title}")

            if app_context.dry_run:
                audio_count = len(lib.audio_files_for_card(card_id)) if source == "imported" else 0
                summary["pushed"] += 1
                summary["results"].append({
                    "card_id": card_id,
                    "title": title,
                    "source": source,
                    "status": "dry_run",
                    "audio_files_to_upload": audio_count,
                })
                progress.advance(task_id)
                continue

            try:
                # Enrich icons and upload cover for all cards
                from yoto_bulk.card_utils import enrich_payload_icons
                enrich_payload_icons(payload)

                await _upload_cover_for_card(
                    yoto_client, lib, card_id, payload, progress, task_id, title,
                )

                # For imported cards, upload audio files first
                if source == "imported":
                    await _upload_audio_for_card(
                        yoto_client, lib, card_id, payload, progress, task_id, title,
                    )

                # For imported cards, strip the temp ID so the API creates a new card
                push_payload = payload.copy()
                if source == "imported" and card_id.startswith("import-"):
                    push_payload.pop("cardId", None)

                progress.update(task_id, description=f"Creating {title}")
                result = await yoto_client.save_content(push_payload)

                # The API returns the real cardId for newly created cards
                new_card = result.get("card", result)
                real_id = new_card.get("cardId") or new_card.get("contentId") or card_id

                if source == "imported" and real_id != card_id:
                    # Rename locally to the real API-assigned cardId, preserving media
                    payload["cardId"] = real_id
                    lib.rename_card(card_id, real_id, payload)
                else:
                    lib.save_card(card_id, payload, update_pull_hash=True)

                summary["pushed"] += 1
                summary["results"].append({
                    "card_id": real_id,
                    "title": title,
                    "source": source,
                    "status": "pushed",
                    "old_id": card_id if real_id != card_id else None,
                })
            except Exception as exc:
                logger.warning(
                    "Failed to push card",
                    extra={"card_id": card_id, "title": title, "error": str(exc)},
                )
                summary["failed"] += 1
                summary["results"].append({"card_id": card_id, "title": title, "source": source, "status": "failed", "error": str(exc)})

            progress.advance(task_id)

    return summary


@library_app.command("pull")
def library_pull(
    ctx: typer.Context,
    media: Annotated[
        bool,
        typer.Option("--media/--no-media", help="Download audio, cover, and icon files."),
    ] = True,
) -> None:
    """Download all card data from Yoto into the local library."""
    app_context = get_context(ctx)
    summary = run_async_command(
        _pull_library(app_context, download_media=media), app_context.console
    )
    app_context.console.print(summary)


@library_app.command("push")
def library_push(ctx: typer.Context) -> None:
    """Push locally modified cards back to Yoto. Use --apply to persist."""
    app_context = get_context(ctx)
    lib = app_context.library
    if not lib.manifest_path.exists():
        raise typer.BadParameter("No local library found. Run `library pull` first.")
    summary = run_async_command(_push_library(app_context), app_context.console)
    app_context.console.print(summary)


@library_app.command("status")
def library_status(ctx: typer.Context) -> None:
    """Show which local cards have been modified or imported."""
    app_context = get_context(ctx)
    lib = app_context.library
    if not lib.manifest_path.exists():
        raise typer.BadParameter("No local library found. Run `library pull` first.")
    modified = lib.modified_cards()
    imported = lib.imported_cards()
    total_local = len(lib.list_card_ids())
    app_context.console.print(
        {
            "library_dir": str(lib.library_dir),
            "total_cards": total_local,
            "modified_count": len(modified),
            "modified": [{"card_id": cid, "title": title} for cid, _, title in modified],
            "imported_count": len(imported),
            "imported": [{"card_id": cid, "title": title} for cid, _, title in imported],
        }
    )


@library_app.command("import")
def library_import(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Path to a zip file or directory of zips to import."),
    ],
    title: Annotated[
        str | None,
        typer.Option(help="Override the title for raw-audio imports."),
    ] = None,
) -> None:
    """Import audiobook zip files into the local library.

    Supports Yoto export zips (containing card-data.json) and raw audio zips
    (MP3/M4A files from publishers). Use ``library push`` afterwards to upload
    to Yoto.
    """
    from pathlib import Path as P

    app_context = get_context(ctx)
    lib = app_context.library
    lib.ensure_directories()

    target = P(path).expanduser().resolve()
    if target.is_file() and target.suffix.lower() == ".zip":
        zips = [target]
    elif target.is_dir():
        zips = sorted(target.glob("*.zip"))
        if not zips:
            raise typer.BadParameter(f"No .zip files found in {target}")
    else:
        raise typer.BadParameter(f"Not a zip file or directory: {target}")

    results: list[dict[str, Any]] = []
    for zip_path in zips:
        try:
            result = lib.import_from_zip(zip_path, title=title)
            results.append(result)
        except Exception as exc:
            logger.warning("Failed to import %s: %s", zip_path.name, exc)
            results.append({"file": zip_path.name, "status": "failed", "error": str(exc)})

    imported = [r for r in results if r.get("status") == "imported"]
    failed = [r for r in results if r.get("status") == "failed"]
    app_context.console.print({
        "total_zips": len(zips),
        "imported": len(imported),
        "failed": len(failed),
        "results": results,
    })


@library_app.command("migrate")
def library_migrate(ctx: typer.Context) -> None:
    """Migrate from the old cards/{id}.json layout to the new human-readable layout.

    Moves card data and media into Author/Title [cardId]/ directories.
    The old layout is removed after a successful migration.
    """
    app_context = get_context(ctx)
    lib = app_context.library
    lib.ensure_directories()
    summary = lib.migrate_from_old_layout()
    app_context.console.print(summary)


@library_app.command("fix-imports")
def library_fix_imports(ctx: typer.Context) -> None:
    """One-shot repair of imported cards.

    Removes junk imports (no author), removes duplicates of pulled cards,
    and adds series/sequence metadata parsed from album-style titles.
    """
    app_context = get_context(ctx)
    lib = _require_local_library(app_context)
    summary = lib.fix_imports()
    app_context.console.print(summary)


@library_app.command("enrich")
def library_enrich(ctx: typer.Context) -> None:
    """Add missing numbered chapter icons to all local cards.

    Modifies card.json files so they show as modified for the next push.
    """
    app_context = get_context(ctx)
    lib = _require_local_library(app_context)
    summary = lib.enrich_cards()
    app_context.console.print(summary)


async def _recover_covers(app_context: AppContext, card_ids: list[str] | None = None) -> dict[str, Any]:
    """Download first audio track per card, extract ID3 cover art, upload to Yoto."""
    import tempfile

    lib = app_context.library
    yoto_client = app_context.yoto

    # Find cards missing local cover files
    candidates: list[tuple[str, dict[str, Any], str]] = []
    for card_id, payload in lib.load_all_cards():
        if card_ids and card_id not in card_ids:
            continue
        cover = payload.get("metadata", {}).get("cover", {}).get("imageL")
        if cover:
            continue  # Already has cover on API
        if lib.cover_file_for_card(card_id):
            continue  # Already has local cover (not yet uploaded)
        candidates.append((card_id, payload, resolve_title(payload)))

    summary: dict[str, Any] = {"total": len(candidates), "recovered": 0, "failed": 0, "results": []}
    if not candidates:
        return summary

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=app_context.console,
    )

    with progress:
        task_id = progress.add_task("Recovering covers", total=len(candidates))
        for card_id, payload, title in candidates:
            progress.update(task_id, description=f"Recovering {title}")
            try:
                # Get resolved (playable) payload with signed URLs
                resolved = await yoto_client.get_content_card(card_id, playable=True)
                chapters = resolved.get("content", {}).get("chapters", [])
                if not chapters:
                    raise ValueError("No chapters found")

                # Find a signed audio track URL from the first chapter
                track = chapters[0].get("tracks", [{}])[0]
                track_url = track.get("trackUrl", "")
                if not track_url.startswith("http"):
                    raise ValueError(f"No signed URL for first track: {track_url[:40]}")

                # Download into temp file and extract ID3 cover art
                async with httpx.AsyncClient(timeout=120) as dl_client:
                    resp = await dl_client.get(track_url)
                    resp.raise_for_status()
                    audio_bytes = resp.content

                cover_data, cover_mime = _extract_cover_from_bytes(audio_bytes)
                if not cover_data:
                    raise ValueError("No embedded cover art in first track")

                # Save cover locally
                ext = ".jpg" if "jpeg" in cover_mime else ".png"
                manifest = lib.load_manifest()
                entry = manifest.get("cards", {}).get(card_id, {})
                book_dir = lib.library_dir / entry["path"]
                cover_path = book_dir / f"cover{ext}"
                cover_path.write_bytes(cover_data)

                # Upload cover to Yoto
                if not app_context.dry_run:
                    img_content_type = "image/jpeg" if "jpeg" in cover_mime else "image/png"
                    media_url = await yoto_client.upload_cover_image(cover_data, img_content_type)
                    payload.setdefault("metadata", {}).setdefault("cover", {})["imageL"] = media_url
                    await yoto_client.save_content(payload)
                    lib.save_card(card_id, payload, update_pull_hash=True)

                summary["recovered"] += 1
                summary["results"].append({"card_id": card_id, "title": title, "status": "recovered"})
            except Exception as exc:
                logger.warning("Failed to recover cover for %s: %s", card_id, exc)
                summary["failed"] += 1
                summary["results"].append({"card_id": card_id, "title": title, "status": "failed", "error": str(exc)})
            progress.advance(task_id)

    return summary


def _extract_cover_from_bytes(audio_bytes: bytes) -> tuple[bytes | None, str]:
    """Extract embedded cover art from audio bytes using mutagen."""
    import io
    try:
        from mutagen import File as MutagenFile
        f = MutagenFile(io.BytesIO(audio_bytes))
        if f is None:
            return None, ""
        # ID3 tags (MP3)
        if hasattr(f, "tags") and f.tags:
            for key in f.tags:
                if key.startswith("APIC"):
                    apic = f.tags[key]
                    return apic.data, apic.mime
        # MP4 tags (M4A)
        if hasattr(f, "tags") and f.tags and "covr" in f.tags:
            covers = f.tags["covr"]
            if covers:
                return bytes(covers[0]), "image/jpeg"
    except Exception:
        pass
    return None, ""


@library_app.command("recover-covers")
def library_recover_covers(
    ctx: typer.Context,
    card_id: Annotated[
        list[str] | None,
        typer.Argument(help="Specific card IDs to recover. Omit for all cards missing covers."),
    ] = None,
) -> None:
    """Recover cover art from audio track ID3 tags and upload to Yoto.

    Downloads the first audio track from Yoto, extracts embedded cover art,
    saves it locally, and uploads it as the card's cover image.
    """
    app_context = get_context(ctx)
    _require_local_library(app_context)
    summary = run_async_command(_recover_covers(app_context, card_id), app_context.console)
    app_context.console.print(summary)


async def _add_cover(app_context: AppContext, card_id: str, image_path: Path) -> dict[str, Any]:
    """Copy cover image to card directory, upload to Yoto, and update the card."""
    lib = app_context.library
    yoto_client = app_context.yoto

    payload = lib.load_card(card_id)
    if payload is None:
        raise typer.BadParameter(f"Card {card_id} not found in library.")

    title = resolve_title(payload)

    # Copy to card directory
    manifest = lib.load_manifest()
    entry = manifest.get("cards", {}).get(card_id, {})
    book_dir = lib.library_dir / entry["path"]
    dest = book_dir / f"cover{image_path.suffix.lower()}"
    shutil.copy2(str(image_path), str(dest))

    if app_context.dry_run:
        return {"card_id": card_id, "title": title, "status": "dry_run", "cover_path": str(dest)}

    # Upload to Yoto
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp"}
    content_type = mime_map.get(image_path.suffix.lower(), "image/jpeg")
    file_data = image_path.read_bytes()
    media_url = await yoto_client.upload_cover_image(file_data, content_type)

    # Update card metadata
    payload.setdefault("metadata", {}).setdefault("cover", {})["imageL"] = media_url
    await yoto_client.save_content(payload)
    lib.save_card(card_id, payload, update_pull_hash=True)

    return {"card_id": card_id, "title": title, "status": "uploaded", "media_url": media_url}


@library_app.command("add-cover")
def library_add_cover(
    ctx: typer.Context,
    card_id: Annotated[str, typer.Argument(help="Card ID to add cover to.")],
    image: Annotated[str, typer.Argument(help="Path to cover image file (jpg/png).")],
) -> None:
    """Add a cover image to a card and upload it to Yoto."""
    app_context = get_context(ctx)
    _require_local_library(app_context)
    image_path = Path(image)
    if not image_path.exists():
        raise typer.BadParameter(f"Image file not found: {image}")
    summary = run_async_command(_add_cover(app_context, card_id, image_path), app_context.console)
    app_context.console.print(summary)


def _require_local_library(app_context: AppContext) -> LocalLibrary:
    """Return the local library or exit with a helpful error."""
    lib = app_context.library
    if not lib.manifest_path.exists():
        raise typer.BadParameter("No local library found. Run `library pull` first.")
    return lib


@series_app.command("suggest")
def series_suggest(
    ctx: typer.Context,
    limit: Annotated[
        int | None,
        typer.Option(help="Optionally limit how many library books are sent to OpenAI. Defaults to all books."),
    ] = None,
) -> None:
    """Use OpenAI to suggest likely series groupings from the local library."""
    app_context = get_context(ctx)
    lib = _require_local_library(app_context)
    books = lib.as_yoto_books()
    selected_books = books if limit is None else books[:limit]
    try:
        suggestions = app_context.llm.suggest_series(selected_books)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    # Cache suggestions so that `metadata tag-series` can use them later
    app_context.settings.ensure_directories()
    app_context.settings.series_suggestions_path.write_text(
        json.dumps(suggestions, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    apply_summary = run_async_command(
        _apply_series_suggestions(app_context, selected_books, suggestions),
        app_context.console,
    )
    app_context.console.print(
        {
            "dry_run": app_context.dry_run,
            "model": app_context.settings.openai_model,
            "library_count": len(books),
            "sent_to_llm": len(selected_books),
            "limit": limit,
            "suggestions": suggestions,
            "apply_summary": apply_summary,
        }
    )


@metadata_app.command("enrich")
def metadata_enrich(ctx: typer.Context) -> None:
    """Guess and fill missing metadata authors for library cards."""
    app_context = get_context(ctx)
    summary = run_async_command(_enrich_metadata_authors(app_context), app_context.console)
    app_context.console.print(summary)


def _tag_series_descriptions(app_context: AppContext) -> dict[str, Any]:
    """Stamp 'Series: X | Book: N' into descriptions using cached series suggestions."""
    lib = _require_local_library(app_context)
    cache_path = app_context.settings.series_suggestions_path
    if not cache_path.exists():
        raise typer.BadParameter(
            "No cached series suggestions found. Run `series suggest` first to generate them."
        )

    suggestions = json.loads(cache_path.read_text(encoding="utf-8"))
    series_mapping = suggestions.get("series", {})
    if not isinstance(series_mapping, dict):
        series_mapping = {}

    # Build a normalized-title -> (series_name, sequence_number) lookup
    title_to_series: dict[str, tuple[str, int]] = {}
    for series_name, titles in series_mapping.items():
        if not isinstance(titles, list):
            continue
        for seq, raw_title in enumerate(titles, start=1):
            title_to_series[_normalize_title(str(raw_title).strip())] = (series_name, seq)

    all_cards = lib.load_all_cards()

    summary: dict[str, Any] = {
        "library_count": len(all_cards),
        "series_in_cache": len(series_mapping),
        "processed": 0,
        "updated": 0,
        "already_tagged": 0,
        "skipped_no_series": 0,
        "failed": 0,
        "results": [],
    }

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=app_context.console,
    )

    with progress:
        task_id = progress.add_task("Tagging series descriptions", total=len(all_cards))
        for card_id, payload in all_cards:
            title = _resolve_card_title(payload)
            progress.update(task_id, description=f"Processing {title}")

            if not card_id:
                progress.advance(task_id)
                continue

            lookup = title_to_series.get(_normalize_title(title))
            if lookup is None:
                summary["skipped_no_series"] += 1
                progress.advance(task_id)
                continue

            series_name, sequence_number = lookup
            series_tag = f"Series: {series_name} | Book: {sequence_number}"

            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                payload["metadata"] = metadata

            full_description = metadata.get("description") or ""
            if series_tag in full_description:
                summary["already_tagged"] += 1
                progress.advance(task_id)
                continue

            summary["processed"] += 1
            result: dict[str, Any] = {
                "card_id": card_id,
                "title": title,
                "series": series_name,
                "sequence_number": sequence_number,
            }

            try:
                separator = "\n\n" if full_description else ""
                new_desc = f"{full_description}{separator}{series_tag}"
                from yoto_bulk.card_utils import MAX_DESCRIPTION_LENGTH
                metadata["description"] = new_desc[:MAX_DESCRIPTION_LENGTH]
                lib.save_card(card_id, payload)
                summary["updated"] += 1
                result["status"] = "updated_locally"
            except Exception as exc:
                logger.warning(
                    "Skipping series description tag because local save failed",
                    extra={"card_id": card_id, "title": title, "error": str(exc)},
                )
                summary["failed"] += 1
                result.update({"status": "save_failed", "error": str(exc)})

            summary["results"].append(result)
            progress.advance(task_id)

    return summary


@metadata_app.command("tag-series")
def metadata_tag_series(ctx: typer.Context) -> None:
    """Stamp 'Series: X | Book: N' into card descriptions using cached series suggestions."""
    app_context = get_context(ctx)
    summary = _tag_series_descriptions(app_context)
    app_context.console.print(summary)


@series_app.command("missing")
def series_missing(
    ctx: typer.Context,
    limit: Annotated[
        int | None,
        typer.Option(help="Optionally limit how many library books are sent to OpenAI. Defaults to all books."),
    ] = None,
) -> None:
    """Identify missing books in each detected series."""
    app_context = get_context(ctx)
    lib = _require_local_library(app_context)
    books = lib.as_yoto_books()
    selected_books = books if limit is None else books[:limit]
    try:
        suggestions = app_context.llm.suggest_series(selected_books)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    series_mapping = suggestions.get("series", {})
    if not series_mapping:
        app_context.console.print({"series": {}, "total_missing": 0})
        return

    # Only check series with 2+ owned titles to keep costs down
    filtered = {name: titles for name, titles in series_mapping.items() if isinstance(titles, list) and len(titles) >= 2}
    if not filtered:
        app_context.console.print({"series": {}, "total_missing": 0, "note": "No multi-book series detected."})
        return

    try:
        completion = app_context.llm.find_missing_in_series(filtered)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    total_missing = sum(
        len(s.get("missing", []))
        for s in completion.get("series", {}).values()
        if isinstance(s, dict)
    )
    app_context.console.print(
        {
            "model": app_context.settings.openai_model,
            "library_count": len(books),
            "series_checked": len(filtered),
            "total_missing": total_missing,
            "series": completion.get("series", {}),
        }
    )
