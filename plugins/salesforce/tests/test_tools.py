"""The Salesforce plugin against a small fake that answers like Salesforce's REST API, through the SDK's runner (as
`ops plugin test` runs it), by failure mode: a tool sends a request Salesforce does not take; the listing loses a page,
or its watermark, at nextRecordsUrl; a `since` that is not a date changes the SOQL the plugin builds; an article's
attachment is not listed as its child; and a tool reaches a host other than the instance."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ops_plugin_salesforce import plugin

from ops_plugin_sdk import BadInput, EgressDenied
from ops_plugin_sdk.testing import Fake, call

INSTANCE = "https://acme.my.salesforce.com"
API = "/services/data/v61.0"
SETTINGS = {"instance_url": INSTANCE}
ARTICLES = [
    {"Id": f"ka{i}", "Title": f"Article {i}", "LastModifiedDate": f"2026-10-0{i}T09:00:00.000+0000"} for i in (1, 2, 3)
]


class FakeSalesforce:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}])
        if path == f"{API}/query" and "Knowledge__kav" in request.url.params["q"]:
            return httpx.Response(
                200, json={"done": False, "records": ARTICLES[:2], "nextRecordsUrl": f"{API}/query/01g-2"}
            )
        if path == f"{API}/query/01g-2":
            return httpx.Response(200, json={"done": True, "records": ARTICLES[2:]})
        if path == f"{API}/query":
            return httpx.Response(200, json={"done": True, "records": [{"Id": "500A", "CaseNumber": "00044818"}]})
        if path == f"{API}/sobjects/Case/describe":
            return httpx.Response(200, json={"fields": [{"name": "Id"}, {"name": "Status"}]})
        if path == f"{API}/sobjects/Case/500A" and request.method == "PATCH":
            return httpx.Response(204)
        if path == f"{API}/sobjects/Knowledge__kav/ka1":
            att = {"Id": "ka1a", "Title": "pin.txt", "LastModifiedDate": "2026-10-01T09:00:00.000+0000"}
            body = {**ARTICLES[0], "Body__c": "<p>Reset a PIN.</p>", "attachments": [att]}
            return httpx.Response(200, json=body)
        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": path}])


@pytest.fixture
def sf() -> tuple[FakeSalesforce, Fake]:
    fake = FakeSalesforce()
    return fake, Fake(httpx.Client(transport=httpx.MockTransport(fake)), "tok")


def test_query_sends_the_soql_as_q(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    out = call(plugin, "query", {"q": "SELECT Id FROM Case WHERE CaseNumber = '00044818'"}, SETTINGS, client)
    assert out["records"][0]["CaseNumber"] == "00044818"
    assert fake.seen[-1].url.params["q"].startswith("SELECT Id FROM Case")


def test_describe_lists_field_names(sf: tuple[FakeSalesforce, Fake]) -> None:
    assert call(plugin, "describe", {"object": "Case"}, SETTINGS, sf[1]) == ["Id", "Status"]


def test_update_sends_only_the_fields(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    call(plugin, "update_record", {"object": "Case", "id": "500A", "fields": {"Status": "Escalated"}}, SETTINGS, client)
    assert fake.seen[-1].method == "PATCH" and json.loads(fake.seen[-1].content) == {"Status": "Escalated"}


def test_the_listing_follows_next_records_url_and_ends_with_a_watermark(sf: tuple[FakeSalesforce, Fake]) -> None:
    first: dict[str, Any] = call(plugin, "list_articles", {}, SETTINGS, sf[1])
    assert [i["id"] for i in first["items"]] == ["ka1", "ka2"] and "watermark" not in first
    last: dict[str, Any] = call(plugin, "list_articles", {"cursor": first["next_cursor"]}, SETTINGS, sf[1])
    assert [i["id"] for i in last["items"]] == ["ka3"] and "next_cursor" not in last
    assert last["watermark"] == ARTICLES[2]["LastModifiedDate"]


def test_since_is_parsed_into_soql_and_anything_else_refused(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    call(plugin, "list_articles", {"since": "2026-10-01T09:00:00Z"}, SETTINGS, client)
    assert "LastModifiedDate > 2026-10-01T09:00:00Z" in fake.seen[-1].url.params["q"]
    sent = len(fake.seen)
    with pytest.raises(BadInput, match="since must be a date"):
        call(plugin, "list_articles", {"since": "2026' OR Name != '"}, SETTINGS, client)
    assert len(fake.seen) == sent


def test_an_article_reads_as_a_knowledge_item_with_its_attachment(sf: tuple[FakeSalesforce, Fake]) -> None:
    item: dict[str, Any] = call(plugin, "get_article", {"id": "ka1"}, SETTINGS, sf[1])
    assert (item["id"], item["mime"], item["content"]) == ("ka1", "text/html", "<p>Reset a PIN.</p>")
    assert item["url"] == f"{INSTANCE}/ka1"
    assert [c["id"] for c in item["children"]] == ["ka1a"]


def test_a_tool_reaches_only_the_instance(sf: tuple[FakeSalesforce, Fake]) -> None:
    with pytest.raises(EgressDenied):
        call(plugin, "list_articles", {"cursor": "@evil.example/steal"}, SETTINGS, sf[1])
