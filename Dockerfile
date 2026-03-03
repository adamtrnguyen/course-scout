FROM python:3.12-slim
 
WORKDIR /app
 
# Install system dependencies for weasyprint and other PDF tools
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    python3-cffi \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
 
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
 
# Ensure reports and media_cache directories exist
RUN mkdir -p reports media_cache
 
# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
 
# Command to run the worker
CMD ["python", "-m", "telebot.application.worker"]
