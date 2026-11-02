# Ops AI built-in plugins

The plugins that ship with Ops AI, one folder per outside system under `plugins/`. Each is a Python package holding a
`plugin.yaml` manifest (its sign-in, tools and knowledge sources) and, where needed, a little Python. Ops AI loads them
when plugins are switched on (`OPS_PLUGINS`); a release pins each plugin's version. The first is Salesforce.

Status: current. Private repository at github.com/chanl-ai/ops-ai-plugins.

## Run a plugin's checks

You need the ops-ai checkout beside this one (`../ops-ai`), set up with `uv sync` there.

```
cd ../ops-ai
uv run ops plugin validate ../ops-ai-plugins/plugins/salesforce
uv run ops plugin test ../ops-ai-plugins/plugins/salesforce
```

`validate` prints `OK opsai.salesforce 0.1.0: 15 tools, 1 knowledge sources, 1 templates` or each problem with its
field. `test` validates, then runs the plugin's own tests against its fake Salesforce.

## Common tasks

| Task | How |
|---|---|
| Load the plugins on a local Ops AI stack | Start the stack with `OPS_PLUGINS=true` and `OPS_PLUGIN_PATHS='["<path>/ops-ai-plugins/plugins/salesforce/src/ops_plugin_salesforce"]'` |
| Add a plugin | Copy `plugins/salesforce` to `plugins/<system>`, change its manifest and tests, and run `ops plugin test` on it |
| Write a plugin | Follow ops-ai's `docs/guide/plugins.md`: the manifest, Python tools, knowledge sources and the switches that turn a plugin on |
