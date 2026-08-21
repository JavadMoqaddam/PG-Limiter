.PHONY: build run stop logs shell clean dev test install

# Docker image name
IMAGE_NAME = ghcr.io/javadmoqaddam/pg-limiter
CONTAINER_NAME = pg-limiter

# Install pg-limiter (recommended way)
install:
	@echo "Installing PG-Limiter..."
	@chmod +x pg-limiter.sh
	@sudo ./pg-limiter.sh install

# Build the Docker image locally
build:
	docker build -t $(IMAGE_NAME):latest .

# Build with no cache
build-fresh:
	docker build --no-cache -t $(IMAGE_NAME):latest .

# Run the container
run:
	@./pg-limiter.sh start 2>/dev/null || docker compose up -d

# Stop the container
stop:
	@./pg-limiter.sh stop 2>/dev/null || docker compose down

# View logs
logs:
	@./pg-limiter.sh logs 2>/dev/null || docker compose logs -f

# View last 100 lines of logs
logs-tail:
	docker compose logs --tail=100 -f

# Open a shell in the running container
shell:
	docker exec -it $(CONTAINER_NAME) /bin/bash

# Run CLI command inside container
cli:
	docker exec -it $(CONTAINER_NAME) python cli_main.py $(CMD)

# Clean up containers and images
clean:
	docker compose down -v 2>/dev/null || true
	docker rmi $(IMAGE_NAME):latest 2>/dev/null || true

# Restart the container
restart:
	@./pg-limiter.sh restart 2>/dev/null || docker compose restart

# Update to latest version
update:
	@./pg-limiter.sh update 2>/dev/null || (docker pull $(IMAGE_NAME):latest && docker compose up -d)

# Development: run locally without Docker
dev:
	python limiter.py

# Run tests
test:
	python -m pytest tests/ -v

# Show running status
status:
	docker-compose ps

# Backup (use pg-limiter script)
backup:
	mkdir -p backups
	cd /etc/opt/pg-limiter && zip -r backups/pg-limiter-backup-$$(date +%Y%m%d_%H%M%S).zip . /var/lib/pg-limiter/
	@echo "Backup created in backups/"

# Help
help:
	@echo "PG-Limiter Docker Commands:"
	@echo "  make build        - Build Docker image"
	@echo "  make build-fresh  - Build without cache"
	@echo "  make run          - Start container (docker-compose up -d)"
	@echo "  make stop         - Stop container"
	@echo "  make restart      - Restart container"
	@echo "  make logs         - Follow container logs"
	@echo "  make logs-tail    - Show last 100 lines + follow"
	@echo "  make shell        - Open bash shell in container"
	@echo "  make cli CMD=xxx  - Run CLI command"
	@echo "  make status       - Show container status"
	@echo "  make clean        - Remove container and image"
	@echo "  make update       - Pull latest, rebuild, restart"
	@echo "  make backup       - Backup config and data"
	@echo "  make dev          - Run locally (no Docker)"
	@echo "  make test         - Run tests"
