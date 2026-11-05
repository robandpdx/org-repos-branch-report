import csv
import io
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import org_branch_report as report  # noqa: E402

NOW = datetime(2024, 6, 30, tzinfo=timezone.utc)
RATE = {"cost": 1, "remaining": 4999, "resetAt": "2024-06-30T01:00:00Z"}


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Calls ``handler(query, variables)`` for each request."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return self.handler(json["query"], json["variables"])


def make_client(handler):
    sleeps = []
    client = report.GitHubClient("token", session=FakeSession(handler), sleep=sleeps.append)
    return client, sleeps


def pr_node(**overrides):
    pr = {
        "number": 7,
        "url": "https://github.com/acme/app/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "createdAt": "2024-06-01T00:00:00Z",
        "updatedAt": "2024-06-20T00:00:00Z",
        "author": {"login": "alice"},
        "participants": {"nodes": [{"login": "bob"}, {"login": "alice"}, {"login": "carol"}]},
        "timelineItems": {
            "nodes": [
                {"__typename": "ReviewRequestedEvent", "requestedReviewer": {"login": "bob"}},
                {"__typename": "ReviewRequestedEvent", "requestedReviewer": {"login": "dave"}},
                {"__typename": "ReviewRequestedEvent", "requestedReviewer": {"slug": "core"}},
                {"__typename": "ReviewRequestRemovedEvent", "requestedReviewer": {"login": "dave"}},
            ]
        },
        "latestOpinionatedReviews": {
            "nodes": [
                {"state": "APPROVED", "author": {"login": "bob"}},
                {"state": "CHANGES_REQUESTED", "author": {"login": "carol"}},
            ]
        },
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {"state": "SUCCESS"},
                        "checkSuites": {
                            "nodes": [
                                {"workflowRun": {"updatedAt": "2024-06-19T00:00:00Z"}},
                                {"workflowRun": None},
                                {"workflowRun": {"updatedAt": "2024-06-21T00:00:00Z"}},
                            ]
                        },
                    }
                }
            ]
        },
    }
    pr.update(overrides)
    return pr


REPO = {
    "name": "app",
    "createdAt": "2020-01-01T00:00:00Z",
    "pushedAt": "2024-06-25T00:00:00Z",
    "defaultBranchRef": {"name": "main"},
    "pullRequests": {"totalCount": 3},
}

BRANCHES = [
    {"name": "main", "target": {"committedDate": "2024-06-25T00:00:00Z"}, "associatedPullRequests": {"nodes": []}},
    {"name": "feature", "target": {"committedDate": "2024-05-15T00:00:00Z"},
     "associatedPullRequests": {"nodes": [pr_node()]}},
    {"name": "old", "target": {"committedDate": "2023-01-01T00:00:00Z"}, "associatedPullRequests": {"nodes": []}},
]


def api_handler(query, variables):
    if "organization(login:" in query:
        return FakeResponse(body={"data": {"rateLimit": RATE, "organization": {"repositories": {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": variables["cursor"] is None, "endCursor": "r1"},
            "nodes": [REPO] if variables["cursor"] is None else [
                {"name": "empty", "createdAt": "2024-01-01T00:00:00Z", "pushedAt": None,
                 "defaultBranchRef": None, "pullRequests": {"totalCount": 0}}],
        }}}})
    if "refs(refPrefix" in query:
        first = variables["cursor"] is None
        return FakeResponse(body={"data": {"rateLimit": RATE, "repository": {"refs": {
            "totalCount": 3,
            "pageInfo": {"hasNextPage": first, "endCursor": "b1"},
            "nodes": BRANCHES[:2] if first else BRANCHES[2:],
        }}}})
    if "compare(" in query:
        assert variables["base"] == "refs/heads/main"
        comps = {
            "feature": {"aheadBy": 2, "behindBy": 5, "commits": {"nodes": [
                {"committedDate": "2024-05-15T00:00:00Z"}, {"committedDate": "2024-05-10T00:00:00Z"}]}},
            "old": {"aheadBy": 0, "behindBy": 40, "commits": {"nodes": []}},
        }
        ref = {f"c{i}": comps[variables[f"h{i}"]] for i in range(len(variables) - 3)}
        return FakeResponse(body={"data": {"rateLimit": RATE, "repository": {"ref": ref}}})
    raise AssertionError(query)


