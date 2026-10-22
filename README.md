# JobHarbor — Local AI Job Search

A self-hosted AI job-search assistant designed to discover, organize, compare, and track opportunities while keeping the application and its data under your control. **JobHarbor** is the working product name; the Compose service and volume identifiers remain stable for existing installs.

The stack targets **Linux Mint**, **Docker Engine**, an **NVIDIA RTX 4080 / 4080 SUPER (16 GB VRAM)**, **Ollama**, and **MySQL 8.4**. The dashboard and Ollama API bind to loopback by default, so they are available on the Mint host and are not published to the LAN.

> **Status:** personal-project MVP. The repository includes a private Docker stack, MySQL-backed job storage, local Ollama extraction, a resume-based career profile, a dashboard, user-saved search preferences, and user-started scans of public company career pages. Scheduled collection and application tracking remain future work.

## Goals

- Scan user-selected public company career pages for matching postings.
- Search company career pages and, where permitted, LinkedIn and Handshake.
- Rank discovered postings against configurable role, skill, location, and work-style preferences.
- Build a structured career profile from an uploaded PDF, DOCX, or TXT resume using the local Ollama model.
- Save manually captured and scanned job descriptions in MySQL; use local AI to extract details from manually pasted postings.
- Use a local Ollama model to summarize descriptions and explain why a role may fit.
- Provide a private dashboard for capturing and reviewing results.

## Principles

1. **Local-first inference:** use Ollama on the home server.
2. **Private by default:** MySQL has no published host port; the dashboard/API and Ollama ports bind to loopback.
3. **Separate from the existing homelab:** this has its own Compose project name, network, and named volumes.
4. **Respect source rules:** use user-selected public career pages, follow `robots.txt`, and limit requests. Prefer official feeds and APIs where available. Any LinkedIn or Handshake integration must respect terms, authentication requirements, and rate limits. Do not bypass access controls.
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

The API needs outbound internet access for company career-page scans. Therefore, the Compose network is a normal project-specific bridge network, not an isolated `internal: true` network. The services are not intentionally published on the LAN.

## Requirements

