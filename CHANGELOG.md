# Changelog

## [Unreleased]

### Added
- The worker (`shikigami-run`), dry run: `requests-sync` reads Meru/requests at one commit, validates it, compares it
  with Thoth's apps and starts one child workflow per app step (register / update / retire), each only logging what
  it would do. HTTP: `/ping` (signal-with-start, Gitea signature checked when a secret is set), `/backlog` (for
  KEDA), `/heartbeat`, `/ready`. A nightly schedule, created once by whichever replica starts first. Temporal over
  mTLS with an Authentik token renewed in the background.
- `validate.load()`: the loaded requests as well as the problems.
- The request format v1 (`App`, `Database`) as Pydantic models, generated JSON Schema in `schema/`, and
  `shikigami-validate`, which checks a Meru/requests checkout: every file valid, names match file names, a database
  names an existing app (one per app), hosts unique, nothing but requests in the repo.
