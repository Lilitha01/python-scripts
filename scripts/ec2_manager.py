#!/usr/bin/env python3
"""
ec2_manager.py — List, filter, stop, and start EC2 instances.

Usage:
    python3 scripts/ec2_manager.py --list
    python3 scripts/ec2_manager.py --list --state running
    python3 scripts/ec2_manager.py --stop --tag Environment=dev --dry-run
    python3 scripts/ec2_manager.py --stop --tag Environment=dev
    python3 scripts/ec2_manager.py --start --tag Environment=dev --dry-run
"""

import argparse
import sys
import boto3
from botocore.exceptions import ClientError, NoCredentialsError


def parse_args():
    parser = argparse.ArgumentParser(description="Manage EC2 instances")
    parser.add_argument("--region", default="eu-west-1", help="AWS region (default: eu-west-1)")
    parser.add_argument("--list", action="store_true", help="List instances")
    parser.add_argument("--state", help="Filter by state: running, stopped, etc.")
    parser.add_argument("--tag", help="Filter by tag in KEY=VALUE format (e.g. Environment=dev)")
    parser.add_argument("--stop", action="store_true", help="Stop matching instances")
    parser.add_argument("--start", action="store_true", help="Start matching instances")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    return parser.parse_args()


def build_filters(state=None, tag=None):
    filters = []
    if state:
        filters.append({"Name": "instance-state-name", "Values": [state]})
    if tag:
        key, value = tag.split("=", 1)
        filters.append({"Name": f"tag:{key}", "Values": [value]})
    return filters


def get_instances(ec2, filters=None):
    kwargs = {"Filters": filters} if filters else {}
    try:
        response = ec2.describe_instances(**kwargs)
    except ClientError as e:
        print(f"[ERROR] AWS API error: {e}")
        sys.exit(1)

    instances = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            name = "—"
            for tag in instance.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]
            instances.append({
                "id": instance["InstanceId"],
                "name": name,
                "state": instance["State"]["Name"],
                "type": instance["InstanceType"],
            })
    return instances


def print_instances(instances):
    if not instances:
        print("  No instances found.")
        return
    print(f"\n  {'ID':<22} {'Name':<30} {'State':<12} {'Type'}")
    print(f"  {'-' * 75}")
    for i in instances:
        print(f"  {i['id']:<22} {i['name']:<30} {i['state']:<12} {i['type']}")


def stop_instances(ec2, instances, dry_run):
    targets = [i for i in instances if i["state"] == "running"]
    if not targets:
        print("  No running instances to stop.")
        return
    for i in targets:
        if dry_run:
            print(f"  [DRY RUN] Would stop: {i['id']} ({i['name']})")
        else:
            try:
                ec2.stop_instances(InstanceIds=[i["id"]])
                print(f"  [STOPPED] {i['id']} ({i['name']})")
            except ClientError as e:
                print(f"  [ERROR] Could not stop {i['id']}: {e}")


def start_instances(ec2, instances, dry_run):
    targets = [i for i in instances if i["state"] == "stopped"]
    if not targets:
        print("  No stopped instances to start.")
        return
    for i in targets:
        if dry_run:
            print(f"  [DRY RUN] Would start: {i['id']} ({i['name']})")
        else:
            try:
                ec2.start_instances(InstanceIds=[i["id"]])
                print(f"  [STARTED] {i['id']} ({i['name']})")
            except ClientError as e:
                print(f"  [ERROR] Could not start {i['id']}: {e}")


def main():
    args = parse_args()

    if not any([args.list, args.stop, args.start]):
        print("[ERROR] Specify at least one action: --list, --stop, --start")
        sys.exit(1)

    try:
        ec2 = boto3.client("ec2", region_name=args.region)
    except NoCredentialsError:
        print("[ERROR] No AWS credentials found. Run 'aws configure' or set environment variables.")
        sys.exit(1)

    filters = build_filters(state=args.state, tag=args.tag)
    instances = get_instances(ec2, filters)

    if args.list:
        print(f"Instances in {args.region}:")
        print_instances(instances)

    if args.stop:
        print(f"\nStopping instances{' (dry run)' if args.dry_run else ''}:")
        stop_instances(ec2, instances, dry_run=args.dry_run)

    if args.start:
        print(f"\nStarting instances{' (dry run)' if args.dry_run else ''}:")
        start_instances(ec2, instances, dry_run=args.dry_run)


if __name__ == "__main__":
    main()