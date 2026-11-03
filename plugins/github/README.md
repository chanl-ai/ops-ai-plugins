# GitHub plugin

Status: current. Lets a team's agents and workflows read GitHub issues and comment on them, signed in with GitHub's
OAuth app sign-in (authorization code), as the team or as each person.

| Tool | Access | What it does |
|---|---|---|
| `list_issues` | read | Issues and pull requests in one repository, newest first, up to 50 (`state`: open, closed or all) |
| `get_issue` | read | One issue by its number, with its body, labels and comment count |
| `add_comment` | write | Adds a Markdown comment to an issue or pull request |

`issue-helper` is an agent template the team adds from the app's Resources. It needs all three tools.

Settings: `web_url` (the sign-in host, `https://github.com`), `api_url` (`https://api.github.com`; a GitHub Enterprise
Server team sets its own) and `api_version` (`2022-11-28`, sent as `X-GitHub-Api-Version`). Register an OAuth app on
GitHub with the return address the Connect window shows, and give its client id and secret there.

`list_issues` reads one page of 50. GitHub pages by the `Link` header, which no paging rule in the plugin kit reads yet.

## Check it

From the ops-ai checkout beside this repository:

```
uv run ops plugin validate ../ops-ai-plugins/plugins/github
uv run ops plugin test ../ops-ai-plugins/plugins/github
```

`tests/fake_github.py` is the fake GitHub the tests call in process. Run as a script it serves the same fake over
https on this machine's private address, for trying the plugin on a local stack:

```
uv run --directory ../ops-ai/services/tools-service python ../ops-ai-plugins/plugins/github/tests/fake_github.py --dir /tmp/fake-github
```

It prints its address, certificate, host map and OAuth client. Give tools-service the certificate as
`OPS_EGRESS_EXTRA_CA` and the host map as `OPS_EGRESS_RESOLVE`, list `github.test`, `api.github.test` and the address
range on the Egress page, and set the app's sign-in address to `https://github.test` and API address to
`https://api.github.test`.
