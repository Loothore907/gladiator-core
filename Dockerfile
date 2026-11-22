# Gladiator v2: Linux container for benchmark / hosted environments.
#   docker build -t gladiator .
#   docker run --rm --env-file .env -v "$PWD/runs:/app/runs" gladiator \
#       match --suite suites/six_case_mvp.yaml --a openai:gpt-6-luna --b anthropic:claude-haiku-5-5 --max-usd 1
#
# BASE can be swapped for an organisation's approved image (e.g. one that trusts a
# corporate proxy certificate):  docker build --build-arg BASE=registry.example/python:3.12 .
ARG BASE=python:3.12-slim
FROM ${BASE}

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first (cached layer), exactly as pinned in uv.lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
    && chmod -R a+rX /app \
    && useradd --create-home gladiator && mkdir -p runs && chown gladiator runs
USER gladiator

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["gladiator"]
CMD ["--help"]
