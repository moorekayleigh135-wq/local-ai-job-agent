# JobHarbor — Local AI Job Search

A self-hosted AI job-search assistant designed to discover, organize, compare, and track opportunities while keeping the application and its data under your control. **JobHarbor** is the working product name; the Compose service and volume identifiers remain stable for existing installs.

The stack targets **Linux Mint**, **Docker Engine**, an **NVIDIA RTX 4080 / 4080 SUPER (16 GB VRAM)**, **Ollama**, and **MySQL 8.4**. The dashboard and Ollama API bind to loopback by default, so they are available on the Mint host and are not published to the LAN.

> **Status:** personal-project MVP. The repository includes a private Docker stack, MySQL-backed job storage, local Ollama extraction, a resume-based career profile, a dashboard, scheduled scans of user-selected public career pages, match alerts, and opt-in web push. ATS-specific source coverage and application tracking remain future work.

## Goals

- Scan user-selected public company career pages for matching postings.
- Search company career pages and, where permitted, LinkedIn and Handshake.
- Rank discovered postings against configurable role, skill, location, and work-style preferences.
- Build a structured career profile from an uploaded PDF, DOCX, or TXT resume using the local Ollama model.
- Save manually captured and scanned job descriptions in MySQL; use local AI to extract details from manually pasted postings.
- Use a local Ollama model to summarize descriptions and explain why a role may fit.
- Provide a private dashboard for capturing and reviewing results.
- Scan saved company pages automatically and send opt-in browser push alerts for newly discovered matches.

## Principles

1. **Local-first inference:** use Ollama on the home server.
2. **Private by default:** MySQL has no published host port; the dashboard/API and Ollama ports bind to loopback.
3. **Separate from the existing homelab:** this has its own Compose project name, network, and named volumes.
4. **Respect source rules:** use user-selected public career pages, follow `robots.txt`, and limit requests. Prefer official feeds and APIs where available. Any LinkedIn or Handshake integration must respect terms, authentication requirements, and rate limits. Do not bypass access controls.
5. **Secrets stay local:** credentials belong in `.env`, which Git ignores. Commit `.env.example` with placeholders only.
6. **Human review:** AI-generated summaries and fit scores are suggestions, not guarantees.
7. **Accessible identity:** use JMU purple for primary actions and dark text on gold or light-purple accents; include labels and percentages so meaning does not depend on color alone.

## Architecture

```text
Local browser ------------------------┐
    | 127.0.0.1:8000                 |
    v                                |
FastAPI API --------> Ollama         | (optional)
    |                                |
    v                                v
MySQL 8.4                  Tailscale Serve (HTTPS, tailnet only)
                                     ^
Remote browser with Tailscale -------┘

FastAPI API --> isolated JavaScript renderer --> public career pages
```

Services:
- `api`: FastAPI application in `backend/`.
- `db`: MySQL 8.4 with a persistent named volume; no host port mapping.
- `ollama`: local model server with persistent model storage, available only to the API on the private Compose network.
- `renderer`: a separate, non-root Playwright/Chromium worker with a seccomp profile for public career pages that need JavaScript. It has no database, Ollama, or profile credentials; it communicates with the API over a private control network and uses a separate internet-access network.

The API needs outbound internet access for company career-page scans. The Compose stack separates the renderer's private control network from its internet-access network; the renderer applies public-URL checks to browser requests. The services are not intentionally published on the LAN.

Optional employer discovery runs through a private SearXNG service in Docker Compose, with no paid API key. SearXNG sends saved role and location terms to its configured upstream search engines; resume files and extracted career profiles stay local. Suggestions are not scanned until the user chooses **Monitor this site**. Upstream engines can still rate-limit searches, so automatic discovery is best-effort.

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

Generate a separate `SEARXNG_SECRET` with `openssl rand -hex 32`. SearXNG is self-hosted and does not require a search API key. Set `COMPANY_DISCOVERY_INTERVAL_HOURS` if you want a different refresh interval (default 24 hours).

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

Use the **Settings** page to choose light or dark theme, set the saved-company scan frequency (15 minutes to 24 hours), cap new alerts per day (1–100), and choose the minimum match percentage. Scan and match controls are stored in the local database; the theme setting is kept in the current browser. Once the daily alert cap is reached, scanning and job discovery continue, but new notifications wait until the next day.

