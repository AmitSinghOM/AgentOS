# AgentOS image (Phase 8 #10). Built by .github/workflows/publish.yml from the wheels the SAME
# run built and attested — never from PyPI — so the image and the published wheel are the same
# bytes. Base pinned by digest; runs as a non-root user; no build tools in the final stage.
#
#   docker run --rm ghcr.io/amitsinghom/agentos agentos --help
#   docker run --rm -p 127.0.0.1:8000:8000 -e AGENTOS_STORE=postgres -e AGENTOS_PG_DSN=... ghcr.io/amitsinghom/agentos
#   docker run --rm -e AGENTOS_STORE=postgres -e AGENTOS_PG_DSN=... ghcr.io/amitsinghom/agentos python -m dagentos.worker
#
# Local build: `python -m build` (core) and each provider into ./dist first, then
#   docker build -t agentos:local .

# python:3.12-slim — digest resolved 2026-09-20 from Docker Hub (multi-arch index)
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
WORKDIR /wheels
COPY dist/ ./dist/
COPY deploy/constraints.txt ./constraints.txt
RUN python -m venv /opt/agentos \
 # every third-party distribution in the image (and pip itself) comes from deploy/constraints.txt:
 # one exact version each, verified against the sha256 of the file PyPI serves; anything not on
 # that list is refused (audit I-21/E-20; regenerate with scripts/refresh_constraints.py)
 && /opt/agentos/bin/pip install --no-cache-dir --require-hashes -r ./constraints.txt \
 # our own packages by wheel PATH (never resolved from an index); --no-deps because every
 # dependency is already installed from the constraints above, so nothing else can slip in
 && /opt/agentos/bin/pip install --no-cache-dir --no-deps \
      "$(ls ./dist/dagentos-*.whl)[providerkit,observability]" \
      ./dist/agentos_provider_openai_compat-*.whl ./dist/agentos_provider_anthropic-*.whl \
      ./dist/agentos_provider_openai_agents-*.whl ./dist/agentos_provider_pydantic_ai-*.whl \
      ./dist/agentos_provider_acp-*.whl ./dist/agentos_provider_mcp-*.whl \
 && /opt/agentos/bin/pip check \
 && /opt/agentos/bin/agentos --help > /dev/null

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
LABEL org.opencontainers.image.source="https://github.com/AmitSinghOM/AgentOS" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="AgentOS: durable, observable, human-in-the-loop LLM agent workflows"
RUN useradd --system --uid 10001 --create-home --home-dir /var/lib/agentos agentos
COPY --from=build /opt/agentos /opt/agentos
ENV PATH="/opt/agentos/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AGENTOS_STORE=sqlite \
    AGENTOS_SQLITE_PATH=/var/lib/agentos/agentos.db
USER agentos
WORKDIR /var/lib/agentos
VOLUME ["/var/lib/agentos"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" || exit 1
CMD ["uvicorn", "dagentos.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
