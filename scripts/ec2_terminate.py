#!/usr/bin/env python3
"""
ec2_terminate.py — Terminate EC2 instances by tag or explicit instance ID.

Terminate is PERMANENT and IRREVERSIBLE. Instances cannot be recovered after termination.
Use ec2_manager.py --stop if you just want to pause instances.

Usage:
    python3 scripts/ec2_terminate.py --tag Environment=dev --dry-run
    python3 scripts/ec2_terminate.py --tag Environment=dev
    python3 scripts/ec2_terminate.py --id i-1234567890abcdef0
    python3 scripts/ec2_terminate.py --id i-1234567890abcdef0 --id i-0987654321fedcba0
"""

import argparse
import sys
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

DEFAULT_REGION = "eu-west-1"
SKIP_STATES = {"terminated", "shutting-down"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Terminate EC2 instances. PERMANENT — cannot be undone.",
        epilog="WARNING: Termination is irreversible. Always dry-run first."
    )
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})")
    parser.add_argument("--tag", action="append", metavar="KEY=VALUE", help="Target instances with this tag. Repeatable.")
    parser.add_argument("--id", action="append", metavar="INSTANCE_ID", dest="ids", help="Target a specific instance ID. Repeatable.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be terminated without terminating")
    return parser.parse_args()


def parse_tag_filters(raw_tags):
    if not raw_tags:
        return []
    filters = []
    for tag in raw_tags:
        if "=" not in tag:
            print(f"[WARN] Skipping invalid tag format (expected KEY=VALUE): {tag}")
            continue
        key, value = tag.split("=", 1)
        filters.append({"Name": f"tag:{key.strip()}", "Values": [value.strip()]})
    return filters


def get_instances_by_tag(ec2, filters):
    try:
        response = ec2.describe_instances(Filters=filters)
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


def get_instances_by_id(ec2, instance_ids):
    try:
        response = ec2.describe_instances(InstanceIds=instance_ids)
    except ClientError as e:
        print(f"[ERROR] AWS API error (one or more instance IDs may not exist): {e}")
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


def confirm_termination(instances):
    print("\nThe following instances will be PERMANENTLY TERMINATED:\n")
    for i in instances:
        print(f"  {i['id']:<22} {i['name']:<30} {i['state']:<12} {i['type']}")

    print(f"\n  Total: {len(instances)} instance(s)")
    print("\nThis cannot be undone. Use ec2_manager.py --stop to pause instead.")

    response = input('\nType "terminate" to confirm, anything else to cancel: ').strip()
    return response == "terminate"


def terminate(ec2, instances, dry_run):
    targets = [i for i in instances if i["state"] not in SKIP_STATES]

    skipped = len(instances) - len(targets)
    if skipped:
        print(f"\n[INFO] Skipping {skipped} instance(s) already terminated or shutting down.")

    if not targets:
        print("  No eligible instances to terminate.")
        return

    if dry_run:
        print(f"\n[DRY RUN] Would terminate {len(targets)} instance(s):")
        for i in targets:
            print(f"  [DRY RUN] Would terminate: {i['id']} ({i['name']}) — currently {i['state']}")
        return

    confirmed = confirm_termination(targets)
    if not confirmed:
        print("\nCancelled. No instances were terminated.")
        return

    print()
    for i in targets:
        try:
            ec2.terminate_instances(InstanceIds=[i["id"]])
            print(f"  [TERMINATED] {i['id']} ({i['name']})")
        except ClientError as e:
            print(f"  [ERROR] Could not terminate {i['id']}: {e}")


def main():
    args = parse_args()

    if not args.tag and not args.ids:
        print("[ERROR] Specify at least one --tag or --id to target instances.")
        print("        This script will not terminate all instances in a region.")
        sys.exit(1)

    try:
        ec2 = boto3.client("ec2", region_name=args.region)
    except NoCredentialsError:
        print("[ERROR] No AWS credentials found. Run 'aws configure' or set environment variables.")
        sys.exit(1)

    if args.ids:
        instances = get_instances_by_id(ec2, args.ids)
    else:
        filters = parse_tag_filters(args.tag)
        instances = get_instances_by_tag(ec2, filters)

    if not instances:
        print("  No instances found matching the given criteria.")
        return

    terminate(ec2, instances, dry_run=args.dry_run)


if __name__ == "__main__":
    main()