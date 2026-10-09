# AGENTS

This repo is no longer primarily a local-library CLI project.

When working here, optimize for the current runtime model:

- BorrowBox acquires books
- Audiobookshelf stores books and canonical metadata
- Yoto is a sync target

## Architectural rules

1. Treat Audiobookshelf as the source of truth for library metadata.
2. Do not add new runtime features that read the old local filesystem library as authoritative state.
3. If local-library code is used, keep it transient and explicit: parsing, import staging, or migration only.
4. Prefer service APIs and shared helpers over duplicating logic in the frontend.
5. Preserve `source` and `external_id` semantics for Redis-cached cards; they are required to distinguish ABS-backed cards from Yoto-backed cards.

## Service boundaries

### `packages/borrowbox-server`

Owns:

- BorrowBox library browsing
- loan and history views
- BorrowBox import into ABS
- frontend HTML/CSS/JS

Should not:

- become the canonical store for books
- read filesystem-backed Yoto library manifests as live library state

### `packages/yoto-server`

Owns:

- Redis-backed cache of library cards for the UI
- ABS sync into cache
- ABS-backed metadata edits
- Yoto push workflows

Should not:

- treat Yoto pulls as higher authority than ABS-backed cards
- silently drop ABS provenance metadata

### `src/yoto_bulk`

Owns shared domain logic:

- ABS API helpers
- payload normalization
- Redis store helpers
- legacy local import/parser code used by CLI or migration paths

## Cleanup bias

Prefer:

- explicit provenance (`source`, `external_id`, timestamps)
- narrow helpers for ABS/Yoto/BorrowBox boundaries
- package-level docs that match the current microservice architecture

Avoid:

- “migrate” code paths that secretly mean “read local library from disk”
- UI labels that imply Redis or local cache is the source of truth
- broad single-file route modules when a concern has stabilized enough to extract
