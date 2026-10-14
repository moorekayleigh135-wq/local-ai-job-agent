# Architecture notes

## Current services

- `api`: FastAPI application and future job collection/matching logic.
- `db`: MySQL 8.4. No host port is published.
- `ollama`: local model API with persistent model storage. The host mapping is loopback-only.

Services communicate on a project-specific Compose bridge network. The API needs outbound access for job-source collection, so this starter does not use an `internal: true` network. Host ports are still bound to loopback for local development.

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
