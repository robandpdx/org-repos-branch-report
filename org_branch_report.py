#!/usr/bin/env python3
"""Generate a CSV report of every repository in a GitHub organization and its branches.

Authentication is done with the ``GITHUB_TOKEN`` environment variable.
All data is collected with the GitHub GraphQL API to keep the number of
requests (and the rate limit cost) as low as possible.
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

DEFAULT_API_URL = "https://api.github.com"
REPO_PAGE_SIZE = 50
BRANCH_PAGE_SIZE = 50
COMPARE_BATCH_SIZE = 25
MAX_RETRIES = 6
REQUEST_TIMEOUT = 120
# Wait for the rate limit to reset when fewer points than this remain.
RATE_LIMIT_BUFFER = 25
PUSH_WINDOWS_DAYS = (30, 60, 90)
PERMISSION_ERROR_TYPES = {"FORBIDDEN", "INSUFFICIENT_SCOPES", "UNAUTHORIZED"}
LIST_SEPARATOR = ";"

log = logging.getLogger("org_branch_report")

CSV_COLUMNS = [
    "repository",
    "repo_created_at",
    "repo_last_push_at",
    "repo_open_prs",
    "repo_branches_total",
    "repo_branches_pushed_last_30_days",
    "repo_branches_pushed_last_60_days",
    "repo_branches_pushed_last_90_days",
    "default_branch",
    "branch",
    "is_default_branch",
    "branch_created_at",
    "branch_last_push_at",
    "branch_has_pr",
    "commits_behind_default",
    "commits_ahead_default",
    "branch_merged",
    "pr_number",
    "pr_url",
    "pr_state",
    "pr_created_at",
    "pr_opened_by",
    "pr_contributors",
    "pr_last_activity_at",
    "pr_checks_passing",
    "pr_last_workflow_run_at",
    "pr_reviewers_requested",
    "pr_approvals_received",
]

RATE_LIMIT_FIELDS = "rateLimit { cost remaining resetAt }"

REPOS_QUERY = """
query($org: String!, $pageSize: Int!, $cursor: String) {
  %s
  organization(login: $org) {
    repositories(first: $pageSize, after: $cursor, orderBy: {field: NAME, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        createdAt
        pushedAt
        defaultBranchRef { name }
        pullRequests(states: OPEN) { totalCount }
      }
    }
  }
}
""" % RATE_LIMIT_FIELDS

BRANCHES_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
  %s
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: $pageSize, after: $cursor,
         orderBy: {field: ALPHABETICAL, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target { ... on Commit { committedDate } }
        associatedPullRequests(first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes { ...PullRequestDetails }
        }
      }
    }
  }
}

fragment Reviewer on RequestedReviewer {
  ... on User { login }
  ... on Team { slug }
  ... on Mannequin { login }
}

fragment PullRequestDetails on PullRequest {
  number
  url
  state
  isDraft
  createdAt
  updatedAt
  author { login }
  participants(first: 100) { nodes { login } }
  timelineItems(first: 100, itemTypes: [REVIEW_REQUESTED_EVENT, REVIEW_REQUEST_REMOVED_EVENT]) {
    nodes {
      __typename
      ... on ReviewRequestedEvent { requestedReviewer { ...Reviewer } }
      ... on ReviewRequestRemovedEvent { requestedReviewer { ...Reviewer } }
    }
  }
  latestOpinionatedReviews(first: 100) { nodes { state author { login } } }
  commits(last: 1) {
    nodes {
      commit {
        statusCheckRollup { state }
        checkSuites(first: 50) { nodes { workflowRun { updatedAt } } }
      }
    }
  }
}
""" % RATE_LIMIT_FIELDS


class PermissionDenied(Exception):
    """The token is missing or lacks the permissions needed for the report."""


class GitHubAPIError(Exception):
    """A non-recoverable error returned by the GitHub API."""


def _parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_compare_query(count):
    """Build a query comparing the default branch (base) with ``count`` head branches."""
    variables = "".join(f", $h{i}: String!" for i in range(count))
    fields = "\n".join(
        f"      c{i}: compare(headRef: $h{i}) {{ aheadBy behindBy "
        f"commits(first: 100) {{ nodes {{ committedDate }} }} }}"
        for i in range(count)
    )
    return (
        f"query($owner: String!, $name: String!, $base: String!{variables}) {{\n"
        f"  {RATE_LIMIT_FIELDS}\n"
        f"  repository(owner: $owner, name: $name) {{\n"
        f"    ref(qualifiedName: $base) {{\n{fields}\n    }}\n"
        f"  }}\n"
        f"}}"
    )


