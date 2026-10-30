from pathlib import Path

import pytest

from shikigami.schema import schemas
from shikigami.validate import check

ECHO = """\
apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: echo
spec:
  owner: mario
  description: Placeholder app
"""

THOTH = """\
apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: thoth
spec:
  owner: mario
  description: Meru's CMDB
  size: medium
"""

THOTH_DB = """\
apiVersion: meru.clockmost.com/v1
kind: Database
metadata:
  name: thoth
spec:
  owner: mario
  storage: {size: 2Gi, class: ssd}
  app: thoth
"""


def repo(tmp_path: Path, files: dict[str, str | None]) -> Path:
    for name, text in files.items():
        if text is None:  # a case that removes a file from GOOD
            continue
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


GOOD: dict[str, str | None] = {
    "apps/echo.yaml": ECHO,
    "apps/thoth.yaml": THOTH,
    "databases/thoth.yaml": THOTH_DB,
    "README.md": "# x\n",
}


def test_good_repo(tmp_path: Path) -> None:
    assert check(repo(tmp_path, GOOD | {".gitea/workflows/check.yaml": "on: push\n"})) == []


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"apps/echo.yaml": ECHO.replace("name: echo", "name: Echo")}, "metadata.name"),
        ({"apps/other.yaml": ECHO}, "must match the file name"),
        ({"apps/echo.yaml": ECHO + "  colour: blue\n"}, "spec.colour: Extra inputs are not permitted"),
        ({"apps/echo.yaml": ECHO.replace("kind: App", "kind: Database")}, "kind"),
        ({"apps/echo.yaml": ECHO + "  size: huge\n"}, "spec.size"),
        ({"apps/echo.yaml": ECHO + "  template: nuwa/upstream\n"}, "image is required"),
        ({"apps/echo.yaml": ECHO + "  image: nginx\n"}, "image is required"),
        ({"apps/echo.yaml": ECHO + "  route: {host: echo.example.com}\n"}, "spec.route.host"),
        ({"apps/echo.yaml": ECHO + "  exceptions: [{kind: privileged, reason: x}]\n"}, "spec.exceptions.0.reason"),
        ({"apps/echo.yaml": ECHO + "  storage: [{name: data, size: 1Gi, mountPath: /data}]\n"}, "class"),
        ({"apps/echo.yaml": "a: [\n"}, "not YAML"),
        ({"apps/thoth.yaml": None}, "has no apps/thoth.yaml"),
        ({"databases/other.yaml": THOTH_DB.replace("name: thoth", "name: other", 1)}, "one database per app"),
        ({"apps/thoth.yaml": THOTH + "  route: {host: echo.clockmost.com}\n"}, "used by 2 apps"),
        ({"notes.txt": "hi\n"}, "not a request"),
        ({"apps/sub/echo.yaml": ECHO}, "not a request"),
    ],
)
def test_rejects(tmp_path: Path, files: dict[str, str | None], expected: str) -> None:
    problems = check(repo(tmp_path, GOOD | files))
    assert any(expected in p for p in problems), problems


def test_schema_files_match_the_models() -> None:
    schema_dir = Path(__file__).parent.parent / "schema"
    for name, text in schemas().items():
        assert (schema_dir / name).read_text() == text, f"schema/{name} is stale - run shikigami-schema"
