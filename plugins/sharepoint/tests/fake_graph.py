"""A small Microsoft Graph for the SharePoint plugin's tests, answering in the shapes of the public v1.0 reference:
`/sites?search=`, `/sites/{id}/drives`, a drive's `root/delta` (nextLink and deltaLink, deletions as a `deleted`
facet, folders alongside files), an item, its `/content` (a 302 to the tenant's SharePoint host) and its permissions.

Every change bumps one counter, and a delta token is the counter's value when the deltaLink was issued, so asking
with a token returns exactly what changed after it."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
SHAREPOINT = "https://contoso.sharepoint.test"
SITE_HR = "contoso.sharepoint.test,0f1a,9b2c"
SITE_OPS = "contoso.sharepoint.test,7d4e,3a6f"
LIB_POLICIES = "b!policies"
LIB_HANDBOOK = "b!handbook"
LIB_RUNBOOKS = "b!runbooks"


@dataclass
class Entry:
    id: str
    name: str
    folder: str
    content: bytes = b""
    mime: str = "text/plain"
    is_folder: bool = False
    deleted: bool = False
    version: int = 1
    seq: int = 0
    size: int | None = None
    permissions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


class FakeGraph:
    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.seq = 0
        self.drives: dict[str, dict[str, Entry]] = {LIB_POLICIES: {}, LIB_HANDBOOK: {}, LIB_RUNBOOKS: {}}
        self.sites = {
            SITE_HR: ("Human Resources", "hr", [LIB_POLICIES, LIB_HANDBOOK]),
            SITE_OPS: ("Operations", "ops", [LIB_RUNBOOKS]),
        }
        self.expired: set[str] = set()
        self.forbid_permissions = False
        self.requests: list[httpx.Request] = []
        self.add(LIB_POLICIES, "f0", "Policies", "", is_folder=True)
        self.add(LIB_POLICIES, "p1", "leave.md", "Policies", b"# Leave\nTwenty days a year.\n", "text/markdown")
        self.add(LIB_POLICIES, "p2", "expenses.txt", "Policies", b"Claim within 30 days.\n")
        self.add(LIB_POLICIES, "p3", "travel.pdf", "Policies/Archive", b"%PDF-1.7\x00\xff\xfe binary", "application/pdf")
        self.add(LIB_HANDBOOK, "h1", "welcome.docx", "", b"PK\x03\x04 docx bytes", "application/vnd.openxmlformats")
        self.add(LIB_RUNBOOKS, "r1", "restore.md", "Databases", b"# Restore\nSteps.\n", "text/markdown")

    def add(self, drive: str, item_id: str, name: str, folder: str, content: bytes = b"", mime: str = "text/plain",
            is_folder: bool = False) -> str:
        self.seq += 1
        self.drives[drive][item_id] = Entry(item_id, name, folder, content, mime, is_folder, seq=self.seq)
        return f"{drive}:{item_id}"

    def edit(self, drive: str, item_id: str, content: bytes) -> str:
        self.seq += 1
        e = self.drives[drive][item_id]
        e.content, e.version, e.seq = content, e.version + 1, self.seq
        return f"{drive}:{item_id}"

    def remove(self, drive: str, item_id: str) -> str:
        self.seq += 1
        e = self.drives[drive][item_id]
        e.deleted, e.seq = True, self.seq
        return f"{drive}:{item_id}"

    def web_url(self, drive: str, e: Entry) -> str:
        site = {LIB_POLICIES: "hr", LIB_HANDBOOK: "hr", LIB_RUNBOOKS: "ops"}[drive]
        library = {LIB_POLICIES: "Policies Library", LIB_HANDBOOK: "Handbook", LIB_RUNBOOKS: "Runbooks"}[drive]
        parts = ["sites", site, library, *([e.folder] if e.folder else []), e.name]
        return f"{SHAREPOINT}/" + "/".join(quote(p, safe="/") for p in parts)

    def item(self, drive: str, e: Entry) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": e.id,
            "name": e.name,
            "eTag": f'"{{{e.id.upper()}-0000}},{e.version}"',
            "cTag": f'"c:{{{e.id.upper()}-0000}},{e.version}"',
            "webUrl": self.web_url(drive, e),
            "size": e.size if e.size is not None else len(e.content),
            "lastModifiedDateTime": f"2026-11-0{min(e.seq, 9)}T09:00:00Z",
            "lastModifiedBy": {"user": {"displayName": "Dana Ortiz", "id": "u-dana"}},
            "createdBy": {"user": {"displayName": "Sam Okafor", "id": "u-sam"}},
            "parentReference": {"driveId": drive, "id": "root"},
        }
        out["folder" if e.is_folder else "file"] = {"childCount": 1} if e.is_folder else {"mimeType": e.mime}
        return out

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        path = unquote(url.path)
        q = parse_qs(url.query.decode())
        if url.host == "contoso.sharepoint.test":
            # The pre-authenticated download address Graph's redirect names.
            _, _, drive, item = path.split("/", 3)
            return httpx.Response(200, content=self.drives[drive][item].content)
        assert url.host == "graph.microsoft.com", url.host
        path = path.removeprefix("/v1.0")
        if path == "/sites":
            return self.search_sites(q.get("search", ["*"])[0], q)
        if path.startswith("/sites/") and path.endswith("/drives"):
            site = path.split("/")[2]
            if site not in self.sites:
                return self.error(404, "itemNotFound")
            return httpx.Response(200, json={"value": [self.drive_row(d) for d in self.sites[site][2]]})
        if path.startswith("/drives/"):
            _, _, drive, *rest = path.split("/")
            if drive not in self.drives:
                return self.error(404, "itemNotFound")
            if rest == ["root", "delta"]:
                return self.delta(drive, q)
            if rest[0] == "items":
                e = self.drives[drive].get(rest[1])
                if e is None or e.deleted:
                    return self.error(404, "itemNotFound")
                if len(rest) == 2:
                    return httpx.Response(200, json=self.item(drive, e))
                if rest[2] == "content":
                    return httpx.Response(302, headers={"Location": f"{SHAREPOINT}/dl/{drive}/{e.id}?tempauth=x"})
                if rest[2] == "permissions":
                    if self.forbid_permissions:
                        return self.error(403, "accessDenied")
                    return httpx.Response(200, json={"value": e.permissions})
        return self.error(400, "invalidRequest")

    def error(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": code}})

    def drive_row(self, drive: str) -> dict[str, Any]:
        names = {LIB_POLICIES: "Policies Library", LIB_HANDBOOK: "Handbook", LIB_RUNBOOKS: "Runbooks"}
        return {"id": drive, "name": names[drive], "driveType": "documentLibrary", "quota": {"used": 640 * 1024 * 1024}}

    def search_sites(self, text: str, q: dict[str, list[str]]) -> httpx.Response:
        rows = [
            {"id": sid, "displayName": name, "name": slug, "webUrl": f"{SHAREPOINT}/sites/{slug}"}
            for sid, (name, slug, _) in self.sites.items()
            if text == "*" or text.lower() in name.lower()
        ]
        return httpx.Response(200, json={"value": rows})

    def delta(self, drive: str, q: dict[str, list[str]]) -> httpx.Response:
        token = q.get("token", [None])[0]
        if token in self.expired:
            return httpx.Response(410, json={"error": {"code": "resyncRequired", "message": "resync"}})
        skip = int(q.get("$skiptoken", ["0"])[0])
        after = int(token) if token is not None else None
        changed = [e for e in self.drives[drive].values() if (after is None and not e.deleted) or (after is not None and e.seq > after)]
        window = changed[skip : skip + self.page_size]
        value = [{"id": e.id, "deleted": {"state": "deleted"}} if e.deleted else self.item(drive, e) for e in window]
        base = f"{GRAPH}/drives/{drive}/root/delta"
        if skip + self.page_size < len(changed):
            more = f"?$skiptoken={skip + self.page_size}" + (f"&token={token}" if token is not None else "")
            return httpx.Response(200, json={"value": value, "@odata.nextLink": base + more})
        return httpx.Response(200, json={"value": value, "@odata.deltaLink": f"{base}?token={self.seq}"})

    def downloads(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.endswith("/content")]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