def test_generate_report_rows():
    client, _ = make_client(api_handler)
    out = io.StringIO()
    count = report.generate_report(client, "acme", out, now=NOW)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert count == 4 == len(rows)
    assert list(rows[0].keys()) == report.CSV_COLUMNS
    # 2 repo pages + 2 branch pages + 1 compare batch; no branch queries for the empty repo.
    assert len(client.session.calls) == 5

    main, feature, old, empty = rows
    for row in (main, feature, old):
        assert row["repository"] == "app"
        assert row["repo_created_at"] == "2020-01-01T00:00:00Z"
        assert row["repo_last_push_at"] == "2024-06-25T00:00:00Z"
        assert row["repo_open_prs"] == "3"
        assert row["repo_branches_total"] == "3"
        assert row["repo_branches_pushed_last_30_days"] == "1"
        assert row["repo_branches_pushed_last_60_days"] == "2"
        assert row["repo_branches_pushed_last_90_days"] == "2"

    assert main["is_default_branch"] == "true"
    assert main["commits_ahead_default"] == "0" and main["commits_behind_default"] == "0"
    assert main["branch_created_at"] == REPO["createdAt"]
    assert main["branch_has_pr"] == "false" and main["pr_number"] == ""
    assert main["branch_merged"] == "false"

    assert feature["branch_created_at"] == "2024-05-10T00:00:00Z"
    assert feature["branch_last_push_at"] == "2024-05-15T00:00:00Z"
    assert feature["commits_ahead_default"] == "2"
    assert feature["commits_behind_default"] == "5"
    assert feature["branch_has_pr"] == "true"
    assert feature["pr_created_at"] == "2024-06-01T00:00:00Z"
    assert feature["pr_opened_by"] == "alice"
    assert feature["pr_contributors"] == "alice;bob;carol"
    assert feature["pr_reviewers_requested"] == "bob;team:core"
    assert feature["pr_approvals_received"] == "bob"
    assert feature["pr_checks_passing"] == "true"
    assert feature["pr_last_workflow_run_at"] == "2024-06-21T00:00:00Z"
    assert feature["pr_last_activity_at"] == "2024-06-20T00:00:00Z"
    assert feature["pr_state"] == "Open"
    assert feature["branch_merged"] == "false"

    assert old["commits_behind_default"] == "40"
    assert old["commits_ahead_default"] == "0"
    assert old["branch_merged"] == "true"
    assert old["branch_created_at"] == "2023-01-01T00:00:00Z"

    assert empty["repository"] == "empty"
    assert empty["branch"] == "" and empty["repo_branches_total"] == "0"


def test_summarize_pr_failing_checks_and_draft():
    pr = pr_node(isDraft=True, author=None)
    pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {"state": "FAILURE"}
    summary = report.summarize_pr(pr)
    assert summary["pr_checks_passing"] == "false"
    assert summary["pr_state"] == "Draft"
    assert summary["pr_opened_by"] == "ghost"

    pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    assert report.summarize_pr(pr)["pr_checks_passing"] == ""


def test_org_not_found_is_permission_error():
    def handler(query, variables):
        return FakeResponse(body={"data": {"rateLimit": RATE, "organization": None},
                                  "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]})

    client, _ = make_client(handler)
    with pytest.raises(report.PermissionDenied):
        list(report.fetch_repositories(client, "nope"))


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status_code=401, body={"message": "Bad credentials"}),
        FakeResponse(status_code=403, body={"message": "Resource not accessible by personal access token"}),
        FakeResponse(body={"data": None, "errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs read:org"}]}),
        FakeResponse(body={"data": {"organization": None},
                           "errors": [{"type": "FORBIDDEN", "message": "SAML enforcement"}]}),
    ],
)
def test_permission_errors(response):
    client, _ = make_client(lambda q, v: response)
    with pytest.raises(report.PermissionDenied):
        client.query("query { viewer { login } }")


def test_main_exits_1_on_permission_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    class Session(FakeSession):
        def __init__(self):
            super().__init__(lambda q, v: FakeResponse(status_code=401, body={"message": "Bad credentials"}))

    monkeypatch.setattr(report.requests, "Session", Session)
    assert report.main(["-o", "acme", "-f", str(tmp_path / "out.csv")]) == 1


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert report.main(["--org", "acme"]) == 1


