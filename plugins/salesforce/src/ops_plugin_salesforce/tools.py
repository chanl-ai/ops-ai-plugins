"""Python tools for what a manifest entry cannot describe.

`save_records` sends several record writes as one Salesforce composite request with allOrNone, so a change made of
two writes (an opportunity, then the task that notes the meeting on it) is one call: one approval in Ops AI's write
gate, applied in order, and nothing applied when any part is refused.
"""

from __future__ import annotations

import re
from typing import Any, cast

from ops_plugin_sdk import BadInput, Context, PluginError, tool

# Salesforce's own cap is 25 subrequests; a change a person reviews in one card stays far smaller.
MAX_STEPS = 5
OBJECT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
RECORD_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
REF = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
# A later step names an earlier one's new record as "@{ref.id}" in a field or as its id.
REFERENCE = re.compile(r"^@\{([A-Za-z][A-Za-z0-9_]{0,39})\.id\}$")


def _step(i: int, raw: Any, refs: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BadInput(
            f"Step {i + 1} is not an object with object, fields and an optional id and ref."
        )
    step = cast(dict[str, Any], raw)
    obj, record, ref = (
        str(step.get("object") or ""),
        step.get("id"),
        str(step.get("ref") or f"step{i + 1}"),
    )
    fields = step.get("fields")
    if not OBJECT.match(obj):
        raise BadInput(
            f"Step {i + 1} names no object, or one that is not a Salesforce object name."
        )
    if not isinstance(fields, dict) or not fields:
        raise BadInput(f"Step {i + 1} has no fields to save.")
    if not REF.match(ref) or ref in refs:
        raise BadInput(
            f"Step {i + 1}'s ref must be a new name of letters, digits and underscores."
        )
    if record is not None:
        named = REFERENCE.match(str(record))
        if not (RECORD_ID.match(str(record)) or (named and named.group(1) in refs)):
            raise BadInput(
                f"Step {i + 1}'s id is neither a Salesforce id nor an earlier step's @{{ref.id}}."
            )
    for value in cast(dict[str, Any], fields).values():
        named = REFERENCE.match(str(value)) if isinstance(value, str) else None
        if named and named.group(1) not in refs:
            raise BadInput(
                f"Step {i + 1} names @{{{named.group(1)}.id}}, which no earlier step makes."
            )
    refs.add(ref)
    return {"object": obj, "id": record, "fields": fields, "ref": ref}


def _fields(error: dict[str, Any]) -> str:
    names = [str(f) for f in cast(list[Any], error.get("fields") or []) if f]
    return f" {', '.join(names)}" if names else ""


@tool(access="write")
def save_records(ctx: Context, records: list[Any]) -> Any:
    """Creates and changes records in order, all or none: each step is {object, fields, id?, ref?}; a step with an id
    changes that record, one without creates it."""
    if not isinstance(records, list) or not records or len(records) > MAX_STEPS:
        raise BadInput(f"Give one to {MAX_STEPS} steps to save.")
    refs: set[str] = set()
    steps = [_step(i, r, refs) for i, r in enumerate(cast(list[Any], records))]
    base = f"/services/data/v{ctx.settings.api_version}/sobjects"
    body = {
        "allOrNone": True,
        "compositeRequest": [
            {
                "method": "PATCH" if s["id"] else "POST",
                "url": f"{base}/{s['object']}/{s['id']}"
                if s["id"]
                else f"{base}/{s['object']}",
                "referenceId": s["ref"],
                "body": s["fields"],
            }
            for s in steps
        ],
    }
    answer = ctx.http.post(
        f"{ctx.settings.instance_url}/services/data/v{ctx.settings.api_version}/composite",
        json=body,
    )
    if answer.status_code >= 400:
        raise PluginError(
            f"Salesforce refused the save ({answer.status_code}): {answer.text[:300]}"
        )
    results = cast(list[dict[str, Any]], answer.json().get("compositeResponse") or [])
    saved: list[dict[str, Any]] = []
    failed: list[str] = []
    for s, r in zip(steps, results, strict=False):
        status = int(r.get("httpStatusCode") or 0)
        got = r.get("body")
        if status >= 400:
            errors = cast(list[dict[str, Any]], got if isinstance(got, list) else [])
            # PROCESSING_HALTED marks the steps not applied because another failed; the cause is the other one.
            real = [e for e in errors if e.get("errorCode") != "PROCESSING_HALTED"]
            # Salesforce names the fields it refused, so the review can mark each one.
            failed += [
                f"{s['object']}{_fields(e)}: {e.get('message') or e.get('errorCode')}"
                for e in real
            ]
            continue
        made = cast(dict[str, Any], got).get("id") if isinstance(got, dict) else None
        saved.append(
            {
                "object": s["object"],
                "id": made or s["id"],
                "action": "updated" if s["id"] else "created",
            }
        )
    if failed or len(saved) != len(steps):
        raise PluginError(
            "Nothing was saved: "
            + "; ".join(failed or ["Salesforce did not answer every step."])
        )
    return {"saved": saved}
