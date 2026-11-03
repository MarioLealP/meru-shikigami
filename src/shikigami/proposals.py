"""Pull requests into repos where Shikigami may only read (D82): AGit - `git push origin HEAD:refs/for/main` - which
Gitea accepts from a reader and turns into a PR. No branch, no write right: a leaked token can propose, never change
`main`. The merge is someone else's (Xolotl; Mario until it exists)."""

import asyncio
import base64
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import httpx2
from pydantic import BaseModel, ConfigDict, TypeAdapter

from shikigami.incoming import Incoming

AUTHOR = ("Shikigami (Meru orchestrator)", "shikigami@clockmost.local")  # the bot's Gitea name + email


class ProposalFailed(Exception):
    """git refused (the message is git's, never the token: it travels in a header, not the URL)."""


class MainMoved(ProposalFailed):
    """A file the PR changes is no longer what it was when the change was worked out: another PR merged in between.
    Pushing the stale text would undo that PR (Meru/zot#21, 2026-11-03), so the activity fails and its retry reads
    again."""


class Edit(BaseModel):
    """One file of a proposal: its text when it was read (None: it wasn't there) and the text wanted (None: delete
    it). `before` is what main must still hold when the PR is pushed - a compare-and-swap, like the sha Gitea's file
    API asks for."""

    model_config = ConfigDict(frozen=True)

    before: str | None
    after: str | None


class _Ref(Incoming):
    sha: str


class Pull(Incoming):
    """A PR as Gitea lists it - the fields the trust steps look at."""

    number: int
    title: str
    state: str  # open | closed
    merged: bool = False
    mergeable: bool = True  # Gitea's own conflict check; false for a moment after any push, while it checks
    base: _Ref | None = None  # the main commit the PR was built on


_PULLS = TypeAdapter(list[Pull])


async def find_pr(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, titles: list[str]) -> Pull | None:
    """The newest PR in a public repo whose title is one of `titles`, open or closed, or None. Asked with both
    directions' titles ("onboard x: ..." and "retire x: ..."), so after a retire and a restore the newest one
    decides."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/pulls", params={"state": "all", "sort": "newest", "limit": 50}
    )
    response.raise_for_status()
    matching = [pr for pr in _PULLS.validate_json(response.content) if pr.title in titles]
    return max(matching, key=lambda pr: pr.number) if matching else None


async def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cwd),  # the root filesystem is read-only; no user or system git config either
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            **(env or {}),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0:
        raise ProposalFailed(f"git {args[0]}: {err.decode(errors='replace').strip()[-500:]}")


async def open_pr(
    remote: str,
    user: str,
    token: str,
    topic: str,
    title: str,
    description: str,
    files: Mapping[str, str | None],
    read: Mapping[str, str | None],
    force: bool = False,
) -> None:
    """One commit with `files` (path -> full text, or None to delete it) on top of the remote's main, pushed as an AGit
    PR on `topic`. `read` holds what each of those files was when `files` was worked out (None: absent); if main no
    longer has exactly that, MainMoved and nothing is pushed. Called when no PR with this title exists (find_pr), or
    with `force` to rebuild Shikigami's own open PR on `topic` that conflicts with a newer main - the only force-push
    it makes: same PR, same number, rebuilt on top of what main is now."""
    if set(read) != set(files):
        raise ValueError(f"read must name exactly the changed files: {sorted(files)}, got {sorted(read)}")
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    with tempfile.TemporaryDirectory(prefix="shikigami-pr-") as tmp:
        work = Path(tmp) / "repo"
        await _git(Path(tmp), "clone", "--quiet", "--depth", "1", "--branch", "main", remote, str(work))
        for path in files:
            target = work / path
            if (target.read_text(encoding="utf-8") if target.is_file() else None) != read[path]:
                raise MainMoved(f"{path} changed on main since it was read")
        for path, text in files.items():
            target = work / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        await _git(work, "add", "--all", "--", *files)
        name, email = AUTHOR
        await _git(work, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "--quiet", "-m", title)
        await _git(
            work,
            "push",
            "--quiet",
            "origin",
            "HEAD:refs/for/main",
            "-o",
            f"topic={topic}",
            "-o",
            f"title={title}",
            "-o",  # a push option can't hold a newline; Gitea decodes this prefix (services/agit)
            f"description={{base64}}{base64.b64encode(description.encode()).decode()}",
            *(["-o", "force-push=true"] if force else []),
            env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
            },
        )
