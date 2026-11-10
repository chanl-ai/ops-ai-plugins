# SharePoint and OneDrive plugin

Status: current, tested against a fake Microsoft Graph only. No live tenant has been connected.

A knowledge source (`opsai.sharepoint`) over files in SharePoint document libraries and OneDrive. It reads through
Microsoft Graph as the account a team admin signs in with, keeps each library current with a delta query, and fetches
a file only when its eTag moved. A file is identified as `<library id>:<item id>`.

| Tool | Role |
|---|---|
| `list_files` | List: the files of the picked libraries, one Graph page a call; the last page's `cursor` holds one delta token per library |
| `get_file` | Fetch: content as text or base64, `allowed` from the file's permissions, `{unchanged: true}` for a matching eTag |
| `list_file_changes` | Changes: added and changed files, and deleted ones as `{id, deleted: true, updated_at}` |
| `list_scopes` | Picker: sites by name (`/sites?search=`), then a site's libraries when given `parent` |

Sign-in: Microsoft Entra app registration, delegated `Sites.Read.All`, `Files.Read.All`, `offline_access`, signed in as
a team. The Microsoft 365 admin usually has to grant consent for the first two.

| Setting | Meaning |
|---|---|
| `tenant_id` | Entra tenant id; `organizations` (default) accepts any work account. Set on the auth config |
| `sharepoint_url` | `https://<tenant>.sharepoint.com`, filled at sign-in; Graph redirects downloads to this host |
| `onedrive_url` | `https://<tenant>-my.sharepoint.com`, only for OneDrive libraries |

Source inputs: `sites` (site ids), `libraries` (drive ids; all libraries of the sites when empty, and the only way to
take in a OneDrive), `include` and `exclude` (path patterns of the library path, `**` crosses folders, exclude wins).

Known limits: a deleted folder reports the folder only, so its files leave at the weekly full listing; files over 25
MB are listed as skipped; Graph national clouds are not supported; whether SharePoint accepts the sign-in header on the
redirected download is not confirmed without a tenant.

Check it from the ops-ai checkout (`../ops-ai`):

```
uv run ops plugin validate ../ops-ai-plugins/plugins/sharepoint
uv run ops plugin test ../ops-ai-plugins/plugins/sharepoint
```
