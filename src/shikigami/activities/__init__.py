"""Temporal activities: the only places Shikigami touches the outside world.

Each one may retry, so each is safe to run twice. An activity's name is its method's name: running workflows replay
against those names, so they never change. One module per domain.
"""

from shikigami.activities.build import BuildActivities
from shikigami.activities.combined import Activities

__all__ = ["Activities", "BuildActivities"]
