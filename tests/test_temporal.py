from shikigami.temporal import REFRESH_EVERY
from shikigami.tokens import EARLY


def test_refresh_lands_in_the_renewal_window():
    # a slower loop can step over the last EARLY seconds of a token and leave the worker on an expired one
    assert REFRESH_EVERY < EARLY
