"""A website as a knowledge source: which pages a source takes in, and each page's content.

A source names one site by its start addresses (pages or sitemaps). Its pages are found from the site's sitemaps
(the start addresses that are sitemaps, the `Sitemap:` lines of robots.txt, and /sitemap.xml, an index followed one
level) and, with `follow_links`, from the links of its pages on the same host, to `depth`. A page is taken in only
when its path passes the source's include and exclude rules (exclude wins) and robots.txt lets OpsAI-Knowledge read
it; a page refused is listed as skipped with the reason, so kb-service removes an indexed copy and says why.

Requests are made one at a time, at most `rate` a second (one by default), slower when robots.txt's Crawl-delay asks;
a 429 or 503 stops the call so kb-service resumes later. A listing that follows links has to read each page to find
the next ones, so it hands the page's content over with it and the page is not fetched twice. Otherwise each changed
page is fetched by `get_page`, which sends the ETag or date it was last read with, so an unchanged page answers 304
and costs no transfer. The listing is whole every run (`lists_whole`): a page that left the sitemap, its links or the
rules is gone.

Every address a call reaches is checked by tools-service against the source's own hosts and the account's egress
policy, which refuses private and link-local addresses unless the account allows them. Pages that need JavaScript to
show their text are flagged by the preview; rendering them is not done here."""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from ops_plugin_sdk import BadInput, Context, NotFound, PluginError, RateLimited, Response, tool

AGENT = "OpsAI-Knowledge"
DEPTH, MAX_DEPTH = 2, 5
MAX_PAGES, MAX_PAGES_CAP = 500, 5000
RATE, MIN_RATE, MAX_RATE = 1.0, 0.2, 5.0
# Each call stops in time to answer well inside the gateway's 30-second deadline; the cursor carries on from there.
BUDGET_S = 20.0
# A sitemap larger than this is not read: a site's real sitemaps are far smaller, and parsing has to stay bounded.
SITEMAP_MAX_BYTES = 10 * 1024 * 1024
PREVIEW_PAGES = 50
# The preview reads this many pages to see which show their text only with JavaScript.
JS_CHECK = 10
JS_MIN_CHARS = 200
# Redirects followed per request, each hop back through the context (so the egress check applies to every hop), on
# the source's own site only.
MAX_REDIRECTS = 5
REDIRECTS = (301, 302, 303, 307, 308)
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")


