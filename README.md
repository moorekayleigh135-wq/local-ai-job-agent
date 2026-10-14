# Local AI Job Agent

A self-hosted job-search assistant designed to discover, organize, compare, and track opportunities while keeping the application and its data under your control.

The initial stack targets **Linux Mint**, **Docker Engine**, an **NVIDIA RTX 4080 / 4080 SUPER (16 GB VRAM)**, **Ollama**, and **MySQL 8.4**. The dashboard is intended to be accessible privately through Tailscale rather than exposed to the public internet.

> **Status:** early development. The repository currently provides a container foundation and basic API health checks. Job-source connectors, matching logic, scheduling, and dashboard UI are not implemented yet.

## Goals

- Collect and normalize job postings from supported sources.
- Search company career pages and, where permitted, LinkedIn and Handshake.
- Match postings against configurable skills, interests, location, and role preferences.
- Save opportunities, notes, and application status in MySQL.
- Use a local Ollama model to summarize descriptions and explain why a role may fit.
- Provide a private dashboard for reviewing results.

## Principles

1. **Local-first inference:** use Ollama on the home server.
2. **Private by default:** MySQL has no published host port; the API and Ollama ports bind to loopback for initial development.
3. **Separate from the existing homelab:** this has its own Compose project name, network, and named volumes.
4. **Respect source rules:** prefer official career-page feeds and APIs where available. Any LinkedIn or Handshake integration must respect terms, authentication requirements, robots directives, and rate limits. Do not bypass access controls.
5. **Secrets stay local:** credentials belong in `.env`, which Git ignores. Commit `.env.example` with placeholders only.
6. **Human review:** AI-generated summaries and fit scores are suggestions, not guarantees.

## Architecture

```text
Local browser
    | 127.0.0.1:8000
    v
FastAPI API --------> Ollama (local inference)
    |
    v
MySQL 8.4

Private remote dashboard access via Tailscale is a later milestone.
```

Services:
- `api`: FastAPI application in `backend/`.
- `db`: MySQL 8.4 with a persistent named volume; no host port mapping.
- `ollama`: local model server with persistent model storage; host port bound to `127.0.0.1`.

The API needs outbound internet access for future job-source collection. Therefore, the Compose network is a normal project-specific bridge network, not an isolated `internal: true` network. The services are not intentionally published on the LAN.

## Requirements

- Linux Mint (or another Linux distribution) with Docker Engine and Docker Compose plugin.
- Git.
- Working NVIDIA host driver and NVIDIA Container Toolkit for GPU access inside Docker.
- Disk space for images, database, and model weights.

Check the host first:

```bash
nvidia-smi
docker --version
docker compose version
```

If `nvidia-smi` is unavailable, resolve the host driver before troubleshooting Docker GPU access. Do not change existing homelab stacks as part of this setup.

## Quick start

### 1. Configure local secrets

```bash
cp .env.example .env
nano .env
```

Replace both password placeholders with long, unique values. Never commit `.env`.

### 2. Validate and start

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

### 3. Pull the model

```bash
docker compose exec ollama ollama pull qwen3:14b
```

The initial download may take time and several gigabytes of disk space. You can choose another model by changing `OLLAMA_MODEL` in `.env`, then recreating the API container:

```bash
docker compose up -d --build api
```

A 14B model may be a useful starting point for a 16 GB GPU, but VRAM usage depends on quantization, context length, and concurrent workloads. Verify actual utilization on your machine.

### 4. Check the API

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/db
curl http://127.0.0.1:8000/health/ollama
```

Interactive API docs: <http://127.0.0.1:8000/docs>

## Useful commands

```bash
# Logs and status
docker compose ps
docker compose logs --tail=100 api
docker compose logs -f db

# Stop without deleting data
docker compose down

# Restart
docker compose up -d

# Check what Git will ignore
git status --short
git check-ignore .env
```

**Warning:** `docker compose down -v` deletes the named database and model volumes. Do not use `-v` unless you intend to delete that data.

## Security notes

- Never commit `.env`, credentials, browser cookies, private job-search history, or database dumps.
- Do not expose MySQL, the API, or Ollama directly to the public internet.
- Current port bindings are for local development. Before remote access, configure Tailscale deliberately and review firewall rules.
- Store only the minimum data needed for job tracking. Avoid real personal data in fixtures and screenshots.
- Follow each source's terms and access rules.

## Roadmap

- [x] Public-repository-safe starter files
- [x] Docker Compose services for MySQL, Ollama, and FastAPI
- [x] Basic API liveness and dependency health endpoints
- [ ] Verify GPU access from the Ollama container
- [ ] Add SQLAlchemy models and schema migrations
- [ ] Define company, job posting, match, and application entities
- [ ] Build first compliant company-career-page connector
- [ ] Add deduplication and scheduled collection
- [ ] Add local-model summarization and explainable matching
- [ ] Build dashboard
- [ ] Configure private Tailscale access and backups
- [ ] Evaluate compliant LinkedIn and Handshake workflows
