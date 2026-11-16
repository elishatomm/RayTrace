# Production image: the API and the built UI in public mode (docs/DEMO_HOSTING.md, section 3.3, item 10).
# Dockerfile.test, the test image, is separate. Every base image is pinned by multi-arch index digest; bump a tag and
# its digest together. The UI stage runs `npm ci`, so building needs the network once; the image holds no secret.
#
#   docker build -t <tag> .
#   docker run -e WORKBENCH_PUBLIC_ORIGIN=https://<name>.<host> -e WORKBENCH_DEMO_TOKEN -p 127.0.0.1:8000:8000 <tag>
#
# That publishes the port on this machine's loopback only, for a tunnel to reach; a host routes to the container itself.
#
# The container refuses to start without public mode: it listens on 0.0.0.0, which only public mode allows, and
# public mode refuses to start without its origin and a strong demo token (both from the host's settings, never here).

FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

# The UI, built as README section 5 builds it: `npm run build` type-checks, then bundles into dist/.
FROM node:24.21.0-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS ui
WORKDIR /opt/ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/recovery-workbench

# The locked dependencies without the dev group first, so a source change reuses this layer. uv is mounted for the
# build only: the image does not need its 50 MB at run time.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project

# What the server reads at run time: its source, and the fixtures the simulator finds relative to src/. The project
# stays an editable install for that reason. README.md is the project's readme, which the build backend reads. uv leaves
# its lock file in .venv writable by everyone (0666), and nothing reads it at run time, so it goes with the last sync.
COPY README.md ./
COPY src/ src/
COPY fixtures/ fixtures/
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev \
 && rm -f .venv/.lock \
 && python -m compileall -q src

# The demo runs (Q48). Limited to 0.1 CPU, a free host's share, seeding one took up to 68 s, and public mode empties
# /data at every start, so they are seeded here, once: every demo scenario with each seed in WORKBENCH_PUBLIC_SEEDS,
# which also becomes the image's published list. Root owns the template and UID 1000 can only read it; public mode copies it into the
# emptied /data at every start (src/recovery_workbench/public_template.py says what is copied and why).
ARG WORKBENCH_PUBLIC_SEEDS=101
ENV WORKBENCH_PUBLIC_SEEDS=${WORKBENCH_PUBLIC_SEEDS} \
    WORKBENCH_PUBLIC_TEMPLATE_DIR=/opt/recovery-workbench/demo-runs
RUN .venv/bin/python -m recovery_workbench.public_template "$WORKBENCH_PUBLIC_TEMPLATE_DIR" \
 && chmod -R u=rwX,go=rX "$WORKBENCH_PUBLIC_TEMPLATE_DIR"
COPY --from=ui /opt/ui/dist frontend/dist

# UID 1000, as Hugging Face expects. It owns only the data directory, so everything else is read-only to it.
RUN useradd --uid 1000 --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin workbench \
 && install -d -o workbench -g workbench -m 0700 /data
USER 1000:1000

# auto (Q46): the local model if one answers on loopback, which none does in this image; else the hosted model when
# its settings are set; else the rules baseline. The deploy can still set rules, hosted or local explicitly.
ENV PATH=/opt/recovery-workbench/.venv/bin:$PATH \
    WORKBENCH_DATA_DIR=/data \
    WORKBENCH_MODEL_MODE=auto

EXPOSE 8000
ENTRYPOINT ["python", "-m", "recovery_workbench.api", "--host", "0.0.0.0"]