@dataclass
class Site:
    """A source's settings, checked: one site's start addresses and how far to go."""

    starts: list[str]
    scheme: str
    host: str
    follow: bool
    depth: int
    max_pages: int
    include: list[str]
    exclude: list[str]
    rate: float

    @property
    def root(self) -> str:
        return f"{self.scheme}://{self.host}"

    def same_site(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == self.scheme and (parts.hostname or "").lower() == self.host


def site_of(source: dict[str, Any]) -> Site:
    starts = [normal(str(s)) for s in cast(list[Any], source.get("start") or []) if str(s).strip()]
    if not starts:
        raise BadInput("A website source needs at least one start page or sitemap address.")
    hosts = {(urlsplit(s).scheme, (urlsplit(s).hostname or "").lower()) for s in starts}
    if len(hosts) != 1 or not next(iter(hosts))[1]:
        raise BadInput("A website source names one site: every start address must be on the same host.")
    scheme, host = next(iter(hosts))
    if scheme not in ("https", "http"):
        raise BadInput("A start address is a web address beginning https://.")
    include = [str(p) for p in cast(list[Any], source.get("include") or [])]
    exclude = [str(p) for p in cast(list[Any], source.get("exclude") or [])]
    # An include written as !pattern is an exclude, as the console's rule list writes it.
    exclude += [p[1:] for p in include if p.startswith("!")]
    include = [p for p in include if not p.startswith("!")]
    return Site(
        starts=starts,
        scheme=scheme,
        host=host,
        follow=bool(source.get("follow_links")),
        depth=min(max(int(source.get("depth") or DEPTH), 1), MAX_DEPTH),
        max_pages=min(max(int(source.get("max_pages") or MAX_PAGES), 1), MAX_PAGES_CAP),
        include=include,
        exclude=exclude,
        rate=min(max(float(source.get("rate") or RATE), MIN_RATE), MAX_RATE),
    )


def normal(url: str) -> str:
    """An address as compared and stored: no fragment, a lower-case host, a path of at least /."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _glob(pattern: str) -> re.Pattern[str]:
    """`**` crosses folders, `*` stays within one; a pattern is matched against the whole path."""
    out, i = "", 0
    p = pattern.strip()
    if not p.startswith("/"):
        p = "/" + p
    while i < len(p):
        if p.startswith("**", i):
            out += ".*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(out + r"\Z")


def rule_says(site: Site, url: str) -> str | None:
    """Why a rule leaves the page out, or None when the rules take it in. Exclude wins."""
    path = urlsplit(url).path or "/"
    for p in site.exclude:
        if _glob(p).match(path):
            return f"Excluded by rule !{p}"
    if site.include and not any(_glob(p).match(path) for p in site.include):
        return "Excluded: matches no include rule"
    return None


class Fetcher:
    """One request at a time to the site, at most `rate` a second, slower when robots.txt asks."""

    def __init__(self, ctx: Context, site: Site) -> None:
        self.ctx, self.site = ctx, site
        self.interval = 1.0 / site.rate
        self.last = 0.0
        self.started = time.monotonic()

    def slower(self, delay: float | None) -> None:
        if delay and delay > self.interval:
            self.interval = float(delay)

    def time_left(self) -> bool:
        return time.monotonic() - self.started < BUDGET_S - self.interval

    def _one(self, url: str, headers: dict[str, str] | None) -> Response:
        wait = self.last + self.interval - time.monotonic()
        if self.last and wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        r = self.ctx.http.get(url, headers=headers)
        if r.status_code == 503:
            raise RateLimited(f"{self.site.host} answered 503; it asks to be read later.")
        return r

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        """The answer at `url` after its redirects (up to MAX_REDIRECTS, each one paced like any request); `final`
        says where it came from. A redirect off the site, or one hop too many, raises OffSite with the reason."""
        self.final = url
        for _ in range(MAX_REDIRECTS + 1):
            r = self._one(self.final, headers)
            location = r.headers.get("location") or r.headers.get("Location")
            if r.status_code not in REDIRECTS or not location:
                return r
            target = normal(urljoin(self.final, location))
            if not self.site.same_site(target):
                raise OffSite(f"{self.final} redirects to {target}, off {self.site.host}.")
            self.final = target
        raise OffSite(f"{url} redirects more than {MAX_REDIRECTS} times.")


class OffSite(PluginError):
    """A redirect the crawler does not follow: to another site, or past MAX_REDIRECTS."""


def robots_of(fetch: Fetcher) -> RobotFileParser:
    """The site's robots.txt for OpsAI-Knowledge (and *): a missing one allows everything, a server error refuses
    everything (RFC 9309), and its Crawl-delay slows the fetcher."""
    parser = RobotFileParser()
    try:
        r = fetch.get(f"{fetch.site.root}/robots.txt")
    except OffSite:
        # RFC 9309: a robots.txt that cannot be reached through its redirects is unavailable, which allows crawling.
        parser.parse([])
        return parser
    if r.status_code >= 500:
        parser.parse(["User-agent: *", "Disallow: /"])
    elif r.status_code >= 400:
        parser.parse([])
    else:
        parser.parse(r.text.splitlines())
    fetch.slower(parser.crawl_delay(AGENT) or parser.crawl_delay("*"))  # pyright: ignore[reportArgumentType]
    return parser


def sitemap_entries(text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """(page address, lastmod) of a urlset, and the child sitemaps of an index."""
    if len(text) > SITEMAP_MAX_BYTES or "<!DOCTYPE" in text[:500].upper() or "<!ENTITY" in text:
        return [], []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return [], []
    pages: list[tuple[str, str | None]] = []
    children: list[str] = []
    for node in root:
        tag = node.tag.rsplit("}", 1)[-1]
        loc = next((c.text or "" for c in node if c.tag.rsplit("}", 1)[-1] == "loc"), "").strip()
        lastmod = next((c.text for c in node if c.tag.rsplit("}", 1)[-1] == "lastmod"), None)
        if not loc:
            continue
        if tag == "sitemap":
            children.append(loc)
        elif tag == "url":
            pages.append((loc, lastmod.strip() if lastmod else None))
    return pages, children


class _Page(HTMLParser):
    """A page's links, its title and the text a person sees (no script, style or template)."""

    HIDDEN = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.title = ""
        self.chars = 0
        self._hidden = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.HIDDEN:
            self._hidden += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("mailto:", "tel:", "javascript:", "#")):
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.HIDDEN and self._hidden:
            self._hidden -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._hidden:
            self.chars += len(" ".join(data.split()))


def read_page(text: str) -> _Page:
    page = _Page()
    # A broken page still yields what was read before it broke.
    with contextlib.suppress(Exception):
        page.feed(text)
    return page


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(url: str, *, updated: str, marker: str | None = None, skipped: str | None = None) -> dict[str, Any]:
    path = urlsplit(url).path or "/"
    row: dict[str, Any] = {"id": url, "title": path, "url": url, "updated_at": updated, "folder": _folder(path)}
    if marker:
        row["marker"] = marker
    if skipped:
        row["skipped"] = skipped
    return row


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


