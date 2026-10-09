# AGENTS

This service sits between Audiobookshelf, Redis, and Yoto.

## Rules

1. Audiobookshelf is upstream authority for ABS-backed cards.
2. Redis is a cache and working copy, not a canonical library.
3. Never drop provenance fields like `source` or `external_id` accidentally.
4. Yoto pulls must not overwrite ABS-backed cards.
5. Browser-safe cover access should stay proxied through this service.

## Refactor bias

Prefer extracting route concerns by domain:

- card listing/detail/edit
- ABS sync/import
- Yoto push/pull
- admin or AI-assisted maintenance

Keep ABS integration logic in shared helpers where practical, not duplicated across routes.
