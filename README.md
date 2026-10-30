# Shikigami

The orchestrator of **Meru**, a small in-house container platform (CaaS) on k3s. Someone asks for an app or a
database in [Meru/requests](https://git.clockmost.com/Meru/requests) (private); Shikigami turns that request into
everything it needs - repo, build runner, signing key, OpenBao entries, database, route, CMDB entry - by opening pull
requests and calling APIs. It never touches the cluster itself: Argo CD applies what lands in git, and the External
Secrets Operator fills secrets from OpenBao.

This repo holds the **request format v1** and its validator, and the **worker**: Temporal workflows that compare the
requests with what exists in Thoth (Meru's CMDB) and work out each app's step. For now it is a **dry run** - every
step only logs what it would do.

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

## How it runs

One process per pod: a Temporal worker plus a small HTTP server. Replicas are identical - Temporal hands each task
to one of them - and KEDA adds replicas when tasks queue up.

- `POST /ping` - a Gitea webhook on Meru/requests: "go look". Starts `requests-sync`, or tells the running one to
  make one more pass; the body is never trusted, only its signature checked.
- `requests-sync` - reads `main` of Meru/requests, validates it with the same library as the gate (and does nothing
  if anything is wrong), asks Thoth which apps exist, and starts one child workflow per app step (register,
  update, retire), side by side. A nightly schedule runs it too, to catch a missed ping.
- `GET /backlog` - tasks waiting on Shikigami's queue, `{"backlog": n}`; KEDA scales on it.
- `GET /heartbeat`, `GET /ready` - alive; connected to Temporal.
- `onboard-build-<app>` (`OnboardBuild`) - an app's build credentials: its signing key pair and Zot push login
  (`apps/meru/<app>-build/build`, what Sindri build jobs read), its line in Zot's htpasswd, and its runner token.
  What exists is kept; every OpenBao write is check-and-set. Returns the public key.
  `shikigami-bootstrap <app>` runs it once with an admin session's authority, on its own queue - how Shikigami made
  its own (clockmost-infra `projects/openbao/admin/meru-shikigami-bootstrap.sh`).

Temporal is reached with the pod's in-cluster certificate (mTLS) plus a 10-minute Authentik token that is swapped
before it runs out. Settings are `SHIKIGAMI_*` environment variables (`src/shikigami/config.py`).

## Usage

    uv run shikigami-validate path/to/requests   # one line per problem, exit 1 if any
    uv run shikigami-schema                      # regenerate schema/ after changing the models
    uv run shikigami-run                         # the worker + HTTP side (needs SHIKIGAMI_* settings)

## Development

    uv sync
    uv run ruff format --check . && uv run ruff check . && uv run ty check && uv run pytest

The workflow tests start Temporal's test server (downloaded once by the SDK).

Conventional commits; `main` is protected, changes go in as pull requests.

## License

MIT - see `LICENSE`.
