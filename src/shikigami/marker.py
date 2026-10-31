"""An app's deploy files (D81): the template's deploy/meru.yaml.in becomes deploy/meru.yaml - the marker that makes Meru
deploy the repo - with the owner from the request, once the app's fence and signing key are in place. Pure text
work; the Gitea calls are in repos.py."""

import re

import yaml

EMPTY_OWNER = re.compile(r'^owner: ""(?:[ \t]*#[^\n]*)?$', re.MULTILINE)


class BadTemplate(Exception):
    """The template's file doesn't have the line Shikigami fills in."""


def with_owner(text: str, owner: str, path: str) -> str:
    """The file with its one `owner: ""` line set to the request's owner (a validated name, safe in YAML)."""
    filled, count = EMPTY_OWNER.subn(f"owner: {owner}", text)
    if count != 1:
        raise BadTemplate(f'{path}: expected one `owner: ""` line, found {count}')
    return filled


def owner_unset(text: str) -> bool:
    return EMPTY_OWNER.search(text) is not None


def _body(key: str) -> str:
    return "".join(line.strip() for line in key.splitlines() if line.strip() and "-----" not in line)


def key_trusted(signing_keys: str, app: str, public_key: str) -> bool:
    """Whether Meru/kyverno's signing-keys ConfigMap trusts exactly this key for the app (its namespace)."""
    data = (yaml.safe_load(signing_keys) or {}).get("data") or {}
    listed = data.get(app)
    return isinstance(listed, str) and bool(_body(public_key)) and _body(listed) == _body(public_key)
