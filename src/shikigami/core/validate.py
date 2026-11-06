"""Check a Meru/requests checkout: every file is a valid request, and the files agree with each other.

The repo holds requests and nothing else (D35: not a god repo), so anything that isn't an `App` in apps/ or a
`Database` in databases/ is an error. Usage: `shikigami-validate [repo dir]`; exit 1 with one line per problem.
"""

import sys
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shikigami.models.request_format import App, Database

ALLOWED_EXTRA = {"README.md", ".editorconfig", ".gitignore"}  # plus anything under .gitea/


def _load(path: Path, model: type[App] | type[Database], problems: list[str]) -> App | Database | None:
    try:
        data = yaml.safe_load(path.read_text())
        request = model.model_validate(data)
    except yaml.YAMLError as e:
        problems.append(f"{path}: not YAML: {e}")
        return None
    except ValidationError as e:
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"]) or "(file)"
            problems.append(f"{path}: {where}: {err['msg']}")
        return None
    if request.metadata.name != path.stem:
        problems.append(f"{path}: metadata.name {request.metadata.name!r} must match the file name {path.stem!r}")
    return request


class Loaded(BaseModel):
    """Every request in a checkout, and what's wrong with it - nothing acts on a checkout with problems."""

    model_config = ConfigDict(frozen=True)

    apps: dict[str, App] = Field(default_factory=dict)
    databases: dict[str, Database] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)


def load(repo: Path) -> Loaded:
    """Every request in the checkout at `repo`, with the problems found in and across the files."""
    problems: list[str] = []
    apps: dict[str, App] = {}
    databases: dict[str, Database] = {}

    for path in sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts[:1]):
        rel = path.relative_to(repo)
        if rel.parts[0] == ".gitea" or (len(rel.parts) == 1 and rel.name in ALLOWED_EXTRA):
            continue
        if len(rel.parts) == 2 and rel.suffix == ".yaml" and rel.parts[0] in ("apps", "databases"):
            model = App if rel.parts[0] == "apps" else Database
            request = _load(path, model, problems)
            if isinstance(request, App):
                apps[request.metadata.name] = request
            elif isinstance(request, Database):
                databases[request.metadata.name] = request
            continue
        problems.append(f"{rel}: not a request - only apps/<name>.yaml and databases/<name>.yaml belong here")

    return Loaded(apps=apps, databases=databases, problems=problems + _across_files(apps, databases))


def _across_files(apps: dict[str, App], databases: dict[str, Database]) -> list[str]:
    """What's wrong between the requests, each file being fine on its own."""
    problems: list[str] = []
    for name, db in databases.items():
        if db.spec.app not in apps:
            problems.append(f"databases/{name}.yaml: spec.app {db.spec.app!r} has no apps/{db.spec.app}.yaml")
        elif apps[db.spec.app].spec.removed and not db.spec.removed:
            # its credentials would outlive the app they're for: retire both together
            problems.append(f"databases/{name}.yaml: its app {db.spec.app!r} is removed, the database isn't")
    for app, count in Counter(db.spec.app for db in databases.values()).items():
        if count > 1:
            problems.append(f"apps/{app}.yaml: {count} databases name it as spec.app - one database per app")
    hosts = Counter(a.spec.route.host or f"{a.metadata.name}.clockmost.com" for a in apps.values())
    problems += [f"route.host {host} is used by {n} apps" for host, n in hosts.items() if n > 1]
    return problems


def check(repo: Path) -> list[str]:
    """The problems in the checkout at `repo`; none means it's valid."""
    return load(repo).problems


def main() -> None:
    """The `shikigami-validate` command."""
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    problems = check(repo)
    for line in problems:
        print(line)
    if problems:
        sys.exit(1)
    print(f"{repo}: ok")
