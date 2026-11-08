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
        app = apps.get(db.spec.app)
        if app is None:
            problems.append(f"databases/{name}.yaml: spec.app {db.spec.app!r} has no apps/{db.spec.app}.yaml")
        elif app.spec.removed and not db.spec.removed:
            # its credentials would outlive the app they're for: retire both together
            problems.append(f"databases/{name}.yaml: its app {db.spec.app!r} is removed, the database isn't")
        elif db.spec.environment and db.spec.environment not in {e.name for e in app.spec.environments}:
            problems.append(
                f"databases/{name}.yaml: spec.environment {db.spec.environment!r} isn't one of "
                f"{db.spec.app!r}'s environments"
            )
    for (app, env), count in Counter((db.spec.app, db.spec.environment) for db in databases.values()).items():
        if count > 1:
            where = f"its environment {env!r}" if env else "it"
            problems.append(f"apps/{app}.yaml: {count} databases name {where} - one database per app or environment")
    hosts = Counter(_hosts(apps))
    problems += [f"route.host {host} is used by {n} apps or environments" for host, n in hosts.items() if n > 1]
    for name, app in apps.items():
        for env in app.spec.environments:
            if (namespace := f"{name}-{env.name}") in apps:
                problems.append(f"apps/{name}.yaml: environment {env.name!r} would be namespace {namespace}, an app")
    return problems


def _hosts(apps: dict[str, App]) -> list[str]:
    """Every host the apps and their environments are reached at (D126: an environment's default <app>-<env>)."""
    hosts = []
    for name, app in apps.items():
        hosts.append(app.spec.route.host or f"{name}.clockmost.com")
        hosts += [env.route.host or f"{name}-{env.name}.clockmost.com" for env in app.spec.environments]
    return hosts


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
