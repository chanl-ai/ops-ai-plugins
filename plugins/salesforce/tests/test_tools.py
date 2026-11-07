"""The Salesforce plugin against a small fake that answers like Salesforce's REST API, through the SDK's runner (as
`ops plugin test` runs it), by failure mode: a tool sends a request Salesforce does not take; a model's value changes
the query it goes into (a status outside its list, a date that is not a date, SOSL's reserved characters); a listing
loses a page or its watermark at nextRecordsUrl, an offset or a token; deleted cases leak into a normal query or go
missing from queryAll; an article's attachment is not listed as its child; and a tool reaches a host other than the
instance."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ops_plugin_salesforce import plugin

from ops_plugin_sdk import EgressDenied
from ops_plugin_sdk.formats import InputRefused
from ops_plugin_sdk.testing import Fake, call

INSTANCE = "https://acme.my.salesforce.com"
API = "/services/data/v61.0"
SETTINGS = {"instance_url": INSTANCE}
CASES = [
    {"Id": "500A", "CaseNumber": "00044818", "Status": "Escalated", "IsDeleted": False},
    {"Id": "500B", "CaseNumber": "00044819", "Status": "Escalated", "IsDeleted": False},
    {"Id": "500C", "CaseNumber": "00044820", "Status": "Escalated", "IsDeleted": True},
]
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
        if path == f"{API}/query" and "FROM Case" in request.url.params["q"]:
            return httpx.Response(
                200, json={"done": False, "records": CASES[:1], "nextRecordsUrl": f"{API}/query/01g-c2"}
            )
        if path == f"{API}/query/01g-c2":
            return httpx.Response(200, json={"done": True, "records": CASES[1:2]})
        if path == f"{API}/queryAll":
            return httpx.Response(200, json={"done": True, "records": CASES})
        if path == f"{API}/search":
            return httpx.Response(200, json={"searchRecords": [{"attributes": {"type": "Account"}, "Id": "001A"}]})
        if path == f"{API}/parameterizedSearch":
            offset, limit = int(request.url.params["offset"]), int(request.url.params["overallLimit"])
            rows = [{"Id": f"500{i}"} for i in range(30)][offset : offset + limit]
            return httpx.Response(200, json={"searchRecords": rows})
        if path == f"{API}/sobjects/Account/001A00000000001AAA/Cases":
            return httpx.Response(
                200, json={"done": False, "records": CASES[:1], "nextRecordsUrl": f"{API}/query/01g-c2"}
            )
        if path == f"{API}/sobjects/Case/updated/":
            return httpx.Response(200, json={"ids": ["500A"], "latestDateCovered": request.url.params["end"]})
        if path == f"{API}/ui-api/list-records/00B000000000001":
            token = request.url.params.get("pageToken")
            row = {
                "id": "500B" if token else "500A",
                "fields": {"Subject": {"value": "Card declined", "displayValue": None}},
            }
            return httpx.Response(200, json={"records": [row], "nextPageToken": None if token else "T2"})
        if path == f"{API}/query":
            return httpx.Response(200, json={"done": True, "records": [{"Id": "500A", "CaseNumber": "00044818"}]})
        if path == f"{API}/sobjects/Case/describe":
            return httpx.Response(200, json={"fields": [{"name": "Id"}, {"name": "Status"}]})
        if path == f"{API}/sobjects/Account/001A" and request.method == "DELETE":
            return httpx.Response(204)
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


def test_delete_sends_a_delete_to_the_record_and_nothing_else(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    call(plugin, "delete_record", {"object": "Account", "id": "001A"}, SETTINGS, client)
    sent = fake.seen[-1]
    assert sent.method == "DELETE" and sent.url.path.endswith("/sobjects/Account/001A") and not sent.content


def test_the_listing_follows_next_records_url_and_ends_with_a_watermark(sf: tuple[FakeSalesforce, Fake]) -> None:
    first: dict[str, Any] = call(plugin, "list_articles", {}, SETTINGS, sf[1])
    assert [i["id"] for i in first["items"]] == ["ka1", "ka2"] and "watermark" not in first
    last: dict[str, Any] = call(plugin, "list_articles", {"cursor": first["next_cursor"]}, SETTINGS, sf[1])
    assert [i["id"] for i in last["items"]] == ["ka3"] and "next_cursor" not in last
    assert last["watermark"] == ARTICLES[2]["LastModifiedDate"]


def test_find_cases_writes_the_status_and_date_into_soql_and_pages_by_next_records_url(
    sf: tuple[FakeSalesforce, Fake],
) -> None:
    fake, client = sf
    first: dict[str, Any] = call(
        plugin, "find_cases", {"status": "Escalated", "since": "2026-10-01T11:00:00+02:00"}, SETTINGS, client
    )
    q = fake.seen[-1].url.params["q"]
    assert "Status = 'Escalated'" in q and "LastModifiedDate > 2026-10-01T09:00:00Z" in q, q
    assert [r["Id"] for r in first["records"]] == ["500A"] and "nextRecordsUrl" not in first
    second: dict[str, Any] = call(plugin, "find_cases", {"cursor": first["next_cursor"]}, SETTINGS, client)
    assert [r["Id"] for r in second["records"]] == ["500B"] and "next_cursor" not in second


@pytest.mark.parametrize(
    ("args", "said"),
    [
        ({"status": "Escalated' OR Status != '"}, "status must be one of"),
        ({"status": "New", "since": "2026' OR Name != '"}, "since must be a date and time"),
    ],
)
def test_a_value_that_would_change_the_query_never_reaches_salesforce(
    sf: tuple[FakeSalesforce, Fake], args: dict[str, Any], said: str
) -> None:
    fake, client = sf
    with pytest.raises(InputRefused, match=said):
        call(plugin, "find_cases", args, SETTINGS, client)
    assert fake.seen == []


def test_deleted_cases_come_only_with_query_all(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    every: dict[str, Any] = call(plugin, "find_cases_with_deleted", {"status": "Escalated"}, SETTINGS, client)
    assert fake.seen[-1].url.path == f"{API}/queryAll" and "IsDeleted" in fake.seen[-1].url.params["q"]
    assert [r["Id"] for r in every["records"] if r["IsDeleted"]] == ["500C"]
    call(plugin, "find_cases", {"status": "Escalated"}, SETTINGS, client)
    assert fake.seen[-1].url.path == f"{API}/query"


def test_a_search_term_stays_inside_find(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    call(plugin, "search", {"term": "card} RETURNING User(Email"}, SETTINGS, client)
    q = fake.seen[-1].url.params["q"]
    assert q.startswith("FIND {card\\} RETURNING User\\(Email} IN ALL FIELDS RETURNING Account"), q


def test_a_search_by_fields_sends_the_chosen_fields_and_pages_by_offset(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    args = {"term": "declined", "status": "New", "fields": ["Id", "Subject"]}
    first: dict[str, Any] = call(plugin, "search_by_fields", args, SETTINGS, client)
    sent = fake.seen[-1].url.params
    assert (sent["Case.fields"], sent["Case.where"], sent["sobject"]) == ("Id,Subject", "Status = 'New'", "Case")
    assert (sent["offset"], sent["overallLimit"]) == ("0", "25") and len(first["searchRecords"]) == 25
    second: dict[str, Any] = call(plugin, "search_by_fields", {"cursor": first["next_cursor"]}, SETTINGS, client)
    assert fake.seen[-1].url.params["offset"] == "25" and len(second["searchRecords"]) == 5
    assert "next_cursor" not in second
    with pytest.raises(InputRefused, match="fields\\[0\\] must be one of"):
        call(plugin, "search_by_fields", {**args, "fields": ["Email"]}, SETTINGS, client)


def test_an_accounts_cases_take_only_an_id_and_page_by_link(sf: tuple[FakeSalesforce, Fake]) -> None:
    client = sf[1]
    with pytest.raises(InputRefused, match="a Salesforce id"):
        call(plugin, "account_cases", {"account_id": "../Contact"}, SETTINGS, client)
    first: dict[str, Any] = call(plugin, "account_cases", {"account_id": "001A00000000001AAA"}, SETTINGS, client)
    second: dict[str, Any] = call(plugin, "account_cases", {"cursor": first["next_cursor"]}, SETTINGS, client)
    assert [r["Id"] for r in first["records"] + second["records"]] == ["500A", "500B"]


def test_cases_changed_sends_two_times_in_utc(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    args = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T02:00:00+02:00"}
    out: dict[str, Any] = call(plugin, "cases_changed", args, SETTINGS, client)
    sent = fake.seen[-1].url.params
    assert (sent["start"], sent["end"]) == ("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z") and out["ids"] == ["500A"]


def test_a_list_view_reads_flat_fields_and_pages_by_token(sf: tuple[FakeSalesforce, Fake]) -> None:
    fake, client = sf
    first: dict[str, Any] = call(plugin, "list_view", {"list_view": "00B000000000001"}, SETTINGS, client)
    assert first["records"] == [{"id": "500A", "fields": {"Subject": "Card declined"}}], first
    second: dict[str, Any] = call(plugin, "list_view", {"cursor": first["next_cursor"]}, SETTINGS, client)
    assert fake.seen[-1].url.params["pageToken"] == "T2" and second["records"][0]["id"] == "500B"
    assert "next_cursor" not in second


def test_an_article_reads_as_a_knowledge_item_with_its_attachment(sf: tuple[FakeSalesforce, Fake]) -> None:
    item: dict[str, Any] = call(plugin, "get_article", {"id": "ka1"}, SETTINGS, sf[1])
    assert (item["id"], item["mime"], item["content"]) == ("ka1", "text/html", "<p>Reset a PIN.</p>")
    assert item["url"] == f"{INSTANCE}/ka1"
    assert [c["id"] for c in item["children"]] == ["ka1a"]


def test_a_tool_reaches_only_the_instance(sf: tuple[FakeSalesforce, Fake]) -> None:
    cursor = json.dumps({"link": "https://evil.example/steal", "args": {}})
    with pytest.raises(EgressDenied):
        call(plugin, "list_articles", {"cursor": cursor}, SETTINGS, sf[1])
