"""An app's trust files (D82).

Its fence in Meru/meru and its push rule in Meru/zot (the signing key + build exception in Meru/kyverno went with
Kyverno, 2026-11-15). All derived - the request picks none of it: the fence is the one shape every app has (Meru/meru's
scripts/fences.py checks it), the push rule lets only its builder push. Shikigami only ever adds an app; an existing
entry is never changed here. Text work that keeps each file's layout (and the YAML files' comments); the git side is in
proposals.py.
"""

import json
import re
from collections.abc import Sequence

from shikigami.core.build_creds import builder


class BadLayoutError(Exception):
    """A trust repo's file doesn't look the way Shikigami adds to it."""


class ConflictError(Exception):
    """A trust repo already has another entry for the app (a key, a push rule).

    Replacing it is a person's call, never Shikigami's.
    """


def title(app: str) -> str:
    """The app's name with a capital first letter, as the generated comments write it."""
    return app[:1].upper() + app[1:]


def namespaces(app: str, environments: Sequence[str] = ()) -> list[str]:
    """The app's namespaces: its own, its build's, then one per environment (D126) in the request's order."""
    return [app, f"{app}-build", *(f"{app}-{env}" for env in environments)]


def fence(app: str, description: str, environments: Sequence[str] = ()) -> str:
    """projects/<app>.yaml: its own repo plus the Nüwa and Sindri charts, into exactly its namespaces."""
    where = (
        "into exactly its namespaces - the app's, its build's and its environments' (D126) -"
        if environments
        else "into exactly its two namespaces,"
    )
    destinations = "".join(
        f"    - server: https://kubernetes.default.svc\n      namespace: {ns}\n" for ns in namespaces(app, environments)
    )
    kinds = "".join(f'    - {{ group: "", kind: Namespace, name: {ns} }}\n' for ns in namespaces(app, environments))
    return f"""# {title(app)}'s fence: where its Application may read from and deploy to - its own repo plus the
# Nüwa and Sindri charts, {where} which it may also create. Written by Shikigami (D82).
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: meru-{app}
  namespace: argocd
spec:
  description: {json.dumps(f"{title(app)} - {description}", ensure_ascii=False)}
  sourceRepos:
    - https://git.clockmost.com/Meru/{app}.git
    - https://git.clockmost.com/Meru/nuwa.git
    - https://git.clockmost.com/Meru/sindri.git
  destinations:
{destinations}  clusterResourceWhitelist:
{kinds}"""


def with_resource(kustomization: str, folder: str, path: str) -> str:
    """The kustomization with `- <path>` listed after the last entry from the same folder (kept if already there)."""
    if re.search(rf"^  - {re.escape(path)}$", kustomization, re.MULTILINE):
        return kustomization
    entries = list(re.finditer(rf"^  - {re.escape(folder)}/[^\n]+$", kustomization, re.MULTILINE))
    if not entries:
        raise BadLayoutError(f"kustomization.yaml: no `  - {folder}/...` entry to add {path} after")
    end = entries[-1].end()
    return f"{kustomization[:end]}\n  - {path}{kustomization[end:]}"


def without_resource(kustomization: str, path: str) -> str:
    """The kustomization without its `- <path>` line (kept if it isn't listed)."""
    return re.sub(rf"^  - {re.escape(path)}\n", "", kustomization, count=1, flags=re.MULTILINE)


def push_rule(app: str) -> dict:
    """Zot access control for meru/<app>: anyone reads, only the app's builder (its Zot login) pushes."""
    return {
        "anonymousPolicy": ["read"],
        "defaultPolicy": ["read"],
        "policies": [{"users": [builder(app)], "actions": ["read", "create", "update"]}],
    }


def with_push_rule(config: str, app: str) -> str:
    """Zot's config.json with meru/<app>'s push rule added (kept if it's already exactly that).

    The file is plain JSON kept as `json.dumps(indent=2)` writes it, so re-writing it changes only the new block; a file
    in another layout or a different rule for the repo is left for a person.
    """
    parsed = json.loads(config)
    if json.dumps(parsed, indent=2) + "\n" != config:
        raise BadLayoutError("config.json isn't in json.dumps(indent=2) layout - add the push rule by hand")
    repositories = parsed.get("http", {}).get("accessControl", {}).get("repositories")
    if not isinstance(repositories, dict):
        raise BadLayoutError("config.json: no http.accessControl.repositories")
    repo = f"meru/{app}"
    if repo in repositories:
        if repositories[repo] == push_rule(app):
            return config
        raise ConflictError(f"Meru/zot already has another rule for {repo}")
    repositories[repo] = push_rule(app)
    return json.dumps(parsed, indent=2) + "\n"


def without_push_rule(config: str, app: str) -> str:
    """Zot's config.json without meru/<app>'s rule (same layout rule as with_push_rule)."""
    parsed = json.loads(config)
    if json.dumps(parsed, indent=2) + "\n" != config:
        raise BadLayoutError("config.json isn't in json.dumps(indent=2) layout - remove the push rule by hand")
    repositories = parsed.get("http", {}).get("accessControl", {}).get("repositories")
    if not isinstance(repositories, dict):
        raise BadLayoutError("config.json: no http.accessControl.repositories")
    if repositories.pop(f"meru/{app}", None) is None:
        return config
    return json.dumps(parsed, indent=2) + "\n"
