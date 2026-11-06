"""What the activity groups share: their dependencies, and opening a PR once in a repo Shikigami may only read."""

from collections.abc import Mapping

import httpx2
from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shikigami.activities.texts import PROPOSALS, trust_title
from shikigami.clients.proposals import Edit, ProposalFailedError, Pull, find_pr, open_pr
from shikigami.clients.repos import head, public_file
from shikigami.clients.tokens import TokenSource
from shikigami.config import Settings
from shikigami.core import trust


class ActivityBase(BaseModel):
    """The clients every activity group works with, plus the shared steps for proposing a change."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    settings: Settings
    http: httpx2.AsyncClient
    thoth_tokens: TokenSource
    kube: httpx2.AsyncClient | None = None  # trusts the cluster's CA; only the retire pass reads the API

    async def _public(self, app: str, path: str) -> str | None:
        return await public_file(self.http, self.settings.gitea_url, self.settings.apps_org, app, path)

    async def _required(self, org: str, repo: str, path: str) -> str:
        text = await public_file(self.http, self.settings.gitea_url, org, repo, path)
        if text is None:
            raise trust.BadLayoutError(f"{org}/{repo} has no {path}")
        return text

    async def _ping_merger(self, result: str) -> str:
        """Tell Xolotl to sweep now if this pass opened a PR (D87).

        Best effort: its own sweep every 15 minutes is the backstop, so a failed ping is logged, never retried. Returns
        `result` unchanged.
        """
        if "proposed in" in result:
            try:
                (await self.http.post(self.settings.merger_ping_url, json={})).raise_for_status()
            except httpx2.HTTPError as e:
                activity.logger.warning("Xolotl not pinged: %s", e)
        return result

    async def _propose(
        self, app: str, repo: str, what: str, files: Mapping[str, Edit], direction: str = "onboard"
    ) -> str:
        if not files:
            return "kept"
        # the newest PR of either direction decides: after a retire and a restore, the old onboard PR doesn't count
        return await self._open_once(
            repo,
            trust_title(direction, app, what),
            [trust_title(d, app, what) for d in PROPOSALS],
            f"{direction}-{app}",
            PROPOSALS[direction].format(app=app, what=what, files="\n".join(f"- `{path}`" for path in files)),
            files,
        )

    async def _open_once(
        self, repo: str, title: str, titles: list[str], topic: str, description: str, files: Mapping[str, Edit]
    ) -> str:
        """One AGit PR with `title`, unless the newest PR among `titles` already is one.

        Open -> waiting on it, merged -> not on main yet, closed unmerged -> a rejection, never proposed again. An open
        one that conflicts with a newer main (another app's PR to the same file merged first) is rebuilt in place - same
        PR, pushed again on top of main - and Xolotl checks the new commit like any other. Each Edit's `before` must
        still be on main at the push (MainMovedError otherwise: retried, so the change is worked out again).
        """
        s = self.settings
        where = f"{s.apps_org}/{repo}"
        pr = await find_pr(self.http, s.gitea_url, s.apps_org, repo, titles)
        rebuild = False
        if pr is not None and pr.title == title:
            if pr.state == "open":
                if not await self._conflicts_with_newer_main(repo, pr):
                    return f"waiting on {where}#{pr.number}"
                rebuild = True
            elif pr.merged:
                return f"{where}#{pr.number} merged, not on main yet"
            else:
                return f"{where}#{pr.number} was closed unmerged - not proposed again"
        try:
            await open_pr(
                f"{s.gitea_url}/{where}.git",
                s.gitea_user,
                s.gitea_token.get_secret_value(),
                topic,
                title,
                description,
                {path: edit.after for path, edit in files.items()},
                {path: edit.before for path, edit in files.items()},
                force=rebuild,
            )
        except ProposalFailedError as e:
            raise ApplicationError(str(e)) from e  # retried: a push can fail on the network too
        if rebuild and pr is not None:
            activity.logger.info("%s rebuilt on main in %s#%d", title, where, pr.number)
            return f"re-proposed in {where}#{pr.number} (it conflicted with main)"  # "proposed in": Xolotl is pinged
        activity.logger.info("%s proposed in %s", title, where)
        return f"proposed in {where}"

    async def _conflicts_with_newer_main(self, repo: str, pr: Pull) -> bool:
        """Gitea says it can't merge and main has moved past the commit it was built on.

        Gitea's flag alone isn't enough: it reads false for a moment after every push while Gitea checks - a fresh PR,
        built on today's main, is never rebuilt.
        """
        if pr.mergeable or pr.merge_base is None:
            return False
        s = self.settings
        return pr.merge_base != await head(self.http, s.gitea_url, None, s.apps_org, repo)
