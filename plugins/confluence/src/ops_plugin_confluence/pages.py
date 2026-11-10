"""Confluence pages as a knowledge source, over the REST API v1 that Cloud and Data Center both serve.

A source names spaces by key. The full listing reads each space's current pages (`/rest/api/content`), one REST page
per call, following the `_links.next` address the site gives rather than counting offsets, since Cloud pages some
listings by cursor. Changes are a CQL query, `lastmodified > "<time>"` within the picked spaces, ordered by that time.

Cursors: a pass's first call records when it began. A page of a pass carries `{"pass", "i", "next"}` (and `since` for
changes); the last page's `cursor` is the pass's start time, which the next changes pass reads from. CQL reads a date
literal in the searching user's time zone, which the plugin does not know, so a pass starts `OVERLAP` before the time
it was given; pages seen twice cost nothing, because kb-service skips a page whose version number did not move.

Page ids are numbers. An attachment is `att:<id>`, listed as a child of its page when the source asks for attachments,
and is read through the same get tool. Cloud serves an attachment's bytes from Atlassian's media host after a redirect
that this module follows, because tools do not follow redirects."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote, urljoin

from ops_plugin_sdk import BadInput, Context, NotFound, PluginError, Response, TooLarge, tool

LIMIT = 50
# tools-service refuses an answer larger than this.
MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 303, 307, 308)
# More than any two time zones can differ (UTC-12 to UTC+14 is 26 hours).
OVERLAP = timedelta(hours=27)
# Keys go inside CQL quotes, so no quote, backslash or space can be part of one.
SPACE_KEY = re.compile(r"^[A-Za-z0-9~_.\-]{1,255}$")
PAGE_ID = re.compile(r"^[0-9]{1,20}$")
ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
ATT = "att:"
TEXT_TYPES = ("application/json", "application/xml", "application/x-yaml", "text/")
SPACES_PER_CALL = 5
SPACE_MATCHES = 20


def _base(ctx: Context) -> str:
    return str(ctx.settings.site_url).rstrip("/")


def _spaces(source: dict[str, Any]) -> list[str]:
    keys = [str(k).strip() for k in cast(list[Any], source.get("spaces") or []) if str(k).strip()]
    for key in keys:
        if not SPACE_KEY.match(key):
            raise BadInput(f"{key[:40]!r} is not a space key.")
    if not keys:
        raise BadInput("The source names no spaces.")
    return list(dict.fromkeys(keys))


def _stamp(when: datetime) -> str:
    return f"{when.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"


def _parse(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as e:
        raise BadInput("The cursor is not one this plugin issued.") from e


def _next_link(link: str) -> str:
    """A page address the site named, relative to the site address; refused unless it is a REST path."""
    if not link.startswith("/rest/api/"):
        raise BadInput("The cursor names a page outside the Confluence REST API.")
    return link


def _fail(r: Response, what: str) -> PluginError:
    detail = ""
    try:
        detail = str(r.json().get("message") or "")[:120]
    except (ValueError, AttributeError):
        pass
    return PluginError(f"{what} answered {r.status_code}{': ' + detail if detail else ''}.")


def _get(ctx: Context, path: str, what: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = ctx.http.get(_base(ctx) + path, params=params)
    if r.status_code >= 400:
        raise _fail(r, what)
    return cast(dict[str, Any], r.json())


def _row(ctx: Context, page: dict[str, Any], space: str | None = None) -> dict[str, Any]:
    version = page.get("version") or {}
    key = space or (page.get("space") or {}).get("key")
    ancestors = [str(a["title"]) for a in page.get("ancestors") or [] if a.get("title")]
    row: dict[str, Any] = {
        "id": str(page["id"]),
        "title": page.get("title") or str(page["id"]),
        "updated_at": version.get("when") or _stamp(datetime.now(UTC)),
        "folder": "/".join([p for p in [key, *ancestors] if p]),
    }
    if webui := (page.get("_links") or {}).get("webui"):
        row["url"] = _base(ctx) + webui
    if version.get("number") is not None:
        row["marker"] = str(version["number"])
    return row


def _begin(cursor: str | None, changes: bool) -> dict[str, Any]:
    """The state of the pass a call belongs to: carried in the cursor of a pass under way, started fresh otherwise."""
    now = datetime.now(UTC)
    if cursor is not None and cursor.startswith("{"):
        try:
            st = cast(dict[str, Any], json.loads(cursor))
            str(st["pass"]), int(st["i"])
        except (ValueError, KeyError, TypeError) as e:
            raise BadInput("The cursor is not one this plugin issued.") from e
        # A listing's place means nothing to the changes query and the reverse; kb-service starts again on this.
        if ("since" in st) != changes:
            raise BadInput("The cursor belongs to the other kind of pass.")
        return st
    st = {"pass": _stamp(now), "i": 0, "next": None}
    if changes:
        # CQL reads its date in the searching user's time zone, so the pass reaches back OVERLAP past the saved time.
        st["since"] = _stamp(_parse(cursor) - OVERLAP) if cursor else "1970-01-01T00:00:00Z"
    return st


def _list_page(ctx: Context, key: str, st: dict[str, Any]) -> dict[str, Any]:
    if st.get("next"):
        return _get(ctx, _next_link(st["next"]), f"The pages of space {key}")
    params = {"spaceKey": key, "type": "page", "status": "current", "expand": "version,ancestors", "limit": LIMIT}
    return _get(ctx, "/rest/api/content", f"The pages of space {key}", params)


def _changes_page(ctx: Context, spaces: list[str], st: dict[str, Any]) -> dict[str, Any]:
    if st.get("next"):
        return _get(ctx, _next_link(st["next"]), "The changed pages")
    since = _parse(st["since"]).strftime("%Y/%m/%d %H:%M")
    quoted = ",".join(f'"{k}"' for k in spaces)
    cql = f'type = page AND space in ({quoted}) AND lastmodified > "{since}" ORDER BY lastmodified'
    params = {"cql": cql, "expand": "version,ancestors,space", "limit": LIMIT}
    return _get(ctx, "/rest/api/content/search", "The changed pages", params)


def _pass(ctx: Context, source: dict[str, Any], cursor: str | None, changes: bool) -> dict[str, Any]:
    spaces = _spaces(source)
    st = _begin(cursor, changes)
    if changes:
        data = _changes_page(ctx, spaces, st)
        more = (data.get("_links") or {}).get("next")
        st["next"] = _next_link(more) if more else None
        rows = [_row(ctx, p) for p in data.get("results", [])]
        finished = not more
    else:
        if st["i"] >= len(spaces):
            raise BadInput("The cursor is past the source's last space.")
        key = spaces[st["i"]]
        data = _list_page(ctx, key, st)
        more = (data.get("_links") or {}).get("next")
        if more:
            st["next"] = _next_link(more)
        else:
            st["next"], st["i"] = None, st["i"] + 1
        rows = [_row(ctx, p, key) for p in data.get("results", [])]
        finished = st["i"] >= len(spaces)
    out: dict[str, Any] = {"items": rows}
    if finished:
        out["cursor"] = st["pass"]
    else:
        out["next_cursor"] = json.dumps(st, separators=(",", ":"))
    return out


@tool(access="read")
def list_pages(
    ctx: Context, source: dict[str, Any], since: str | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """The current pages of the picked spaces, one REST page a call. `since` is not used: the listing is whole."""
    del since
    return _pass(ctx, source, cursor, changes=False)


@tool(access="read")
def list_page_changes(ctx: Context, source: dict[str, Any], cursor: str | None = None) -> dict[str, Any]:
    """Pages added or edited since `cursor` (the start time of the pass before). Deletions are not in this feed: the
    weekly full listing finds them."""
    return _pass(ctx, source, cursor, changes=True)


def _is_text(mime: str) -> bool:
    return mime.startswith(TEXT_TYPES)


def _label(who: dict[str, Any]) -> str | None:
    """A user or group as the site names it: Cloud by account id or group id, Data Center by user key or group name."""
    found = who.get("accountId") or who.get("userKey") or who.get("username") or who.get("id") or who.get("name")
    return str(found) if found else None


def _allowed(page: dict[str, Any]) -> list[str]:
    read = ((page.get("restrictions") or {}).get("read") or {}).get("restrictions") or {}
    out: list[str] = []
    for kind in ("user", "group"):
        for who in (read.get(kind) or {}).get("results", []):
            if (label := _label(who)) and label not in out:
                out.append(label)
    return out


def _attachments(ctx: Context, page_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    path: str | None = f"/rest/api/content/{page_id}/child/attachment"
    params: dict[str, Any] | None = {"expand": "version", "limit": 100}
    while path:
        data = _get(ctx, path, f"The attachments of page {page_id}", params)
        for a in data.get("results", []):
            version = a.get("version") or {}
            out.append(
                {
                    "id": ATT + str(a["id"]),
                    "title": a.get("title") or str(a["id"]),
                    "updated_at": version.get("when") or _stamp(datetime.now(UTC)),
                    "marker": str(version.get("number", "")),
                }
            )
        more = (data.get("_links") or {}).get("next")
        path, params = (_next_link(more), None) if more else (None, None)
    return out


def _fetch(ctx: Context, url: str) -> Response:
    for _ in range(MAX_REDIRECTS + 1):
        r = ctx.http.get(url)
        if r.status_code not in REDIRECTS:
            break
        where = r.headers.get("location") or r.headers.get("Location")
        if not where:
            raise PluginError("The site redirected the download without saying where.")
        url = urljoin(url, where)
    else:
        raise PluginError("The download was redirected too many times.")
    if r.status_code in (404, 410):
        raise NotFound(f"The download answered {r.status_code}.")
    if r.status_code >= 400:
        raise _fail(r, "The download")
    return r


def _light(ctx: Context, content_id: str, what: str, expand: str) -> dict[str, Any]:
    r = ctx.http.get(f"{_base(ctx)}/rest/api/content/{content_id}", params={"expand": expand})
    if r.status_code in (404, 410):
        raise NotFound(f"{what} answered {r.status_code}.")
    if r.status_code >= 400:
        raise _fail(r, what)
    data = cast(dict[str, Any], r.json())
    if data.get("status", "current") != "current":
        raise NotFound(f"{what} is {data.get('status')}.")
    return data


def _get_attachment(ctx: Context, raw: str, tag: str | None) -> dict[str, Any]:
    if not ATTACHMENT_ID.match(raw):
        raise BadInput("That is not an attachment id.")
    meta = _light(ctx, raw, f"Attachment {raw}", "version,container")
    version = str((meta.get("version") or {}).get("number", ""))
    if tag and tag == version:
        return {"unchanged": True}
    extensions = meta.get("extensions") or {}
    if int(extensions.get("fileSize") or 0) > MAX_BYTES:
        raise TooLarge(f"{meta.get('title')} is larger than 25 MB.")
    link = (meta.get("_links") or {}).get("download")
    if not link:
        raise NotFound(f"Attachment {raw} has no download.")
    body = _fetch(ctx, _base(ctx) + link)
    title = str(meta.get("title") or raw)
    mime = str(extensions.get("mediaType") or mimetypes.guess_type(title)[0] or "application/octet-stream")
    out: dict[str, Any] = {
        "id": ATT + raw,
        "title": title,
        "mime": mime,
        "metadata": {"page": str((meta.get("container") or {}).get("id") or ""), "version": version},
        "marker": version,
    }
    if web := (meta.get("_links") or {}).get("webui"):
        out["url"] = _base(ctx) + web
    if _is_text(mime):
        out |= {"encoding": "text", "content": body.content.decode("utf-8-sig", errors="replace")}
    else:
        out |= {"encoding": "base64", "content": base64.b64encode(body.content).decode()}
    return out


@tool(access="read")
def get_page(ctx: Context, source: dict[str, Any], id: str, tag: str | None = None) -> dict[str, Any]:
    """One page as HTML (its rendered view), with its attachments as children when the source takes them in; or one
    attachment. `{"unchanged": true}` when `tag` is still the version number. A page gone or trashed is NOT_FOUND."""
    if id.startswith(ATT):
        return _get_attachment(ctx, id[len(ATT) :], tag)
    if not PAGE_ID.match(id):
        raise BadInput(f"{id[:40]!r} is not a page id.")
    meta = _light(ctx, id, f"Page {id}", "version")
    version = str((meta.get("version") or {}).get("number", ""))
    if tag and tag == version:
        return {"unchanged": True}
    expand = "body.export_view,version,ancestors,space,restrictions.read.restrictions.user,restrictions.read.restrictions.group"
    page = _get(ctx, f"/rest/api/content/{quote(id)}", f"Page {id}", {"expand": expand})
    row = _row(ctx, page)
    out: dict[str, Any] = {
        "id": id,
        "title": row["title"],
        "mime": "text/html",
        "encoding": "text",
        "content": ((page.get("body") or {}).get("export_view") or {}).get("value", ""),
        "folder": row["folder"],
        "metadata": {
            "space": (page.get("space") or {}).get("key"),
            "modified_by": ((page.get("version") or {}).get("by") or {}).get("displayName"),
            "version": version,
        },
        "allowed": _allowed(page),
        "marker": version,
    }
    if "url" in row:
        out["url"] = row["url"]
    if source.get("attachments") is True:
        out["children"] = _attachments(ctx, id)
    return out


@tool(access="read")
def list_spaces(
    ctx: Context,
    source: dict[str, Any] | None = None,
    query: str | None = None,
    parent: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Spaces whose name or key contains `query`, the site's space list read a few pages a call and filtered here:
    the space listing has no name filter. `cursor` is the offset to read on from."""
    del source
    if parent:
        return {"scopes": []}
    text = (query or "").strip().lower()
    start = 0
    if cursor:
        if not cursor.isdigit():
            raise BadInput("The cursor is not one this plugin issued.")
        start = int(cursor)
    scopes: list[dict[str, Any]] = []
    more = True
    for _ in range(SPACES_PER_CALL):
        data = _get(ctx, "/rest/api/space", "The space list", {"limit": 100, "start": start})
        results = data.get("results", [])
        start += len(results)
        more = bool((data.get("_links") or {}).get("next")) and bool(results)
        for s in results:
            if text in f"{s.get('name', '')} {s.get('key', '')}".lower():
                scopes.append(
                    {"id": s["key"], "title": s.get("name") or s["key"], "kind": "space", "detail": f"{s.get('type', 'global')} space"}
                )
        if not more or len(scopes) >= SPACE_MATCHES:
            break
    out: dict[str, Any] = {"scopes": scopes}
    if more:
        out["next_cursor"] = str(start)
    return out