- Linux Mint with Docker Engine and the Docker Compose plugin. Docker documents that Ubuntu derivative distributions such as Mint are not officially supported, although the Ubuntu packages may work; use the Ubuntu base codename shown in `/etc/os-release` when following the [Docker Engine install guide](https://docs.docker.com/engine/install/ubuntu/).
- Git.
- Working NVIDIA host driver and NVIDIA Container Toolkit for GPU access inside Docker.
- Disk space for images, database, and model weights.

Check the host first:

```bash
nvidia-smi
docker --version
docker compose version
```

If `nvidia-smi` is unavailable, resolve the host driver in Mint's Driver Manager before troubleshooting Docker GPU access. Install and configure the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) for Docker; Compose reserves the GPU for Ollama. Do not change existing homelab stacks as part of this setup.

On Mint, use the Ubuntu base release codename rather than Mint's own release name when setting up Docker's Ubuntu package repository:

```bash
grep -E '^(UBUNTU_CODENAME|VERSION_CODENAME)=' /etc/os-release
```

Install Docker Engine and the Compose plugin from Docker's apt repository using the [official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/). After installing the NVIDIA Container Toolkit packages, register the runtime with Docker and restart Docker:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl enable --now docker
sudo systemctl restart docker
docker compose version
```

If Docker reports a permissions error, either prefix Docker commands with `sudo` or add your account to the `docker` group, understanding that membership grants root-equivalent access to the host.

The Compose services use Docker's `unless-stopped` restart policy. Docker will restart them after a daemon or host reboot, as long as the stack was not manually stopped or removed. Keep the machine awake (disable automatic suspend while plugged in) and configure the BIOS/UEFI to restore power after an outage if it needs to return automatically after a power loss.

## Quick start

### 1. Configure local secrets

```bash
cp .env.example .env
nano .env
```

Replace both password placeholders with long, unique values. Never commit `.env`.
Generate `PROFILE_ENCRYPTION_KEY` with `openssl rand -hex 32`. Keep it in `.env`, store a separate offline backup, and never put it in Git or a database backup. Losing or changing this key makes encrypted profile and search data unreadable. Do not start the stack with the example placeholder.

### 2. Validate and start

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

### 3. Download the configured model

```bash
docker compose exec ollama ollama pull qwen3:8b
```

The initial download may take time and several gigabytes of disk space. The example configuration uses `qwen3:8b` to fit more comfortably on a 16 GB GPU. If you change `OLLAMA_MODEL` in `.env`, download that exact model and recreate the API container:

```bash
docker compose up -d --build api
```

The dashboard's AI status stays unavailable until the configured model is downloaded. VRAM use depends on the model, context length, and other workloads; verify actual utilization on the Mint machine.

### 4. Check the API

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/db
curl http://127.0.0.1:8000/health/ollama
```

Interactive API docs are available at <http://127.0.0.1:8000/docs> only when `APP_ENV=development`. They are disabled in production.

### 5. Open the dashboard

Visit <http://127.0.0.1:8000/> on the Mint machine. The first visit opens account setup; create the local user and a password of at least 12 characters. Later visits require that username and password. The account page includes password change and sign out. This build has one account because its profile and job workspace are single-user; adding multiple accounts would require per-user data separation. After signing in, paste a job description, extract structured details with your local Ollama model, and review saved opportunities. Open **Career profile** to upload a PDF, DOCX, or TXT resume (up to 10 MB) and build a structured profile using local Ollama. The uploaded file is not retained; the extracted career profile is encrypted in MySQL. Search preferences and match terms are encrypted too. You can choose **Use in company search** to add its roles, skills, locations, and work preferences to your scanner profile. Open **Company search** to review those signals, add career-page URLs, and start a scan. The scanner checks `robots.txt`, stays on the submitted host, reads job links from HTML and embedded JSON/JavaScript application data, and reads at most 10 job pages per scan. It does not execute site scripts. It skips pages that disallow access, require a redirect, or link to another host; for those sites, add the direct hosted career page (such as the company's ATS page). Matches use transparent keyword coverage rather than an AI-generated judgment. Job descriptions must be at least 30 characters. The dashboard works without downloading external fonts.

## Useful commands

```bash
# Run the backend test suite from the backend directory after installing its dev dependencies.
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest -q
cd ..

# Logs and status
docker compose ps
docker compose logs --tail=100 ollama
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
- Resume-derived profile fields, the source filename, search preferences, and match terms are encrypted with AES-256-GCM before they are written to MySQL. This protects database files and dumps from someone who has only obtained the database. The encryption key must be backed up separately from the database.
- The API needs outbound network access to scan public company pages. Uploaded resume content is sent only to the configured Ollama endpoint, which is local in the provided Compose setup; it is not included in career-site scan requests. A complete compromise of the API host or process can access both the key and decrypted data, so application encryption cannot guarantee secrecy under that level of intrusion. Keep Mint patched, use full-disk encryption, and do not publish the services to the internet.
- Do not expose MySQL, the API, or Ollama directly to the public internet.
- Port bindings are loopback-only. Before remote access, configure Tailscale deliberately and review firewall rules.
- Store only the minimum data needed for job tracking. Avoid real personal data in fixtures and screenshots.
- Follow each source's terms and access rules.

## Roadmap

- [x] Public-repository-safe starter files
- [x] Docker Compose services for MySQL, Ollama, and FastAPI
- [x] Basic API liveness and dependency health endpoints
- [x] Dashboard for local AI job capture, search, and workspace status
- [x] Persist extracted job details in MySQL
- [x] Upload resumes and build a local AI career profile
- [x] Add user-started, same-host company career-page scanning with robots.txt checks
- [x] Save search preferences and rank discovered postings by keyword coverage
- [x] Configure Docker restart policies and GPU reservation for an always-on Linux host
- [ ] Verify GPU passthrough and reboot recovery on the Linux Mint machine
- [ ] Add versioned database migrations
- [ ] Add ATS-specific connectors and structured job-posting extraction
- [ ] Add scheduled collection
- [ ] Add local-model summarization and explainable matching
- [ ] Track application status, notes, and follow-ups
- [ ] Configure private Tailscale access and backups
- [ ] Evaluate compliant LinkedIn and Handshake workflows
