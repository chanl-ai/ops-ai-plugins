"""SharePoint and OneDrive files as a knowledge source, read through Microsoft Graph.

A source names document libraries (drives), directly or as every library of its sites. Each library is listed and
followed with a delta query (`/drives/{id}/root/delta`): the first run enumerates the library and Graph answers with a
deltaLink, which is where the next run starts, so a run after that returns only what was added, changed or deleted. One
call reads one Graph page; the cursor carries the place in the walk across libraries.

The cursor is JSON: the libraries, the index of the one being read, the next page address of that library, the delta
token each library starts from (`from`), and the tokens its finished libraries answered with (`done`). The last page
of a pass holds `done` alone, and that is the cursor the next pass starts from, so the one cursor is also the change
cursor. A library that has no token yet (newly picked) is enumerated in full.

A file is identified as `<drive id>:<item id>`: Graph item ids are unique only within their drive, and the get tool
receives nothing but the id. Content is read through `/content`, which Graph answers with a redirect to the tenant's
SharePoint host; the redirect is followed here because tools do not follow redirects."""

from __future__ import annotations

import base64
import json
import mimetypes
import posixpath
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from ops_plugin_sdk import BadInput, Context, NotFound, PluginError, Response, TooLarge, tool

GRAPH = "https://graph.microsoft.com/v1.0"
# tools-service refuses an answer larger than this, so a larger file is listed as skipped instead of failing its fetch.
MAX_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 303, 307, 308)
# Site and drive ids are written into a path, so nothing that could end a segment or start a query is accepted.
GRAPH_ID = re.compile(r"^[A-Za-z0-9!_.,\-]{1,300}$")
TEXT_TYPES = ("application/json", "application/xml", "application/x-yaml", "text/")


def _id(value: Any, what: str) -> str:
    text = str(value)
    if not GRAPH_ID.match(text):
        raise BadInput(f"{what} {text[:40]!r} is not a Graph id.")
    return text


def _is_site(value: str) -> bool:
    """Graph names a site `<host>,<site collection id>,<web id>`; a drive (library) id has no commas."""
    return value.count(",") == 2


def _in_path(value: str) -> str:
    return quote(value, safe="!,")


@dataclass
class Rules:
    include: list[re.Pattern[str]]
    exclude: list[re.Pattern[str]]

    def says(self, path: str) -> str | None:
        """Why the rules leave the file out, or None when they take it in. Exclude wins."""
        if any(p.match(path) for p in self.exclude):
            return "Excluded by a rule"
        if self.include and not any(p.match(path) for p in self.include):
            return "Excluded: matches no include rule"
        return None


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """ops_ingest's rule syntax: `**` crosses folders, `*` and `?` stay within one, and a pattern without `/` matches
    the file name anywhere. Matched against the path as the library shows it, case-insensitively."""
    pattern = pattern.strip().lstrip("/")
    if "/" not in pattern:
        pattern = "**/" + pattern
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


@dataclass
class Source:
    sites: list[str]
    libraries: list[str]
    rules: Rules


def source_of(source: dict[str, Any] | None) -> Source:
    given = source or {}

    def many(name: str) -> list[str]:
        return [str(v) for v in cast(list[Any], given.get(name) or []) if str(v).strip()]

    include = many("include")
    # An include written as !pattern is an exclude, as the console's rule list writes it.
    exclude = many("exclude") + [p[1:] for p in include if p.startswith("!")]
    include = [p for p in include if not p.startswith("!")]
    return Source(
        sites=[_id(s, "site") for s in many("sites")],
        libraries=[_id(s, "library") for s in many("libraries")],
        rules=Rules([glob_to_regex(p) for p in include], [glob_to_regex(p) for p in exclude]),
    )


def _fail(r: Response, what: str) -> PluginError:
    code = ""
    try:
        code = str(r.json().get("error", {}).get("code") or "")
    except (ValueError, AttributeError):
        pass
    return PluginError(f"{what} answered {r.status_code}{' ' + code if code else ''}.")


