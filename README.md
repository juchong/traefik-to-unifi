# Traefik to UniFi

## Description

This project aims to integrate Traefik with UniFi, allowing for routes populated in Traefik to be updated in the static DNS of UniFi.
## Installation

1. Clone the repository: `git clone https://github.com/ThomasLomas/traefik-to-unifi.git`
2. Install the required dependencies: `pip install -r requirements.txt`
3. Set up the necessary environment variables:

### Required Environment Variables:

**Always required:**
- `UNIFI_URL`: The URL of the UniFi controller (e.g., `https://192.168.1.1/`)
- `TRAEFIK_API_URL`: The URL of the Traefik reverse proxy API (e.g., `http://traefik:8080/api/`)
- `TRAEFIK_IP`: For A records, the IP of Traefik. For CNAME records, the hostname resolving to the IP.

**Authentication (choose one):**

| Method | Variables | Notes |
|--------|-----------|-------|
| API Key (recommended) | `UNIFI_API_KEY` | More secure, no password rotation issues |
| Username/Password | `UNIFI_USERNAME` + `UNIFI_PASSWORD` | Legacy method, requires local admin account |

### Generating a UniFi API Key

API key authentication is recommended over username/password. Follow these steps to generate one:

1. **Access the UniFi Network Application**
   - Log in to your UniFi Network application via the web interface
   - Or access through your Official UniFi Hosting account (unifi.ui.com)

2. **Navigate to the Integrations Section**
   - Go to **Network** → **Settings** → **System** → **Integrations**
   - Or access directly via: `https://<your-controller>/network/default/settings/system/integrations`

3. **Generate the API Key**
   - In the Integrations section, find the option to generate API keys
   - Click **Create API Key** or **Generate**
   - Give it a descriptive name (e.g., "traefik-to-unifi")

4. **Save the API Key**
   - **Important:** Copy and securely save the API key immediately
   - The key will not be displayed again after you close the dialog
   - Store it in your `.env` file or secrets manager

### Optional Environment Variables (with defaults):

- `DNS_RECORD_TYPE`: Either A or CNAME. Defaults to A.
- `LOG_LEVEL`: Either CRITICAL, ERROR, WARNING, INFO, DEBUG. Defaults to INFO.
- `FULL_SYNC_INTERVAL`: Trigger a full sync every N runs. Defaults to 5.
- `IGNORE_SSL_WARNINGS`: Set to "true" to ignore SSL warnings. Defaults to "false".
- `DNS_OUTPUT_FILE`: Path to write a JSON file tracking all synced DNS entries. If not set, no file is written.

### DNS Output File (Optional):

When `DNS_OUTPUT_FILE` is set, the application writes a JSON file after each sync containing all current DNS entries. This is useful for:

- Auditing which routes are synced to UniFi
- Integration with monitoring or documentation tools
- Debugging DNS sync issues

Example output (`/data/dns-entries.json`):

```json
{
  "last_updated": "2025-12-28T21:45:00.000000+00:00",
  "traefik_ip": "10.0.10.50",
  "dns_record_type": "A",
  "total_entries": 3,
  "entries": [
    {
      "hostname": "grafana.example.com",
      "target": "10.0.10.50",
      "type": "A"
    },
    {
      "hostname": "lidarr.example.com",
      "target": "10.0.10.50",
      "type": "A"
    },
    {
      "hostname": "sonarr.example.com",
      "target": "10.0.10.50",
      "type": "A"
    }
  ]
}
```

**Note:** Make sure to mount a volume for persistence:
```yaml
volumes:
  - /path/to/data:/data
environment:
  - DNS_OUTPUT_FILE=/data/dns-entries.json
```

### Docker Label Filtering (Optional):

