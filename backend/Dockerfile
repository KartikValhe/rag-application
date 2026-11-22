# syntax=docker/dockerfile:1
# Backend dev image. Dependencies come first so a code-only change reuses the cached layers.
# Every dependency installs from a wheel, so no compiler is needed.

# ---- builder: resolve pyproject.toml (including the dev extras) and install into a venv ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Only pyproject.toml is copied here, so this layer is rebuilt only when dependencies change.
COPY pyproject.toml /tmp/pyproject.toml

# Install the dependencies (base + dev extras) but not the project itself: the source is
# copied into /app below and run from there. Extras that point back at the project
# ("rag-platform-backend[parser]") are expanded to their own requirement lists.
RUN python - <<'PY' > /tmp/requirements.txt
import re
import tomllib

with open("/tmp/pyproject.toml", "rb") as f:
    project = tomllib.load(f)["project"]

extras = project["optional-dependencies"]
selected: list[str] = list(project["dependencies"])


def add_extra(name: str) -> None:
    for req in extras[name]:
        match = re.fullmatch(rf"{re.escape(project['name'])}\[(\w+)\]", req)
        if match:
            add_extra(match.group(1))
        else:
            selected.append(req)


add_extra("dev")
print("\n".join(dict.fromkeys(selected)))
PY
RUN pip install -r /tmp/requirements.txt


# ---- runtime: slim image with the venv and the source ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Source last. .dockerignore keeps .env files, models, document_store and tests out.
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
