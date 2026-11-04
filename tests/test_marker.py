import pytest

from shikigami.marker import BadTemplateError, key_trusted, owner_unset, with_owner

TEMPLATE = '# comment about owner: "" stays\nowner: ""\nchart: nuwa-app-0.7.0\n'
KEY = "-----BEGIN PUBLIC KEY-----\nAAAA\nBBBB\n-----END PUBLIC KEY-----\n"
KEYS = """apiVersion: v1
kind: ConfigMap
data:
  echo: |
    -----BEGIN PUBLIC KEY-----
    AAAA
    BBBB
    -----END PUBLIC KEY-----
"""


def test_fills_the_one_empty_owner_line():
    filled = with_owner(TEMPLATE, "mario", "x")
    assert filled == '# comment about owner: "" stays\nowner: mario\nchart: nuwa-app-0.7.0\n'


def test_keeps_a_trailing_comment_out():
    assert with_owner('owner: "" # from the request\n', "mario", "x") == "owner: mario\n"


@pytest.mark.parametrize("text", ["owner: mario\n", 'owner: ""\nowner: ""\n', "chart: x\n"])
def test_refuses_a_file_without_exactly_one_empty_owner(text: str):
    with pytest.raises(BadTemplateError):
        with_owner(text, "mario", "deploy/meru.yaml.in")


def test_knows_when_the_owner_is_still_unset():
    assert owner_unset('owner: "" # from the request\n')
    assert not owner_unset("owner: mario\n")


def test_trusts_only_the_same_key_for_the_same_app():
    assert key_trusted(KEYS, "echo", KEY)
    assert not key_trusted(KEYS, "thoth", KEY)
    assert not key_trusted(KEYS, "echo", KEY.replace("BBBB", "CCCC"))
    assert not key_trusted(KEYS, "echo", "")
    assert not key_trusted("", "echo", KEY)
