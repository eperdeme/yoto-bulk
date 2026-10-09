# AGENTS

Work in this package as if Audiobookshelf already owns the library.

## Keep this package focused on

- BorrowBox discovery and loan workflows
- server-side BorrowBox ZIP download/import
- frontend delivery
- orchestration into ABS and `yoto-server`

## Avoid

- reintroducing filesystem-backed card listings
- storing canonical book metadata here
- duplicating ABS business logic that already belongs in shared helpers or `yoto-server`

## Frontend guidance

- The main Library view should read as ABS-first.
- Advanced maintenance actions like cache nuking or Yoto pulls should not dominate the default UI.
- If browser code needs ABS data, prefer going through `yoto-server` or a backend proxy rather than exposing ABS credentials client-side.
