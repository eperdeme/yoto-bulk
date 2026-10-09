# yoto-server

FastAPI service for the ABS-backed library cache and Yoto sync workflows.

Default local endpoint:

- `http://localhost:8001/`

## Responsibilities

- sync Audiobookshelf books and downloaded podcast episodes into Redis for the UI
- expose card list/detail endpoints for the frontend
- proxy ABS cover art for browser-safe rendering
- edit ABS-backed metadata
- push selected books to Yoto
- keep Yoto-only pulls from overwriting ABS-backed cards

## Key model

Redis is a working cache for the UI. It is not the source of truth.

Important metadata fields on cached cards:

- `source`
  - `abs` for cards sourced from Audiobookshelf
  - `yoto` for cards sourced from Yoto pulls
- `external_id`
  - ABS item id for `source=abs`
- `pull_hash`
  - content hash used to detect local modifications

Preserving that metadata is critical for correct sync behavior.

## Main endpoints

- `GET /api/yoto/cards`
- `GET /api/yoto/cards/{card_id}`
- `PATCH /api/yoto/cards/{card_id}/metadata`
- `GET /api/yoto/cards/{card_id}/cover`
- `POST /api/yoto/cards/{card_id}/push`
- `POST /api/yoto/migrate`
- `POST /api/yoto/pull`
- `POST /api/yoto/cards/{card_id}/import-to-abs`
- `POST /api/yoto/series/discover`
- `POST /api/yoto/series/discover/manual`
- `GET /api/yoto/sync/status`
- `GET /api/yoto/jobs`
- `POST /api/yoto/jobs` starts a durable workflow (`action`: `push`, `sync`, `pull`, or `adopt`)
- `GET /api/yoto/jobs/{job_id}`

## Configuration

Important env vars:

- `YOTO_BULK_REDIS_URL`
- `YOTO_BULK_ABS_URL`
- `YOTO_BULK_ABS_TOKEN`
- `YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID`
- `YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET`
- `YOTO_BULK_ABS_LIBRARY_NAME`
- `YOTO_BULK_YOTO_CLIENT_ID`
- `YOTO_BULK_YOTO_CLIENT_SECRET`
- `YOTO_BULK_OPENAI_API_KEY`
- `YOTO_BULK_OPENAI_MODEL` (optional; default `gpt-5.4`)

## Notes

- `POST /api/yoto/migrate` now means “sync from Audiobookshelf”, not “import local library from disk”.
- Podcast libraries are discovered automatically. Each podcast show becomes one card, with every downloaded episode represented as a chapter in Audiobookshelf order.
- Podcast updates are manual: sync from Audiobookshelf, then push cards marked `Needs push` to update Yoto. Episodes that are present only in the remote feed are not included until Audiobookshelf downloads them.
- ABS-backed cards should stay ABS-backed even after edits, pushes, or pull-hash updates.
- Redis is a cache for the UI and push workflows; it should not be described as the canonical library state.
- Workflow status is user-scoped in Redis for 48 hours. Browser reloads recover progress;
  interrupted service work is reported as failed and can be retried explicitly.
- Series metadata searches are partial catalogues, not proof of completeness.
- Audiobookshelf TLS verification stays enabled. Renew an expired upstream certificate at
  its server or reverse proxy; already-uploaded Yoto artwork can provide a cover fallback.
- Series discovery is ABS-first. When OpenAI is unavailable or the automatic LLM step fails, `POST /api/yoto/series/discover` returns `manual_prompt` — a self-contained prompt for the ungrouped residue that can be run in any external LLM tool. The resulting JSON is posted back to `POST /api/yoto/series/discover/manual`, which maps it onto card ids (no server-side LLM call needed).
