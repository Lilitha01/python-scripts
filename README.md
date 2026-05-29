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