class GitHubClient:
    """Minimal GraphQL client with retry, rate limit and permission handling."""

    def __init__(self, token, api_url=DEFAULT_API_URL, session=None, sleep=time.sleep):
        base = api_url.rstrip("/")
        if base.endswith("/api/v3"):
            # GitHub Enterprise Server REST URL; GraphQL is served from /api/graphql.
            base = base[: -len("/v3")]
        self.url = base + "/graphql"
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "org-repos-branch-report",
            }
        )
        self.sleep = sleep
        self.remaining = None
        self.reset_at = None
        self.total_cost = 0
        self.token_scopes = None

    def _wait_until(self, reset_at, reason):
        delay = max(0.0, (reset_at - datetime.now(timezone.utc)).total_seconds()) + 1
        log.warning("%s; waiting %d seconds until %s", reason, delay, reset_at.isoformat())
        self.sleep(delay)

    def _reset_from_headers(self, response):
        reset = response.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return datetime.fromtimestamp(int(reset), tz=timezone.utc)
        return None

    def _wait_for_rate_limit(self, response, attempt):
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            log.warning("Secondary rate limit hit; waiting %s seconds", retry_after)
            self.sleep(int(retry_after) + 1)
            return
        reset_at = self._reset_from_headers(response)
        if response.headers.get("x-ratelimit-remaining") == "0" and reset_at:
            self._wait_until(reset_at, "Rate limit exhausted")
            return
        # Secondary rate limit without a hint: back off exponentially (at least a minute).
        delay = 60 * (2 ** attempt)
        log.warning("Secondary rate limit hit; waiting %d seconds", delay)
        self.sleep(delay)

    @staticmethod
    def _is_rate_limited(response):
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get("retry-after"):
            return True
        return "rate limit" in response.text.lower()

    def query(self, query, variables=None):
        """Run a GraphQL query and return ``(data, errors)``.

        Raises :class:`PermissionDenied` for authentication/authorization problems and
        :class:`GitHubAPIError` when the request cannot be completed after retries.
        Partial errors (``data`` returned together with ``errors``) are returned to the
        caller so it can decide how to handle them.
        """
        if self.remaining is not None and self.reset_at and self.remaining < RATE_LIMIT_BUFFER:
            self._wait_until(self.reset_at, f"Only {self.remaining} rate limit points left")
            self.remaining = None

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.post(
                    self.url, json={"query": query, "variables": variables or {}}, timeout=REQUEST_TIMEOUT
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"network error: {exc}"
                log.warning("%s (attempt %d/%d)", last_error, attempt + 1, MAX_RETRIES)
                self.sleep(2 ** attempt)
                continue

            if self.token_scopes is None and "x-oauth-scopes" in response.headers:
                self.token_scopes = {
                    s.strip() for s in response.headers["x-oauth-scopes"].split(",") if s.strip()
                }

            if response.status_code == 401:
                raise PermissionDenied("GitHub rejected the token (401 Bad credentials). Check GITHUB_TOKEN.")
            if self._is_rate_limited(response):
                last_error = "rate limited"
                self._wait_for_rate_limit(response, attempt)
                continue
            if response.status_code == 403:
                raise PermissionDenied(f"Access forbidden (403): {_error_message(response)}")
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                log.warning("GitHub returned %s (attempt %d/%d)", last_error, attempt + 1, MAX_RETRIES)
                self.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise GitHubAPIError(f"HTTP {response.status_code}: {_error_message(response)}")

            try:
                body = response.json()
            except ValueError:
                last_error = "invalid JSON response"
                self.sleep(2 ** attempt)
                continue

            errors = body.get("errors") or []
            data = body.get("data")

            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                last_error = "rate limited"
                reset_at = self._reset_from_headers(response) or (
                    datetime.now(timezone.utc) + timedelta(seconds=60 * (2 ** attempt))
                )
                self._wait_until(reset_at, "GraphQL rate limit exhausted")
                continue

            for error in errors:
                message = error.get("message", "")
                if error.get("type") in PERMISSION_ERROR_TYPES or "SAML" in message:
                    raise PermissionDenied(message or error.get("type"))

            if data is None:
                last_error = "; ".join(e.get("message", str(e)) for e in errors) or "empty response"
                # Timeouts on GitHub's side are transient; anything else is not.
                if any("timeout" in m.lower() or "something went wrong" in m.lower()
                       for m in (e.get("message", "") for e in errors)):
                    log.warning("GitHub query failed: %s (attempt %d/%d)", last_error, attempt + 1, MAX_RETRIES)
                    self.sleep(2 ** attempt)
                    continue
                raise GitHubAPIError(last_error)

            rate = data.get("rateLimit") if isinstance(data, dict) else None
            if rate:
                self.remaining = rate.get("remaining")
                self.reset_at = _parse_time(rate.get("resetAt"))
                self.total_cost += rate.get("cost") or 0
            return data, errors

        raise GitHubAPIError(f"giving up after {MAX_RETRIES} attempts: {last_error}")


def _error_message(response):
    try:
        return response.json().get("message", response.text)
    except ValueError:
        return response.text[:500]


def _warn_errors(errors, context):
    for error in errors:
        log.warning("%s: %s", context, error.get("message", error))


def fetch_repositories(client, org):
    """Yield every repository of ``org`` (handles pagination)."""
    cursor = None
    while True:
        data, errors = client.query(REPOS_QUERY, {"org": org, "pageSize": REPO_PAGE_SIZE, "cursor": cursor})
        organization = data.get("organization")
        if organization is None:
            message = "; ".join(e.get("message", "") for e in errors)
            raise PermissionDenied(
                f"Organization '{org}' was not found or the token does not have access to it. {message}".strip()
            )
        _warn_errors(errors, f"organization {org}")
        connection = organization["repositories"]
        if cursor is None:
            log.info("Organization %s has %d visible repositories", org, connection["totalCount"])
        for repo in connection["nodes"]:
            if repo:
                yield repo
        if not connection["pageInfo"]["hasNextPage"]:
            return
        cursor = connection["pageInfo"]["endCursor"]


def fetch_branches(client, org, repo_name):
    """Return the list of branch nodes (with associated PR details) for a repository."""
    branches = []
    cursor = None
    while True:
        data, errors = client.query(
            BRANCHES_QUERY,
            {"owner": org, "name": repo_name, "pageSize": BRANCH_PAGE_SIZE, "cursor": cursor},
        )
        _warn_errors(errors, f"{org}/{repo_name} branches")
        repository = data.get("repository")
        if not repository or not repository.get("refs"):
            return branches
        connection = repository["refs"]
        branches.extend(node for node in connection["nodes"] if node)
        if not connection["pageInfo"]["hasNextPage"]:
            return branches
        cursor = connection["pageInfo"]["endCursor"]


def fetch_comparisons(client, org, repo_name, default_branch, branch_names):
    """Compare each branch with the default branch.

    Returns ``{branch: {"behind": int, "ahead": int, "first_commit_at": str|None}}``.
    Batches that fail (for example because the comparison is too expensive) are split
    into smaller batches; branches that still fail are left out of the result.
    """
    results = {}
    batches = [branch_names[i:i + COMPARE_BATCH_SIZE] for i in range(0, len(branch_names), COMPARE_BATCH_SIZE)]
    while batches:
        batch = batches.pop(0)
        variables = {"owner": org, "name": repo_name, "base": f"refs/heads/{default_branch}"}
        variables.update({f"h{i}": name for i, name in enumerate(batch)})
        try:
            data, errors = client.query(build_compare_query(len(batch)), variables)
        except GitHubAPIError as exc:
            if len(batch) > 1:
                mid = len(batch) // 2
                batches[0:0] = [batch[:mid], batch[mid:]]
            else:
                log.warning("%s/%s: could not compare branch %s with %s: %s",
                            org, repo_name, batch[0], default_branch, exc)
            continue
        _warn_errors(errors, f"{org}/{repo_name} compare")
        ref = (data.get("repository") or {}).get("ref") or {}
        for i, name in enumerate(batch):
            comparison = ref.get(f"c{i}")
            if not comparison:
                continue
            dates = [n["committedDate"] for n in comparison["commits"]["nodes"] if n and n.get("committedDate")]
            results[name] = {
                # The default branch is the base: "aheadBy" counts commits on the branch only.
                "ahead": comparison["aheadBy"],
                "behind": comparison["behindBy"],
                "first_commit_at": min(dates, key=_parse_time) if dates else None,
            }
    return results


def pr_state(pr):
    """Return ``Draft``, ``Open`` or ``Closed`` (merged pull requests are ``Closed``)."""
    if pr["state"] != "OPEN":
        return "Closed"
    return "Draft" if pr.get("isDraft") else "Open"


def branch_merged(is_default, ahead, pr):
    """Return whether a branch has been merged into the default branch.

    A branch counts as merged when its pull request was merged (this also covers squash
    and rebase merges) or when it has no commits that are not on the default branch.
    Returns ``None`` when this can't be determined.
    """
    if is_default:
        return False
    if pr and pr.get("state") == "MERGED":
        return True
    if ahead is None:
        return None
    return ahead == 0


def summarize_pr(pr):
    """Flatten the GraphQL pull request node into the report's PR columns."""
    if not pr:
        return {}

    def reviewer_name(reviewer):
        if not reviewer:
            return None
        if reviewer.get("login"):
            return reviewer["login"]
        if reviewer.get("slug"):
            return f"team:{reviewer['slug']}"
        return None

    requested = {}
    for event in pr["timelineItems"]["nodes"]:
        name = reviewer_name(event and event.get("requestedReviewer"))
        if not name:
            continue
        if event["__typename"] == "ReviewRequestedEvent":
            requested[name] = True
        else:
            requested.pop(name, None)

    approvals = [
        r["author"]["login"]
        for r in pr["latestOpinionatedReviews"]["nodes"]
        if r and r.get("state") == "APPROVED" and r.get("author")
    ]

    rollup_state = None
    workflow_dates = []
    commit_nodes = pr["commits"]["nodes"]
    if commit_nodes and commit_nodes[0]:
        commit = commit_nodes[0]["commit"]
        rollup_state = (commit.get("statusCheckRollup") or {}).get("state")
        for suite in (commit.get("checkSuites") or {}).get("nodes") or []:
            run = suite and suite.get("workflowRun")
            if run and run.get("updatedAt"):
                workflow_dates.append(run["updatedAt"])

    return {
        "pr_number": pr["number"],
        "pr_url": pr["url"],
        "pr_state": pr_state(pr),
        "pr_created_at": pr["createdAt"],
        "pr_opened_by": (pr.get("author") or {}).get("login", "ghost"),
        "pr_contributors": LIST_SEPARATOR.join(
            sorted({p["login"] for p in pr["participants"]["nodes"] if p and p.get("login")})
        ),
        "pr_last_activity_at": pr["updatedAt"],
        "pr_checks_passing": "" if rollup_state is None else str(rollup_state == "SUCCESS").lower(),
        "pr_last_workflow_run_at": max(workflow_dates, key=_parse_time) if workflow_dates else "",
        "pr_reviewers_requested": LIST_SEPARATOR.join(sorted(requested)),
        "pr_approvals_received": LIST_SEPARATOR.join(sorted(set(approvals))),
    }


def build_rows(repo, branches, comparisons, now):
    """Build the CSV rows (one per branch) for a repository."""
    default_branch = (repo.get("defaultBranchRef") or {}).get("name") or ""
    repo_info = {
        "repository": repo["name"],
        "repo_created_at": repo["createdAt"],
        "repo_last_push_at": repo.get("pushedAt") or "",
        "repo_open_prs": repo["pullRequests"]["totalCount"],
        "repo_branches_total": len(branches),
        "default_branch": default_branch,
    }

    last_push = {}
    for branch in branches:
        last_push[branch["name"]] = (branch.get("target") or {}).get("committedDate")
    for days in PUSH_WINDOWS_DAYS:
        cutoff = now - timedelta(days=days)
        repo_info[f"repo_branches_pushed_last_{days}_days"] = sum(
            1 for value in last_push.values() if value and _parse_time(value) >= cutoff
        )

    if not branches:
        return [dict(repo_info)]

    rows = []
    for branch in branches:
        name = branch["name"]
        is_default = name == default_branch
        row = dict(repo_info)
        row.update(
            {
                "branch": name,
                "is_default_branch": str(is_default).lower(),
                "branch_last_push_at": last_push[name] or "",
            }
        )
        if is_default:
            row.update(
                {"branch_created_at": repo["createdAt"], "commits_behind_default": 0, "commits_ahead_default": 0}
            )
        elif name in comparisons:
            comparison = comparisons[name]
            row.update(
                {
                    # Oldest commit not on the default branch; falls back to the head commit date.
                    "branch_created_at": comparison["first_commit_at"] or last_push[name] or "",
                    "commits_behind_default": comparison["behind"],
                    "commits_ahead_default": comparison["ahead"],
                }
            )
        else:
            row["branch_created_at"] = last_push[name] or ""

        prs = (branch.get("associatedPullRequests") or {}).get("nodes") or []
        pr = prs[0] if prs else None
        row["branch_has_pr"] = str(pr is not None).lower()
        merged = branch_merged(is_default, (comparisons.get(name) or {}).get("ahead"), pr)
        row["branch_merged"] = "" if merged is None else str(merged).lower()
        row.update(summarize_pr(pr))
        rows.append(row)
    return rows


def generate_report(client, org, output, now=None):
    """Write the report for ``org`` to the file object ``output``. Returns the number of rows."""
    now = now or datetime.now(timezone.utc)
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, restval="")
    writer.writeheader()
    row_count = 0
    repo_count = 0
    for repo in fetch_repositories(client, org):
        repo_count += 1
        name = repo["name"]
        log.info("Processing %s/%s", org, name)
        default_branch = (repo.get("defaultBranchRef") or {}).get("name")
        branches = fetch_branches(client, org, name) if default_branch else []
        comparisons = {}
        if default_branch:
            others = [b["name"] for b in branches if b["name"] != default_branch]
            if others:
                comparisons = fetch_comparisons(client, org, name, default_branch, others)
        for row in build_rows(repo, branches, comparisons, now):
            writer.writerow(row)
            row_count += 1
        output.flush()
    if repo_count == 0:
        log.warning("No repositories visible in organization %s. Check the token's repository access.", org)
    return row_count


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a CSV report of all repositories in a GitHub organization and their branches."
    )
    parser.add_argument("-o", "--org", required=True, help="GitHub organization name")
    parser.add_argument(
        "-f", "--output", help="Output CSV file (default: <org>-branch-report.csv). Use '-' for stdout."
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL),
        help="GitHub API base URL, e.g. https://ghes.example.com/api/v3 (default: $GITHUB_API_URL or %s)"
        % DEFAULT_API_URL,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.error("The GITHUB_TOKEN environment variable is not set.")
        return 1

    output_path = args.output or f"{args.org}-branch-report.csv"
    client = GitHubClient(token, api_url=args.api_url)
    exit_code = 0
    opened = False
    try:
        if output_path == "-":
            rows = generate_report(client, args.org, sys.stdout)
        else:
            with open(output_path, "w", newline="", encoding="utf-8") as handle:
                opened = True
                rows = generate_report(client, args.org, handle)
    except PermissionDenied as exc:
        log.error("Insufficient permissions: %s", exc)
        log.error("See README.md for the token permissions required by this script.")
        exit_code = 1
    except (GitHubAPIError, requests.RequestException) as exc:
        log.error("GitHub API error: %s", exc)
        exit_code = 1
    except OSError as exc:
        log.error("Could not write %s: %s", output_path, exc)
        exit_code = 1
    except KeyboardInterrupt:
        log.error("Interrupted")
        exit_code = 130
    if exit_code:
        if opened and os.path.exists(output_path):
            # Don't leave an incomplete report behind.
            os.remove(output_path)
        return exit_code

    if client.token_scopes is not None and "repo" not in client.token_scopes:
        log.warning("The token does not have the 'repo' scope; private repositories are not included.")
    log.info(
        "Wrote %d rows to %s (GraphQL rate limit points used: %d)",
        rows,
        "stdout" if output_path == "-" else output_path,
        client.total_cost,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
