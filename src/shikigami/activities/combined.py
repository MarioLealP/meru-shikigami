"""`Activities`: the domains gathered, so the workflows and the worker have one class to name."""

from shikigami.activities.deploy import DeployActivities
from shikigami.activities.retire import RetireActivities
from shikigami.activities.sync import SyncActivities
from shikigami.activities.trust import TrustActivities


class Activities(SyncActivities, TrustActivities, DeployActivities, RetireActivities):
    """Every activity except the build credentials', over one set of clients."""
