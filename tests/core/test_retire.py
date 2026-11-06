import json
from pathlib import Path

import pytest
import yaml

from shikigami.clients.argo import Application, not_stopped
from shikigami.core import retire, trust
from shikigami.core.build_creds import htpasswd_without
from shikigami.core.marker import owner_unset, with_owner

FIXTURES = Path(__file__).parent.parent / "fixtures"
TEMPLATE_IN = 'owner: ""\nchart: nuwa-app-0.9.0 # tested with\nbuild: true\nbuildLabels:\n  sindri-check: uv\n'
VALUES = "# golem's deployment\nname: golem\nowner: mario\nimage:\n  digest: sha256:aa\n"


def test_the_stop_commit_turns_off_the_workload_and_the_runner():
    marker = with_owner(TEMPLATE_IN, "mario", "x")
    values = retire.stopped_values(VALUES)
    assert yaml.safe_load(values)["enabled"] is False
    assert values.startswith("# golem's deployment\nname: golem\nowner: mario\nenabled: false")
    stopped = retire.stopped_marker(marker)
    assert yaml.safe_load(stopped)["build"] is False
    assert retire.is_stopped(values, stopped)
    assert not retire.is_stopped(VALUES, marker)


def test_an_enabled_line_already_there_is_flipped_not_doubled():
    values = retire.stopped_values(VALUES + "enabled: true\n")
    assert values.count("enabled:") == 1
    assert yaml.safe_load(values)["enabled"] is False


def test_values_without_an_owner_line_are_a_bad_layout():
    with pytest.raises(trust.BadLayoutError):
        retire.stopped_values("name: golem\n")


def test_a_restore_takes_out_only_the_stops_line():
    values = "name: golem\nowner: mario\nenabled: false # own reason\n"
    stopped = retire.stopped_values("name: golem\nowner: mario\n")
    assert retire.restarted_values(stopped) == "name: golem\nowner: mario\n"
    assert retire.restarted_values(values) == values  # the owner's own line stays theirs


def test_parking_gives_back_what_onboarding_turns_into_a_marker():
    parked = retire.parked(retire.stopped_marker(with_owner(TEMPLATE_IN, "mario", "x")))
    assert yaml.safe_load(parked) == yaml.safe_load(TEMPLATE_IN)
    assert owner_unset(parked)  # D81 fills it in again on a restore
    assert with_owner(parked, "mario", "deploy/meru.yaml.in")


def test_removals_undo_what_onboarding_added():
    keys = (
        "data:\n  echo: |\n    -----BEGIN PUBLIC KEY-----\n    AAAA\n    -----END PUBLIC KEY-----\n"
        "  thoth: |\n    -----BEGIN PUBLIC KEY-----\n    BBBB\n    -----END PUBLIC KEY-----\n"
    )
    key = "-----BEGIN PUBLIC KEY-----\nGOLEM\n-----END PUBLIC KEY-----\n"
    assert trust.without_key(trust.with_key(keys, "golem", key), "golem") == keys
    assert trust.without_key(keys, "echo") == (
        "data:\n  thoth: |\n    -----BEGIN PUBLIC KEY-----\n    BBBB\n    -----END PUBLIC KEY-----\n"
    )
    assert trust.without_key(keys, "golem") == keys  # nothing to remove
    kustomization = "resources:\n  - projects/echo.yaml\n  - appsets/meru-apps.yaml\n"
    added = trust.with_resource(kustomization, "projects", "projects/golem.yaml")
    assert trust.without_resource(added, "projects/golem.yaml") == kustomization
    config = (FIXTURES / "zot-config.json").read_text()
    assert trust.without_push_rule(trust.with_push_rule(config, "golem"), "golem") == config
    assert (
        "meru/echo" not in json.loads(trust.without_push_rule(config, "echo"))["http"]["accessControl"]["repositories"]
    )


def test_a_zot_line_comes_out_and_the_rest_stays():
    text = "admin:$2y$x\ngolem-builder:$2y$y\necho-builder:$2y$z\n"
    assert htpasswd_without(text, "golem-builder") == "admin:$2y$x\necho-builder:$2y$z\n"
    assert htpasswd_without(text, "nobody") is None


def app(
    revisions: list[str], sync: str = "Synced", kinds: tuple[str, ...] = ("Service",), phase: str = "Succeeded"
) -> Application:
    return Application.model_validate(
        {
            "status": {
                "sync": {"status": sync, "revisions": revisions},
                "operationState": {"phase": phase},
                "resources": [{"kind": kind} for kind in kinds],
            }
        }
    )


@pytest.mark.parametrize(
    ("found", "reason"),
    [
        (app(["nuwa", "old", "sindri"]), "hasn't seen"),
        (app(["nuwa", "c0ffee", "sindri"], sync="OutOfSync"), "sync is OutOfSync"),
        (app(["nuwa", "c0ffee", "sindri"], phase="Running"), "a sync is running"),
        (app(["nuwa", "c0ffee", "sindri"], kinds=("Service", "Deployment", "Job")), "still has Deployment, Job"),
    ],
)
def test_the_app_isnt_stopped_until_argo_says_so(found: Application, reason: str):
    assert reason in (not_stopped(found, "c0ffee") or "")


def test_synced_on_the_stop_commit_with_no_workload_is_stopped():
    assert not_stopped(app(["nuwa", "c0ffee"], kinds=("Service", "ExternalSecret")), "c0ffee") is None
