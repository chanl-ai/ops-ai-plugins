# Website plugin

Status: current. A knowledge source over a website's pages for Ops AI (`opsai.web`). A source names one site by its
start pages or sitemaps; the plugin finds its pages from the site's sitemaps (and robots.txt's `Sitemap:` lines, an
index followed one level) and, when asked, from the links of its pages to a set depth, keeps to the source's include
and exclude rules and to robots.txt for `OpsAI-Knowledge`, and asks the site at most once a second (slower when
Crawl-delay says so). A page is fetched only when it changed: its ETag or date goes back to the site, and an unchanged
page answers 304. A preview lists what a source would take in, skipped pages with their reason, and the pages that
show their text only with JavaScript. Rendering those pages is not done here.

| Tool | Role in the knowledge-source contract |
|---|---|
| `list_pages` | List: the site's pages after rules and robots.txt, whole every run, a share per call |
| `get_page` | Fetch: one page, `{unchanged: true}` for a matching tag, NOT_FOUND when gone (404 or 410) |
| `preview_site` | Preview: the first 50 pages and their outcome; nothing is stored |

Source settings (`inputs`): `start` (addresses on one site), `follow_links`, `depth` (1 to 5, 2), `max_pages` (1 to
5,000, 500), `include` and `exclude` (path patterns, `**` across folders, exclude wins), `rate` (pages a second, 0.2 to
5, 1), `render` (not used yet). The hosts of `start` are the only ones a source's sync reaches, through tools-service's
egress policy, which refuses private and link-local addresses unless the account allows them.

Check it from the ops-ai checkout: `uv run ops plugin validate ../ops-ai-plugins/plugins/web`, then its tests with the
plugin on the path: `PYTHONPATH=../ops-ai-plugins/plugins/web/src uv run pytest ../ops-ai-plugins/plugins/web/tests`.
