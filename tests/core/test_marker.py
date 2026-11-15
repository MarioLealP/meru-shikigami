import pytest

from shikigami.core.marker import BadTemplateError, owner_unset, with_owner

TEMPLATE = '# comment about owner: "" stays\nowner: ""\nchart: nuwa-app-0.7.0\n'


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


def test_secrets_all_in_env_give_the_d99_block_unchanged():
    from shikigami.core.marker import SECRETS_COMMENT, with_secrets

    want = (
        "a: 1\n\n" + SECRETS_COMMENT + "\nexternalSecrets:\n  env: {}\nsecretEnv:\n  API_KEY:\n    secret: env\n"
        "    key: api-key\n"
    )
    assert with_secrets("a: 1\n", ["api-key"], "x") == want
    assert with_secrets("a: 1\n", ["api-key"], "x", {}) == want


def test_a_name_moved_to_a_change_entry_lists_that_entry_once():
    from shikigami.core.marker import with_secrets

    out = with_secrets("", ["a", "b", "c"], "x", {"a": "env-20261105-a1b2c3", "c": "env-20261105-a1b2c3"})
    assert "externalSecrets:\n  env-20261105-a1b2c3: {}\n  env: {}\nsecretEnv:" in out  # first use first, each once
