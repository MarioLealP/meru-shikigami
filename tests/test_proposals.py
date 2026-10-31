import base64
import subprocess
from pathlib import Path

import httpx2
import pytest

from shikigami.proposals import AUTHOR, ProposalFailed, find_pr, open_pr

# Like Gitea for a reader (routers/private/serv.go): only refs/for/* pushes get in; anything else is refused.
HOOK = """#!/bin/sh
while read old new ref; do
  case "$ref" in
    refs/for/*) ;;
    *) echo "User permission denied for writing." >&2; exit 1 ;;
  esac
done
i=0
while [ "$i" -lt "${GIT_PUSH_OPTION_COUNT:-0}" ]; do
  eval "echo \\"\\$GIT_PUSH_OPTION_$i\\"" >> push-options
  i=$((i + 1))
done
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "kustomization.yaml").write_text("resources: []\n")
    git(seed, "add", ".")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    bare = tmp_path / "meru.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    git(bare, "config", "receive.advertisePushOptions", "true")
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(HOOK)
    hook.chmod(0o755)
    return bare


async def test_a_proposal_is_one_commit_pushed_to_refs_for_main_with_its_options(remote: Path):
    await open_pr(
        f"file://{remote}",
        "shikigami",
        "tok",
        "onboard-golem",
        "onboard golem: fence",
        "line one\nline two",
        {"projects/golem.yaml": "kind: AppProject\n", "kustomization.yaml": "resources: [x]\n"},
    )
    options = (remote / "push-options").read_text().splitlines()
    assert options[:2] == ["topic=onboard-golem", "title=onboard golem: fence"]
    assert options[2] == "description={base64}" + base64.b64encode(b"line one\nline two").decode()
    commit = git(remote, "rev-parse", "refs/for/main").strip()
    assert (
        git(remote, "log", "-1", "--format=%an <%ae>|%s", commit).strip()
        == f"{AUTHOR[0]} <{AUTHOR[1]}>|onboard golem: fence"
    )
    assert git(remote, "show", f"{commit}:projects/golem.yaml") == "kind: AppProject\n"
    assert git(remote, "rev-list", "--count", commit).strip() == "2"  # on top of main, one commit
    assert git(remote, "rev-list", "--count", "main").strip() == "1"  # main untouched


async def test_none_deletes_a_file_in_the_proposal(remote: Path):
    await open_pr(
        f"file://{remote}", "shikigami", "tok", "retire-golem", "retire golem: fence", "d", {"kustomization.yaml": None}
    )
    commit = git(remote, "rev-parse", "refs/for/main").strip()
    assert git(remote, "ls-tree", "--name-only", commit).strip() == ""  # the seed's only file is gone
    assert git(remote, "ls-tree", "--name-only", "main").strip() == "kustomization.yaml"  # main untouched


async def test_a_refused_push_says_why(remote: Path):
    (remote / "hooks" / "pre-receive").write_text("#!/bin/sh\necho 'User permission denied for writing.' >&2\nexit 1\n")
    with pytest.raises(ProposalFailed, match="permission denied"):
        await open_pr(f"file://{remote}", "shikigami", "tok", "t", "title", "d", {"a.yaml": "a: 1\n"})


async def test_find_pr_matches_the_exact_title_open_or_closed():
    pulls = [
        {"number": 9, "title": "onboard golem: fence (old)", "state": "open"},
        {"number": 7, "title": "onboard golem: fence", "state": "closed", "merged": False},
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert "authorization" not in request.headers  # public repos, read anonymously
        assert request.url.params["state"] == "all"
        return httpx2.Response(200, json=pulls)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    found = await find_pr(http, "https://git.test", "Meru", "meru", ["onboard golem: fence"])
    assert found is not None and found["number"] == 7
    assert await find_pr(http, "https://git.test", "Meru", "meru", ["onboard other: fence"]) is None
