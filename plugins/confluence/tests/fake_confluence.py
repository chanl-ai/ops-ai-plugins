"""A small Confluence for the plugin's tests, answering in the shapes of the REST API v1 that Cloud and Data Center
share: `/rest/api/space`, `/rest/api/content` (a space's pages, paged by `_links.next`), `/rest/api/content/search`
(CQL `lastmodified`), one content item with `expand`, its attachments, and a download. On Cloud (`prefix="/wiki"`)
the download redirects to Atlassian's media host, as the real site does; Data Center serves it from its own address.

Page and space keys are the fake's; no site was involved."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, unquote

import httpx


def iso(when: datetime) -> str:
    return f"{when.astimezone(UTC):%Y-%m-%dT%H:%M:%S}.000Z"


LONG_AGO = datetime.now(UTC) - timedelta(days=10)


@dataclass
class Attachment:
    id: str
    title: str
    content: bytes
    media_type: str
    version: int = 1
    when: datetime = LONG_AGO


@dataclass
class Page:
    id: str
    space: str
    title: str
    body: str
    version: int = 1
    when: datetime = LONG_AGO
    ancestors: list[str] = field(default_factory=list[str])
    status: str = "current"
    users: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    groups: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    attachments: list[Attachment] = field(default_factory=list[Attachment])


class FakeConfluence:
    def __init__(
        self, prefix: str = "/wiki", host: str = "acme.atlassian.test", page_size: int = 2, cql_tz_hours: int = 0
    ) -> None:
        self.prefix, self.host, self.page_size = prefix, host, page_size
        # CQL reads a date literal in the searching user's time zone, as the real site does: -8 is US Pacific.
        self.cql_tz_hours = cql_tz_hours
        self.pages: dict[str, Page] = {}
        self.spaces = [
            ("ENG", "Engineering", "global"),
            ("HR", "People and Culture", "global"),
            ("~dana", "Dana Ortiz", "personal"),
            ("LEG", "Legal", "global"),
        ]
        self.requests: list[httpx.Request] = []
        self.add("ENG", "101", "Deploys", "<p>Deploy with the pipeline.</p>", ["Handbook"])
        self.add("ENG", "102", "On call", "<p>Page the primary.</p>", ["Handbook"])
        self.add("ENG", "103", "Postmortems", "<p>Write them blameless.</p>")
        self.add("HR", "201", "Leave policy", "<p>Twenty days.</p>")

    def add(self, space: str, page_id: str, title: str, body: str, ancestors: list[str] | None = None) -> Page:
        self.pages[page_id] = Page(page_id, space, title, body, ancestors=ancestors or [])
        return self.pages[page_id]

    def edit(self, page_id: str, body: str, when: datetime | None = None) -> str:
        p = self.pages[page_id]
        p.body, p.version, p.when = body, p.version + 1, when or datetime.now(UTC)
        return page_id

    def trash(self, page_id: str) -> str:
        self.pages[page_id].status = "trashed"
        return page_id

    @property
    def site_url(self) -> str:
        return f"https://{self.host}{self.prefix}"

    def page_json(self, p: Page, expand: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": p.id,
            "type": "page",
            "status": p.status,
            "title": p.title,
            "_links": {"webui": f"/spaces/{p.space}/pages/{p.id}", "self": f"{self.site_url}/rest/api/content/{p.id}"},
        }
        if "version" in expand:
            out["version"] = {"number": p.version, "when": iso(p.when), "by": {"displayName": "Dana Ortiz"}}
        if "ancestors" in expand:
            out["ancestors"] = [{"id": str(n), "title": t} for n, t in enumerate(p.ancestors)]
        if "space" in expand:
            out["space"] = {"key": p.space}
        if "body.export_view" in expand:
            out["body"] = {"export_view": {"value": p.body, "representation": "export_view"}}
        if "restrictions" in expand:
            out["restrictions"] = {
                "read": {"restrictions": {"user": {"results": p.users}, "group": {"results": p.groups}}}
            }
        return out

    def paged(self, results: list[dict[str, Any]], start: int, route: str, query: str) -> httpx.Response:
        window = results[start : start + self.page_size]
        links: dict[str, Any] = {"base": self.site_url, "context": self.prefix}
        if start + self.page_size < len(results):
            links["next"] = f"{route}?{query}&start={start + self.page_size}"
        return httpx.Response(
            200, json={"results": window, "start": start, "limit": self.page_size, "size": len(window), "_links": links}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        path = unquote(url.path)
        q = {k: v[0] for k, v in parse_qs(url.query.decode()).items()}
        if url.host == "api.media.atlassian.com":
            return httpx.Response(200, content=self.attachment(path.rsplit("/", 2)[-2]).content)
        assert url.host == self.host, url.host
        route = path.removeprefix(self.prefix)
        start = int(q.get("start", 0))
        extra = "&".join(f"{k}={quote(v)}" for k, v in q.items() if k != "start")
        if route == "/rest/api/space":
            rows = [{"key": k, "name": n, "type": t} for k, n, t in self.spaces]
            return self.paged(rows, start, "/rest/api/space", extra)
        if route == "/rest/api/content":
            rows = [self.page_json(p, q.get("expand", "")) for p in self.pages.values()
                    if p.space == q["spaceKey"] and p.status == q.get("status", "current")]
            return self.paged(rows, start, "/rest/api/content", extra)
        if route == "/rest/api/content/search":
            return self.search(q, start, extra)
        if m := re.fullmatch(r"/rest/api/content/(\w+)/child/attachment", route):
            page = self.pages[m.group(1)]
            rows = [self.attachment_json(a, page) for a in page.attachments]
            return self.paged(rows, start, route, extra)
        if m := re.fullmatch(r"/rest/api/content/([\w-]+)", route):
            return self.content(m.group(1), q.get("expand", ""))
        if m := re.fullmatch(r"/download/attachments/(\d+)/(att\w+)", route):
            att = self.attachment(m.group(2))
            if self.host.endswith("atlassian.test"):
                return httpx.Response(302, headers={"Location": f"https://api.media.atlassian.com/file/{att.id}/binary"})
            return httpx.Response(200, content=att.content)
        return httpx.Response(400, json={"message": f"no route {route}"})

    def attachment(self, att_id: str) -> Attachment:
        return next(a for p in self.pages.values() for a in p.attachments if a.id == att_id)

    def owner(self, att_id: str) -> Page:
        return next(p for p in self.pages.values() if any(a.id == att_id for a in p.attachments))

    def attachment_json(self, a: Attachment, page: Page) -> dict[str, Any]:
        return {
            "id": a.id,
            "type": "attachment",
            "status": "current",
            "title": a.title,
            "version": {"number": a.version, "when": iso(a.when)},
            "extensions": {"mediaType": a.media_type, "fileSize": len(a.content)},
            "container": {"id": page.id, "type": "page"},
            "_links": {"download": f"/download/attachments/{page.id}/{a.id}", "webui": f"/pages/viewpageattachments.action?pageId={page.id}"},
        }

    def content(self, content_id: str, expand: str) -> httpx.Response:
        if content_id.startswith("att"):
            a = self.attachment(content_id)
            return httpx.Response(200, json=self.attachment_json(a, self.owner(content_id)))
        page = self.pages.get(content_id)
        if page is None:
            return httpx.Response(404, json={"message": "No content found with id " + content_id})
        return httpx.Response(200, json=self.page_json(page, expand))

    def search(self, q: dict[str, str], start: int, extra: str) -> httpx.Response:
        cql = q["cql"]
        keys = re.findall(r'"([^"]+)"', re.search(r"space in \((.*?)\)", cql).group(1))  # type: ignore[union-attr]
        since = datetime.strptime(re.search(r'lastmodified > "([^"]+)"', cql).group(1), "%Y/%m/%d %H:%M").replace(tzinfo=UTC) - timedelta(hours=self.cql_tz_hours)  # type: ignore[union-attr]
        hits = sorted((p for p in self.pages.values() if p.space in keys and p.status == "current" and p.when > since), key=lambda p: p.when)
        return self.paged([self.page_json(p, q.get("expand", "")) for p in hits], start, "/rest/api/content/search", extra)

    def downloads(self) -> list[str]:
        return [r.url.path for r in self.requests if "/download/" in r.url.path]

    def bodies(self) -> list[httpx.Request]:
        return [r for r in self.requests if "body.export_view" in r.url.query.decode() or "body.export_view" in unquote(r.url.query.decode())]
