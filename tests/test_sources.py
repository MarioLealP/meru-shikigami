import io
import tarfile

import httpx2

from shikigami.sources import read_existing_apps, read_requests

ECHO = b"""\
apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: echo
spec:
  owner: mario
  description: Placeholder app
"""


def archive(files: dict[str, bytes]) -> bytes:
    """A tarball the way Gitea serves one: everything under a <repo>/ folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"requests/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def gitea(files: dict[str, bytes], seen: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/branches/main"):
            return httpx2.Response(200, json={"commit": {"id": "abc123"}})
        if request.url.path.endswith("/archive/abc123.tar.gz"):
            return httpx2.Response(200, content=archive(files))
        return httpx2.Response(404)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_reads_the_commit_it_names():
    seen: list[httpx2.Request] = []
    async with gitea({"apps/echo.yaml": ECHO, "README.md": b"hi"}, seen) as http:
        snapshot = await read_requests(http, "https://git.test", "Meru/requests", "main", "tok")
    assert snapshot.commit == "abc123"
    assert list(snapshot.apps) == ["echo"]
    assert snapshot.problems == []
    assert all(r.headers["authorization"] == "token tok" for r in seen)


async def test_a_bad_file_is_a_problem_not_a_crash():
    async with gitea({"apps/echo.yaml": ECHO, "notes.txt": b"stray"}, []) as http:
        snapshot = await read_requests(http, "https://git.test", "Meru/requests", "main", "tok")
    assert snapshot.problems == [
        "notes.txt: not a request - only apps/<name>.yaml and databases/<name>.yaml belong here"
    ]


async def test_thoth_apps_by_key():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params["type"] == "app"
        assert request.headers["authorization"] == "Bearer t"
        item = {"key": "echo", "status": "active", "attributes": {"template": "nuwa/python"}}
        return httpx2.Response(200, json=[item])

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        apps = await read_existing_apps(http, "http://thoth.test", "t")
    assert apps["echo"].template == "nuwa/python"