@dataclass
class Walk:
    """Where a listing is: sitemaps still to read, pages to visit with their depth, and what was already listed."""

    sitemaps: list[list[Any]] = field(default_factory=list[list[Any]])  # [address, level]
    queue: list[list[Any]] = field(default_factory=list[list[Any]])  # [address, depth]
    seen: list[str] = field(default_factory=list[str])
    taken: int = 0
    started: str = ""

    # Sitemap lines of robots.txt left out (another site's, or not an address), each [address, reason], said once.
    refused: list[list[str]] = field(default_factory=list[list[str]])

    @classmethod
    def first(cls, site: Site, robots: RobotFileParser) -> Walk:
        found = [s for s in site.starts if _is_sitemap(s)]
        refused: list[list[str]] = []
        for line in robots.site_maps() or []:
            address = normal(line)
            parts = urlsplit(address)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                refused.append([line.strip(), "A Sitemap line of robots.txt is not an address."])
                continue
            if site.same_site(address):
                found.append(address)
            else:
                refused.append([address, f"A Sitemap line of robots.txt names another site than {site.host}."])
        if not found:
            found.append(f"{site.root}/sitemap.xml")
        pages = [[s, 0] for s in site.starts if not _is_sitemap(s)]
        started = _now()
        return cls(sitemaps=[[s, 0] for s in dict.fromkeys(found)], queue=pages, started=started, refused=refused)

    def dump(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))


