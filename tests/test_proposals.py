import base64
import subprocess
from pathlib import Path

import httpx2
import pytest

from shikigami.proposals import AUTHOR, MainMoved, ProposalFailed, find_pr, open_pr

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
        "chore(trust): onboard golem - fence",
        "line one\nline two",
        {"projects/golem.yaml": "kind: AppProject\n", "kustomization.yaml": "resources: [x]\n"},
        {"projects/golem.yaml": None, "kustomization.yaml": "resources: []\n"},
    )
    options = (remote / "push-options").read_text().splitlines()
    assert options[:2] == ["topic=onboard-golem", "title=chore(trust): onboard golem - fence"]
    assert options[2] == "description={base64}" + base64.b64encode(b"line one\nline two").decode()
    commit = git(remote, "rev-parse", "refs/for/main").strip()
    assert (
        git(remote, "log", "-1", "--format=%an <%ae>|%s", commit).strip()
        == f"{AUTHOR[0]} <{AUTHOR[1]}>|chore(trust): onboard golem - fence"
    )
    assert git(remote, "show", f"{commit}:projects/golem.yaml") == "kind: AppProject\n"
    assert git(remote, "rev-list", "--count", commit).strip() == "2"  # on top of main, one commit
    assert git(remote, "rev-list", "--count", "main").strip() == "1"  # main untouched


async def test_none_deletes_a_file_in_the_proposal(remote: Path):
    await open_pr(
        f"file://{remote}",
        "shikigami",
        "tok",
        "retire-golem",
        "chore(trust): retire golem - fence",
        "d",
        {"kustomization.yaml": None},
        {"kustomization.yaml": "resources: []\n"},
    )
    commit = git(remote, "rev-parse", "refs/for/main").strip()
    assert git(remote, "ls-tree", "--name-only", commit).strip() == ""  # the seed's only file is gone
    assert git(remote, "ls-tree", "--name-only", "main").strip() == "kustomization.yaml"  # main untouched


async def test_a_refused_push_says_why(remote: Path):
    (remote / "hooks" / "pre-receive").write_text("#!/bin/sh\necho 'User permission denied for writing.' >&2\nexit 1\n")
    with pytest.raises(ProposalFailed, match="permission denied"):
        await open_pr(f"file://{remote}", "shikigami", "tok", "t", "title", "d", {"a.yaml": "a: 1\n"}, {"a.yaml": None})


@pytest.mark.parametrize(
    ("read", "why"),
    [
        ({"kustomization.yaml": "resources: [kitsune]\n"}, "kustomization.yaml changed"),  # another PR merged since
        ({"kustomization.yaml": None}, "kustomization.yaml changed"),  # read as absent, there now
    ],
)
async def test_a_file_that_moved_on_main_since_it_was_read_is_never_pushed(remote: Path, read: dict, why: str):
    """Meru/zot#21: a change worked out from an older main would undo whatever merged in between."""
    with pytest.raises(MainMoved, match=why):
        await open_pr(f"file://{remote}", "shikigami", "tok", "t", "title", "d", {"kustomization.yaml": "x\n"}, read)
    assert not (remote / "push-options").exists()  # nothing reached Gitea
    assert git(remote, "for-each-ref", "refs/for").strip() == ""


async def test_read_must_name_exactly_the_changed_files(remote: Path):
    with pytest.raises(ValueError, match="read must name"):
        await open_pr(f"file://{remote}", "shikigami", "tok", "t", "title", "d", {"a.yaml": "a\n"}, {})


async def test_find_pr_matches_the_exact_title_open_or_closed():
    pulls = [
        {"number": 9, "title": "chore(trust): onboard golem - fence (old)", "state": "open"},
        {"number": 7, "title": "chore(trust): onboard golem - fence", "state": "closed", "merged": False},
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert "authorization" not in request.headers  # public repos, read anonymously
        assert request.url.params["state"] == "all"
        return httpx2.Response(200, json=pulls)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    found = await find_pr(http, "https://git.test", "Meru", "meru", ["chore(trust): onboard golem - fence"])
    assert found is not None and found.number == 7
    assert await find_pr(http, "https://git.test", "Meru", "meru", ["chore(trust): onboard other - fence"]) is None
