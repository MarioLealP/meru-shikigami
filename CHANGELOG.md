# Changelog

## [Unreleased]

### Added
- `spec.removed` on App and Database requests (D83): retiring keeps the request file; Charon deletes it last, after
  the retention. `metadata.removedBy` + `removedAt` (Xolotl writes them, like `approvedBy`) only on a removed
  request. A removed app plans as `retire` on every pass (still the dry run until the retire workflow). Validation:
  an app can't be removed while its database isn't.

### Fixed
- `shikigami-run` logs at INFO: the workflows' and activities' lines (the dry run's "would ...") were dropped.
- The HTTP port setting is `http_port` (`SHIKIGAMI_HTTP_PORT`): Kubernetes' service link `SHIKIGAMI_PORT=tcp://...`
  for the Service named shikigami crashed the pod at start.

### Added
- An app's trust files proposed as PRs (D82): its fence (`Meru/meru projects/<app>.yaml` + kustomization entry) and
  its signing key + build-namespace exception (`Meru/kyverno`), derived - the request picks none of it. Shikigami is
  only a reader there: the PRs go in by AGit (`refs/for/main`), one per repo, never again while one with the same
  title exists (open, merged, or closed unmerged). Only adds: another key already listed for the app is left for a
  person. The image carries git. Also the app's Zot push rule (`Meru/zot config.json`: only `<app>-builder` pushes
  to `meru/<app>`), a third PR - the login alone got golem's first build as far as a refused push.
- Shikigami is a Meru app: `deploy/meru.yaml` + `deploy/values.yaml` (nuwa-app 0.5.0, certificate for Temporal,
  KEDA autoscaling on `/backlog`, its Authentik + Gitea logins via ESO), CI on its Sindri runner (checks, then build,
  push, sign and deploy on `main`), `scripts/check.sh`.
- `OnboardBuild`: an app's build credentials (cosign key pair, Zot push login + its htpasswd line, Sindri runner
  token) into OpenBao, each step keeping what exists, writes check-and-set. `shikigami-bootstrap <app>` runs it
  once on its own queue with borrowed authority. The image carries cosign v3.1.4 (checksum-pinned).
- The worker (`shikigami-run`), dry run: `requests-sync` reads Meru/requests at one commit, validates it, compares it
  with Thoth's apps and starts one child workflow per app step (register / update / retire), each only logging what
  it would do. HTTP: `/ping` (signal-with-start, Gitea signature checked when a secret is set), `/backlog` (for
  KEDA), `/heartbeat`, `/ready`. A nightly schedule, created once by whichever replica starts first. Temporal over
  mTLS with an Authentik token renewed in the background.
- `validate.load()`: the loaded requests as well as the problems.
- The request format v1 (`App`, `Database`) as Pydantic models, generated JSON Schema in `schema/`, and
  `shikigami-validate`, which checks a Meru/requests checkout: every file valid, names match file names, a database
  names an existing app (one per app), hosts unique, nothing but requests in the repo.
