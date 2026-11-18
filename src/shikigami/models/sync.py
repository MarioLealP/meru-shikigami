"""The data Shikigami's workflows pass around (Temporal stores it as JSON in its history).

No I/O imports here: workflow code imports this inside Temporal's sandbox.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from shikigami.models.request_format import App, Database


class RequestsSnapshot(BaseModel):
    """Every request at one commit of the requests repo's main branch."""

    model_config = ConfigDict(frozen=True)

    commit: str
    apps: dict[str, App]
    databases: dict[str, Database]
    problems: list[str]


class ExistingApp(BaseModel):
    """A Meru app that exists: its Gitea repo or its Argo CD Application (`read_existing_apps`).

    `status` keeps plan()'s vocabulary: `active` (in service or still onboarding), `decommissioning`/`retained`
    (its retire pass started/finished - a request back gets a restore, D106), `retired` (gone for good). A name
    absent from the reading is not here at all.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    status: str
    template: str | None = None  # never known from Gitea/Argo (Thoth remembered it): a template change re-checks


class AppAction(BaseModel):
    """One app's pass.

    Register a new one, update a changed one, check one that matches (every requested app runs its steps on every sync,
    each keeping what exists), retire one whose request is gone, or restore a retired one whose request is back -
    onboarding again (D106).
    """

    model_config = ConfigDict(frozen=True)

    app: str
    kind: Literal["register", "update", "check", "retire", "restore"]
    reasons: list[str]
    template: str | None = None  # requested apps only
    description: str | None = None
    owner: str | None = None
    secrets: list[str] = []  # the request's secret names (D99), wired into deploy/values.yaml with the marker
    secret_entries: dict[str, str] = {}  # name -> OpenBao entry, for the names not in env (D123)
    environments: list[str] = []  # the request's environment names (D126): fence + signing key per <app>-<env>
    removed_at: datetime | None = (
        None  # removedAt (a legacy request's stamp): only the fallback for the retention clock's start
    )
    theme: str = "unsorted"  # the request's theme: every pass writes it onto the app's namespaces
