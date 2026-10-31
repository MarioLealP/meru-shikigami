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

AUTHOR = ("Shikigami (Meru orchestrator)", "shikigami@clockmost.local")  # the bot's Gitea name + email


class ProposalFailed(Exception):
    """git refused (the message is git's, never the token: it travels in a header, not the URL)."""


async def find_pr(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, titles: list[str]) -> dict | None:
    """The newest PR in a public repo whose title is one of `titles`, open or closed, or None. Asked with both
    directions' titles ("onboard x: ..." and "retire x: ..."), so after a retire and a restore the newest one
    decides."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/pulls", params={"state": "all", "sort": "newest", "limit": 50}
    )
    response.raise_for_status()
    matching = [pr for pr in response.json() if pr["title"] in titles]
    return max(matching, key=lambda pr: pr["number"]) if matching else None


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
) -> None:
    """One commit with `files` (path -> full text, or None to delete it) on top of the remote's main, pushed as an AGit
    PR on `topic`. Called only when no PR with this title exists (find_pr): Gitea refuses a different commit on an
    open topic without force-push, and Shikigami never forces."""
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    with tempfile.TemporaryDirectory(prefix="shikigami-pr-") as tmp:
        work = Path(tmp) / "repo"
        await _git(Path(tmp), "clone", "--quiet", "--depth", "1", "--branch", "main", remote, str(work))
        for path, text in files.items():
            target = work / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
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
            env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
            },
        )
