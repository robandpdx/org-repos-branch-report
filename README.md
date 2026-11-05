# org-repos-branch-report

`org_branch_report.py` writes a CSV report of every repository in a GitHub organization, with one row per branch.
Branches are the focus of the report. Each row repeats its repository's fields, so you can sort and filter on any
column in a spreadsheet.

## Requirements

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/)

```bash
pip install -r requirements.txt
```

## Usage

```bash
export GITHUB_TOKEN=<your token>
python org_branch_report.py --org <organization>
```

| Option | Description |
| --- | --- |
| `-o`, `--org` | **Required.** Organization to report on. |
| `-f`, `--output` | Output CSV file. Default: `<org>-branch-report.csv`. Use `-` to write to stdout. |
| `--api-url` | API base URL for GitHub Enterprise Server, e.g. `https://ghes.example.com/api/v3`. Default: `$GITHUB_API_URL` or `https://api.github.com`. |
| `-v`, `--verbose` | Enable debug logging. |

Progress, warnings and errors are written to stderr.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The report was written. |
| `1` | `GITHUB_TOKEN` is missing, the token is invalid, or it lacks a required permission. Also used when the organization cannot be found or accessed, or an unrecoverable API error occurs. The error message says what went wrong, and any incomplete output file is deleted. |
| `130` | Interrupted (Ctrl+C). |

## Token permissions (`GITHUB_TOKEN`)

The script only reads data. If the token lacks a permission it needs, the script prints an `Insufficient permissions` error and exits with `1`.

**Classic personal access token**

- `repo`: read private repositories, branches, pull requests, checks and statuses. Without it, only public repositories are visible.
- `read:org`: resolve team review requests.
- If the organization uses SAML single sign-on, [authorize the token for the organization](https://docs.github.com/en/enterprise-cloud@latest/authentication/authenticating-with-saml-single-sign-on/authorizing-a-personal-access-token-for-use-with-saml-single-sign-on).

**Fine-grained personal access token** (or GitHub App installation token)

- Resource owner: the organization
- Repository access: *All repositories* (or the repositories you want in the report)
- Repository permissions (all **Read-only**):
  - Metadata
  - Contents
  - Pull requests
  - Checks
  - Commit statuses
  - Actions
- Organization permissions: **Members: Read-only**, to resolve team review requests

## Report columns

### Repository columns (repeated on every branch row)

| Column | Description |
| --- | --- |
| `repository` | Repository name |
| `repo_created_at` | Date the repository was created |
| `repo_last_push_at` | Date of the last push to the repository |
| `repo_open_prs` | Number of open pull requests |
| `repo_branches_total` | Total number of branches |
| `repo_branches_pushed_last_30_days` | Branches with a push in the last 30 days |
| `repo_branches_pushed_last_60_days` | Branches with a push in the last 60 days |
| `repo_branches_pushed_last_90_days` | Branches with a push in the last 90 days |
| `default_branch` | Name of the default branch |

A repository with no branches (an empty repository) gets one row, with the branch and PR columns blank.

### Branch columns

| Column | Description |
| --- | --- |
| `branch` | Branch name |
| `is_default_branch` | `true` / `false` |
| `branch_created_at` | Approximate creation date of the branch (see notes) |
| `branch_last_push_at` | Last push date of the branch (see notes) |
| `branch_has_pr` | `true` if a pull request uses this branch as its head branch |
| `commits_behind_default` | Commits on the default branch that are not on this branch |
| `commits_ahead_default` | Commits on this branch that are not on the default branch |
| `branch_merged` | `true` if the branch has been merged into the default branch, `false` otherwise (always `false` for the default branch). See notes. |

### Pull request columns (blank when `branch_has_pr` is `false`)

| Column | Description |
| --- | --- |
| `pr_number`, `pr_url` | The pull request |
| `pr_state` | `Draft`, `Open` or `Closed`. Merged PRs are `Closed`; see `branch_merged`. |
| `pr_created_at` | Date the PR was created |
| `pr_opened_by` | Login of the PR author |
| `pr_contributors` | Everyone who took part in the PR: author, commenters, reviewers, etc. |
| `pr_last_activity_at` | Date of the last activity on the PR |
| `pr_checks_passing` | `true` if the combined status of all checks and statuses on the head commit is successful, `false` otherwise, blank if the commit has no checks |
| `pr_last_workflow_run_at` | Date of the most recent GitHub Actions workflow run on the PR's head commit |
| `pr_reviewers_requested` | Users requested as reviewers, including those who have already reviewed. Requests that were later removed are excluded. Teams are shown as `team:<slug>`. |
| `pr_approvals_received` | Users whose latest review is an approval |

Columns that hold several users separate them with `;`. Dates are ISO 8601 in UTC, for example `2024-05-10T14:03:00Z`.

### Notes and limitations

- **Branch dates are approximate.** Git does not record when a branch was created or pushed, and the GitHub API no longer returns push dates.
  - `branch_last_push_at` is the commit date of the branch's latest commit.
  - `branch_created_at` is the commit date of the oldest commit that is on the branch but not on the default branch. If the branch has no such commits, the latest commit date is used. For the default branch, the repository's creation date is used.
  - The push counts per window (`repo_branches_pushed_last_*_days`) are based on `branch_last_push_at`.
- If several pull requests use a branch as their head, the most recently updated one is reported. It can be a draft, open, closed or merged; see `pr_state` and `branch_merged`.
- To keep each request small, PR lists are capped: 100 contributors, 100 reviewers and review requests, and 50 check suites.
- `branch_merged` is `true` when the branch's pull request was merged (this covers squash and rebase merges) or the branch has no commits that aren't on the default branch (`commits_ahead_default` is `0`). A new branch with no commits of its own therefore also shows as merged. The column is blank if the branch couldn't be compared with the default branch and has no merged pull request.
- If a branch can't be compared with the default branch (for example, the comparison times out on a very large repository), the script logs a warning and leaves `commits_behind_default` and `commits_ahead_default` blank.

## How API usage is kept efficient

- All data comes from the GitHub GraphQL API, which fetches a page of up to 50 branches with their pull request details in one request.
- Ahead/behind counts are fetched in batches: one request compares up to 25 branches with the default branch.
- Every list of repositories and branches is paginated with cursors.
- Each response includes the remaining GraphQL rate limit. When fewer than 25 points are left, the script pauses until the limit resets.
- When GitHub reports a rate limit:
  - Primary limit: the script waits until the limit resets (`x-ratelimit-reset`).
  - Secondary limit: the script waits for the time in the `Retry-After` header, or backs off exponentially if there isn't one.
  - The request is then retried.
- Transient errors (network errors, 5xx responses, GraphQL timeouts) are retried with exponential backoff. If a comparison batch keeps failing, it is split into smaller batches.
- At the end of the run, the script logs the total rate limit points used.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```
