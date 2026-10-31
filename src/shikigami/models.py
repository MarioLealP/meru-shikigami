"""The data Shikigami's workflows pass around (Temporal stores it as JSON in its history). No I/O imports here:
workflow code imports this inside Temporal's sandbox."""

from typing import Literal

from pydantic import BaseModel

from shikigami.request_format import App, Database


class RequestsSnapshot(BaseModel):
    """Every request at one commit of the requests repo's main branch."""

    commit: str
    apps: dict[str, App]
    databases: dict[str, Database]
    problems: list[str]


class ExistingApp(BaseModel):
    """A Meru app Thoth knows (item type `app`, written by Shikigami)."""

    key: str
    status: str
    template: str | None = None


class AppAction(BaseModel):
    """One app's pass: register a new one, update a changed one, check one that matches (every requested app runs its
    steps on every sync, each keeping what exists), or retire one whose request is gone."""

    app: str
    kind: Literal["register", "update", "check", "retire"]
    reasons: list[str]
    template: str | None = None  # requested apps only
    description: str | None = None
    owner: str | None = None
