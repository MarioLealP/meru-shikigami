# Shikigami

The orchestrator of **Meru**, a small in-house container platform (CaaS) on k3s. Someone asks for an app or a
database in [Meru/requests](https://git.clockmost.com/Meru/requests) (private); Shikigami turns that request into
everything it needs - repo, build runner, signing key, OpenBao entries, database, route, CMDB entry - by opening pull
requests and calling APIs. It never touches the cluster itself: Argo CD applies what lands in git, and the External
Secrets Operator fills secrets from OpenBao.

So far this repo holds the **request format v1** and its validator. The workflows (Temporal, Python) come next.

## The request format

One YAML file per thing: `apps/<name>.yaml` (`kind: App`) or `databases/<name>.yaml` (`kind: Database`).

```yaml
apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: echo # namespace, repo Meru/echo, default host, CMDB key - never changes
spec:
  owner: mario
  description: Placeholder app - GET /heartbeat answers ok
  route:
    host: echo.clockmost.com
```

The Pydantic models in `src/shikigami/request_format.py` are the definition; `schema/*.v1.json` is the same thing
as JSON Schema, generated from them (a test fails if they drift). Everything optional has a default; fields such as
`size: large`, `route.auth: none` or `exceptions` pause for an approval in the atlas.

## Usage

    uv run shikigami-validate path/to/requests   # one line per problem, exit 1 if any
    uv run shikigami-schema                      # regenerate schema/ after changing the models

## Development

    uv sync
    uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest

Conventional commits; `main` is protected, changes go in as pull requests.

## License

MIT - see `LICENSE`.
