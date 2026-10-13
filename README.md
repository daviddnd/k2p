# K2P — execution plane (pre-release test build)

K2P is a single-tenant web application for Hong Kong company-secretarial work
(TCSP and in-house teams). This repository holds the **execution plane**: the
stack a TCSP runs on its own hardware — the backend, the frontend, and the
desktop launcher that installs and operates it. Client data stays in the
TCSP's own environment.

This is a **pre-release test build**, published so the download → install →
run flow can be exercised end to end. It is not yet licensed for production
use; source is visible for evaluation only.

## Layout

- `backend/` — the API service (Python/FastAPI): company master data,
  filing drafts (NAR1, ND2A), documents, audit trail. Ships with the
  Companies Registry form registry under `backend/forms/`.
- `frontend/` — the browser app (React, built to static files).
- `desktop/` — the desktop launcher: a double-click installer that starts
  the pinned service images and owns start / stop / backup / update.
- `deploy/` — Dockerfiles and the compose file the images are built from.

## Releases

Pushing a `desktop-v*` tag builds the `k2p-backend` / `k2p-frontend` images
and the desktop installer, and publishes a GitHub Release. The launcher
pulls the matching pinned images from GHCR on first start.

## Status

Early test builds. No security review of this repository has been performed
for third-party evaluation. Not legal advice; filings produced by the
software are reviewed and signed off by a human before submission.