def test_main_writes_csv(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    class Session(FakeSession):
        def __init__(self):
            super().__init__(api_handler)

    monkeypatch.setattr(report.requests, "Session", Session)
    output = tmp_path / "out.csv"
    assert report.main(["-o", "acme", "-f", str(output)]) == 0
    with open(output, newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 4


def test_retries_rate_limit_and_server_errors():
    responses = [
        FakeResponse(status_code=403, body={"message": "You have exceeded a secondary rate limit"},
                     headers={"retry-after": "5"}),
        FakeResponse(body={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
                     headers={"x-ratelimit-reset": "0"}),
        FakeResponse(status_code=502, body={}),
        FakeResponse(body={"data": {"rateLimit": {"cost": 1, "remaining": 10,
                                                  "resetAt": "2000-01-01T00:00:00Z"}, "ok": True}}),
        FakeResponse(body={"data": {"ok": True}}),
    ]
    client, sleeps = make_client(lambda q, v: responses.pop(0))
    data, errors = client.query("query { ok }")
    assert data["ok"] and errors == []
    assert sleeps[0] == 6 and len(sleeps) == 3
    # Remaining points are below the buffer, so the next call waits for the reset first.
    client.query("query { ok }")
    assert len(sleeps) == 4


def test_compare_batch_split_on_failure(monkeypatch):
    monkeypatch.setattr(report, "COMPARE_BATCH_SIZE", 4)
    monkeypatch.setattr(report, "MAX_RETRIES", 1)

    def handler(query, variables):
        heads = [variables[f"h{i}"] for i in range(len(variables) - 3)]
        if "bad" in heads:
            return FakeResponse(body={"data": None, "errors": [{"message": "Something went wrong"}]})
        ref = {f"c{i}": {"aheadBy": 1, "behindBy": 0, "commits": {"nodes": []}} for i in range(len(heads))}
        return FakeResponse(body={"data": {"repository": {"ref": ref}}})

    client, _ = make_client(handler)
    result = report.fetch_comparisons(client, "acme", "app", "main", ["a", "b", "bad", "c"])
    assert set(result) == {"a", "b", "c"}


def test_main_removes_incomplete_output(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def handler(query, variables):
        if "organization(login:" in query:
            return api_handler(query, variables)
        return FakeResponse(body={"data": {"repository": None},
                                  "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible"}]})

    class Session(FakeSession):
        def __init__(self):
            super().__init__(handler)

    monkeypatch.setattr(report.requests, "Session", Session)
    output = tmp_path / "out.csv"
    assert report.main(["-o", "acme", "-f", str(output)]) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "api_url, expected",
    [
        ("https://api.github.com", "https://api.github.com/graphql"),
        ("https://ghes.example.com/api/v3/", "https://ghes.example.com/api/graphql"),
        ("https://ghes.example.com/api", "https://ghes.example.com/api/graphql"),
    ],
)
def test_graphql_url(api_url, expected):
    assert report.GitHubClient("t", api_url=api_url, session=FakeSession(None)).url == expected


@pytest.mark.parametrize(
    "state, is_draft, expected",
    [("OPEN", False, "Open"), ("OPEN", True, "Draft"), ("CLOSED", True, "Closed"),
     ("CLOSED", False, "Closed"), ("MERGED", False, "Closed")],
)
def test_pr_state(state, is_draft, expected):
    assert report.pr_state({"state": state, "isDraft": is_draft}) == expected


@pytest.mark.parametrize(
    "is_default, ahead, pr, expected",
    [
        (True, 0, None, False),
        (False, 0, None, True),
        (False, 3, None, False),
        (False, 3, {"state": "MERGED"}, True),  # squash/rebase merge
        (False, 3, {"state": "CLOSED"}, False),
        (False, None, None, None),
        (False, None, {"state": "MERGED"}, True),
    ],
)
def test_branch_merged(is_default, ahead, pr, expected):
    assert report.branch_merged(is_default, ahead, pr) is expected


def test_branch_merged_blank_when_compare_unknown():
    repo = dict(REPO)
    branches = [{"name": "x", "target": {"committedDate": "2024-06-01T00:00:00Z"},
                 "associatedPullRequests": {"nodes": []}}]
    row = report.build_rows(repo, branches, {}, NOW)[0]
    assert row["branch_merged"] == ""
    assert "pr_is_draft" not in report.CSV_COLUMNS