Filter which containers get DNS entries by checking Docker container labels. This is useful when you only want certain containers to have UniFi DNS records (similar to how [cloudflare-companion](https://github.com/tiredofit/docker-traefik-cloudflare-companion) works).

- `DOCKER_FILTER_LABEL`: The Docker label name to check (e.g., `traefik.unifi-dns`).
- `DOCKER_FILTER_VALUE`: The required label value (e.g., `true`). If not set, any value is accepted.

**Note:** When using Docker label filtering, you must mount the Docker socket:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

#### Example: Only create DNS for containers with `traefik.unifi-dns=true`

Container labels:
```yaml
# This container WILL get UniFi DNS
labels:
  - traefik.enable=true
  - traefik.http.routers.myapp.rule=Host(`myapp.example.com`)
  - traefik.unifi-dns=true  # ← This label triggers UniFi DNS creation

# This container will NOT get UniFi DNS (no traefik.unifi-dns label)
labels:
  - traefik.enable=true
  - traefik.http.routers.public.rule=Host(`public.example.com`)
  - traefik.constraint=proxy-public  # ← Only for Cloudflare, not UniFi
```

traefik-to-unifi configuration:
```yaml
environment:
  - DOCKER_FILTER_LABEL=traefik.unifi-dns
  - DOCKER_FILTER_VALUE=true
```

### Prune / Sync Mode (Optional):

By default the app only **adds and updates** DNS entries; removing a container
leaves an orphan record in UniFi. Prune mode deletes those stale entries so
UniFi DNS stays in sync as you stand projects up and tear them down.

**Safety is the priority — the app only ever deletes entries it created:**

- Prune requires `DOCKER_FILTER_LABEL` (ownership is tracked via the Docker
  label scan). It refuses to start otherwise.
- A persistent **ownership ledger** (`DNS_STATE_FILE`, two buckets) records
  exactly the hosts this tool POSTed. Manual UniFi entries never enter the
  `managed` bucket, so they are **never** prune-eligible — they only surface
  under `unmanaged` for your review.
- A host is deleted only after it is absent for `PRUNE_GRACE_CYCLES` consecutive
  syncs (absorbs container restarts/redeploys).
- Before deleting, the entry is re-checked against UniFi (value + type must
  still match ours); after deleting, a fresh fetch confirms it is gone before
  the ledger drops it. If UniFi drifted or was adopted manually, the entry is
  **released** (moved to `unmanaged`) instead of deleted.
- If the Docker query fails on a cycle (ownership unknowable), pruning is
  **skipped** entirely for that cycle; add/update still runs.

Prune environment variables:

- `UNIFI_DNS_PRUNE`: Set to `true` to enable delete mode. Defaults to `false`.
- `PRUNE_GRACE_CYCLES`: Consecutive absent syncs before deletion. Defaults to `3`.
- `DRY_RUN`: Set to `true` to log intended deletes without executing any. Defaults to `false`.
- `PRUNE_LABEL`: Per-container opt-out label. Defaults to `<DOCKER_FILTER_LABEL>.prune` (e.g. `traefik.unifi-dns.prune`).
- `DNS_STATE_FILE`: Path to the ownership ledger. Defaults to `/data/dns-state.json`.

**Per-container opt-out:** set the prune label to `false` to keep a host's entry
even after its container is removed (the entry moves to `unmanaged`). When
multiple containers share a hostname, opt-out wins.

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.keep.rule=Host(`keep.example.com`)
  - traefik.unifi-dns=true
  - traefik.unifi-dns.prune=false  # ← never delete this entry
```

The ledger (`DNS_STATE_FILE`) makes ownership auditable:

```json
{
  "managed": {
    "app.example.com": { "value": "10.0.10.50", "record_type": "A",
                         "prune_eligible": true, "missing_count": 0 }
  },
  "unmanaged": {
    "legacy.example.com": { "value": "10.0.10.50", "record_type": "A",
                            "reason": "not-created-by-us", "first_seen": "2026-07-21T..." }
  }
}
```

`reason` is one of `not-created-by-us` (manual/pre-existing), `value-drift`
(we created it but the value was changed outside the tool), or
`opted-out-orphan` (prune=false container removed).

### Read-only Web UI & File Logging (Optional):

An optional read-only web page renders the DNS map (managed + unmanaged),
recent sync history, and a tail of the log — all from files in `/data`, so it
needs no Docker socket or root to view. It runs in a daemon thread using only
the Python standard library (no extra dependencies) and is **GET-only** (any
other method returns `405`).

Routes: `/` (HTML dashboard), `/api/state`, `/api/history`, `/api/log`,
`/healthz` (readiness based on the last sync timestamp — usable as a container
healthcheck).

Web UI / logging environment variables:

- `WEB_UI_ENABLED`: Set to `true` to start the UI. Defaults to `false`.
- `WEB_UI_PORT`: Listen port. Defaults to `8080`.
- `WEB_UI_BIND`: Bind address. Defaults to `0.0.0.0`.
- `WEB_UI_LOG_TAIL`: Lines of log to show/serve. Defaults to `200`.
- `WEB_UI_LIVENESS_MAX_AGE`: Seconds since last sync before `/healthz` reports stale. Defaults to `300`.
- `LOG_FILE`: Plain-text log path (rotating). Defaults to `/data/traefik-to-unifi.log`. Set empty to disable.
- `LOG_FILE_MAX_BYTES` / `LOG_FILE_BACKUPS`: Rotation size/backups. Default `1000000` / `3`.
- `SYNC_HISTORY_FILE`: Per-sync JSON-lines history. Defaults to `/data/sync-history.jsonl`.
- `SYNC_HISTORY_MAX`: Records to retain. Defaults to `200`.

All web UI / logging / prune state lives under `/data`, so mount a volume for
persistence (see the `DNS_OUTPUT_FILE` note above).

## Usage

### 1. Using a published image

You can pull the latest image from Docker Hub:

```bash
docker pull ghcr.io/thomaslomas/traefik-to-unifi:latest
```

Then run the container with the required environment variables:

```bash
docker run -e TRAEFIK_API_URL=http://traefik:8080/api/ \
           -e TRAEFIK_IP=192.168.1.10 \
           -e UNIFI_URL=https://unifi:8443/ \
           -e UNIFI_USERNAME=admin \
           -e UNIFI_PASSWORD=supersecret \
           ghcr.io/thomaslomas/traefik-to-unifi:latest
```

### 2. Running with Docker (without docker-compose)

#### 1. Build the Docker image:

```bash
docker build -t traefik-to-unifi .
```

#### 2. Run the container using a `.env` file:

Create a `.env` file with the required environment variables:

```.env
TRAEFIK_API_URL=http://traefik:8080/api/
TRAEFIK_IP=192.168.1.10
UNIFI_URL=https://unifi:8443/
UNIFI_USERNAME=admin
UNIFI_PASSWORD=supersecret
```

```bash
docker run --env-file .env traefik-to-unifi
```

Or by passing environment variables directly:

```bash
docker run -e TRAEFIK_API_URL=http://traefik:8080/api/ \
           -e TRAEFIK_IP=192.168.1.10 \
           -e UNIFI_URL=https://unifi:8443/ \
           -e UNIFI_USERNAME=admin \
           -e UNIFI_PASSWORD=supersecret \
           traefik-to-unifi
```

### 3. Running with Docker Compose

Build the image and start the service:

```bash
docker compose build
docker compose up
```

Make sure your `.env` file is next to `docker-compose.yml` so that secrets are loaded automatically.

## Development

This project uses Poetry for dependency management and includes automated code quality checks.

### Setup Development Environment

1. Install dependencies and set up pre-commit hooks:
   ```bash
   make dev-setup
   ```

### Code Quality

This project uses several tools to maintain code quality:

- **Ruff** - Fast Python linter and formatter
- **Black** - Code formatter
- **Pre-commit** - Git hooks for automatic checks

#### Available Commands

```bash
# Check code style and formatting
make lint

# Auto-fix formatting issues
make format

# Run all CI checks locally
make ci

# Run pre-commit hooks on all files
make pre-commit
```

#### GitHub Actions

All pull requests automatically run a single CI workflow that includes:

- Code linting with Ruff
- Format checking with Black and Ruff
- Pre-commit hook validation
- Security scanning with Trivy

The CI will fail if code doesn't meet the formatting and linting standards.

## Contributing

Contributions are welcome! Please follow the guidelines outlined in [CONTRIBUTING.md](./CONTRIBUTING.md).

Before submitting a pull request:

1. Run `make ci` to ensure your changes pass all checks
2. Make sure your code is properly formatted with `make format`
3. Add tests for new functionality

## License

This project is licensed under the [MIT License](./LICENSE).
