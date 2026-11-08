import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

from shikigami.core.validate import check
from shikigami.models.request_format import App
from shikigami.models.schema import schemas

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
        ({"apps/echo.yaml": ECHO.replace("  name: echo\n", "  name: echo\n  removedBy: mario\n")}, "only on a request"),
        ({"apps/thoth.yaml": THOTH + "  removed: true\n"}, "the database isn't"),
        ({"notes.txt": "hi\n"}, "not a request"),
        ({"apps/sub/echo.yaml": ECHO}, "not a request"),
        ({"apps/echo.yaml": ECHO + "  environments: [{name: uat}, {name: uat}]\n"}, "each environment name only once"),
        ({"apps/echo.yaml": ECHO + "  environments: [{name: build}]\n"}, "build is taken"),
        ({"apps/echo.yaml": ECHO + "  environments: [{name: Prod}]\n"}, "spec.environments.0.name"),
        ({"apps/echo.yaml": ECHO + "  environments: [{name: production}]\n"}, "spec.environments.0.name"),
        (
            {"apps/echo.yaml": ECHO + "  environments: [{name: a}, {name: b}, {name: c}, {name: d}, {name: e}]\n"},
            "spec.environments",
        ),
        (
            {
                "apps/echo.yaml": ECHO + "  environments: [{name: uat}]\n",
                "apps/echo-uat.yaml": ECHO.replace("echo", "echo-uat"),
            },
            "would be namespace echo-uat",
        ),
        (
            {"apps/echo.yaml": ECHO + "  environments: [{name: uat, route: {host: thoth.clockmost.com}}]\n"},
            "used by 2 apps",
        ),
        (
            {
                "apps/echo.yaml": ECHO + "  environments: [{name: uat}]\n",
                "apps/thoth.yaml": THOTH + "  route: {host: echo-uat.clockmost.com}\n",
            },
            "used by 2 apps",
        ),
        ({"databases/thoth.yaml": THOTH_DB + "  environment: uat\n"}, "isn't one of 'thoth'"),
        (
            {
                "apps/thoth.yaml": THOTH + "  environments: [{name: uat}]\n",
                "databases/thoth.yaml": THOTH_DB + "  environment: uat\n",
                "databases/other.yaml": THOTH_DB.replace("name: thoth", "name: other", 1) + "  environment: uat\n",
            },
            "one database per app or environment",
        ),
    ],
)
def test_rejects(tmp_path: Path, files: dict[str, str | None], expected: str) -> None:
    problems = check(repo(tmp_path, GOOD | files))
    assert any(expected in p for p in problems), problems


def test_an_app_with_environments_and_a_database_each_is_valid(tmp_path: Path) -> None:
    thoth = THOTH + "  environments: [{name: uat}, {name: prod, route: {host: cmdb.clockmost.com}}]\n"
    files = {
        "apps/thoth.yaml": thoth,
        "databases/thoth-uat.yaml": THOTH_DB.replace("name: thoth", "name: thoth-uat", 1) + "  environment: uat\n",
        "databases/thoth-prod.yaml": THOTH_DB.replace("name: thoth", "name: thoth-prod", 1) + "  environment: prod\n",
    }
    assert check(repo(tmp_path, GOOD | files)) == []


def test_a_removed_app_with_its_removal_recorded_is_valid(tmp_path: Path) -> None:
    removed = ECHO.replace("  name: echo\n", "  name: echo\n  removedBy: mario\n  removedAt: 2026-10-31T21:00:00Z\n")
    assert check(repo(tmp_path, GOOD | {"apps/echo.yaml": removed + "  removed: true\n"})) == []


def test_schema_files_match_the_models() -> None:
    schema_dir = Path(__file__).parent.parent.parent / "schema"
    for name, text in schemas().items():
        assert (schema_dir / name).read_text() == text, f"schema/{name} is stale - run shikigami-schema"


FULL_APP = {
    "apiVersion": "meru.clockmost.com/v1",
    "kind": "App",
    "metadata": {"name": "echo", "approvedBy": "a", "removedBy": "b", "removedAt": "2026-11-03T12:31:18Z"},
    "spec": {
        "owner": "mario",
        "description": "Placeholder app",
        "removed": True,
        "storage": [{"name": "data", "size": "1Gi", "class": "ssd", "mountPath": "/data"}],
    },
}


def test_temporal_payloads_keep_the_request_files_names():
    app = App.model_validate(FULL_APP)
    payload = pydantic_data_converter.payload_converter.to_payloads([app])
    sent = json.loads(payload[0].data)
    assert sent["apiVersion"] == "meru.clockmost.com/v1"
    assert {"approvedBy", "removedBy", "removedAt"} <= set(sent["metadata"])
    assert {"class", "mountPath"} <= set(sent["spec"]["storage"][0])
    assert pydantic_data_converter.payload_converter.from_payloads(payload, [App]) == [app]


@pytest.mark.parametrize(
    "request_file",
    [
        {k.replace("apiVersion", "api_version"): v for k, v in FULL_APP.items()},
        {**FULL_APP, "metadata": {"name": "echo", "removed_at": "2026-11-03T12:31:18Z"}},
        {
            **FULL_APP,
            "spec": {
                **FULL_APP["spec"],
                "storage": [{"name": "d", "size": "1Gi", "class": "ssd", "mount_path": "/d"}],
            },
        },
    ],
)
def test_a_request_file_cant_use_the_python_names(request_file: dict):
    with pytest.raises(ValidationError):
        App.model_validate(request_file)
