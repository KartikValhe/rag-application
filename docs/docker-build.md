# Backend Docker build

- Build context is `backend/` (see `docker-compose.yml`). `backend/.dockerignore` keeps it at about 15 kB. Without it the context was about 490 MB (`models/`, `.venv/`, `.mypy_cache/`).
- `.env` files, `models/`, `document_store/`, `uploads/` and `tests/` are never copied into the image. Models and documents stay on the host or on volumes. In dev the compose file bind-mounts `./backend` over `/app`.
- `backend/Dockerfile` is multi-stage:
  1. builder: reads `pyproject.toml` (base + `dev` extras), installs the dependencies into `/opt/venv`. Only `pyproject.toml` is copied, so this layer is cached until dependencies change.
  2. runtime: `python:3.12-slim` + the venv + the source. No compiler and no curl (all dependencies are wheels; the API has no container healthcheck).
- A code-only change rebuilds only the final `COPY . .` layer.
- `docker compose up -d postgres redis` does not build anything; only the `api` service has a `build:` section.
- If a new dependency has no wheel for Python 3.12 and needs a compiler, install `build-essential` in the builder stage only.
- Build only the API image: `docker compose build api`. The Dockerfile is not used by CI.
