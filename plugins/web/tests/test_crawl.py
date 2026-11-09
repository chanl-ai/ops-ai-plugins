"""The website plugin against a small fake site, through the SDK's runner (as `ops plugin test` runs it), by failure
mode: a page robots.txt refuses, or a rule excludes, is taken in; a sitemap index or the robots.txt sitemap is not
followed; links are followed off the site or past their depth; an unchanged page is fetched whole again; a page gone
at the site reads as a failure instead of gone; requests come faster than the site's rate; the page cap is passed;
an address off the source's site is reached; and the plugin breaks the knowledge-source contract."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from ops_plugin_web import plugin

from ops_plugin_sdk.testing import Fake, call, knowledge_contract

SITE = "https://help.northbank.test"


def page(title: str, body: str, links: list[str] | None = None) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a> ' for href in links or [])
    head = f"<html><head><title>{title}</title></head>"
    return f"{head}<body><main><h1>{title}</h1><p>{body}</p>{anchors}</main></body></html>"


class FakeSite:
    """A bank's help site: robots.txt with a sitemap index and a refused folder, pages with ETags that answer 304."""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {
            "/lending/early-repayment": page(
                "Early repayment", "You can repay all or part of your loan at any time. " * 8
            ),
            "/lending/payment-holidays": page(
                "Payment holidays", "Ask for a payment holiday of up to three months. " * 8
            ),
            "/lending/archive/2019-rates": page("2019 rates", "Old rates. " * 30),
            "/admin/": page("Admin", "Staff only. " * 30),
            "/branches": "<html><body><div id='app'></div><script>render()</script></body></html>",
            "/start": page(
                "Start", "Lending help. " * 30, ["/lending/early-repayment", "/about", "https://elsewhere.test/x"]
            ),
            "/about": page("About", "About us. " * 30, ["/about/team"]),
            "/about/team": page("Team", "Our team. " * 30, ["/about/team/deep"]),
        }
        self.lastmod = {"/lending/early-repayment": "2026-11-01", "/lending/payment-holidays": "2026-11-02"}
        self.seen: list[httpx.Request] = []
        self.times: list[float] = []
        # path -> (status, Location): a redirect the site answers before the real answer.
        self.redirects: dict[str, tuple[int, str]] = {}
        self.robots = f"User-agent: *\nDisallow: /admin/\n\nSitemap: {SITE}/sitemap-index.xml\n"
        self.broken: dict[str, httpx.Response] = {}

    def etag(self, path: str) -> str:
        return f'"{abs(hash(self.pages[path])) % 10**8}"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        self.times.append(time.monotonic())
        path = request.url.path
        if path in self.redirects:
            status, location = self.redirects[path]
            return httpx.Response(status, headers={"location": location})
        if path in self.broken:
            return self.broken[path]
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if path == "/sitemap-index.xml":
            return httpx.Response(
                200, text=f"<sitemapindex><sitemap><loc>{SITE}/sitemap-lending.xml</loc></sitemap></sitemapindex>"
            )
        if path == "/sitemap-lending.xml":
            urls = "".join(
                f"<url><loc>{SITE}{p}</loc>"
                + (f"<lastmod>{self.lastmod[p]}</lastmod>" if p in self.lastmod else "")
                + "</url>"
                for p in (
                    "/lending/early-repayment",
                    "/lending/payment-holidays",
                    "/lending/archive/2019-rates",
                    "/admin/",
                    "/branches",
                )
                if p in self.pages
            )
            return httpx.Response(200, text=f"<urlset>{urls}</urlset>")
        if path == "/sitemap.xml":
            return httpx.Response(404)
        if path not in self.pages:
            return httpx.Response(404)
        tag = self.etag(path)
        if request.headers.get("if-none-match") == tag:
            return httpx.Response(304)
        return httpx.Response(200, text=self.pages[path], headers={"content-type": "text/html", "etag": tag})


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


def fake(site: FakeSite) -> Fake:
    return Fake(httpx.Client(transport=httpx.MockTransport(site)), "unused")


