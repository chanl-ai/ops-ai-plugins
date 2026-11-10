"""The Confluence plugin against a fake site, through the SDK's runner (as `ops plugin test` runs it), by failure mode:
edits missed because CQL reads its date in another time zone, a space listing longer than one REST page, a page
fetched whole though its version did not move, an attachment read as text or never followed through Cloud's media
redirect, a trashed page treated as live, a space key that breaks out of the CQL, and the plugin breaking the
knowledge-source contract on Cloud or on Data Center. No site was involved: every answer here is the fake's, shaped
after the Confluence REST reference."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fake_confluence import Attachment, FakeConfluence
from ops_plugin_confluence import pages, plugin

from ops_plugin_sdk import BadInput, NotFound
from ops_plugin_sdk.testing import Fake, call, knowledge_contract

ENG = {"spaces": ["ENG"]}
BOTH = {"spaces": ["ENG", "HR"]}


def run(site: FakeConfluence, tool: str, args: dict[str, Any]) -> Any:
    fake = Fake(httpx.Client(transport=httpx.MockTransport(site)), "tok")
    return call(plugin, tool, args, {"site_url": site.site_url}, fake)


def listing(site: FakeConfluence, source: dict[str, Any], tool: str = "list_pages", cursor: str | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    pages_read = 0
    while True:
        page = run(site, tool, {"source": source, **({"cursor": cursor} if cursor else {})})
        rows += page["items"]
        pages_read += 1
        cursor = page.get("next_cursor")
        if not cursor:
            return {"items": rows, "cursor": page["cursor"], "pages": pages_read}


@pytest.mark.parametrize(
    "site", [FakeConfluence(), FakeConfluence(prefix="", host="wiki.acme.test")], ids=["cloud", "data-center"]
)
def test_plugin_meets_the_knowledge_contract(site: FakeConfluence) -> None:
    problems = knowledge_contract(
        plugin,
        "spaces",
        Fake(httpx.Client(transport=httpx.MockTransport(site)), "tok"),
        settings={"site_url": site.site_url},
        values=BOTH,
        change=lambda: site.edit("102", "<p>Page the secondary first.</p>"),
        delete=lambda: site.trash("201"),
    )
    assert problems == []


def test_scopes_answer_spaces_matching_a_query_and_nothing_under_a_space() -> None:
    site = FakeConfluence()
    got = run(site, "list_spaces", {"query": "eng"})
    assert got == {"scopes": [{"id": "ENG", "title": "Engineering", "kind": "space", "detail": "global space"}]}
    assert [s["id"] for s in run(site, "list_spaces", {"query": ""})["scopes"]] == ["ENG", "HR", "~dana", "LEG"]
    assert [s["id"] for s in run(site, "list_spaces", {"query": "ORTIZ"})["scopes"]] == ["~dana"]
    assert run(site, "list_spaces", {"query": "", "parent": "ENG"}) == {"scopes": []}


def test_a_space_list_longer_than_one_call_reads_continues_from_the_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pages, "SPACES_PER_CALL", 1)
    site = FakeConfluence(page_size=2)
    first = run(site, "list_spaces", {"query": ""})
    assert [s["id"] for s in first["scopes"]] == ["ENG", "HR"] and first["next_cursor"] == "2"
    last = run(site, "list_spaces", {"query": "", "cursor": first["next_cursor"]})
    assert [s["id"] for s in last["scopes"]] == ["~dana", "LEG"] and "next_cursor" not in last


def test_a_space_with_more_pages_than_one_rest_page_is_listed_whole() -> None:
    site = FakeConfluence(page_size=2)
    for n in range(4):
        site.add("ENG", f"30{n}", f"Extra {n}", "<p>x</p>")
    got = listing(site, ENG)
    assert got["pages"] >= 3
    assert sorted(r["id"] for r in got["items"]) == sorted(p.id for p in site.pages.values() if p.space == "ENG")


def test_a_page_row_carries_its_version_as_marker_and_its_ancestors_as_folder() -> None:
    row = next(r for r in listing(FakeConfluence(), ENG)["items"] if r["id"] == "101")
    assert (row["marker"], row["folder"], row["title"]) == ("1", "ENG/Handbook", "Deploys")
    assert row["url"] == "https://acme.atlassian.test/wiki/spaces/ENG/pages/101"


@pytest.mark.parametrize("tz", [-8, 0, 10])
def test_an_edit_is_found_whatever_time_zone_cql_reads_the_date_in(tz: int) -> None:
    site = FakeConfluence(cql_tz_hours=tz)
    first = listing(site, BOTH)
    site.edit("101", "<p>New.</p>")
    changes = listing(site, BOTH, "list_page_changes", first["cursor"])
    assert "101" in {r["id"] for r in changes["items"]}


def test_changes_are_the_pages_edited_since_not_every_page() -> None:
    site = FakeConfluence()
    first = listing(site, BOTH)
    site.edit("103", "<p>Edited.</p>")
    changes = listing(site, BOTH, "list_page_changes", first["cursor"])
    assert [r["id"] for r in changes["items"]] == ["103"]
    assert changes["items"][0]["marker"] == "2"


def test_a_changes_query_longer_than_one_rest_page_is_read_to_its_end() -> None:
    site = FakeConfluence(page_size=2)
    first = listing(site, BOTH)
    for pid in ("101", "102", "103", "201"):
        site.edit(pid, "<p>Edited.</p>")
    changes = listing(site, BOTH, "list_page_changes", first["cursor"])
    assert changes["pages"] == 2
    assert sorted(r["id"] for r in changes["items"]) == ["101", "102", "103", "201"]


def test_a_page_whose_version_did_not_move_is_not_fetched_whole() -> None:
    site = FakeConfluence()
    first = run(site, "get_page", {"source": ENG, "id": "101"})
    before = len(site.bodies())
    assert run(site, "get_page", {"source": ENG, "id": "101", "tag": first["marker"]}) == {"unchanged": True}
    assert len(site.bodies()) == before
    site.edit("101", "<p>Moved.</p>")
    assert run(site, "get_page", {"source": ENG, "id": "101", "tag": first["marker"]})["content"] == "<p>Moved.</p>"


def test_a_page_is_html_with_its_title_folder_and_version() -> None:
    got = run(FakeConfluence(), "get_page", {"source": ENG, "id": "101"})
    assert (got["mime"], got["encoding"], got["content"]) == ("text/html", "text", "<p>Deploy with the pipeline.</p>")
    assert (got["title"], got["folder"], got["marker"]) == ("Deploys", "ENG/Handbook", "1")
    assert got["allowed"] == []


def test_who_may_read_a_restricted_page_is_taken_from_its_restrictions() -> None:
    cloud, dc = FakeConfluence(), FakeConfluence(prefix="", host="wiki.acme.test")
    cloud.pages["101"].users = [{"accountId": "5b10ac8d82e05b22cc7d4ef5", "displayName": "Dana"}]
    cloud.pages["101"].groups = [{"id": "g-eng-0001", "name": "engineering"}]
    dc.pages["101"].users = [{"userKey": "8a7f9c0e", "username": "dana"}]
    dc.pages["101"].groups = [{"name": "engineering"}]
    assert run(cloud, "get_page", {"source": ENG, "id": "101"})["allowed"] == ["5b10ac8d82e05b22cc7d4ef5", "g-eng-0001"]
    assert run(dc, "get_page", {"source": ENG, "id": "101"})["allowed"] == ["8a7f9c0e", "engineering"]


def test_attachments_are_children_only_when_the_source_takes_them_in() -> None:
    site = FakeConfluence()
    site.pages["101"].attachments = [Attachment("att9001", "runbook.pdf", b"%PDF-1.7\x00\xff", "application/pdf", version=3)]
    without = run(site, "get_page", {"source": ENG, "id": "101"})
    with_them = run(site, "get_page", {"source": {**ENG, "attachments": True}, "id": "101"})
    assert "children" not in without
    assert [(c["id"], c["marker"]) for c in with_them["children"]] == [("att:att9001", "3")]


@pytest.mark.parametrize(
    "site", [FakeConfluence(), FakeConfluence(prefix="", host="wiki.acme.test")], ids=["cloud-media-redirect", "data-center"]
)
def test_a_pdf_attachment_is_returned_as_base64_after_any_redirect(site: FakeConfluence) -> None:
    pdf = b"%PDF-1.7\x00\xff\xfe binary"
    site.pages["101"].attachments = [Attachment("att9001", "runbook.pdf", pdf, "application/pdf", version=3)]
    got = run(site, "get_page", {"source": ENG, "id": "att:att9001"})
    assert (got["encoding"], got["mime"], got["marker"]) == ("base64", "application/pdf", "3")
    assert got["content"] == __import__("base64").b64encode(pdf).decode()
    assert any(r.url.host == "api.media.atlassian.com" for r in site.requests) == (site.prefix == "/wiki")
    assert run(site, "get_page", {"source": ENG, "id": "att:att9001", "tag": "3"}) == {"unchanged": True}


def test_a_trashed_page_is_not_found_rather_than_indexed() -> None:
    site = FakeConfluence()
    site.trash("101")
    with pytest.raises(NotFound):
        run(site, "get_page", {"source": ENG, "id": "101"})


def test_a_space_key_cannot_break_out_of_the_cql() -> None:
    site = FakeConfluence()
    hostile = {"spaces": ['ENG") OR (space = "SECRET']}
    with pytest.raises(BadInput):
        run(site, "list_page_changes", {"source": hostile, "cursor": "2026-11-01T00:00:00Z"})
    assert not site.requests


def test_a_listing_cursor_given_to_changes_is_refused_so_the_sync_starts_again() -> None:
    site = FakeConfluence(page_size=1)
    mid = run(site, "list_pages", {"source": BOTH})["next_cursor"]
    with pytest.raises(BadInput):
        run(site, "list_page_changes", {"source": BOTH, "cursor": mid})
    forged = json.dumps({"pass": "2026-11-01T00:00:00Z", "i": 0, "next": "https://evil.test/x"})
    with pytest.raises(BadInput):
        run(site, "list_pages", {"source": BOTH, "cursor": forged})


def test_the_last_page_of_a_listing_names_when_it_began() -> None:
    before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    cursor = listing(FakeConfluence(), BOTH)["cursor"]
    assert datetime.fromisoformat(cursor.replace("Z", "+00:00")) >= before
