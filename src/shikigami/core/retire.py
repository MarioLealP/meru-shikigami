"""An app's retirement in its own repo (D84, D85): the stop commit and parking the marker.

Pure text work on the two deploy files, keeping their comments; the Gitea and Argo CD calls are in activities.py.
"""

import re

from shikigami.core.trust import BadLayoutError

ENABLED = re.compile(r"^enabled: (?:true|false)\b.*$", re.MULTILINE)
OWNER = re.compile(r"^owner: .*$", re.MULTILINE)
BUILD = re.compile(r"^build: (?:true|false)\b.*$", re.MULTILINE)
STOPPED_LINE = "enabled: false # retired (D85): no workload until Charon purges the namespace"
STOPPED = re.compile(rf"^{re.escape(STOPPED_LINE)}\n?", re.MULTILINE)


def stopped_values(values: str) -> str:
    """deploy/values.yaml with `enabled: false` (nuwa-app renders no workload).

    Set if the line exists, else added after the top-level owner line.
    """
    line = STOPPED_LINE
    if ENABLED.search(values):
        return ENABLED.sub(line, values, count=1)
    owner = OWNER.search(values)
    if owner is None:
        raise BadLayoutError("deploy/values.yaml: no top-level owner line to put `enabled: false` after")
    return f"{values[: owner.end()]}\n{line}{values[owner.end() :]}"


def stopped_marker(marker: str) -> str:
    """deploy/meru.yaml with `build: false`: the ApplicationSet drops the Sindri source, so Argo prunes the runner."""
    if not BUILD.search(marker):
        raise BadLayoutError("deploy/meru.yaml: no `build:` line")
    return BUILD.sub("build: false # retired (D85): no build namespace or runner", marker, count=1)


def is_stopped(values: str, marker: str) -> bool:
    """Whether both the values file and the marker already say stopped."""
    return bool(re.search(r"^enabled: false\b", values, re.MULTILINE)) and bool(
        re.search(r"^build: false\b", marker, re.MULTILINE)
    )


def restarted_values(values: str) -> str:
    """deploy/values.yaml without the stop's `enabled: false` line (a restore, D106): nuwa-app's default is a workload.

    Only the line the stop wrote - an owner's own `enabled:` stays theirs.
    """
    return STOPPED.sub("", values, count=1)


def parked(marker: str) -> str:
    """deploy/meru.yaml back into the template's deploy/meru.yaml.in.

    Owner emptied, build back on - what onboarding (D81) turns into a marker again, so undoing a retirement is
    onboarding once more.
    """
    if not OWNER.search(marker) or not BUILD.search(marker):
        raise BadLayoutError("deploy/meru.yaml: no owner or build line to park")
    return BUILD.sub("build: true", OWNER.sub('owner: ""', marker, count=1), count=1)
