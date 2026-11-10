# Confluence plugin

Status: current, tested against a fake Confluence only. No live Cloud or Data Center site has been connected.

A knowledge source (`opsai.confluence`) over the current pages of Confluence spaces, on Cloud and Data Center alike.
Both serve REST API v1 with the same paths, so one code path covers them. A page is stored as its rendered HTML, with
its space and ancestors as the folder, and is fetched again only when its version number moved.

| Tool | Role |
|---|---|
| `list_pages` | List: current pages of the picked spaces, one REST page a call; the last page's `cursor` is when the pass began |
| `get_page` | Fetch: HTML, `allowed` from read restrictions, attachments as `children`, `{unchanged: true}` for a matching version |
| `list_page_changes` | Changes: pages with `lastmodified` after the cursor (CQL), within the picked spaces |
| `list_spaces` | Picker: spaces whose name or key contains the query; a space has no children, so `parent` answers nothing |

Sign-in (team only): Cloud uses `basic`, an Atlassian account's email and an API token; Data Center uses `bearer`, a
personal access token. OAuth 3LO is not offered: its calls go through `api.atlassian.com` with a cloud id, which
Data Center lacks.

| Setting | Meaning |
|---|---|
| `site_url` | Cloud: `https://<site>.atlassian.net/wiki`. Data Center: its address with any context path, `https://wiki.acme.com` |

Source inputs: `spaces` (space keys), `attachments` (also take in each page's attachments; Cloud sends their bytes
from `api.media.atlassian.com`, which the manifest lists).

Known limits: CQL reads its date in the searching user's time zone, so each changes pass reaches back 27 hours and
kb-service skips pages whose version did not move; deleted and trashed pages leave at the weekly full listing; an
attachment changed without editing its page is noticed when the page next changes; attachments carry no `allowed`;
blog posts are not taken in; whether the media host accepts the sign-in header on Cloud downloads is unconfirmed.

Check it from the ops-ai checkout (`../ops-ai`):

```
uv run ops plugin validate ../ops-ai-plugins/plugins/confluence
uv run ops plugin test ../ops-ai-plugins/plugins/confluence
```
