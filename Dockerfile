FROM python:3.12-slim

WORKDIR /app

# System dependencies for weasyprint, native claude binary install, and ssh
# (used to deliver TaskNotes stubs to Mac vault over tailscale).
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    python3-cffi \
    curl \
    ca-certificates \
    openssh-client \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --uid 1000 appuser

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml uv.lock README.md ./

# Install dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code
COPY . .

# Install the project itself
RUN uv sync --frozen --no-dev

# Ensure writable directories exist with correct ownership
RUN mkdir -p reports media_cache logs && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Native claude binary — installs to /home/appuser/.local/share/claude with
# symlink at /home/appuser/.local/bin/claude. Auth via CLAUDE_CODE_OAUTH_TOKEN
# at runtime (set in docker-compose .env).
RUN curl -fsSL https://claude.ai/install.sh | bash

# SSH config alias — lets us `ssh adams-mac-studio` despite no MagicDNS resolver
# on the NAS host. The IP is the Mac Studio's static tailscale address.
RUN mkdir -p /home/appuser/.ssh && chmod 700 /home/appuser/.ssh \
    && printf '%s\n' \
        'Host adams-mac-studio' \
        '  HostName 100.108.60.81' \
        '  User adam' \
        '  IdentityFile /home/appuser/.ssh/id_ed25519' \
        '  IdentitiesOnly yes' \
        '  StrictHostKeyChecking accept-new' \
        '  UserKnownHostsFile /home/appuser/.ssh/known_hosts' \
        > /home/appuser/.ssh/config \
    && chmod 600 /home/appuser/.ssh/config

ENV PYTHONUNBUFFERED=1
ENV PATH="/home/appuser/.local/bin:/app/.venv/bin:$PATH"

# Default: run a scan. Override in docker-compose for other commands.
CMD ["course-scout", "scan"]
