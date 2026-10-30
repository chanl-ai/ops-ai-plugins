"""list_articles, the plugin's one Python tool: it follows Salesforce's nextRecordsUrl and puts a date into SOQL,
which a request template cannot do safely (plugin-contents-and-knowledge.html section 05)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ops_plugin_sdk import BadInput, Context, tool

FIELDS = "Id, Title, LastModifiedDate"


def _stamp(since: str) -> str:
    """`since` as SOQL's datetime literal. It is parsed, never pasted, so a value that is not a date is refused
    before any request and cannot change the query."""
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError as e:
        raise BadInput(f"since must be a date and time, such as 2026-10-01T09:00:00Z; got {since[:40]!r}") from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return f"{parsed.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"


@tool(access="read")
def list_articles(ctx: Context, since: str | None = None, cursor: str | None = None) -> dict[str, Any]:
    """One page of published Knowledge articles changed after `since`, oldest first; `cursor` is the page after."""
    if cursor:
        # Opened from its seal by tools-service: an address under the instance that Salesforce gave on the page before.
        r = ctx.http.get(f"{ctx.settings.instance_url}{cursor}")
    else:
        where = "PublishStatus = 'Online'"
        if since:
            where += f" AND LastModifiedDate > {_stamp(since)}"
        soql = f"SELECT {FIELDS} FROM Knowledge__kav WHERE {where} ORDER BY LastModifiedDate, Id"
        base = f"{ctx.settings.instance_url}/services/data/v{ctx.settings.api_version}"
        r = ctx.http.get(f"{base}/query", params={"q": soql})
    body = r.json()
    items = [{"id": a["Id"], "title": a["Title"], "updated_at": a["LastModifiedDate"]} for a in body["records"]]
    out: dict[str, Any] = {"items": items}
    if body.get("nextRecordsUrl"):
        out["next_cursor"] = body["nextRecordsUrl"]
    if body.get("done") and items:
        out["watermark"] = items[-1]["updated_at"]
    return out