def _is_sitemap(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(".xml") or path.endswith(".xml.gz") or "sitemap" in path


def walk(
    ctx: Context, site: Site, state: Walk | None, *, content: bool
) -> tuple[list[dict[str, Any]], Walk | None, float]:
    """One call's share of a listing: rows for the pages found (skipped ones with their reason), and where the next
    call carries on (None when the listing is done). With `content`, a page read to follow its links carries its
    content, so it is not fetched again."""
    fetch = Fetcher(ctx, site)
    robots = robots_of(fetch)
    state = state or Walk.first(site, robots)
    seen = set(state.seen)
    rows: list[dict[str, Any]] = []

    def take(url: str, lastmod: str | None) -> bool:
        """Lists a page once; False when the page cap is reached."""
        url = normal(url)
        if url in seen or not site.same_site(url):
            return True
        if state.taken >= site.max_pages:
            return False
        seen.add(url)
        why = rule_says(site, url) or (None if robots.can_fetch(AGENT, url) else "Refused by robots.txt")
        if why:
            rows.append(_row(url, updated=state.started, skipped=why))
        else:
            state.taken += 1
            rows.append(_row(url, updated=lastmod or state.started, marker=lastmod))
        return True

    for address, why in state.refused:
        rows.append({"id": address, "title": "Sitemap", "updated_at": state.started, "skipped": why})
    state.refused = []
    capped = False
    while state.sitemaps and fetch.time_left():
        address, level = state.sitemaps.pop(0)
        try:
            r = fetch.get(str(address))
        except (OffSite, PluginError) as e:
            if isinstance(e, RateLimited):
                raise
            # One sitemap that cannot be read leaves the rest of the listing working.
            rows.append({"id": str(address), "title": "Sitemap", "updated_at": state.started, "skipped": str(e)})
            continue
        if r.status_code >= 400:
            rows.append({"id": str(address), "title": "Sitemap", "updated_at": state.started,
                         "skipped": f"The sitemap answered {r.status_code}."})  # fmt: skip
            continue
        pages, children = sitemap_entries(r.text)
        if int(level) == 0:
            state.sitemaps += [[normal(c), 1] for c in children if site.same_site(normal(c))]
        for loc, lastmod in pages:
            if not take(loc, lastmod):
                capped = True
                break
        if capped:
            state.sitemaps = []
    while state.queue and not capped and fetch.time_left():
        address, depth = state.queue.pop(0)
        url = normal(str(address))
        listed = url in seen
        if not listed and not take(url, None):
            capped = True
            break
        if not site.follow or int(depth) >= site.depth or not rows or rows[-1]["id"] != url or "skipped" in rows[-1]:
            continue
        try:
            r = fetch.get(url)
        except OffSite as e:
            rows[-1] = _row(url, updated=state.started, skipped=str(e))
            state.taken -= 1
            continue
        if r.status_code >= 400:
            continue
        page = read_page(r.text)
        if content and r.headers.get("content-type", "text/html").split(";")[0].strip() in TEXT_TYPES:
            rows[-1] |= _content(url, r, page)
        state.queue += [[normal(urljoin(url, href)), int(depth) + 1] for href in page.links]
    state.seen = sorted(seen)
    done = not state.sitemaps and not state.queue
    if capped:
        rows.append({"id": f"{site.root}#capped", "title": "Page cap", "updated_at": state.started,
                     "skipped": f"Stopped at {site.max_pages} pages, the source's cap."})  # fmt: skip
    return rows, None if done or capped else state, fetch.interval


def _content(url: str, r: Response, page: _Page | None = None) -> dict[str, Any]:
    """A fetched page as the contract's item: its content, type, title and version tag."""
    mime = r.headers.get("content-type", "text/html").split(";")[0].strip().lower() or "text/html"
    tag = r.headers.get("etag") or r.headers.get("last-modified")
    out: dict[str, Any] = {"url": url, "mime": mime}
    if mime in TEXT_TYPES:
        read = page or read_page(r.text)
        out |= {"encoding": "text", "content": r.text, "title": " ".join(read.title.split()) or urlsplit(url).path}
    else:
        out |= {"encoding": "base64", "content": base64.b64encode(r.content).decode()}
    if tag:
        out["marker"] = tag
    return out


@tool(access="read")
def list_pages(
    ctx: Context, source: dict[str, Any], since: str | None = None, cursor: str | None = None
) -> dict[str, Any]:
    """The site's pages after its rules and robots.txt, a share per call. Whole every run: `since` is not used."""
    del since
    site = site_of(source)
    state = Walk(**json.loads(cursor)) if cursor else None
    rows, after, pace = walk(ctx, site, state, content=True)
    # The seconds between two requests to the site (its rate, or robots.txt's Crawl-delay when slower): the fetches of
    # changed pages that follow, one call each, keep to it too.
    out: dict[str, Any] = {"items": rows, "pace_s": pace}
    if after is not None:
        out["next_cursor"] = after.dump()
    return out


@tool(access="read")
def get_page(ctx: Context, source: dict[str, Any], id: str, tag: str | None = None) -> dict[str, Any]:
    """One page, asked only if it changed since `tag` (its ETag, or the date it was last read): unchanged answers
    `{"unchanged": true}`; a page gone (404 or 410) raises NOT_FOUND."""
    site = site_of(source)
    url = normal(id)
    if not site.same_site(url):
        raise BadInput(f"{url} is not on {site.host}.")
    headers: dict[str, str] = {}
    if tag:
        headers["If-None-Match" if tag.startswith(('"', "W/")) else "If-Modified-Since"] = tag
    fetch = Fetcher(ctx, site)
    try:
        r = fetch.get(url, headers or None)
    except OffSite as e:
        raise BadInput(str(e)) from e
    if fetch.final != url and (why := rule_says(site, fetch.final)):
        raise BadInput(f"{url} redirects to {fetch.final}, which the source's rules leave out: {why}")
    if r.status_code == 304:
        return {"unchanged": True}
    if r.status_code in (404, 410):
        raise NotFound(f"{url} answered {r.status_code}.")
    if r.status_code >= 400:
        raise PluginError(f"{url} answered {r.status_code}.")
    path = urlsplit(url).path or "/"
    # Kept under the address it was listed by; its content and link are where the redirects ended.
    return {"id": url, "title": path, "folder": _folder(path), **_content(fetch.final, r)}


@tool(access="read")
def preview_site(ctx: Context, source: dict[str, Any]) -> dict[str, Any]:
    """The first pages the source would take in, each with its outcome; the first few that would be fetched are read
    to flag those that show their text only with JavaScript. Nothing is stored."""
    site = site_of(source)
    rows, after, _ = walk(ctx, site, None, content=False)
    fetch = Fetcher(ctx, site)
    pages: list[dict[str, Any]] = []
    checked = 0
    capped = after is not None or any(r["id"].endswith("#capped") for r in rows)
    for r in rows:
        if r["id"].endswith("#capped"):
            continue
        outcome, will = (r["skipped"], False) if "skipped" in r else ("Will fetch", True)
        if will and checked < JS_CHECK and not source.get("render") and fetch.time_left():
            checked += 1
            try:
                answer = fetch.get(r["id"])
            except OffSite as e:
                pages.append({"id": r["id"], "outcome": str(e), "will_fetch": False})
                continue
            if answer.status_code < 400 and answer.headers.get("content-type", "text/html").startswith("text/html"):
                chars = read_page(answer.text).chars
                if chars < JS_MIN_CHARS:
                    outcome = f"Needs JavaScript: {chars} characters without it"
        pages.append({"id": r["id"], "outcome": outcome, "will_fetch": will})
    return {
        "found": len(pages),
        "will_fetch": sum(1 for p in pages if p["will_fetch"]),
        "pages": pages[:PREVIEW_PAGES],
        "capped": capped,
    }
