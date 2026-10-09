# borrowbox-server

FastAPI service that serves the web UI at `http://localhost:8000/` and acts as the BorrowBox-facing edge of the system.

## Responsibilities

- expose BorrowBox catalog search and series APIs
- show current loans and history
- download BorrowBox audiobook zips
- import borrowed books into Audiobookshelf
- serve the frontend and inject the configured `yoto-server` URL

It is not the canonical library. Audiobookshelf is.

## Main flows

### Loans -> Add to ABS

1. Resolve or reuse a BorrowBox download URL
2. Download the ZIP server-side
3. Parse/import it into a temporary workspace
4. Enrich metadata from BorrowBox product data
5. Upload the book to Audiobookshelf
6. Sync the resulting ABS item into the Redis-backed Yoto cache

### Web UI

The root page serves static assets and injects:

- `window.YOTO_API_URL`

The browser then talks to:

- same-origin BorrowBox endpoints under `/api/auth` and `/api/catalog`
- `yoto-server` for library/ABS/Yoto operations

## Endpoints

### Catalog

- `GET /api/catalog/libraries`
- `GET /api/catalog/search/{library}`
- `GET /api/catalog/series/{library}/{series_id}`
- `GET /api/catalog/product/{library}/{product_id}`
- `GET /api/catalog/availability/{product_id}`

### Auth and loans

- `GET /api/auth/loans/{library}`
- `GET /api/auth/history/{library}`
- `GET /api/auth/credits/{library}`
- `GET /api/auth/download/{library}/{loan_id}`
- `POST /api/auth/import/{library}/{loan_id}`
- `POST /api/auth/borrow`
- `POST /api/auth/reserve`
- `POST /api/auth/return`
- `GET /api/jobs`
- `POST /api/jobs` starts a durable import (`library`, `loan_id`, optional `product_id` and `label`)
- `GET /api/jobs/{job_id}`

### Frontend Verification

From the repository root:

```sh
npm ci
npx playwright install chromium
npm run test:ui
```

The browser suite uses a separate static server and mocked API responses. It never borrows,
imports, edits, or pushes real library items. Both desktop and phone layouts are covered.

Shelf refresh reloads cached views; Audiobookshelf sync and Yoto pushes have separate
server-recorded timestamps. Imports and pushes continue when the browser is reloaded.
Task status is retained in Redis for 48 hours; interrupted service work requires an explicit retry.

## Configuration

Important env vars:

- `BORROWBOX_LIBRARIES`
- `BORROWBOX_CREDENTIALS`
- `BORROWBOX_REDIS_URL`
- `BORROWBOX_YOTO_API_URL`
- `YOTO_BULK_ABS_URL`
- `YOTO_BULK_ABS_TOKEN`
- `YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID`
- `YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET`
- `YOTO_BULK_ABS_LIBRARY_NAME`
- `BORROWBOX_API_KEY` if you want API-key protection for `/api/*`

## Notes

- This service still uses transient local import/parsing helpers for BorrowBox ZIP staging.
- That code path should stay staging-only; it is not a source-of-truth library.
