ARG PYTHON_VERSION=3.14.7

FROM python:$PYTHON_VERSION-slim-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:$PYTHON_VERSION-slim-bookworm

# Install zip for backup/restore functionality
RUN apt-get update && apt-get install -y --no-install-recommends \
    zip \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code
COPY . .

# Create data directory for persistent storage
RUN mkdir -p /var/lib/pg-limiter/data /var/lib/pg-limiter/logs

# Make scripts executable
RUN chmod +x /app/start.sh

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV DATABASE_URL=sqlite+aiosqlite:////var/lib/pg-limiter/data/pg_limiter.db

# Labels
LABEL org.opencontainers.image.source="https://github.com/JavadMoqaddam/PG-Limiter"
LABEL org.opencontainers.image.description="IP Limiter for PasarGuard Panel"
LABEL maintainer="JavadMoqaddam"

ENTRYPOINT ["/app/start.sh"]
