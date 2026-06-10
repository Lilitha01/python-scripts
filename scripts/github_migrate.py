#!/usr/bin/env python3
"""
github_migrate.py — GitHub Organisation Repository Migration Tool

Moves repositories from a source GitHub organisation to a target organisation.

Two modes:
  transfer  — Uses GitHub's native transfer API. Repo moves to target; source org
              no longer holds it (a redirect stub remains on GitHub's side).
              Fast. Preserves issues, PRs, stars, releases, labels, milestones.

  clone     — Mirror-clone then push. Source stays completely intact.
              Use this for incremental migrations where you want to validate the
              target before teams switch over, then manually clean up the source.

Credentials are read from environment variables only — never from CLI args.
Required env var:
    GITHUB_TOKEN  — Classic PAT or fine-grained token (see README for required scopes).

Usage:
    python github_migrate.py --help
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("github_migrate")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"

# Pause between mutating API calls to avoid hitting GitHub's secondary rate
# limits. GitHub docs recommend at least 1s; 2s gives comfortable headroom.
RATE_LIMIT_SLEEP = 2

# GitHub's transfer API is asynchronous — the repo is queued for move and
# becomes available in the target org after some seconds (longer for large
# repos with many objects). We poll until it appears or we time out.
TRANSFER_POLL_INTERVAL = 10   # seconds between polls
TRANSFER_TIMEOUT = 300        # 5 minutes before we give up


# ---------------------------------------------------------------------------
# GitHub API client
# ---------------------------------------------------------------------------

class GitHubClient:
    """
    Minimal GitHub REST v3 client.

    Intentionally has no heavy dependencies beyond `requests`. Handles:
      - Auth via Bearer token in headers (token never appears in URLs or logs)
      - JSON request/response
      - Automatic retry on 429 / secondary-rate-limit 403 responses
      - RFC 5988 Link-header pagination
    """

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                # Pin the API version so behaviour doesn't change under us.
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        expected: tuple[int, ...] = (200,),
    ) -> Optional[dict | list]:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"

        for attempt in range(3):
            resp = self._session.request(method, url, json=json_body, params=params)

            # Primary and secondary rate limits both surface as 429 or 403.
            # Retry-After header tells us how long to wait; default to 60s.
            if resp.status_code in (429, 403) and "rate limit" in resp.text.lower():
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning("Rate limited — sleeping %ds (attempt %d/3)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue

            if resp.status_code in expected:
                # 204 No Content and empty bodies are valid success responses.
                if resp.status_code == 204 or not resp.content:
                    return None
                return resp.json()

            if attempt == 2:
                raise RuntimeError(
                    f"{method} {url} → HTTP {resp.status_code}: {resp.text[:400]}"
                )
            time.sleep(2 ** attempt)

        return None  # unreachable — loop always raises or returns above

    def get(self, path: str, *, params: Optional[dict] = None) -> dict | list | None:
        return self._request("GET", path, params=params, expected=(200, 404))

    def post(self, path: str, body: dict, *, expected: tuple[int, ...] = (201,)) -> dict | None:
        return self._request("POST", path, json_body=body, expected=expected)

    def patch(self, path: str, body: dict) -> dict | None:
        return self._request("PATCH", path, json_body=body, expected=(200,))

    def put(self, path: str, body: dict = {}) -> dict | None:
        return self._request("PUT", path, json_body=body, expected=(200, 201, 204))

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def paginate(self, path: str, *, params: Optional[dict] = None) -> list[dict]:
        """
        Fetch all pages of a list endpoint, following RFC 5988 Link headers.

        GitHub uses cursor-based pagination for some endpoints and offset for
        others — the Link header approach works for both.
        """
        results: list[dict] = []
        url: Optional[str] = f"{GITHUB_API}{path}"
        p = {"per_page": 100, **(params or {})}

        while url:
            resp = self._session.get(url, params=p)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                results.extend(data)
            else:
                # Some endpoints wrap results in a key (e.g. search returns
                # {"total_count": N, "items": [...]}). Take the first list value.
                for v in data.values():
                    if isinstance(v, list):
                        results.extend(v)
                        break

            # The next-page URL is embedded in the Link header, not a body field.
            link = resp.headers.get("Link", "")
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")

            p = {}  # params are baked into the next-page URL already

        return results

    # ------------------------------------------------------------------
    # Domain-level helpers
    # ------------------------------------------------------------------

    def get_org_repos(self, org: str) -> list[dict]:
        # type=all includes private, public, forks, and archived repos.
        return self.paginate(f"/orgs/{org}/repos", params={"type": "all"})

    def get_repo(self, org: str, repo: str) -> Optional[dict]:
        return self.get(f"/repos/{org}/{repo}")

    def get_outside_collaborators(self, org: str, repo: str) -> list[dict]:
        """
        Returns users who have direct repo access but are NOT org members.

        This is the set that doesn't carry over automatically on a transfer —
        GitHub only migrates org-member team access, not individual outside
        collaborator grants. Hence why this script re-invites them explicitly.
        """
        return self.paginate(
            f"/repos/{org}/{repo}/collaborators",
            params={"affiliation": "outside"},
        )

    def get_collaborator_permission(self, org: str, repo: str, username: str) -> str:
        """
        Returns the collaborator's effective permission on the repo.

        The response has both `permission` (legacy) and `role_name` (current).
        We prefer role_name as it's more granular (e.g. distinguishes 'maintain'
        from 'push', which both mapped to 'push' in the old model).
        """
        data = self.get(f"/repos/{org}/{repo}/collaborators/{username}/permission")
        if data and isinstance(data, dict):
            return data.get("role_name") or data.get("permission", "push")
        return "push"

    def transfer_repo(self, source_org: str, repo: str, target_org: str) -> dict:
        """
        Initiate a repo transfer. This is async — the response is 202 Accepted,
        not 200 OK. Callers must poll until the repo appears in the target org.
        """
        return self.post(
            f"/repos/{source_org}/{repo}/transfer",
            {"new_owner": target_org},
            expected=(202,),
        )

    def archive_repo(self, org: str, repo: str) -> dict:
        """
        Mark a repo as archived (read-only). Archived repos:
          - Cannot receive pushes, issues, PRs, or comments
          - Still visible and cloneable
          - Can be unarchived via the API or UI (but this script doesn't do that)

        NOTE: You cannot archive a repo that is already archived — the API
        returns 422. This is handled gracefully in migrate_repo().
        """
        return self.patch(f"/repos/{org}/{repo}", {"archived": True})

    def invite_outside_collaborator(
        self, org: str, repo: str, username: str, permission: str
    ) -> None:
        """
        Add or update a collaborator on a repo. This is idempotent — calling it
        again for an existing collaborator just updates their permission level.
        The user receives a GitHub invitation email and must accept it.
        """
        self.put(
            f"/repos/{org}/{repo}/collaborators/{username}",
            {"permission": permission},
        )

    def get_org_members(self, org: str) -> set[str]:
        """
        Returns the set of all member logins in the org.

        Used to skip collaborator invitations for people who are already org
        members in the target — they'll get access through team membership
        rather than needing an individual repo invite.
        """
        members = self.paginate(f"/orgs/{org}/members")
        return {m["login"] for m in members}

    def verify_token_scopes(self) -> list[str]:
        """
        Classic PATs advertise their scopes in X-OAuth-Scopes response headers.
        Fine-grained tokens don't — this will return an empty list for them,
        which is expected. We warn but don't fail.
        """
        resp = self._session.get(f"{GITHUB_API}/user")
        scopes = resp.headers.get("X-OAuth-Scopes", "")
        return [s.strip() for s in scopes.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RepoResult:
    """
    Captures the outcome of migrating a single repository.
    Collected into MigrationSummary and written to the output Markdown file.
    """
    repo: str
    # Possible values: "transferred" | "cloned" | "skipped" | "failed" | "dry-run"
    status: str
    # True if the repo was archived in the target org after migration.
    archived_in_target: bool = False
    collaborators_migrated: int = 0
    wiki_cloned: bool = False
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class MigrationSummary:
    """Accumulates results across all repos and renders the implementation summary."""
    started_at: str
    completed_at: str = ""
    source_org: str = ""
    target_org: str = ""
    mode: str = ""
    dry_run: bool = False
    archive: bool = False
    results: list[RepoResult] = field(default_factory=list)

    def to_markdown(self) -> str:
        transferred = [r for r in self.results if r.status == "transferred"]
        cloned      = [r for r in self.results if r.status == "cloned"]
        skipped     = [r for r in self.results if r.status == "skipped"]
        failed      = [r for r in self.results if r.status == "failed"]
        dry_run_r   = [r for r in self.results if r.status == "dry-run"]

        lines = [
            "# GitHub Repository Migration — Implementation Summary",
            "",
            f"**Generated:** {self.completed_at}",
            f"**Source org:** `{self.source_org}`",
            f"**Target org:** `{self.target_org}`",
            f"**Mode:** `{self.mode}`",
            f"**Dry run:** {'Yes ⚠️' if self.dry_run else 'No — changes were applied'}",
            f"**Archive in target:** {self.archive}",
            f"**Started:** {self.started_at}",
            f"**Completed:** {self.completed_at}",
            "",
            "---",
            "",
            "## Summary",
            "",
            "| Outcome | Count |",
            "|---------|-------|",
            f"| Transferred | {len(transferred)} |",
            f"| Cloned (incremental) | {len(cloned)} |",
            f"| Skipped (already exists) | {len(skipped)} |",
            f"| Failed | {len(failed)} |",
            f"| Dry-run (no changes) | {len(dry_run_r)} |",
            "",
            "---",
            "",
        ]

        def _repo_section(title: str, items: list[RepoResult]) -> list[str]:
            if not items:
                return []
            out = [f"## {title}", ""]
            for r in items:
                out.append(f"### `{r.repo}`")
                out.append(f"- **Status:** {r.status}")
                if r.archived_in_target:
                    out.append(f"- **Archived in `{self.target_org}`:** Yes")
                if r.collaborators_migrated:
                    out.append(f"- **Outside collaborators migrated:** {r.collaborators_migrated}")
                if r.wiki_cloned:
                    out.append("- **Wiki cloned:** Yes")
                for n in r.notes:
                    out.append(f"- {n}")
                if r.error:
                    out.append(f"- ⚠️ **Error:** {r.error}")
                out.append("")
            return out

        lines.extend(_repo_section("Transferred Repositories", transferred + dry_run_r))
        lines.extend(_repo_section("Cloned Repositories", cloned))
        lines.extend(_repo_section("Skipped (already in target)", skipped))
        lines.extend(_repo_section("Failed", failed))

        lines += [
            "---",
            "",
            "## Post-Migration Checklist",
            "",
            "- [ ] Verify CI/CD pipelines are pointing at the new repo URLs",
            "- [ ] Update hardcoded `github.com/SOURCE_ORG/...` references in code and docs",
            "- [ ] Re-configure branch protection rules in the target org (not transferred by GitHub)",
            "- [ ] Rotate deploy keys and repo-scoped secrets (keys are not transferred)",
            "- [ ] Verify outside collaborators have accepted their invitations",
            "- [ ] Reconfigure webhooks on target repos (webhooks are not transferred)",
            "- [ ] If using clone mode: delete source repos once teams have confirmed cutover",
            "- [ ] If source org is being dissolved: revoke any remaining org-level tokens",
            "",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def run_git(args: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """
    Run a git subprocess, masking any embedded token from log output.

    Tokens appear in clone URLs as https://x-access-token:TOKEN@github.com/...
    We strip everything up to and including the @ before logging.
    """
    safe_args = [
        a if "://" not in a else a.split("@")[-1]
        for a in args
    ]
    log.debug("git %s  (cwd=%s)", " ".join(safe_args[1:]), cwd)
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {safe_args[1]} failed:\n{result.stderr.strip()[:400]}"
        )
    return result


def mirror_clone(url_with_token: str, dest: Path) -> None:
    """
    Bare mirror clone — fetches all refs (branches, tags, notes, stash, etc.).
    `--mirror` implies `--bare`, so the result has no working tree.
    """
    run_git(["git", "clone", "--mirror", url_with_token, str(dest)])


def mirror_push(bare_dir: Path, target_url: str) -> None:
    """
    Push all refs from a bare mirror clone to a remote.

    `git push --mirror` pushes everything and also deletes remote refs that no
    longer exist locally, making the target an exact replica. This is what we
    want for a complete migration but be aware of this if using it for partial syncs.
    """
    run_git(["git", "remote", "set-url", "origin", target_url], cwd=bare_dir)
    run_git(["git", "push", "--mirror"], cwd=bare_dir)


def try_clone_wiki(wiki_url: str, dest: Path) -> bool:
    """
    Attempt to clone a wiki repo. Returns False silently if the wiki is empty
    or disabled — GitHub creates the wiki endpoint but the git repo only exists
    once someone has added content, so a 128 exit from git is expected and normal.
    """
    try:
        run_git(["git", "clone", "--mirror", wiki_url, str(dest)])
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# Core migration logic
# ---------------------------------------------------------------------------

class Migrator:
    """
    Orchestrates the migration of individual repositories.

    Holds configuration for the full run so individual migrate_repo() calls
    don't need to pass it all through — keeps the per-repo call site clean.
    """

    def __init__(
        self,
        client: GitHubClient,
        source_org: str,
        target_org: str,
        mode: str,
        archive: bool,
        do_wiki: bool,
        dry_run: bool,
        token: str,
    ) -> None:
        self.client = client
        self.source_org = source_org
        self.target_org = target_org
        self.mode = mode
        self.archive = archive
        self.do_wiki = do_wiki
        self.dry_run = dry_run
        self._token = token  # kept private — only used to build clone URLs, never logged

        # Pre-fetch target org members once for the whole run rather than once
        # per repo. This avoids N API calls for N repos when checking collaborators.
        # We fetch lazily on first use and cache the result.
        self._target_members: Optional[set[str]] = None

    def _get_target_members(self) -> set[str]:
        """Lazily fetch and cache target org members for the duration of the run."""
        if self._target_members is None:
            log.info("Fetching target org members (cached for run) …")
            self._target_members = self.client.get_org_members(self.target_org)
            log.info("  → %d members in %s", len(self._target_members), self.target_org)
        return self._target_members

    def _clone_url(self, org: str, repo: str) -> str:
        # Token embedded in URL — only passed to git subprocess, never logged.
        return f"https://x-access-token:{self._token}@github.com/{org}/{repo}.git"

    def _wiki_url(self, org: str, repo: str) -> str:
        return f"https://x-access-token:{self._token}@github.com/{org}/{repo}.wiki.git"

    def _wait_for_transfer(self, repo: str) -> bool:
        """
        Poll the target org until the repo appears there.

        GitHub's transfer API queues the operation and returns 202 immediately.
        The actual move can take anywhere from a few seconds to several minutes
        depending on repo size and GitHub's queue depth. We poll until
        TRANSFER_TIMEOUT is reached.
        """
        deadline = time.monotonic() + TRANSFER_TIMEOUT
        while time.monotonic() < deadline:
            r = self.client.get_repo(self.target_org, repo)
            if r and not r.get("message"):
                log.info("  ✓ Transfer confirmed: %s/%s", self.target_org, repo)
                return True
            log.debug("  … waiting for transfer to complete")
            time.sleep(TRANSFER_POLL_INTERVAL)
        log.error("  Transfer timed out after %ds for %s", TRANSFER_TIMEOUT, repo)
        return False

    def _migrate_collaborators(self, repo: str, result: RepoResult) -> None:
        """
        Re-invite outside collaborators from the source repo to the target repo.

        Context: GitHub's transfer API only carries over team access for org members.
        Outside collaborators — people with direct repo access who are not org members
        — must be explicitly re-invited. This is the gap that typically gets missed
        in manual migrations and results in access complaints post-cutover.

        We preserve the permission level each collaborator had on the source. If they
        are already an org member in the target, we skip the invite (they'll get access
        via team membership instead).
        """
        log.info("  → Checking outside collaborators for %s", repo)
        collaborators = self.client.get_outside_collaborators(self.source_org, repo)

        if not collaborators:
            log.info("  → No outside collaborators found")
            return

        target_members = self._get_target_members()

        for collab in collaborators:
            username = collab["login"]

            if username in target_members:
                log.info(
                    "  → %s is already a member of %s — skipping invitation",
                    username, self.target_org,
                )
                result.notes.append(
                    f"Collaborator `{username}` is an org member of target — "
                    "access via team membership, no invite needed"
                )
                continue

            permission = self.client.get_collaborator_permission(
                self.source_org, repo, username
            )
            log.info(
                "  → Inviting outside collaborator %s with permission '%s'",
                username, permission,
            )

            if self.dry_run:
                result.notes.append(
                    f"[DRY RUN] Would invite outside collaborator `{username}` ({permission})"
                )
            else:
                try:
                    self.client.invite_outside_collaborator(
                        self.target_org, repo, username, permission
                    )
                    result.collaborators_migrated += 1
                    result.notes.append(
                        f"Invited outside collaborator `{username}` ({permission})"
                    )
                    time.sleep(RATE_LIMIT_SLEEP)
                except RuntimeError as exc:
                    msg = f"Failed to invite {username}: {exc}"
                    log.warning("  ⚠ %s", msg)
                    result.notes.append(f"⚠️ {msg}")

    def _archive_in_target(self, repo: str, result: RepoResult) -> None:
        """
        Archive the repo in the target org, making it read-only.

        Called after a successful transfer or clone-push. The repo now lives
        in the target org and gets frozen there — consistent with the use case
        of migrating to a dedicated archive org when the source org is being
        dissolved.
        """
        log.info("  → Archiving %s/%s (marking read-only in target)", self.target_org, repo)
        try:
            self.client.archive_repo(self.target_org, repo)
            result.archived_in_target = True
            result.notes.append(f"Archived in `{self.target_org}` — repo is now read-only")
        except RuntimeError as exc:
            # 422 means it's already archived — not a real error in our context.
            if "422" in str(exc):
                log.info("  → Already archived — skipping")
                result.notes.append("Already archived in target — no change needed")
            else:
                raise

    # ------------------------------------------------------------------
    # Per-repo entry point
    # ------------------------------------------------------------------

    def migrate_repo(self, repo: str) -> RepoResult:
        """
        Migrate a single repository. Returns a RepoResult capturing the outcome.

        The two main modes share the collaborator migration and archive steps
        but differ in how the git content moves:
          - transfer: GitHub API moves ownership; no git operations needed
          - clone: we do the git work (mirror clone + push) ourselves

        Idempotency:
          - transfer mode: if the repo already exists in the target, skip
          - clone mode: re-push is safe (mirror push is deterministic); we continue
            so re-runs after partial failures pick up where they left off
        """
        result = RepoResult(
            repo=repo,
            status="dry-run" if self.dry_run else "failed",
        )

        log.info("Processing: %s/%s", self.source_org, repo)

        # ── Validate source ────────────────────────────────────────────
        src = self.client.get_repo(self.source_org, repo)
        if not src or src.get("message") == "Not Found":
            result.status = "failed"
            result.error = f"Repo not found in source org: {self.source_org}/{repo}"
            log.error("  ✗ %s", result.error)
            return result

        # ── Idempotency check (transfer mode only) ─────────────────────
        if self.mode == "transfer":
            existing = self.client.get_repo(self.target_org, repo)
            if existing and not existing.get("message"):
                log.info(
                    "  → %s/%s already exists in target — skipping (idempotent)",
                    self.target_org, repo,
                )
                result.status = "skipped"
                result.notes.append(
                    "Already present in target org — no action taken. "
                    "If this is unexpected, verify the transfer completed correctly."
                )
                return result

        # ── TRANSFER MODE ──────────────────────────────────────────────
        if self.mode == "transfer":
            # Collaborators must be migrated before transfer — once the repo
            # moves, the source endpoint returns 404 and we lose the list.
            self._migrate_collaborators(repo, result)

            if self.dry_run:
                result.notes.append(
                    f"[DRY RUN] Would transfer to `{self.target_org}` via POST /repos/.../transfer"
                )
                if self.archive:
                    result.notes.append(
                        f"[DRY RUN] Would archive in `{self.target_org}` after transfer"
                    )
                log.info("  [DRY RUN] Would transfer %s → %s", repo, self.target_org)
                return result

            log.info("  → Initiating transfer to %s", self.target_org)
            self.client.transfer_repo(self.source_org, repo, self.target_org)
            time.sleep(RATE_LIMIT_SLEEP)

            if not self._wait_for_transfer(repo):
                result.status = "failed"
                result.error = (
                    f"Transfer did not complete within {TRANSFER_TIMEOUT}s. "
                    "Re-run the script — if the repo now exists in the target it will be skipped."
                )
                return result

            result.status = "transferred"

            if self.archive:
                self._archive_in_target(repo, result)

        # ── CLONE MODE ─────────────────────────────────────────────────
        elif self.mode == "clone":
            # tempfile.TemporaryDirectory cleans up automatically on exit,
            # even if an exception is raised — no leftover bare repos on disk.
            with tempfile.TemporaryDirectory(prefix="gh_migrate_") as tmpdir:
                tmp = Path(tmpdir)
                bare_dir = tmp / f"{repo}.git"
                wiki_dir = tmp / f"{repo}.wiki.git"

                if not self.dry_run:
                    log.info("  → Mirror-cloning source repo")
                    mirror_clone(self._clone_url(self.source_org, repo), bare_dir)
                else:
                    result.notes.append(
                        f"[DRY RUN] Would mirror-clone `{self.source_org}/{repo}`"
                    )

                # Wikis are a separate git repo at {repo}.wiki.git — they are NOT
                # included in a regular clone and must be handled explicitly.
                if self.do_wiki and src.get("has_wiki"):
                    log.info("  → Attempting wiki clone")
                    if not self.dry_run:
                        cloned = try_clone_wiki(
                            self._wiki_url(self.source_org, repo), wiki_dir
                        )
                        result.wiki_cloned = cloned
                        if not cloned:
                            result.notes.append(
                                "Wiki is enabled on this repo but git clone returned no content "
                                "(wiki exists as a feature but no pages have been created yet)"
                            )
                    else:
                        result.notes.append("[DRY RUN] Would clone wiki")

                if not self.dry_run:
                    # Create the target repo if it doesn't already exist.
                    # We mirror source metadata (private flag, description, wiki toggle)
                    # but do NOT copy topics, branch protection rules, webhooks, or secrets.
                    target_exists = self.client.get_repo(self.target_org, repo)
                    if not target_exists or target_exists.get("message"):
                        log.info("  → Creating target repo %s/%s", self.target_org, repo)
                        self.client.post(
                            f"/orgs/{self.target_org}/repos",
                            {
                                "name": repo,
                                "private": src.get("private", True),
                                "description": src.get("description") or "",
                                "has_wiki": src.get("has_wiki", False),
                            },
                        )
                        # Brief pause — GitHub needs a moment to fully initialise
                        # the repo before we can push into it.
                        time.sleep(RATE_LIMIT_SLEEP)

                    log.info("  → Mirror-pushing to target")
                    mirror_push(bare_dir, self._clone_url(self.target_org, repo))

                    if result.wiki_cloned:
                        log.info("  → Pushing wiki to target")
                        mirror_push(wiki_dir, self._wiki_url(self.target_org, repo))

                self._migrate_collaborators(repo, result)
                result.status = "cloned" if not self.dry_run else "dry-run"

                if self.archive:
                    if not self.dry_run:
                        self._archive_in_target(repo, result)
                    else:
                        result.notes.append(
                            f"[DRY RUN] Would archive `{self.target_org}/{repo}` after clone"
                        )

        log.info("  ✓ Done: %s (%s)", repo, result.status)
        return result


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def resolve_repo_list(
    client: GitHubClient, source_org: str, repos_arg: list[str]
) -> list[str]:
    """
    Resolve the list of repos to migrate.

    If no repos are specified (or the user passes 'ALL'), fetch every repo in
    the source org. The API returns up to 100 per page; paginate() handles it.
    """
    if not repos_arg or repos_arg == ["ALL"]:
        log.info("No repos specified — fetching all repos in %s …", source_org)
        all_repos = client.get_org_repos(source_org)
        names = [r["name"] for r in all_repos]
        log.info("Found %d repos in %s", len(names), source_org)
        return names
    return repos_arg


def confirm(prompt: str) -> bool:
    """
    Prompt for explicit confirmation before a destructive action.

    Requires the user to type 'YES' exactly — not 'y', not 'yes', not Enter.
    This is intentional: it forces a conscious choice and prevents accidents
    from muscle memory or copy-paste.
    """
    print(f"\n⚠️  {prompt}")
    answer = input("   Type YES to confirm, anything else to abort: ").strip()
    return answer == "YES"


def write_summary(summary: MigrationSummary, output_path: Path) -> None:
    output_path.write_text(summary.to_markdown(), encoding="utf-8")
    log.info("Implementation summary written to: %s", output_path)


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Migrate GitHub repositories between organisations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Token permissions required (set GITHUB_TOKEN env var — never pass as CLI arg):
  Classic PAT:      repo (full), admin:org
  Fine-grained PAT: Contents (R/W), Metadata (R), Members (R) on both orgs

Examples:

  # Dry-run: see what would happen for ALL repos in the org
  GITHUB_TOKEN=ghp_... python github_migrate.py \\
      --source dissolving-org --target archive-org --dry-run

  # Transfer specific repos (no archiving — just move them)
  GITHUB_TOKEN=ghp_... python github_migrate.py \\
      --source dissolving-org --target archive-org \\
      --repos service-a service-b

  # Transfer ALL repos and archive them in the target (shelved product use case)
  GITHUB_TOKEN=ghp_... python github_migrate.py \\
      --source dissolving-org --target archive-org \\
      --archive

  # Incremental clone: copy repos over but leave source intact
  GITHUB_TOKEN=ghp_... python github_migrate.py \\
      --source old-org --target new-org \\
      --mode clone --clone-wiki
""",
    )

    p.add_argument(
        "--source",
        required=True,
        metavar="ORG",
        help="Source GitHub organisation slug",
    )
    p.add_argument(
        "--target",
        required=True,
        metavar="ORG",
        help="Target GitHub organisation slug",
    )
    p.add_argument(
        "--repos",
        nargs="+",
        metavar="REPO",
        default=[],
        help=(
            "Space-separated repo names to migrate. "
            "Omit entirely (or pass ALL) to migrate every repo in the source org."
        ),
    )
    p.add_argument(
        "--mode",
        choices=["transfer", "clone"],
        default="transfer",
        help=(
            "transfer: GitHub's transfer API — fast, preserves issues/PRs/stars/releases. "
            "Repo leaves the source org. "
            "clone: mirror-clone + push — source stays intact, use for incremental migration "
            "with manual cleanup later. "
            "Default: transfer"
        ),
    )
    p.add_argument(
        "--archive",
        action="store_true",
        help=(
            "Archive repos in the TARGET org after migration, making them read-only. "
            "Use this when migrating to a dedicated archive org (e.g. a dissolved team's "
            "repos being shelved). Requires explicit YES confirmation."
        ),
    )
    p.add_argument(
        "--clone-wiki",
        action="store_true",
        help="Also clone/push the wiki repo alongside the main repo (clone mode).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making any changes to GitHub.",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        default="migration_summary.md",
        help="Path for the Markdown implementation summary. Default: migration_summary.md",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging (includes git command details).",
    )
    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # ── Token hygiene ────────────────────────────────────────────────
    # Read from env var only. Never accept it as a CLI argument — CLI args
    # land in shell history, ps output, and process environment dumps.
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        log.error(
            "GITHUB_TOKEN environment variable is not set.\n"
            "  Export it before running:\n"
            "    export GITHUB_TOKEN=ghp_yourTokenHere\n"
            "  Or load from a secrets manager:\n"
            "    export GITHUB_TOKEN=$(op read 'op://vault/github/token')"
        )
        return 1

    client = GitHubClient(token)

    # ── Token scope check ────────────────────────────────────────────
    # Classic PATs report scopes; fine-grained tokens don't (returns empty).
    # We warn if required scopes appear missing but don't hard-fail — the
    # first API error will surface the problem clearly enough.
    scopes = client.verify_token_scopes()
    if scopes:
        log.info("Token scopes: %s", ", ".join(scopes))
        required = {"repo", "admin:org"}
        missing = required - set(scopes)
        if missing:
            log.warning(
                "Token may be missing required scopes: %s. "
                "Collaborator migration and org-member checks require 'admin:org'.",
                ", ".join(missing),
            )
    else:
        log.info(
            "No scopes reported — using a fine-grained token or GitHub Apps token. "
            "Ensure it has Contents (R/W) and Members (R) permissions on both orgs."
        )

    # ── Resolve repo list ────────────────────────────────────────────
    repos = resolve_repo_list(client, args.source, args.repos)
    if not repos:
        log.error("No repos found to migrate in %s.", args.source)
        return 1

    # ── Print plan ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  GitHub Repository Migration Plan")
    print("=" * 60)
    print(f"  Source org  : {args.source}")
    print(f"  Target org  : {args.target}")
    print(f"  Mode        : {args.mode}")
    print(f"  Repos       : {len(repos)} repo(s)")
    print(f"  Archive     : {args.archive} (in target org)")
    print(f"  Clone wiki  : {args.clone_wiki}")
    print(f"  Dry run     : {args.dry_run}")
    print(f"  Output file : {args.output}")
    print("=" * 60)
    print()

    # ── Confirmations for destructive actions ────────────────────────
    # We ask for separate YES for the migration action and the archive action.
    # Two confirms for two irreversible things. No bundling.
    if not args.dry_run:
        if args.mode == "transfer":
            if not confirm(
                f"You are about to TRANSFER {len(repos)} repo(s) from "
                f"'{args.source}' to '{args.target}'.\n"
                "  Repos will leave the source org. GitHub leaves a redirect stub "
                "but automations that don't follow redirects will break."
            ):
                print("Aborted.")
                return 0

        elif args.mode == "clone":
            if not confirm(
                f"You are about to CLONE {len(repos)} repo(s) from "
                f"'{args.source}' to '{args.target}'.\n"
                "  Repos will be created/overwritten in the target org. "
                "Source repos remain untouched."
            ):
                print("Aborted.")
                return 0

        if args.archive:
            if not confirm(
                f"You have requested to ARCHIVE repos in '{args.target}' after migration.\n"
                "  Archived repos become permanently read-only. "
                "This is intended for shelved/dissolved products migrated to an archive org."
            ):
                print("Aborted.")
                return 0
    else:
        print("DRY RUN — no changes will be made to GitHub.\n")

    # ── Run migration ────────────────────────────────────────────────
    started_at = datetime.now(timezone.utc).isoformat()
    summary = MigrationSummary(
        started_at=started_at,
        source_org=args.source,
        target_org=args.target,
        mode=args.mode,
        dry_run=args.dry_run,
        archive=args.archive,
    )

    migrator = Migrator(
        client=client,
        source_org=args.source,
        target_org=args.target,
        mode=args.mode,
        archive=args.archive,
        do_wiki=args.clone_wiki,
        dry_run=args.dry_run,
        token=token,
    )

    for i, repo in enumerate(repos, 1):
        log.info("[%d/%d] %s", i, len(repos), repo)
        try:
            result = migrator.migrate_repo(repo)
        except Exception as exc:
            # Catch-all so one bad repo doesn't abort the rest of the run.
            # The individual repo is marked failed; migration continues.
            log.error("  ✗ Unhandled error for %s: %s", repo, exc)
            result = RepoResult(
                repo=repo,
                status="failed",
                error=str(exc),
            )
        summary.results.append(result)
        if not args.dry_run:
            time.sleep(RATE_LIMIT_SLEEP)

    # ── Write summary ────────────────────────────────────────────────
    summary.completed_at = datetime.now(timezone.utc).isoformat()
    output_path = Path(args.output)
    write_summary(summary, output_path)

    # ── Final counts ─────────────────────────────────────────────────
    ok  = sum(1 for r in summary.results if r.status in ("transferred", "cloned", "dry-run", "skipped"))
    err = sum(1 for r in summary.results if r.status == "failed")
    print("\n" + "=" * 60)
    print(f"  Migration complete — {ok} succeeded, {err} failed")
    print(f"  Summary: {output_path.resolve()}")
    print("=" * 60 + "\n")

    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())