def _graph_url(url: str) -> str:
    """A page address Graph named, accepted only on Graph itself so a cursor cannot send the sign-in elsewhere."""
    if not url.startswith(GRAPH + "/"):
        raise BadInput("The cursor names a page outside Microsoft Graph.")
    return url


def _get_json(ctx: Context, url: str, what: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    r = ctx.http.get(url, params=params)
    if r.status_code >= 400:
        raise _fail(r, what)
    return cast(dict[str, Any], r.json())


def _site_drives(ctx: Context, site: str) -> list[str]:
    out: list[str] = []
    url: str | None = f"{GRAPH}/sites/{_in_path(site)}/drives"
    while url:
        data = _get_json(ctx, url, f"The libraries of site {site}")
        out += [str(d["id"]) for d in data.get("value", []) if d.get("driveType", "documentLibrary") == "documentLibrary"]
        nxt = data.get("@odata.nextLink")
        url = _graph_url(nxt) if nxt else None
    return out


def _drives(ctx: Context, src: Source) -> list[str]:
    if not src.sites and not src.libraries:
        raise BadInput("The source names no sites and no libraries.")
    if src.libraries:
        return list(dict.fromkeys(src.libraries))
    drives: list[str] = []
    for site in src.sites:
        drives += _site_drives(ctx, site)
    return list(dict.fromkeys(drives))


def _now() -> str:
    return f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"


def _path(web_url: str | None) -> str:
    """The file's place as the library shows it (`sites/hr/Shared Documents/Policies/leave.docx`). Graph's delta leaves
    parentReference.path out, and webUrl is always there."""
    return unquote(urlsplit(web_url or "").path).lstrip("/")


def _row(drive: str, item: dict[str, Any], rules: Rules) -> dict[str, Any] | None:
    """One delta entry as a listing row: a deletion, a file, or None for a folder or anything else that is not a file."""
    rid = f"{drive}:{item['id']}"
    if "deleted" in item:
        return {"id": rid, "deleted": True, "updated_at": item.get("lastModifiedDateTime") or _now()}
    if "file" not in item:
        return None
    path = _path(item.get("webUrl"))
    row: dict[str, Any] = {
        "id": rid,
        "title": item.get("name") or str(item["id"]),
        "updated_at": item.get("lastModifiedDateTime") or _now(),
        "url": item.get("webUrl"),
        "folder": posixpath.dirname(path),
    }
    if item.get("eTag"):
        row["marker"] = item["eTag"]
    if why := rules.says(path):
        row["skipped"] = why
    elif int(item.get("size") or 0) > MAX_BYTES:
        row["skipped"] = "Larger than 25 MB"
    return row


def _delta_url(drive: str) -> str:
    return f"{GRAPH}/drives/{_in_path(drive)}/root/delta"


def _token(delta_link: str) -> str:
    values = parse_qs(urlsplit(delta_link).query).get("token")
    if not values:
        raise PluginError("Graph's delta answer holds no token to continue from.")
    return values[0]


def _state(ctx: Context, src: Source, cursor: str | None) -> dict[str, Any]:
    if cursor is None:
        return {"drives": _drives(ctx, src), "i": 0, "next": None, "from": {}, "done": {}}
    try:
        st = cast(dict[str, Any], json.loads(cursor))
        drives, i = cast(list[str], st["drives"]), int(st["i"])
    except (ValueError, KeyError, TypeError) as e:
        raise BadInput("The cursor is not one this plugin issued.") from e
    if i >= len(drives):
        # A finished pass starts the next one. Libraries are read again from the source, so a newly picked library is
        # enumerated in full and a dropped one is left alone.
        return {"drives": _drives(ctx, src), "i": 0, "next": None, "from": st.get("done") or {}, "done": {}}
    return st


def _page(ctx: Context, source: dict[str, Any] | None, cursor: str | None) -> dict[str, Any]:
    src = source_of(source)
    st = _state(ctx, src, cursor)
    rows: list[dict[str, Any]] = []
    if st["i"] < len(st["drives"]):
        drive = st["drives"][st["i"]]
        resuming = st.get("next") or drive in st["from"]
        if st.get("next"):
            r = ctx.http.get(_graph_url(st["next"]))
        elif drive in st["from"]:
            r = ctx.http.get(_delta_url(drive), params={"token": st["from"][drive]})
        else:
            r = ctx.http.get(_delta_url(drive))
        if r.status_code == 410 and resuming:
            # Graph no longer holds the saved place (resyncGone): the library is enumerated again from its start. Rows
            # whose marker did not move cost nothing to sync, and what was deleted meanwhile is found by the weekly
            # full listing.
            st["from"].pop(drive, None)
            st["next"] = None
            r = ctx.http.get(_delta_url(drive))
        if r.status_code >= 400:
            raise _fail(r, f"The delta query of library {drive}")
        data = cast(dict[str, Any], r.json())
        for item in data.get("value", []):
            if row := _row(drive, item, src.rules):
                rows.append(row)
        if nxt := data.get("@odata.nextLink"):
            st["next"] = _graph_url(nxt)
        else:
            link = data.get("@odata.deltaLink")
            if not link:
                raise PluginError(f"Graph's delta answer for library {drive} named no next page and no deltaLink.")
            st["done"][drive] = _token(link)
            st["from"].pop(drive, None)
            st["next"] = None
            st["i"] += 1
    out: dict[str, Any] = {"items": rows}
    if st["i"] < len(st["drives"]):
        out["next_cursor"] = json.dumps(st, separators=(",", ":"))
    else:
        out["cursor"] = json.dumps(
            {"drives": st["drives"], "i": len(st["drives"]), "done": st["done"]}, separators=(",", ":")
        )
    return out


@tool(access="read")
def list_files(
    ctx: Context, source: dict[str, Any], since: str | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """The files of the picked libraries, one Graph page a call. `since` is not used: a delta query names its own start."""
    del since
    return _page(ctx, source, cursor)


@tool(access="read")
def list_file_changes(ctx: Context, source: dict[str, Any], cursor: str | None = None) -> dict[str, Any]:
    """What the picked libraries gained, changed or lost since `cursor`; a deleted file is `{id, deleted: true}`."""
    return _page(ctx, source, cursor)


def _split(item_id: str) -> tuple[str, str]:
    drive, sep, item = item_id.partition(":")
    if not sep:
        raise BadInput(f"{item_id[:40]!r} is not a file id of this source.")
    return _id(drive, "library"), _id(item, "file")


def _is_text(mime: str) -> bool:
    return mime.startswith(TEXT_TYPES)


def _mime(meta: dict[str, Any]) -> str:
    named = str((meta.get("file") or {}).get("mimeType") or "")
    if named and named != "application/octet-stream":
        return named
    return mimetypes.guess_type(str(meta.get("name") or ""))[0] or named or "application/octet-stream"


def _download(ctx: Context, drive: str, item: str) -> Response:
    url = f"{GRAPH}/drives/{_in_path(drive)}/items/{_in_path(item)}/content"
    for _ in range(MAX_REDIRECTS + 1):
        r = ctx.http.get(url)
        if r.status_code not in REDIRECTS:
            break
        where = r.headers.get("location") or r.headers.get("Location")
        if not where:
            raise PluginError("Graph redirected the download without saying where.")
        url = urljoin(url, where)
    else:
        raise PluginError("The download was redirected too many times.")
    if r.status_code in (404, 410):
        raise NotFound(f"The file answered {r.status_code}.")
    if r.status_code >= 400:
        raise _fail(r, "The download")
    return r


def _allowed(ctx: Context, drive: str, item: str) -> list[str] | None:
    """User and group ids that hold a permission on the file. Reading permissions needs more than read access on many
    files, so a refusal leaves the list out instead of failing the file."""
    r = ctx.http.get(f"{GRAPH}/drives/{_in_path(drive)}/items/{_in_path(item)}/permissions")
    if r.status_code >= 400:
        return None
    found: list[str] = []
    for perm in cast(list[dict[str, Any]], r.json().get("value", [])):
        identities = [perm.get("grantedToV2"), *(perm.get("grantedToIdentitiesV2") or [])]
        for who in identities:
            for kind in ("user", "group", "siteUser", "siteGroup"):
                ident = (who or {}).get(kind) or {}
                ident_id = ident.get("id") or ident.get("loginName")
                if ident_id and ident_id not in found:
                    found.append(str(ident_id))
    return found


@tool(access="read")
def get_file(ctx: Context, source: dict[str, Any], id: str, tag: str | None = None) -> dict[str, Any]:
    """One file, or `{"unchanged": true}` when `tag` (its eTag) is still the file's. A file gone is NOT_FOUND."""
    del source
    drive, item = _split(id)
    r = ctx.http.get(f"{GRAPH}/drives/{_in_path(drive)}/items/{_in_path(item)}")
    if r.status_code in (404, 410):
        raise NotFound(f"{id} answered {r.status_code}.")
    if r.status_code >= 400:
        raise _fail(r, f"File {id}")
    meta = cast(dict[str, Any], r.json())
    if "file" not in meta:
        raise NotFound(f"{id} is not a file.")
    etag = meta.get("eTag")
    if tag and etag and tag == etag:
        return {"unchanged": True}
    if int(meta.get("size") or 0) > MAX_BYTES:
        raise TooLarge(f"{meta.get('name')} is larger than 25 MB.")
    body = _download(ctx, drive, item)
    mime = _mime(meta)
    path = _path(meta.get("webUrl"))
    out: dict[str, Any] = {
        "id": id,
        "title": meta.get("name") or item,
        "mime": mime,
        "url": meta.get("webUrl"),
        "folder": posixpath.dirname(path),
        "metadata": {
            "modified_by": ((meta.get("lastModifiedBy") or {}).get("user") or {}).get("displayName"),
            "created_by": ((meta.get("createdBy") or {}).get("user") or {}).get("displayName"),
            "modified": meta.get("lastModifiedDateTime"),
            "library": drive,
        },
    }
    if _is_text(mime):
        out |= {"encoding": "text", "content": body.content.decode("utf-8-sig", errors="replace")}
    else:
        out |= {"encoding": "base64", "content": base64.b64encode(body.content).decode()}
    if etag:
        out["marker"] = etag
    if (allowed := _allowed(ctx, drive, item)) is not None:
        out["allowed"] = allowed
    return out


def _size(used: Any) -> str | None:
    if not isinstance(used, int) or used <= 0:
        return None
    value = float(used)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return None


@tool(access="read")
def list_scopes(
    ctx: Context,
    source: dict[str, Any] | None = None,
    query: str | None = None,
    parent: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """What a person picks: sites whose name matches `query`, or, with a site id as `parent`, that site's libraries.
    A library holds nothing to pick, so a library id as `parent` answers no scopes without asking Graph."""
    del source
    scopes: list[dict[str, Any]] = []
    if parent and not _is_site(parent):
        return {"scopes": scopes}
    if parent:
        site = _id(parent, "site")
        url = _graph_url(cursor) if cursor else f"{GRAPH}/sites/{_in_path(site)}/drives"
        data = _get_json(ctx, url, f"The libraries of site {site}")
        for d in data.get("value", []):
            if d.get("driveType", "documentLibrary") != "documentLibrary":
                continue
            row: dict[str, Any] = {"id": d["id"], "title": d.get("name") or d["id"], "kind": "library", "parent": site}
            if detail := _size((d.get("quota") or {}).get("used")):
                row["detail"] = f"{detail} used"
            scopes.append(row)
    else:
        if cursor:
            data = _get_json(ctx, _graph_url(cursor), "The site search")
        else:
            data = _get_json(ctx, f"{GRAPH}/sites", "The site search", {"search": (query or "").strip() or "*"})
        for s in data.get("value", []):
            row = {"id": s["id"], "title": s.get("displayName") or s.get("name") or s["id"], "kind": "site"}
            if s.get("webUrl"):
                row["detail"] = urlsplit(s["webUrl"]).netloc + urlsplit(s["webUrl"]).path
            scopes.append(row)
    out: dict[str, Any] = {"scopes": scopes}
    if nxt := data.get("@odata.nextLink"):
        out["next_cursor"] = nxt
    return out
