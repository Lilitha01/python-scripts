# python-scripts

A collection of Python utility scripts.

---

## scripts/health_checker.py

Checks the health of a list of HTTP endpoints and reports their status and response time.

### Usage

```bash
# Check all endpoints
python3 scripts/health_checker.py

# List endpoints without hitting them
python3 scripts/health_checker.py --dry-run

# Set a custom timeout (default is 5s)
python3 scripts/health_checker.py --timeout 10
```

### Example output

```
Checking 4 endpoints...

      Name                 Status                    Response Time
  -------------------------------------------------------
  ✅  Grafana              200                       43ms
  ✅  Prometheus           200                       12ms
  ✅  Node Exporter        200                       8ms
  ✅  Loki                 200                       19ms

---------------------------------------------------------
  Total: 4  |  Healthy: 4  |  Unhealthy: 0
```

If any endpoint is unhealthy, the script lists them and exits with code `1`.

### Configuration

Edit the `ENDPOINTS` list at the top of the script to point at your own services:

```python
ENDPOINTS = [
    {"name": "Grafana",    "url": "http://your-host:3000"},
    {"name": "Prometheus", "url": "http://your-host:9090"},
]
```

### Requirements

```bash
pip install -r requirements.txt
```

---

## scripts/log_analyser.py

Parses a structured log file and prints a summary: counts by level, top recurring errors, and optionally filters to a single level.

### Usage

```bash
python3 scripts/log_analyser.py logs/sample.log
python3 scripts/log_analyser.py logs/sample.log --level ERROR
python3 scripts/log_analyser.py logs/sample.log --top 3
```

---

## scripts/ec2_manager.py

Lists, stops, and starts EC2 instances. Supports filtering by state or tag.

### Usage

```bash
python3 scripts/ec2_manager.py --list
python3 scripts/ec2_manager.py --list --state running
python3 scripts/ec2_manager.py --stop --tag Environment=dev --dry-run
python3 scripts/ec2_manager.py --stop --tag Environment=dev
python3 scripts/ec2_manager.py --start --tag Environment=dev
```

### Requirements

AWS credentials must be configured (`aws configure` or environment variables). Defaults to `eu-west-1`.

---

## scripts/ec2_terminate.py

Permanently terminates EC2 instances by tag or instance ID. Requires explicit `"terminate"` confirmation before anything is destroyed. Always dry-run first.

> **Warning:** Termination is irreversible. Use `ec2_manager.py --stop` if you just want to pause instances.

### Usage

```bash
python3 scripts/ec2_terminate.py --tag Environment=dev --dry-run
python3 scripts/ec2_terminate.py --tag Environment=dev
python3 scripts/ec2_terminate.py --id i-1234567890abcdef0
python3 scripts/ec2_terminate.py --id i-abc123 --id i-def456
```

Instances already in `terminated` or `shutting-down` state are automatically skipped.

---

## scripts/ec2_spawn.py

Launches one or more EC2 instances with specified tags and instance type.

### Usage

```bash
python3 scripts/ec2_spawn.py --count 3 --tag Environment=dev --tag Project=lab
python3 scripts/ec2_spawn.py --count 1 --type t3.micro --tag Environment=dev --dry-run
```

Defaults to `t3.micro` in `eu-west-1`. Use `--dry-run` to preview without launching.

---

## scripts/github_migrate.py

Migrates repositories from one GitHub organisation to another. Supports two modes:

- **transfer** — GitHub's native transfer API. The repo moves to the target org (source gets a redirect stub). Preserves issues, PRs, stars, releases, labels, and milestones. Fast.
- **clone** — Mirror-clone then push. Source stays intact. Use this for incremental migrations where you want to validate the target before teams cut over.

Credentials are read from `GITHUB_TOKEN` only — never passed as CLI args.

### Token permissions

| Token type | Required scopes |
|---|---|
| Classic PAT | `repo` (full), `admin:org` |
| Fine-grained PAT | Contents (R/W), Metadata (R), Members (R) on both orgs |

### Usage

```bash
# Dry-run — see what would happen for all repos
GITHUB_TOKEN=ghp_... python3 scripts/github_migrate.py \
    --source old-org --target new-org --dry-run

# Transfer specific repos
GITHUB_TOKEN=ghp_... python3 scripts/github_migrate.py \
    --source old-org --target new-org --repos service-a service-b

# Transfer all repos and archive them in the target (read-only)
GITHUB_TOKEN=ghp_... python3 scripts/github_migrate.py \
    --source old-org --target new-org --archive

# Clone mode — leaves source intact
GITHUB_TOKEN=ghp_... python3 scripts/github_migrate.py \
    --source old-org --target new-org --mode clone --clone-wiki
```

A Markdown summary is written to `migration_summary.md` after each run (override with `--output`).