# yoto-bulk

Audiobook workflow tooling built around three systems:

- BorrowBox for discovery, borrowing, and download
- Audiobookshelf for canonical storage and metadata
- Yoto for downstream listening on MYO cards

## Current architecture

### `borrowbox-server`

Runs the Web UI at `http://localhost:8000/` and handles:

- BorrowBox catalog search
- loan and history views
- BorrowBox download/import into Audiobookshelf
- the frontend shell that talks to `yoto-server`

### `yoto-server`

Runs on `http://localhost:8001/` and handles:

- Redis-backed library cache for the UI
- syncing Audiobookshelf items into that cache
- editing ABS-backed metadata
- pushing selected books to Yoto
- cover proxying for ABS-backed items

### `borrowbox`

Shared Python client for BorrowBox catalog and authenticated loan/download flows.

### `src/yoto_bulk`

Shared domain logic used by the web/API services and staging scripts, including:

- Audiobookshelf client helpers
- card metadata helpers
- Redis store logic
- transient local import/parsing code used for staging and migration paths

## Source of truth

The intended ownership model is:

1. BorrowBox is the acquisition source.
2. Audiobookshelf is the canonical library and metadata authority.
3. Yoto is a publish target.

That means:

- book metadata should end up correct in ABS
- UI library views should be based on ABS-backed data
- Yoto sync should read from ABS-backed cached cards
- transient local import/parsing code should not be treated as authoritative runtime state

## Services

### Run locally

```bash
uv sync
docker compose up --build
```

Expected local endpoints:

- `http://localhost:8000/` for the web UI and BorrowBox APIs
- `http://localhost:8001/health` for `yoto-server`

### Deploy into the media-mgmt stack

The server integration in [docker-compose.media-mgmt.yml](docker-compose.media-mgmt.yml)
targets `192.168.1.6` and the existing `traefik_proxy` Docker network. Clone this
repository into `/home/eperdeme/media-mgmt/yoto-bulk`, then add this top-level
entry to `/home/eperdeme/media-mgmt/docker-compose.yaml`:

```yaml
include:
  - ./yoto-bulk/docker-compose.media-mgmt.yml
```

Persistent state lives outside the checkout in `docker/yoto-bulk/`:

- `.env`: private settings, including the original `BORROWBOX_SECRET_KEY` and ABS token
- `redis/`: Redis snapshot and append-only persistence
- `yoto-data/`: Yoto `tokens.json` and transient cache
- `zip-archive/`: new download archives (existing archives need not be migrated)

Create these directories and install `.env` and `tokens.json` with restrictive
permissions before starting. Never commit settings, tokens, or database snapshots.
Preserve the credential encryption key when restoring Redis, or saved BorrowBox
credentials cannot be decrypted.

From `/home/eperdeme/media-mgmt`:

```bash
docker compose config --quiet
docker compose up -d --build --wait yoto-redis borrowbox-server yoto-server
docker compose ps yoto-redis borrowbox-server yoto-server
```

Only these services need to be started or rebuilt; do not restart the rest of
the media stack. The UI is at `http://192.168.1.6:8000/` and its browser-facing
Yoto API is at `http://192.168.1.6:8001`. These are LAN-only HTTP endpoints,
not public Traefik routes. Redis has no published port. Both applications
reach the existing ABS service directly at `http://audiobookshelf:80`, using
the configured ABS token without Cloudflare Access headers.

For a local-to-server cutover, stop both local application containers before
taking a final Redis `SAVE` snapshot and copying `tokens.json`. Back up the
snapshot, settings, tokens, and existing server Compose file first. Restore
`dump.rdb` into an empty server `redis/` directory **before Redis first starts**;
an existing append-only database takes precedence over a restored snapshot.
Verify snapshot checksums, cached card provenance (`source`, `external_id`),
credential decryption, service health, and ABS connectivity before using the
new UI. Keep the local writers stopped after cutover. No local-library import
or destructive ABS migration is needed: ABS remains the source of truth.

## Configuration

Configuration is loaded from `.env`.

Important variables:

- `BORROWBOX_LIBRARIES`
- `BORROWBOX_CREDENTIALS`
- `BORROWBOX_YOTO_API_URL`
- `YOTO_BULK_ABS_URL`
- `YOTO_BULK_ABS_TOKEN`
- `YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID` for a Cloudflare Access service token
- `YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET` for a Cloudflare Access service token
- `YOTO_BULK_ABS_LIBRARY_NAME`
- `YOTO_BULK_YOTO_CLIENT_ID`
- `YOTO_BULK_YOTO_CLIENT_SECRET`
- `YOTO_BULK_OPENAI_API_KEY` for series discovery features

## Repo layout

```text
packages/
  borrowbox/          BorrowBox client library
  borrowbox-server/   Web UI + BorrowBox service
  yoto-server/        Yoto/ABS sync service
src/yoto_bulk/        Shared domain logic and staging helpers
```

## Development

```bash
uv sync --all-extras
python3 -m py_compile src/yoto_bulk/*.py packages/yoto-server/src/yoto_server/*.py packages/borrowbox-server/src/borrowbox_server/*.py
```

## Notes

- ABS metadata should be preferred over embedded source tags when they disagree.
- Quick Match in ABS should be enrichment-only; it should not overwrite curated title/series/order.
- Local import/parsing code remains staging-only; new product work should avoid reintroducing it as a source of truth.
- `borrowbox-server` and `yoto-server` share ABS config via the `YOTO_BULK_ABS_*` variables.