Visit <http://127.0.0.1:8000/> on the Mint machine. The first visit opens account setup; create the local user and a password of at least 12 characters. Later visits require that username and password. The account page includes password change and sign out. This build has one account because its profile and job workspace are single-user; adding multiple accounts would require per-user data separation. After signing in, paste a job description, extract structured details with your local Ollama model, and review saved opportunities. Open **Career profile** to upload a PDF, DOCX, or TXT resume (up to 10 MB) and build a structured profile using local Ollama. The uploaded file is not retained; the extracted career profile is encrypted in MySQL. Search preferences and match terms are encrypted too. You can choose **Use in company search** to add resume-derived roles and skills to your scanner profile. Location and work-style preferences remain separate controls on **Company search**. Company Search uses the private local SearXNG service to discover employer career pages and major job boards from saved role and location preferences, then refreshes suggestions daily. Search is limited to roughly the last month; upstream snippets are used only to find possible source pages and are never treated as job evidence. Only role and location terms are sent to SearXNG's configured upstream search engines; the resume stays local. Choose **Monitor this site** to add a suggestion to the monitored website list; only monitored sites are scanned automatically. You can also add career-page or job-board URLs directly. The server scans monitored pages automatically on startup and about once per `SCAN_INTERVAL_MINUTES` (default 60, minimum 15). It checks `robots.txt`, stays on the submitted host, and reads at most 10 job-detail pages per site each cycle. If the static page has no job links or posting data, the isolated JavaScript renderer can load its public DOM and run the existing extraction rules against that result. Browser rendering is bounded to one page at a time, 120 requests, 15 seconds of navigation, and 2 MB of returned HTML. It does not submit forms, follow cross-site navigation, or solve bot checks/CAPTCHAs; those pages are skipped. A listing is saved or alerted only when its actual job-detail page contains a schema.org `JobPosting` record with a valid title and `datePosted` within `NEW_JOB_WINDOW_HOURS` (default 720 hours / 30 days); an expired `validThrough` posting is skipped. Undated pages, old posts, search summaries, and aggregator snippets are not treated as job evidence. Pages that disallow access, require a redirect, or link to another host are skipped; use the employer's direct hosted ATS page where possible. Set the minimum match score to filter recommendations. Scores are a local AI estimate across role, skills, experience, location, and work style, not a hiring probability. If local AI scoring is unavailable, keyword-based estimates are used. 80%+ is a strong match, 60–79% a good match, and below 60% a potential match. The local **Alerts** inbox remains available even when push is off. To receive phone and desktop push while the page is closed, open Alerts on each device over HTTPS and select **Enable phone & desktop alerts**. Each device must grant permission. Web Push encrypts alert contents for the device; push providers can see the subscription endpoint and delivery timing. The VAPID private key and browser subscription are encrypted in MySQL. On iPhone/iPad, add the site to the Home Screen to enable Web Push. The default loopback address is for the Mint machine; phone access requires the private Tailscale setup described below. Automatic checks reduce detection delay but cannot guarantee a specific application rank. Job descriptions must be at least 30 characters. The dashboard works without downloading external fonts.

Direct posting links are also supported. For IBM, you can add a link like `https://careers.ibm.com/en_US/careers/JobDetail?jobId=129654`; the scanner preserves the query string, records the company-scoped posting ID, and checks that page as one posting instead of treating it as a listings page. IBM's official `www.ibm.com` and `careers.ibm.com` hosts are allowed to link to each other; each destination is still checked against its own `robots.txt`. Match scoring uses the local Ollama model to compare verified postings with saved search preferences and the locally stored resume profile, including extracted role titles, skills, and experience. Set a target experience level in **Company search** if you want to prefer a specific level. Job level is displayed when present in structured posting data or an explicit experience-level field. Expired or missing postings are reported as skipped, including 404/410 links. If Ollama is unavailable, the scanner reports that it used keyword-based fallback scoring.

### 6. Optional private access from your phone with Tailscale

Tailscale Serve gives devices on your tailnet an HTTPS address for the dashboard without publishing the API port to your LAN or the public internet. Install Tailscale on the Linux Mint host and on each device that should connect, then sign them in to the same tailnet. Follow the [official Linux installation guide](https://tailscale.com/docs/install/linux).

Create the JobHarbor account on the Mint machine at <http://127.0.0.1:8000/> before enabling remote access. This is a single-user app; anyone who can sign in uses the same account and workspace. Only connect trusted devices and keep the app password private.

In the [Tailscale admin console](https://login.tailscale.com/admin/dns), enable **MagicDNS** and **HTTPS certificates**. Then, on the Mint host, run:

```bash
sudo tailscale serve --bg http://127.0.0.1:8000
sudo tailscale serve status
```

Open the HTTPS address printed by Tailscale on the host and on a phone that is connected to the same tailnet. The background Serve configuration persists across host restarts while Tailscale is running. Tailscale Serve is tailnet-only; do not use Tailscale Funnel for this private dashboard. Enabling HTTPS certificates publishes the device and tailnet DNS names to the public Certificate Transparency log, but does not make the dashboard itself public. Avoid sensitive information in the machine name. See [Tailscale HTTPS certificate details](https://tailscale.com/docs/how-to/set-up-https-certificates) and the [`tailscale serve` reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

## Search limits and experience matching

Saved opportunities, match recommendations, and alert lists return at most 50 items, and the daily new-alert setting is capped at 50. When **Target experience level** is set to **Any level**, the scanner derives a level from the locally stored resume profile, including years of experience and in-progress education. It uses that level and any stated job-level or years requirements to constrain the final match percentage; entry-level searches also omit senior/director company-search results. Resume contents are not sent to the discovery search service.

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
- The API needs outbound network access to scan public company pages and to deliver opt-in Web Push messages. The push provider sees the endpoint and delivery timing; Web Push encrypts the message body for the subscribed browser. Uploaded resume content is sent only to the configured Ollama endpoint, which is local in the provided Compose setup; it is not included in career-site scan requests. A complete compromise of the API host or process can access both the key and decrypted data, so application encryption cannot guarantee secrecy under that level of intrusion. Keep Mint patched, use full-disk encryption, and do not publish the services to the internet.
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
- [x] Create a local alert inbox for newly discovered jobs above the saved minimum match score
- [x] Schedule recurring scans and opt-in encrypted browser push alerts
- [x] Apply an accessible JMU-inspired purple and gold interface palette
- [x] Label recommendations using match-score tiers and show matching profile signals
- [x] Configure Docker restart policies and GPU reservation for an always-on Linux host
- [x] Add an isolated, bounded JavaScript renderer for public career pages
- [ ] Verify GPU passthrough and reboot recovery on the Linux Mint machine
- [ ] Add versioned database migrations
- [ ] Add ATS-specific connectors and structured job-posting extraction
- [ ] Add local-model summarization and explainable matching
- [ ] Track application status, notes, and follow-ups
- [ ] Configure private Tailscale access and backups
- [ ] Evaluate compliant LinkedIn and Handshake workflows
