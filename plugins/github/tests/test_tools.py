"""The GitHub plugin against a small fake of GitHub (tests/fake_github.py), through the SDK's runner as `ops plugin
test` runs it, by failure mode: a tool sends a request GitHub does not take; a model's owner or repo changes the path
it goes into; a comment's text is not sent intact as JSON (quotes, newlines, Markdown); a list answer loses the fields
an agent needs; the plugin's API version and Accept headers are missing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from ops_plugin_github import plugin

from ops_plugin_sdk.formats import InputRefused
from ops_plugin_sdk.testing import Fake, call

sys.path.insert(0, str(Path(__file__).parent))
from fake_github import FakeGitHub

SETTINGS = {"api_url": "https://api.github.test", "web_url": "https://github.test"}
REPO = {"owner": "acme-logistics", "repo": "dispatch-api"}


@pytest.fixture
def gh() -> tuple[FakeGitHub, Fake]:
    fake = FakeGitHub()
    return fake, Fake(httpx.Client(transport=httpx.MockTransport(fake)), "tok")


def test_list_issues_sends_the_state_and_answers_the_fields_an_agent_needs(gh: tuple[FakeGitHub, Fake]) -> None:
    fake, client = gh
    out: dict[str, Any] = call(plugin, "list_issues", {**REPO, "state": "all"}, SETTINGS, client)
    sent = fake.seen[-1]
    assert sent.url.path == "/repos/acme-logistics/dispatch-api/issues"
    assert (sent.url.params["state"], sent.url.params["per_page"]) == ("all", "50")
    assert [i["number"] for i in out["issues"]] == [3, 2, 1]
    first = out["issues"][0]
    assert (first["author"], first["labels"], first["state"]) == ("dana-okafor", ["bug"], "open")
    assert first["url"] == "https://github.test/acme-logistics/dispatch-api/issues/3"


def test_list_issues_defaults_to_open(gh: tuple[FakeGitHub, Fake]) -> None:
    fake, client = gh
    out: dict[str, Any] = call(plugin, "list_issues", REPO, SETTINGS, client)
    assert fake.seen[-1].url.params["state"] == "open" and [i["number"] for i in out["issues"]] == [3, 2]


@pytest.mark.parametrize(
    ("args", "said"),
    [
        ({"owner": "acme-logistics/../../user", "repo": "dispatch-api"}, "owner"),
        ({"owner": "acme-logistics", "repo": "dispatch-api/issues?x="}, "repo"),
        ({**REPO, "state": "open&per_page=100"}, "state must be one of"),
    ],
)
def test_a_value_that_would_change_the_path_or_query_never_reaches_github(
    gh: tuple[FakeGitHub, Fake], args: dict[str, Any], said: str
) -> None:
    fake, client = gh
    with pytest.raises(InputRefused, match=said):
        call(plugin, "list_issues", args, SETTINGS, client)
    assert fake.seen == []


def test_get_issue_reads_one_issue_with_its_body(gh: tuple[FakeGitHub, Fake]) -> None:
    fake, client = gh
    out: dict[str, Any] = call(plugin, "get_issue", {**REPO, "number": 3}, SETTINGS, client)
    assert fake.seen[-1].url.path == "/repos/acme-logistics/dispatch-api/issues/3"
    assert (out["title"], out["body"]) == ("Route planner times out over 200 stops", "Seen on the Leeds depot run.")


def test_a_comment_reaches_github_intact(gh: tuple[FakeGitHub, Fake]) -> None:
    fake, client = gh
    text = 'Seen again on "Leeds".\n\n- 214 stops\n- `timeout=30s`, then {"retry": true}'
    out: dict[str, Any] = call(plugin, "add_comment", {**REPO, "number": 3, "body": text}, SETTINGS, client)
    sent = fake.seen[-1]
    assert (sent.method, sent.url.path) == ("POST", "/repos/acme-logistics/dispatch-api/issues/3/comments")
    assert json.loads(sent.content) == {"body": text}
    assert fake.comments[-1]["body"] == text and out["url"].endswith("#issuecomment-9000")


def test_the_api_version_and_accept_headers_go_on_every_call(gh: tuple[FakeGitHub, Fake]) -> None:
    # The headers are the plugin's own (plugin.yaml `headers`); tools-service sends them on every request.
    assert plugin.manifest.headers == {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "{api_version}"}


def test_the_fake_signs_in_like_github() -> None:
    """The fake's token answer is form-encoded unless JSON is asked for, as GitHub's is, so the live check against a
    stack shows whether the platform's token call asks for JSON."""
    fake = FakeGitHub()
    client = httpx.Client(transport=httpx.MockTransport(fake), base_url="https://github.test")
    page = client.get(
        "/login/oauth/authorize",
        params={
            "client_id": "Iv1.fakegithubclient",
            "state": "s1",
            "redirect_uri": "http://localhost/cb",
            "scope": "repo",
        },
    )
    assert page.status_code == 200 and "Authorize" in page.text
    back = client.post(
        "/login/oauth/authorize", data={"redirect_uri": "http://localhost/cb", "state": "s1", "decision": "allow"}
    )
    code = httpx.URL(back.headers["location"]).params["code"]
    form = {"client_id": "Iv1.fakegithubclient", "client_secret": "fake-github-secret", "code": code}
    plain = client.post("/login/oauth/access_token", data=form)
    assert plain.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert client.post("/login/oauth/access_token", data=form).text.startswith("error=bad_verification_code")
