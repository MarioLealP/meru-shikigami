"""An app's trust files (D82): its fence in Meru/meru, its signing key + build exception in Meru/kyverno, and its push
rule in Meru/zot. All derived - the request picks none of it: the fence is the one shape every app has (Meru/meru's
scripts/fences.py checks it), the key is the one OnboardBuild made, the push rule lets only its builder push.
Shikigami only ever adds an app; an existing entry is never changed here. Text work that keeps each file's layout (and
the YAML files' comments); the git side is in proposals.py."""

import json
import re

import yaml

from shikigami.build_creds import builder
from shikigami.marker import key_trusted


class BadLayout(Exception):
    """A trust repo's file doesn't look the way Shikigami adds to it."""


class Conflict(Exception):
    """A trust repo already has another entry for the app (a key, a push rule) - replacing it is a person's call, never
    Shikigami's."""


def title(app: str) -> str:
    return app[:1].upper() + app[1:]


def fence(app: str, description: str) -> str:
    """projects/<app>.yaml: its own repo plus the Nüwa and Sindri charts, into exactly its two namespaces."""
    return f"""# {title(app)}'s fence: where its Application may read from and deploy to - its own repo plus the
# Nüwa and Sindri charts, into exactly its two namespaces, which it may also create. Written by Shikigami (D82).
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
    - server: https://kubernetes.default.svc
      namespace: {app}
    - server: https://kubernetes.default.svc
      namespace: {app}-build
  clusterResourceWhitelist:
    - {{ group: "", kind: Namespace, name: {app} }}
    - {{ group: "", kind: Namespace, name: {app}-build }}
"""


def build_exception(app: str) -> str:
    """exceptions/<app>-build.yaml: its build namespace may be Pod Security baseline (rootless BuildKit), no more."""
    return f"""apiVersion: policies.kyverno.io/v1
kind: PolicyException
metadata:
  name: {app}-build
  namespace: kyverno
  annotations:
    meru.clockmost.com/reason: >-
      {title(app)}'s build namespace (D32). Its job pods run rootless BuildKit, which needs privilege escalation,
      SETUID/SETGID, SELinux container_engine_t and a custom seccomp profile - Pod Security "baseline", not
      "restricted". Its check pods and the app (namespace {app}) stay restricted. Every other rule still applies.
      Written by Shikigami (D82).
spec:
  policyRefs:
    - name: require-restricted-namespaces
      kind: ValidatingPolicy
  matchConditions:
    - name: {app}-build-namespace-only
      expression: "object.metadata.name == '{app}-build'"
"""


def with_resource(kustomization: str, folder: str, path: str) -> str:
    """The kustomization with `- <path>` listed after the last entry from the same folder (kept if already there)."""
    if re.search(rf"^  - {re.escape(path)}$", kustomization, re.MULTILINE):
        return kustomization
    entries = list(re.finditer(rf"^  - {re.escape(folder)}/[^\n]+$", kustomization, re.MULTILINE))
    if not entries:
        raise BadLayout(f"kustomization.yaml: no `  - {folder}/...` entry to add {path} after")
    end = entries[-1].end()
    return f"{kustomization[:end]}\n  - {path}{kustomization[end:]}"


def with_key(signing_keys: str, app: str, public_key: str) -> str:
    """The signing-keys ConfigMap with the app's key added at the end of `data` (the file's last section). Kept if it
    already trusts exactly this key; another key for the app raises Conflict."""
    data = (yaml.safe_load(signing_keys) or {}).get("data")
    if not isinstance(data, dict):
        raise BadLayout("keys/meru-signing-keys.yaml: no data section")
    if app in data:
        if key_trusted(signing_keys, app, public_key):
            return signing_keys
        raise Conflict(f"Meru/kyverno already trusts another key for {app}")
    lines = [line.strip() for line in public_key.strip().splitlines() if line.strip()]
    block = f"  {app}: |\n" + "".join(f"    {line}\n" for line in lines)
    added = signing_keys.rstrip("\n") + "\n" + block
    # the text append is only right while data is the last section: check the result says what we meant
    try:
        right = key_trusted(added, app, public_key) and len(yaml.safe_load(added)["data"]) == len(data) + 1
    except yaml.YAMLError:
        right = False
    if not right:
        raise BadLayout("keys/meru-signing-keys.yaml: data isn't the last section - add the key by hand")
    return added


def push_rule(app: str) -> dict:
    """Zot access control for meru/<app>: anyone reads, only the app's builder (its Zot login) pushes."""
    return {
        "anonymousPolicy": ["read"],
        "defaultPolicy": ["read"],
        "policies": [{"users": [builder(app)], "actions": ["read", "create", "update"]}],
    }


def with_push_rule(config: str, app: str) -> str:
    """Zot's config.json with meru/<app>'s push rule added (kept if it's already exactly that). The file is plain JSON
    kept as `json.dumps(indent=2)` writes it, so re-writing it changes only the new block; a file in another layout or
    a different rule for the repo is left for a person."""
    parsed = json.loads(config)
    if json.dumps(parsed, indent=2) + "\n" != config:
        raise BadLayout("config.json isn't in json.dumps(indent=2) layout - add the push rule by hand")
    repositories = parsed.get("http", {}).get("accessControl", {}).get("repositories")
    if not isinstance(repositories, dict):
        raise BadLayout("config.json: no http.accessControl.repositories")
    repo = f"meru/{app}"
    if repo in repositories:
        if repositories[repo] == push_rule(app):
            return config
        raise Conflict(f"Meru/zot already has another rule for {repo}")
    repositories[repo] = push_rule(app)
    return json.dumps(parsed, indent=2) + "\n"
