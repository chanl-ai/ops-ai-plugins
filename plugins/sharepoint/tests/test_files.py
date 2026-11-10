"""The SharePoint plugin against a fake Microsoft Graph, through the SDK's runner (as `ops plugin test` runs it), by
failure mode: a saved delta token Graph no longer holds, a library longer than one Graph page, several libraries
sharing one cursor, a binary file sent as text, a file fetched again though its version did not move, a deletion
that never reaches the changes, a file that cannot be read being treated as an error, and the plugin breaking the
knowledge-source contract. No tenant was involved: every answer here is the fake's, shaped after the Graph reference."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fake_graph import LIB_HANDBOOK, LIB_POLICIES, LIB_RUNBOOKS, SITE_HR, SITE_OPS, FakeGraph, b64
from ops_plugin_sharepoint import plugin

from ops_plugin_sdk import BadInput, NotFound
from ops_plugin_sdk.testing import Fake, call, knowledge_contract

SETTINGS = {"sharepoint_url": "https://contoso.sharepoint.test"}
SITES = {"sites": [SITE_HR, SITE_OPS]}


def run(graph: FakeGraph, tool: str, args: dict[str, Any]) -> Any:
    return call(plugin, tool, args, SETTINGS, Fake(httpx.Client(transport=httpx.MockTransport(graph)), "tok"))


def listing(graph: FakeGraph, source: dict[str, Any], tool: str = "list_files", cursor: str | None = None) -> dict[str, Any]:
    """Every row of a listing or changes pass, and the cursor its last page names."""
    rows: list[dict[str, Any]] = []
    pages = 0
    while True:
        page = run(graph, tool, {"source": source, **({"cursor": cursor} if cursor else {})})
        rows += page["items"]
        pages += 1
        cursor = page.get("next_cursor")
        if not cursor:
            return {"items": rows, "cursor": page["cursor"], "pages": pages}


def test_plugin_meets_the_knowledge_contract() -> None:
    graph = FakeGraph()
    problems = knowledge_contract(
        plugin,
        "files",
        Fake(httpx.Client(transport=httpx.MockTransport(graph)), "tok"),
        settings=SETTINGS,
        values=SITES,
        change=lambda: graph.edit(LIB_POLICIES, "p2", b"Claim within 14 days.\n"),
        delete=lambda: graph.remove(LIB_RUNBOOKS, "r1"),
    )
    assert problems == []


def test_scopes_answer_sites_for_a_query_then_a_sites_libraries() -> None:
    graph = FakeGraph()
    sites = run(graph, "list_scopes", {"query": "human"})
    assert sites == {
        "scopes": [{"id": SITE_HR, "title": "Human Resources", "kind": "site", "detail": "contoso.sharepoint.test/sites/hr"}]
    }
    libraries = run(graph, "list_scopes", {"query": "", "parent": SITE_HR})
    assert [(s["id"], s["kind"], s["parent"]) for s in libraries["scopes"]] == [
        (LIB_POLICIES, "library", SITE_HR),
        (LIB_HANDBOOK, "library", SITE_HR),
    ]
    assert libraries["scopes"][0]["detail"] == "640.0 MB used"
    assert len(run(graph, "list_scopes", {})["scopes"]) == 2


def test_a_library_longer_than_one_graph_page_is_listed_whole() -> None:
    graph = FakeGraph(page_size=2)
    for n in range(5):
        graph.add(LIB_POLICIES, f"x{n}", f"extra-{n}.txt", "Policies", b"x")
    got = listing(graph, {"libraries": [LIB_POLICIES]})
    assert got["pages"] > 2
    assert {r["id"] for r in got["items"]} >= {f"{LIB_POLICIES}:x{n}" for n in range(5)}
    assert len({r["id"] for r in got["items"]}) == len(got["items"])


def test_folders_are_not_listed_as_files() -> None:
    got = listing(FakeGraph(), {"libraries": [LIB_POLICIES]})
    assert f"{LIB_POLICIES}:f0" not in {r["id"] for r in got["items"]}


def test_a_delta_token_graph_answers_410_for_means_a_full_listing() -> None:
    graph = FakeGraph()
    first = listing(graph, {"libraries": [LIB_POLICIES]})
    graph.expired.add(str(json.loads(first["cursor"])["done"][LIB_POLICIES]))
    graph.edit(LIB_POLICIES, "p2", b"Claim within 14 days.\n")
    again = listing(graph, {"libraries": [LIB_POLICIES]}, "list_file_changes", first["cursor"])
    # The whole library comes back, not only the edited file, and the cursor is a fresh one Graph still holds.
    assert {r["id"] for r in again["items"]} == {r["id"] for r in first["items"]}
    assert again["cursor"] != first["cursor"]
    graph.edit(LIB_POLICIES, "p1", b"# Leave\nTwenty-five days.\n")
    after = listing(graph, {"libraries": [LIB_POLICIES]}, "list_file_changes", again["cursor"])
    assert [r["id"] for r in after["items"]] == [f"{LIB_POLICIES}:p1"]


def test_several_libraries_keep_one_delta_token_each_in_the_cursor() -> None:
    graph = FakeGraph()
    first = listing(graph, SITES)
    assert sorted(json.loads(first["cursor"])["done"]) == [LIB_HANDBOOK, LIB_POLICIES, LIB_RUNBOOKS]
    graph.edit(LIB_RUNBOOKS, "r1", b"# Restore\nNew steps.\n")
    graph.remove(LIB_HANDBOOK, "h1")
    changes = listing(graph, SITES, "list_file_changes", first["cursor"])
    assert {r["id"] for r in changes["items"]} == {f"{LIB_RUNBOOKS}:r1", f"{LIB_HANDBOOK}:h1"}
    nothing = listing(graph, SITES, "list_file_changes", changes["cursor"])
    assert nothing["items"] == []


def test_a_deleted_file_is_reported_as_deleted_with_a_time() -> None:
    graph = FakeGraph()
    first = listing(graph, {"libraries": [LIB_POLICIES]})
    gone = graph.remove(LIB_POLICIES, "p2")
    changes = listing(graph, {"libraries": [LIB_POLICIES]}, "list_file_changes", first["cursor"])
    row = next(r for r in changes["items"] if r["id"] == gone)
    assert row["deleted"] is True and row["updated_at"]


def test_a_library_picked_after_the_first_run_is_listed_in_full() -> None:
    graph = FakeGraph()
    first = listing(graph, {"libraries": [LIB_POLICIES]})
    changes = listing(graph, {"libraries": [LIB_POLICIES, LIB_RUNBOOKS]}, "list_file_changes", first["cursor"])
    assert [r["id"] for r in changes["items"]] == [f"{LIB_RUNBOOKS}:r1"]


def test_a_pdf_is_returned_as_base64_and_text_as_text() -> None:
    graph = FakeGraph()
    pdf = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p3"})
    assert pdf["encoding"] == "base64"
    assert pdf["content"] == b64(graph.drives[LIB_POLICIES]["p3"].content)
    assert pdf["mime"] == "application/pdf"
    md = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p1"})
    assert (md["encoding"], md["content"]) == ("text", "# Leave\nTwenty days a year.\n")
    assert md["title"] == "leave.md" and md["folder"] == "sites/hr/Policies Library/Policies"


def test_a_file_whose_version_did_not_move_is_not_downloaded_again() -> None:
    graph = FakeGraph()
    first = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p2"})
    before = len(graph.downloads())
    again = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p2", "tag": first["marker"]})
    assert again == {"unchanged": True}
    assert len(graph.downloads()) == before
    graph.edit(LIB_POLICIES, "p2", b"New.\n")
    moved = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p2", "tag": first["marker"]})
    assert moved["content"] == "New.\n"


def test_a_file_gone_at_graph_is_not_found_rather_than_a_failure() -> None:
    graph = FakeGraph()
    gone = graph.remove(LIB_POLICIES, "p2")
    with pytest.raises(NotFound):
        run(graph, "get_file", {"source": SITES, "id": gone})


def test_who_can_read_a_file_is_taken_from_its_permissions() -> None:
    graph = FakeGraph()
    graph.drives[LIB_POLICIES]["p1"].permissions = [
        {"id": "a", "grantedToV2": {"user": {"id": "u-dana", "displayName": "Dana"}}},
        {"id": "b", "grantedToIdentitiesV2": [{"group": {"id": "g-hr"}}, {"user": {"id": "u-dana"}}]},
        {"id": "c", "link": {"scope": "organization", "type": "view"}},
    ]
    got = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p1"})
    assert got["allowed"] == ["u-dana", "g-hr"]


def test_a_file_whose_permissions_graph_refuses_is_still_fetched() -> None:
    graph = FakeGraph()
    graph.forbid_permissions = True
    got = run(graph, "get_file", {"source": SITES, "id": f"{LIB_POLICIES}:p1"})
    assert got["content"] and "allowed" not in got


def test_a_file_a_rule_leaves_out_is_listed_as_skipped_so_an_indexed_copy_leaves() -> None:
    got = listing(
        FakeGraph(), {"libraries": [LIB_POLICIES], "exclude": ["**/Archive/**", "expenses.*"], "include": ["**/*.md", "**/*.pdf"]}
    )
    why = {r["title"]: r.get("skipped") for r in got["items"]}
    assert why == {
        "leave.md": None,
        "expenses.txt": "Excluded by a rule",
        "travel.pdf": "Excluded by a rule",
    }


def test_a_file_over_graphs_size_cap_is_listed_as_skipped() -> None:
    graph = FakeGraph()
    graph.drives[LIB_POLICIES]["p3"].size = 30 * 1024 * 1024
    got = listing(graph, {"libraries": [LIB_POLICIES]})
    assert {r["title"]: r.get("skipped") for r in got["items"]}["travel.pdf"] == "Larger than 25 MB"


def test_a_source_with_no_sites_and_no_libraries_is_refused() -> None:
    with pytest.raises(BadInput):
        run(FakeGraph(), "list_files", {"source": {}})


def test_a_cursor_pointing_outside_graph_is_refused() -> None:
    graph = FakeGraph()
    state = {"drives": [LIB_POLICIES], "i": 0, "next": "https://evil.test/steal", "from": {}, "done": {}}
    with pytest.raises(BadInput):
        run(graph, "list_files", {"source": SITES, "cursor": json.dumps(state)})
