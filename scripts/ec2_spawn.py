#!/usr/bin/env python3
"""
ec2_spawn.py — Launch multiple EC2 instances with specified tags and instance type.

Usage:
    python3 scripts/ec2_spawn.py --count 3 --tag Environment=dev --tag Project=lab
    python3 scripts/ec2_spawn.py --count 1 --type t3.micro --tag Environment=dev --dry-run
    python3 scripts/ec2_spawn.py --count 2 --region eu-west-1 --tag Environment=dev
"""

import argparse
import sys
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

DEFAULT_INSTANCE_TYPE = "t3.micro"
DEFAULT_REGION = "eu-west-1"


def parse_args():
    parser = argparse.ArgumentParser(description="Launch EC2 instances for lab use")
    parser.add_argument("--count", type=int, required=True, help="Number of instances to launch")
    parser.add_argument("--type", default=DEFAULT_INSTANCE_TYPE, help=f"EC2 instance type (default: {DEFAULT_INSTANCE_TYPE})")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})")
    parser.add_argument("--tag", action="append", metavar="KEY=VALUE", help="Tag in KEY=VALUE format. Repeatable.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be launched without launching")
    return parser.parse_args()


def parse_tags(raw_tags):
    if not raw_tags:
        return []
    tags = []
    for tag in raw_tags:
        if "=" not in tag:
            print(f"[WARN] Skipping invalid tag format (expected KEY=VALUE): {tag}")
            continue
        key, value = tag.split("=", 1)
        tags.append({"Key": key.strip(), "Value": value.strip()})
    return tags


def get_latest_amazon_linux_ami(ec2):
    try:
        response = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"Name": "state", "Values": ["available"]},
            ]
        )
        images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
        if not images:
            print("[ERROR] No Amazon Linux 2 AMI found in this region.")
            sys.exit(1)
        return images[0]["ImageId"]
    except ClientError as e:
        print(f"[ERROR] Could not look up AMI: {e}")
        sys.exit(1)


def launch_instances(ec2, count, instance_type, tags, ami_id, dry_run):
    if dry_run:
        print(f"\n[DRY RUN] Would launch {count}x {instance_type} instance(s)")
        print(f"[DRY RUN] AMI: {ami_id}")
        if tags:
            print(f"[DRY RUN] Tags: {tags}")
        return []

    tag_spec = []
    if tags:
        tag_spec = [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ]

    try:
        response = ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            MinCount=count,
            MaxCount=count,
            TagSpecifications=tag_spec if tag_spec else [],
        )
        return response["Instances"]
    except ClientError as e:
        print(f"[ERROR] Failed to launch instances: {e}")
        sys.exit(1)


def main():
    args = parse_args()

    if args.count < 1 or args.count > 10:
        print("[ERROR] --count must be between 1 and 10")
        sys.exit(1)

    tags = parse_tags(args.tag)

    if not tags:
        print("[WARN] No tags specified. Recommended to add at least Environment and Owner tags.")

    try:
        ec2 = boto3.client("ec2", region_name=args.region)
    except NoCredentialsError:
        print("[ERROR] No AWS credentials found. Run 'aws configure' or set environment variables.")
        sys.exit(1)

    print(f"\nRegion:        {args.region}")
    print(f"Instance type: {args.type}")
    print(f"Count:         {args.count}")
    print(f"Tags:          {tags if tags else 'none'}")

    print("\nLooking up latest Amazon Linux 2 AMI...")
    ami_id = get_latest_amazon_linux_ami(ec2)
    print(f"AMI:           {ami_id}")

    instances = launch_instances(
        ec2=ec2,
        count=args.count,
        instance_type=args.type,
        tags=tags,
        ami_id=ami_id,
        dry_run=args.dry_run
    )

    if args.dry_run:
        print("\n[DRY RUN] No instances were launched.")
        return

    print(f"\nLaunched {len(instances)} instance(s):")
    for i in instances:
        print(f"  {i['InstanceId']}  state: {i['State']['Name']}  type: {i['InstanceType']}")

    print("\nInstances are starting. Use ec2_manager.py --list to check status.")
    print("Remember to stop or terminate instances when done to avoid charges.")


if __name__ == "__main__":
    main()