SOURCE: dict[str, Any] = {"start": [f"{SITE}/"], "exclude": ["/lending/archive/**"], "rate": 5}


def listed(site: FakeSite, source: dict[str, Any] = SOURCE) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        out = call(plugin, "list_pages", {"source": source, **({"cursor": cursor} if cursor else {})}, {}, fake(site))
        rows += out["items"]
        cursor = out.get("next_cursor")
        if not cursor:
            return rows


def test_the_sitemap_index_from_robots_is_followed_and_rules_and_robots_skip_with_reasons(site: FakeSite) -> None:
    rows = {r["id"]: r for r in listed(site)}
    assert rows[f"{SITE}/lending/early-repayment"].get("marker") == "2026-11-01"
    assert rows[f"{SITE}/lending/archive/2019-rates"]["skipped"] == "Excluded by rule !/lending/archive/**"
    assert rows[f"{SITE}/admin/"]["skipped"] == "Refused by robots.txt"
    assert "skipped" not in rows[f"{SITE}/branches"]
    assert all("content" not in r for r in rows.values()), "a sitemap listing reads no page"


def test_links_are_followed_on_the_site_to_their_depth_and_carry_their_content(site: FakeSite) -> None:
    rows = {r["id"]: r for r in listed(site, {"start": [f"{SITE}/start"], "follow_links": True, "depth": 2, "rate": 5})}
    assert f"{SITE}/about/team" in rows and f"{SITE}/about/team/deep" not in rows, sorted(rows)
    assert not any("elsewhere.test" in r for r in rows)
    assert "Lending help." in rows[f"{SITE}/start"]["content"] and rows[f"{SITE}/start"]["marker"].startswith('"')
    reads = [r.url.path for r in site.seen if r.url.path == "/start"]
    assert reads == ["/start"], "a page read for its links is not read again"


def test_an_unchanged_page_answers_304_and_a_gone_one_is_not_found(site: FakeSite) -> None:
    first = call(plugin, "get_page", {"source": SOURCE, "id": f"{SITE}/lending/early-repayment"}, {}, fake(site))
    assert first["title"] == "Early repayment" and first["marker"]
    again = call(
        plugin,
        "get_page",
        {"source": SOURCE, "id": f"{SITE}/lending/early-repayment", "tag": first["marker"]},
        {},
        fake(site),
    )
    assert again == {"unchanged": True}
    with pytest.raises(Exception, match="answered 404"):
        call(plugin, "get_page", {"source": SOURCE, "id": f"{SITE}/gone"}, {}, fake(site))


def test_requests_keep_to_the_sites_rate(site: FakeSite) -> None:
    listed(site, {**SOURCE, "rate": 2})
    gaps = [b - a for a, b in zip(site.times, site.times[1:], strict=False)]
    assert gaps and min(gaps) >= 0.45, gaps


def test_the_page_cap_stops_the_listing_and_says_so(site: FakeSite) -> None:
    rows = listed(site, {**SOURCE, "max_pages": 1})
    taken = [r for r in rows if "skipped" not in r]
    assert len(taken) == 1 and any("Stopped at 1 pages" in r.get("skipped", "") for r in rows), rows


def test_an_address_off_the_sources_site_is_never_reached(site: FakeSite) -> None:
    with pytest.raises(Exception, match=r"not on help\.northbank\.test"):
        call(plugin, "get_page", {"source": SOURCE, "id": "https://elsewhere.test/x"}, {}, fake(site))
    rows = listed(site, {"start": [f"{SITE}/start"], "follow_links": True, "rate": 5})
    assert all(urlsplit(r["id"]).hostname == "help.northbank.test" for r in rows)
    assert not any(r.url.host == "elsewhere.test" for r in site.seen)


def test_the_preview_says_what_is_skipped_and_which_pages_need_javascript(site: FakeSite) -> None:
    out = call(plugin, "preview_site", {"source": SOURCE}, {}, fake(site))
    outcomes = {p["id"]: p["outcome"] for p in out["pages"]}
    assert outcomes[f"{SITE}/branches"].startswith("Needs JavaScript"), outcomes
    assert outcomes[f"{SITE}/admin/"] == "Refused by robots.txt"
    assert out["will_fetch"] == 4 and not out["capped"], out  # the start page and three sitemap pages
    assert json.dumps(out)


