"""An app's deploy files (D81).

The template's deploy/meru.yaml.in becomes deploy/meru.yaml - the marker that makes Meru deploy the repo - with the
owner from the request, once the app's fence and signing key are in place. Pure text work; the Gitea calls are in
repos.py.
"""

import re
from collections.abc import Mapping

import yaml

EMPTY_OWNER = re.compile(r'^owner: ""(?:[ \t]*#[^\n]*)?$', re.MULTILINE)


# The shared contract with Xolotl (xolotl/expected.py rebuilds the marker PR, D99): byte for byte.
SECRETS_COMMENT = (
    "# Secrets from the request (D99): Janus wrote the values to OpenBao apps/meru/<app>/eso/env when the app was"
    " requested."
)
TITLE = "chore(deploy): {app} is a Meru app"  # Xolotl reads it back to know what to rebuild
SET_KEYS = re.compile(r"^(externalSecrets|secretEnv):", re.MULTILINE)


class BadTemplateError(Exception):
    """The template's file doesn't have the line Shikigami fills in."""


def with_owner(text: str, owner: str, path: str) -> str:
    """The file with its one `owner: ""` line set to the request's owner (a validated name, safe in YAML)."""
    filled, count = EMPTY_OWNER.subn(f"owner: {owner}", text)
    if count != 1:
        raise BadTemplateError(f'{path}: expected one `owner: ""` line, found {count}')
    return filled


def owner_unset(text: str) -> bool:
    """Whether the file still has the empty `owner: ""` line."""
    return EMPTY_OWNER.search(text) is not None


def _body(key: str) -> str:
    return "".join(line.strip() for line in key.splitlines() if line.strip() and "-----" not in line)


def key_trusted(signing_keys: str, app: str, public_key: str) -> bool:
    """Whether Meru/kyverno's signing-keys ConfigMap trusts exactly this key for the app (its namespace)."""
    data = (yaml.safe_load(signing_keys) or {}).get("data") or {}
    listed = data.get(app)
    return isinstance(listed, str) and bool(_body(public_key)) and _body(listed) == _body(public_key)


def env_name(secret: str) -> str:
    """`discord-token` -> DISCORD_TOKEN.

    Request names are lowercase letters, digits and dashes, starting with a letter, so every one is a valid environment
    variable name and no two collide.
    """
    return secret.upper().replace("-", "_")


def with_secrets(text: str, secrets: list[str], path: str, entries: Mapping[str, str] | None = None) -> str:
    """deploy/values.yaml with the request's secrets wired (D99).

    One ExternalSecret per OpenBao entry the names live in (ESO fills each from the app's own path) - `env`, plus a
    change's `env-<date>-<id>` on a restore after one (D123) - and one env var per name. `entries` maps the names not in
    env; an app with every name in env gets exactly the D99 block. Appended at the end; kept if that exact block is
    already there (a restore, D106: the first onboarding wrote it). Any other wiring of either key is refused - its
    owner's wiring isn't Shikigami's to merge.
    """
    if not secrets:
        return text
    entry = {name: (entries or {}).get(name, "env") for name in secrets}
    lines = ["", SECRETS_COMMENT, "externalSecrets:"]
    lines += [f"  {used}: {{}}" for used in dict.fromkeys(entry.values())]  # first use first: env leads as before
    lines.append("secretEnv:")
    for name in secrets:
        lines += [f"  {env_name(name)}:", f"    secret: {entry[name]}", f"    key: {name}"]
    block = "\n".join(lines) + "\n"
    if text.endswith(block):
        return text
    if SET_KEYS.search(text):
        raise BadTemplateError(
            f"{path}: already sets externalSecrets or secretEnv - not wiring the request's secrets in"
        )
    return (text if text.endswith("\n") else text + "\n") + block
