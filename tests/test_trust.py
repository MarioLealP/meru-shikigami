import json
from pathlib import Path

import pytest
import yaml

from shikigami import trust
from shikigami.marker import key_trusted

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEnew\nnewnewnew==\n-----END PUBLIC KEY-----\n"
KEYS = """# comment kept
apiVersion: v1
kind: ConfigMap
metadata:
  name: meru-signing-keys
  namespace: kyverno
data:
  echo: |
    -----BEGIN PUBLIC KEY-----
    AAAA
    -----END PUBLIC KEY-----
"""
KUSTOMIZATION = """resources:
  - projects/platform.yaml
  - projects/echo.yaml
  - appsets/meru-apps.yaml
  # The platform's parts
  - platform/cnpg.yaml
"""


def test_the_fence_is_echos_hand_written_one_for_another_app():
    # Meru/meru's own fence, checked by its scripts/fences.py: Shikigami's must have the same shape, name for name
    real = yaml.safe_load((FIXTURES / "meru-echo-fence.yaml").read_text())
    made = yaml.safe_load(trust.fence("echo", "Meru's placeholder app."))
    assert made == real


def test_a_description_with_yaml_in_it_stays_one_string():
    made = yaml.safe_load(trust.fence("golem", 'it says: "hi" # not a comment'))
    assert made["spec"]["description"] == 'Golem - it says: "hi" # not a comment'


def test_the_build_exception_is_echos_for_another_app():
    real = yaml.safe_load((FIXTURES / "kyverno-echo-build.yaml").read_text())
    made = yaml.safe_load(trust.build_exception("echo"))
    del real["metadata"]["annotations"], made["metadata"]["annotations"]  # the reason's wording, not trust
    assert made == real


def test_a_resource_goes_after_its_folders_last_entry():
    added = trust.with_resource(KUSTOMIZATION, "projects", "projects/golem.yaml")
    assert added.splitlines()[:4] == [
        "resources:",
        "  - projects/platform.yaml",
        "  - projects/echo.yaml",
        "  - projects/golem.yaml",
    ]
    assert trust.with_resource(added, "projects", "projects/golem.yaml") == added  # kept


def test_a_kustomization_without_the_folder_is_a_bad_layout():
    with pytest.raises(trust.BadLayoutError):
        trust.with_resource(KUSTOMIZATION, "exceptions", "exceptions/golem-build.yaml")


def test_a_key_is_added_and_the_file_keeps_its_comments():
    added = trust.with_key(KEYS, "golem", KEY)
    assert added.startswith(KEYS)
    assert key_trusted(added, "golem", KEY)
    assert key_trusted(added, "echo", "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n")


def test_the_same_key_is_kept_and_another_one_is_never_swapped_in():
    added = trust.with_key(KEYS, "golem", KEY)
    assert trust.with_key(added, "golem", KEY) == added
    with pytest.raises(trust.ConflictError):
        trust.with_key(KEYS, "echo", KEY)


def test_a_section_after_data_is_a_bad_layout():
    with pytest.raises(trust.BadLayoutError):
        trust.with_key(KEYS + "binaryData: {}\n", "golem", KEY)


ZOT_CONFIG = (FIXTURES / "zot-config.json").read_text()  # Meru/zot's real config.json


def test_a_push_rule_is_added_and_nothing_else_in_zots_config_changes():
    added = trust.with_push_rule(ZOT_CONFIG, "golem")
    before, after = json.loads(ZOT_CONFIG), json.loads(added)
    rule = after["http"]["accessControl"]["repositories"].pop("meru/golem")
    assert after == before
    assert rule == before["http"]["accessControl"]["repositories"]["meru/echo"] | {
        "policies": [{"users": ["golem-builder"], "actions": ["read", "create", "update"]}]
    }
    # the diff a person reviews is only the new block
    assert set(ZOT_CONFIG.splitlines()) <= set(added.splitlines())


def test_the_same_push_rule_is_kept_and_another_one_is_never_replaced():
    assert trust.with_push_rule(ZOT_CONFIG, "echo") == ZOT_CONFIG
    changed = ZOT_CONFIG.replace('"echo-builder"', '"someone-else"')
    with pytest.raises(trust.ConflictError):
        trust.with_push_rule(changed, "echo")


def test_a_zot_config_in_another_layout_is_left_for_a_person():
    with pytest.raises(trust.BadLayoutError):
        trust.with_push_rule(json.dumps(json.loads(ZOT_CONFIG)), "golem")
