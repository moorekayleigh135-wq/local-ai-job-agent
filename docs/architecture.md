# Architecture notes

## Current services

- `api`: FastAPI application, recurring career-page scanner, match alerts, and local AI extraction.
- `db`: MySQL 8.4. No host port is published.
- `ollama`: local model API with persistent model storage. The host mapping is loopback-only.

## Current application flow

- The dashboard is served by the API at `/` and uses the same-origin `/jobs` endpoints.
- A user pastes a job description and may include its source URL.
- The API asks Ollama for supported structured fields, then stores both the original description and extracted details in MySQL.
- A user uploads a PDF, DOCX, or TXT resume (maximum 10 MB). The API extracts up to 30,000 characters, sends that text to the configured local Ollama service, validates the structured career profile, and stores only the profile and safe display filename in MySQL. Both values are encrypted at the application layer before storage. The uploaded file and full resume text are not retained.
- The Career Profile screen lets the user review the AI-created profile and explicitly add extracted roles, skills, locations, and work-style preferences to Company Search. Resume-derived terms do not replace existing search terms.
- The dashboard lists saved roles, supports text search and remote-role filtering, and reports API, database, and Ollama connectivity.
- The Company Search screen stores one user profile and user-selected career-page URLs. The API performs an initial scan shortly after startup and repeats at `SCAN_INTERVAL_MINUTES` (default 60; configurable from 15 minutes to 24 hours). Scans are sequential, limited to the submitted host and at most 10 job-detail pages per company cycle, and respect the site's `robots.txt`.
- Discovered postings are ranked by weighted keyword coverage for role, location, skill, and work-style preferences. The score and matching terms are stored separately from the job record so rescans can update them without changing the existing job schema.
- Discovered jobs are stored with their latest score, including below-threshold results, so rescans can refresh recommendation tiers. The Company Search view applies the current minimum score and labels results as strong (80%+), good (60–79%), or potential (below 60%). These percentages describe weighted keyword coverage, not a probability of getting hired.
- The Settings page stores scan interval, maximum new alerts per day, and match threshold locally in the database. A daily cap suppresses additional alert creation after the limit while allowing scans and job discovery to continue. Theme preference is stored in the current browser.
- A local persistent alert is created when a scan first discovers or newly qualifies a job above the saved minimum, if its published date is inside `NEW_JOB_WINDOW_HOURS` (default 48). If structured `datePosted` data is absent, the app can only report its first-detected time; it cannot promise the listing itself is new or a first-50 applicant position.
- Users can opt in to Web Push per browser/device. Push subscriptions and the VAPID private key are encrypted in MySQL. The push payload is encrypted for the browser; the push provider can see the endpoint and delivery timing. Push requires browser permission and a secure HTTPS origin. Phone access requires a private HTTPS route such as Tailscale; localhost works for setup on the Mint host.
- The interface uses JMU Purple (`#450084`) for primary controls and JMU Gold (`#CBB677`) as an accent with dark text. Match scores and status labels include text so meaning does not depend on color alone.
- `/docs` is available only in development; `/api` reports basic service metadata.

The scanner reads static HTML links and structured job data embedded in JSON-LD or common JavaScript hydration payloads, without executing remote scripts. It prioritizes structured listings with recent publication dates, then reads up to 10 same-origin job detail pages per company cycle. It follows same-host links only. Pages that fetch listings dynamically after load or link to another host are not supported yet; use a company's direct hosted career page where possible. ATS-specific connectors and richer date extraction remain future work. A `robots.txt` error or disallowed path stops or skips that scan.

Resume files are size-limited and accepted only as PDF, DOCX, or UTF-8 TXT. Password-protected PDFs, image-only PDFs, legacy DOC files, and PDFs longer than 100 pages are not supported. The AI profile contains a summary, experience, education, certifications, skills, target roles, locations, and explicitly stated work preferences. The scanner's matching remains transparent keyword coverage after the user chooses to apply those extracted terms; AI match explanations are not currently implemented.

Services communicate on a project-specific Compose bridge network. The API needs outbound access for job-source collection and opt-in push delivery, so this starter does not use an `internal: true` network. Host ports are still bound to loopback for local development.

## Personal-data protection

`PROFILE_ENCRYPTION_KEY` is a required 32-byte hex key. JobHarbor uses AES-256-GCM to encrypt the profile envelope, search preference lists, and match terms before SQLAlchemy writes them. Fresh random 96-bit nonces and authenticated associated data are used for each encryption. Startup migrates existing plaintext values. The raw database does not contain the protected values in readable form, but the API decrypts them in memory when serving profile and search operations. Keep the key in the ignored `.env` file and maintain an offline copy separately from database backups. Loss or rotation without re-encryption makes existing data inaccessible.

The Web Push private key and each browser subscription are encrypted with the same application key before storage. Push payload encryption is performed for the device subscription; push services carry encrypted alert contents but observe subscription endpoints and delivery timing. Push is only sent after a user explicitly enables it on a device.

The API container runs as a non-root user with a read-only root filesystem, dropped Linux capabilities, and a small no-exec temporary filesystem. Production mode disables interactive API docs. Browser mutation requests with an Origin or Referer must be same-origin, and responses receive restrictive browser security headers. These controls reduce common exposure but do not make a compromised host or API process safe: that process has the key and must have internet egress for career-page scans. Resume text goes to the configured Ollama service; company scan requests contain only the public URLs and requests needed to read those sites, not the resume or search profile.

## Local account

The first-run setup creates one local account with an Argon2 password hash. Login sessions use random tokens stored in HttpOnly, SameSite=Strict cookies; only a SHA-256 digest of each token is stored in MySQL. Sessions expire after 30 days. Changing the password revokes all existing sessions, then starts a new session for the current browser. The current data model is single-user, so additional accounts are intentionally disabled until application data has per-user ownership.

## Data boundaries

Keep these out of Git:
- passwords, API tokens, browser cookies, and other credentials;
- personal application notes and job-search exports;
- local MySQL files and database dumps;
- downloaded Ollama model weights.

Commit application code, schema migrations, tests, example configuration with fake values, and documentation.

## Planned data entities

- `Company`: employer name, canonical website, notes.
- `JobPosting`: source, source identifier, title, location, URL, description, first/last-seen timestamps.
- `JobMatch`: posting, profile version, score, explanation, model metadata.
- `Application`: posting, status, dates, and user-authored notes.
- `SearchRun`: source, start/end times, status, and counts.

Deduplicate using normalized canonical URLs and source identifiers where available. Keep AI-generated recommendations separate from user-authored application status and notes.
