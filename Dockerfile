FROM python:3.13-slim

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy workspace definition
COPY pyproject.toml uv.lock README.md ./
COPY src src
COPY packages/borrowbox/pyproject.toml packages/borrowbox/pyproject.toml
COPY packages/borrowbox/README.md packages/borrowbox/README.md
COPY packages/borrowbox-server/pyproject.toml packages/borrowbox-server/pyproject.toml
COPY packages/borrowbox-server/README.md packages/borrowbox-server/README.md
COPY packages/yoto-server/pyproject.toml packages/yoto-server/pyproject.toml
COPY packages/yoto-server/README.md packages/yoto-server/README.md

# Create package source dirs so uv can resolve
COPY packages/borrowbox/src packages/borrowbox/src
COPY packages/borrowbox-server/src packages/borrowbox-server/src
COPY packages/borrowbox-server/static packages/borrowbox-server/static
COPY packages/yoto-server/src packages/yoto-server/src

# Install dependencies
RUN uv sync --package borrowbox-server --no-dev --frozen

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["borrowbox-server"]
