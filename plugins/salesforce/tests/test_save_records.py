"""Saving a meeting's outcome, by failure mode: the opportunity and the task that notes the meeting reach Salesforce as
two calls, so one can apply without the other or need two approvals; the task cannot name the opportunity the same save
creates; a refused step leaves the other applied; a model's malformed step reaches Salesforce; and a picklist's values
are not offered, so a stage outside the list is saved."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ops_plugin_salesforce import plugin

from ops_plugin_sdk import BadInput, PluginError
from ops_plugin_sdk.testing import Fake, call

INSTANCE = "https://acme.my.salesforce.com"
API = "/services/data/v61.0"
SETTINGS = {"instance_url": INSTANCE}
OPP = {
    "object": "Opportunity",
    "ref": "opp",
    "fields": {"Name": "Okafor - Education", "StageName": "Prospecting"},
}
TASK = {
    "object": "Task",
    "fields": {"Subject": "Meeting", "WhatId": "@{opp.id}", "Status": "Completed"},
}


class Composite:
    """Salesforce's composite resource, all or none: the second step fails when `refuse` is set."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{API}/sobjects/Opportunity/describe":
            fields = [
                {
                    "name": "StageName",
                    "type": "picklist",
                    "picklistValues": [
                        {"value": "Prospecting", "active": True},
                        {"value": "Retired", "active": False},
                    ],
                },
                {"name": "Name", "type": "string", "picklistValues": []},
            ]
            return httpx.Response(200, json={"name": "Opportunity", "fields": fields})
        assert request.url.path == f"{API}/composite", request.url.path
        body = json.loads(request.content)
        self.sent.append(body)
        steps = body["compositeRequest"]
        if self.refuse:
            out = [
                {
                    "httpStatusCode": 400,
                    "referenceId": s["referenceId"],
                    "body": [
                        {
                            "errorCode": "PROCESSING_HALTED"
                            if i == 0
                            else "REQUIRED_FIELD_MISSING",
                            "message": "Subject",
                        }
                    ],
                }
                for i, s in enumerate(steps)
            ]
        else:
            out = [
                {
                    "httpStatusCode": 201,
                    "referenceId": s["referenceId"],
                    "body": {"id": f"00{i}X"},
                }
                for i, s in enumerate(steps)
            ]
        return httpx.Response(200, json={"compositeResponse": out})


def fake(sf: Composite) -> Fake:
    return Fake(httpx.Client(transport=httpx.MockTransport(sf)), "tok")


def test_both_records_go_in_one_all_or_none_request_in_order_and_the_task_names_the_new_opportunity() -> (
    None
):
    sf = Composite()
    out = call(plugin, "save_records", {"records": [OPP, TASK]}, SETTINGS, fake(sf))
    assert len(sf.sent) == 1 and sf.sent[0]["allOrNone"] is True
    steps = sf.sent[0]["compositeRequest"]
    assert [(s["method"], s["url"]) for s in steps] == [
        ("POST", f"{API}/sobjects/Opportunity"),
        ("POST", f"{API}/sobjects/Task"),
    ]
    assert steps[1]["body"]["WhatId"] == "@{opp.id}"
    assert out == {
        "saved": [
            {"object": "Opportunity", "id": "000X", "action": "created"},
            {"object": "Task", "id": "001X", "action": "created"},
        ]
    }


def test_an_update_patches_the_record_it_names() -> None:
    sf = Composite()
    change = {
        "object": "Opportunity",
        "id": "006000000000001AAA",
        "fields": {"StageName": "Proposal"},
    }
    call(plugin, "save_records", {"records": [change]}, SETTINGS, fake(sf))
    assert sf.sent[0]["compositeRequest"][0]["method"] == "PATCH"
    assert sf.sent[0]["compositeRequest"][0]["url"].endswith(
        "/sobjects/Opportunity/006000000000001AAA"
    )


def test_a_refused_step_saves_nothing_and_says_the_real_cause() -> None:
    with pytest.raises(PluginError, match="Nothing was saved: Task: Subject"):
        call(
            plugin,
            "save_records",
            {"records": [OPP, TASK]},
            SETTINGS,
            fake(Composite(refuse=True)),
        )


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"object": "Opportunity; DELETE", "fields": {"Name": "x"}}],
        [{"object": "Task", "fields": {}}],
        [{"object": "Task", "fields": {"WhatId": "@{nope.id}"}}],
        [{"object": "Opportunity", "id": "not-an-id", "fields": {"Name": "x"}}],
    ],
)
def test_a_malformed_step_never_reaches_salesforce(records: list[Any]) -> None:
    sf = Composite()
    with pytest.raises(BadInput):
        call(plugin, "save_records", {"records": records}, SETTINGS, fake(sf))
    assert sf.sent == []


def test_field_values_offers_only_a_picklists_active_values() -> None:
    assert call(
        plugin, "field_values", {"object": "Opportunity"}, SETTINGS, fake(Composite())
    ) == {"StageName": ["Prospecting"]}
