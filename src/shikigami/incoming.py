"""The base for what Shikigami reads from outside (Gitea, OpenBao, Thoth, Argo, the identity provider): pydantic,
immutable and strictly typed, only the fields it uses - the rest of an answer is ignored. A missing or mistyped field
fails the call where it happens (the activity retries), instead of a KeyError somewhere further on."""

from pydantic import BaseModel, ConfigDict


class Incoming(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