def test_the_plugin_keeps_the_knowledge_source_contract(site: FakeSite) -> None:
    def change() -> str:
        site.pages["/lending/payment-holidays"] = page("Payment holidays", "Up to six months now. " * 10)
        site.lastmod["/lending/payment-holidays"] = "2026-11-09"
        return f"{SITE}/lending/payment-holidays"

    def delete() -> str:
        del site.pages["/branches"]
        return f"{SITE}/branches"

    problems = knowledge_contract(plugin, "site", fake(site), settings={}, values=SOURCE, change=change, delete=delete)
    assert problems == [], problems


def test_a_redirected_robots_txt_is_obeyed(site: FakeSite) -> None:
    """A robots.txt moved behind a redirect was read as missing, which allows everything."""
    site.redirects["/robots.txt"] = (301, "/policies/robots.txt")
    site.pages["/policies/robots.txt"] = "User-agent: *\nDisallow: /lending/\n"
    rows = {r["id"]: r for r in listed(site, {"start": [f"{SITE}/lending/early-repayment"], "rate": 5})}
    assert rows[f"{SITE}/lending/early-repayment"]["skipped"] == "Refused by robots.txt", rows


def test_a_page_redirected_on_the_site_is_fetched_where_it_ends_and_one_off_the_site_is_refused(
    site: FakeSite,
) -> None:
    site.redirects["/loans/early"] = (301, "/lending/early-repayment")
    moved = call(plugin, "get_page", {"source": SOURCE, "id": f"{SITE}/loans/early"}, {}, fake(site))
    assert moved["id"] == f"{SITE}/loans/early" and moved["url"] == f"{SITE}/lending/early-repayment", moved
    assert "repay all or part" in moved["content"]
    site.redirects["/away"] = (302, "https://elsewhere.test/x")
    with pytest.raises(Exception, match=r"off help\.northbank\.test"):
        call(plugin, "get_page", {"source": SOURCE, "id": f"{SITE}/away"}, {}, fake(site))
    assert not any(r.url.host == "elsewhere.test" for r in site.seen)


def test_a_sixth_redirect_stops(site: FakeSite) -> None:
    for n in range(7):
        site.redirects[f"/hop{n}"] = (302, f"/hop{n + 1}")
    with pytest.raises(Exception, match="more than 5 times"):
        call(plugin, "get_page", {"source": SOURCE, "id": f"{SITE}/hop0"}, {}, fake(site))
    assert sum(1 for r in site.seen if r.url.path.startswith("/hop")) == 6


def test_an_off_site_or_broken_sitemap_is_skipped_with_its_reason_and_the_rest_is_listed(site: FakeSite) -> None:
    site.robots = (
        "User-agent: *\nSitemap: https://elsewhere.test/sitemap.xml\nSitemap: not a url\n"
        f"Sitemap: {SITE}/sitemap-broken.xml\nSitemap: {SITE}/sitemap-index.xml\n"
    )
    site.broken["/sitemap-broken.xml"] = httpx.Response(500)
    rows = {r["id"]: r for r in listed(site)}
    assert "another site" in rows["https://elsewhere.test/sitemap.xml"]["skipped"], rows
    assert "not an address" in rows["not a url"]["skipped"]
    assert rows[f"{SITE}/sitemap-broken.xml"]["skipped"] == "The sitemap answered 500."
    assert "skipped" not in rows[f"{SITE}/lending/payment-holidays"], "the good sitemap is still read"
    assert not any(r.url.host == "elsewhere.test" for r in site.seen)


def test_the_listing_says_the_pace_the_page_fetches_keep_to(site: FakeSite) -> None:
    site.robots = "User-agent: *\nCrawl-delay: 3\n"
    out = call(plugin, "list_pages", {"source": {"start": [f"{SITE}/start"], "rate": 5}}, {}, fake(site))
    assert out["pace_s"] == 3.0, out.get("pace_s")
