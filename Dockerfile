FROM docker.io/library/python:3.15.2-slim@sha256:c4158543ecb725b99e57d129dfd43d761d4c43e4577b5e23326aabb223cf7f4e

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /bin/uv

# cosign makes each app's signing key pair (OnboardBuild); same release + checksum as the apps' CI signs with.
ADD --checksum=sha256:424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa --chmod=755 \
    https://github.com/sigstore/cosign/releases/download/v3.1.4/cosign-linux-amd64 /usr/local/bin/cosign

WORKDIR /app
# Dependencies first, so a code change doesn't rebuild them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# The root filesystem is read-only in Meru, so no .pyc files; the numeric user matches nuwa-app's runAsUser.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
CMD [".venv/bin/shikigami-run"]
