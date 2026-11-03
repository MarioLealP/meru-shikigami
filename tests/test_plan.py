import pytest

from shikigami.models import ExistingApp
from shikigami.plan import plan
from shikigami.request_format import App


def app(name: str, template: str = "nuwa/python", removed: bool = False) -> App:
    spec = {"owner": "mario", "description": "an app", "template": template, "removed": removed}
    return App.model_validate(
        {"apiVersion": "meru.clockmost.com/v1", "kind": "App", "metadata": {"name": name}, "spec": spec}
    )


def existing(key: str, status: str = "active", template: str | None = "nuwa/python") -> ExistingApp:
    return ExistingApp(key=key, status=status, template=template)


def test_new_requests_are_registered_in_name_order():
    actions = plan({"thoth": app("thoth"), "echo": app("echo")}, {})
    assert [(a.app, a.kind) for a in actions] == [("echo", "register"), ("thoth", "register")]


def test_unchanged_app_is_still_checked():
    (action,) = plan({"echo": app("echo")}, {"echo": existing("echo")})
    assert action.kind == "check"


def test_requested_apps_carry_what_their_steps_need():
    (action,) = plan({"echo": app("echo", "nuwa/go")}, {})
    assert (action.template, action.description) == ("nuwa/go", "an app")


def test_retired_apps_carry_no_template():
    (action,) = plan({}, {"old": existing("old")})
    assert action.template is None


@pytest.mark.parametrize("status", ["decommissioning", "retained"])
def test_a_retired_app_whose_request_is_back_is_restored(status: str):
    (action,) = plan({"echo": app("echo")}, {"echo": existing("echo", status)})
    assert (action.kind, action.template) == ("restore", "nuwa/python")  # onboarding again (D106)
    (action,) = plan({"echo": app("echo", removed=True)}, {"echo": existing("echo", status)})
    assert action.kind == "retire"


def test_changed_template_is_an_update():
    (action,) = plan({"echo": app("echo", "nuwa/upstream-like")}, {"echo": existing("echo")})
    assert action.kind == "update"
    assert "nuwa/upstream-like" in action.reasons[0]


def test_app_without_request_is_retired_once():
    actions = plan({}, {"old": existing("old"), "gone": existing("gone", status="retired")})
    assert [(a.app, a.kind) for a in actions] == [("old", "retire")]


def test_retired_app_requested_again_is_registered():
    (action,) = plan({"echo": app("echo")}, {"echo": existing("echo", status="retired")})
    assert action.kind == "register"


def test_a_removed_request_retires_its_app_and_keeps_saying_so():
    for current in ({}, {"golem": existing("golem")}, {"golem": existing("golem", status="decommissioning")}):
        (action,) = plan({"golem": app("golem", removed=True)}, current)
        assert (action.kind, action.reasons) == ("retire", ["removed by its request (D83)"])
        assert action.owner == "mario"  # the retire steps still know whose it was
