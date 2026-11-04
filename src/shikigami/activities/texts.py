"""The titles and descriptions of the PRs Shikigami opens; Xolotl reads the titles back to know what to rebuild."""

PROPOSAL = """Shikigami's onboarding of `{app}` (D82): its {what}, derived - the request picks none of it.

{files}

Shikigami only reads this repo, so this is a proposal. It's merged by Xolotl once the request is approved on
Meru/requests and this PR only adds `{app}`; until Xolotl exists, by Mario. Closing it unmerged stops Shikigami
proposing it again."""

RETIREMENT = """Shikigami's retirement of `{app}` (D85): its {what} comes out - the app is stopped and its Argo CD
Application gone, so nothing uses it any more.

{files}

Shikigami only reads this repo, so this is a proposal. It's merged by Xolotl once the removal is approved on
Meru/requests and this PR only removes `{app}`'s entries; until Xolotl exists, by Mario. Closing it unmerged stops
Shikigami proposing it again."""

PROPOSALS = {"onboard": PROPOSAL, "retire": RETIREMENT}

STOP = """Shikigami's retirement of `{app}` (D85, cm-konk): the stop - `enabled: false` (no workload) and `build: false`
(no runner). The app's request is marked removed on Meru/requests.

Shikigami only reads this repo since onboarding (D89), so this is a proposal; Xolotl merges it once it matches the
rebuild from main and the request backs it. After Argo has applied it, the marker is parked in a second PR."""

PARK = """Shikigami's retirement of `{app}` (D85, cm-konk): the marker goes back to the template's
`deploy/meru.yaml.in`, so Meru stops deploying the repo. Onboarding again undoes it.

Opened only after Argo applied the stop. Xolotl merges it once it matches the rebuild from main."""

DEPLOY_PR = """Shikigami's onboarding of `{app}` (D81, D99): the marker that makes Meru deploy this repo -
`deploy/meru.yaml`, from the template's `deploy/meru.yaml.in` with the owner `{owner}` - and in `deploy/values.yaml`
the owner and the request's secrets as env vars ({secrets}), read by ESO from the app's own OpenBao path.

Opened once the app's fence and signing key are in place. Xolotl merges it once every file matches what it rebuilds
from main and the approved request; the first build starts after that."""


def trust_title(direction: str, app: str, what: str) -> str:
    """A trust PR's title, in the repos' Conventional Commits form - Xolotl reads it back to know what to rebuild."""
    return f"chore(trust): {direction} {app} - {what}"


def retire_title(step: str, app: str) -> str:
    """A retire PR's title in the app's own repo (cm-konk), which Xolotl reads back to know what to rebuild.

    `chore(retire): stop <app>` or `chore(retire): park <app>`.
    """
    return f"chore(retire): {step} {app}"
