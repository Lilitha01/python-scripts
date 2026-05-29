#!/usr/bin/env python3
"""
health_checker.py — Check the health of a list of HTTP endpoints.

Usage:
    python3 scripts/health_checker.py
    python3 scripts/health_checker.py --dry-run
    python3 scripts/health_checker.py --timeout 10
"""

import argparse
import sys
import requests


ENDPOINTS = [
    {"name": "Grafana",        "url": "http://192.168.10.71:3000"},
    {"name": "Prometheus",     "url": "http://192.168.10.71:9090"},
    {"name": "Node Exporter",  "url": "http://192.168.10.71:9100"},
    {"name": "Loki",           "url": "http://192.168.10.71:3100/ready"},
]


def parse_args():
    parser = argparse.ArgumentParser(description="Check health of HTTP endpoints")
    parser.add_argument("--dry-run", action="store_true", help="List endpoints without hitting them")
    parser.add_argument("--timeout", type=int, default=5, help="Request timeout in seconds (default: 5)")
    return parser.parse_args()


def check_endpoint(name, url, timeout=5):
    try:
        response = requests.get(url, timeout=timeout)
        return {
            "name": name,
            "url": url,
            "status": response.status_code,
            "response_time_ms": round(response.elapsed.total_seconds() * 1000),
            "healthy": response.status_code == 200,
        }
    except requests.exceptions.Timeout:
        return {"name": name, "url": url, "status": "TIMEOUT", "response_time_ms": None, "healthy": False}
    except requests.exceptions.ConnectionError:
        return {"name": name, "url": url, "status": "CONNECTION ERROR", "response_time_ms": None, "healthy": False}
    except Exception as e:
        return {"name": name, "url": url, "status": f"ERROR: {e}", "response_time_ms": None, "healthy": False}


def print_result(result):
    status = result["status"]
    name = result["name"]
    rt = f"{result['response_time_ms']}ms" if result["response_time_ms"] else "—"
    indicator = "✅" if result["healthy"] else "❌"
    print(f"  {indicator}  {name:<20} {str(status):<25} {rt}")


def main():
    args = parse_args()

    print(f"Checking {len(ENDPOINTS)} endpoints...\n")
    print(f"  {'':4}{'Name':<20} {'Status':<25} {'Response Time'}")
    print(f"  {'-' * 55}")

    if args.dry_run:
        for ep in ENDPOINTS:
            print(f"  [DRY RUN]  {ep['name']:<20} {ep['url']}")
        return

    results = []
    for ep in ENDPOINTS:
        result = check_endpoint(ep["name"], ep["url"], timeout=args.timeout)
        print_result(result)
        results.append(result)

    unhealthy = [r for r in results if not r["healthy"]]

    print(f"\n{'-' * 57}")
    print(f"  Total: {len(results)}  |  Healthy: {len(results) - len(unhealthy)}  |  Unhealthy: {len(unhealthy)}")

    if unhealthy:
        print("\n  Unhealthy endpoints:")
        for r in unhealthy:
            print(f"    - {r['name']} ({r['url']}) — {r['status']}")
        sys.exit(1)


if __name__ == "__main__":
    main()