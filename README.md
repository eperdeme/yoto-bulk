# yoto-bulk

CLI for bulk-managing a [Yoto](https://yotoplay.com) MYO (Make Your Own) card library — pull, edit, import, enrich, and push audiobook cards from the command line.

## Features

- **OAuth authentication** — headless PKCE login flow with automatic browser callback, token refresh, and secure local storage.
- **Library sync** — pull your full Yoto library (card metadata, audio, covers, icons) to a human-readable local folder structure; push changes back.
- **Audiobook import** — import Yoto export zips or raw publisher audio zips (MP3/M4A) with automatic ID3 tag parsing, chapter detection, and cover art extraction.
- **Series detection** — OpenAI-powered grouping of books into their real-world series, with automatic family library group creation on Yoto.
- **Metadata enrichment** — LLM-assisted author guessing for cards missing author info; numbered chapter icon stamping.
- **Cover recovery** — extract embedded cover art from audio tracks and upload to Yoto.
- **Reconciliation** — compare a local folder of audiobook zips against your Yoto library to find what's missing.
- **Dry-run by default** — all mutating operations preview changes unless `--apply` is passed.

## Quick start

```bash
cp .env.example .env          # fill in credentials
uv sync                        # install dependencies
uv run yoto-bulk --help        # see all commands
```

## Configuration

All settings are read from environment variables (or `.env`). See `.env.example`:

| Variable | Required | Description |
|---|---|---|
| `YOTO_BULK_YOTO_CLIENT_ID` | Yes | OAuth client ID for the Yoto API |
| `YOTO_BULK_OPENAI_API_KEY` | For LLM features | OpenAI API key for series/author enrichment |
| `YOTO_BULK_AUDIOBOOK_DIR` | For reconcile | Path to a local folder of audiobook zip files |
| `YOTO_BULK_YOTO_API_AUDIENCE` | No | Defaults to `https://api.yotoplay.com` |

## Commands

### Authentication

```
yoto-bulk auth login       # start OAuth login flow (opens browser with --open-browser)
yoto-bulk auth exchange     # manually exchange an OAuth code for tokens
yoto-bulk auth refresh      # refresh the saved access token
yoto-bulk auth status       # show current token state
```

### Library

```
yoto-bulk library pull      # download all cards, audio, covers, and icons
yoto-bulk library push      # push locally modified or imported cards back to Yoto
yoto-bulk library status    # show which local cards have been modified or imported
yoto-bulk library list      # fetch and print a sample of books from Yoto
yoto-bulk library import    # import audiobook zips into the local library
yoto-bulk library enrich    # add missing numbered chapter icons to all cards
yoto-bulk library add-cover # add a cover image to a specific card
yoto-bulk library recover-covers  # extract cover art from audio ID3 tags and upload
yoto-bulk library fix-imports     # deduplicate and clean up imported cards
yoto-bulk library migrate         # migrate from old flat layout to human-readable layout
```

### Series

```
yoto-bulk series suggest    # use OpenAI to detect series and create family groups
yoto-bulk series missing    # identify missing books in each detected series
```

### Metadata

```
yoto-bulk metadata enrich      # guess and fill missing authors via OpenAI
yoto-bulk metadata tag-series  # stamp "Series: X | Book: N" into card descriptions
```

### Reconcile

```
yoto-bulk reconcile scan       # scan local audiobook folder for zip files
yoto-bulk reconcile compare    # compare local zips against Yoto library titles
```

### Utilities

```
yoto-bulk version    # show version
yoto-bulk doctor     # show configuration and library health
```

## Local library layout

After `library pull`, cards are stored in a human-readable folder structure:

```
~/.config/yoto-bulk/library/
  Author Name/
    Book Title [cardId]/
      card.json
      cover.jpg
      audio/
        01 - Chapter 1.m4a
      icons/
        01 - Chapter 1.png
    Series Name/
      01 - Book Title [cardId]/
        ...
```

## Development

```bash
uv sync --all-extras          # install dev dependencies
uv run ruff check src/        # lint
uv run pytest                 # test
```
