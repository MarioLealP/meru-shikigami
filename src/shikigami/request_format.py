"""The request format v1 (D34): what someone asks Meru for, one YAML file per thing in Meru/requests.

`App` (apps/<name>.yaml) and `Database` (databases/<name>.yaml). A request says what is wanted, never how: Shikigami
turns it into repos, charts, OpenBao entries and PRs. Status lives in Thoth, not here; the only things written back
into a file are who approved and who removed it, and when - by Xolotl, after the atlas (D82, D83). Retiring keeps the
file: `spec.removed: true`, and Charon deletes the file last, after the retention.

Fields marked "approval" in the docs pause the workflow for Mario's yes in the atlas (D11); this module only checks
the shape, the workflow decides what is risky.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, model_validator

API_VERSION = "meru.clockmost.com/v1"

# The app's name is its namespace, its repo (Meru/<name>), its default host and its Thoth key: a DNS label, short
# enough that "<name>-build" and "meru-<name>-build-eso" still fit, and it never changes.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z]([-a-z0-9]{0,28}[a-z0-9])?$")]
Owner = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9_.]{0,61}[a-z0-9])?$")]
Quantity = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,4}(Mi|Gi)$")]
Days = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,3}d$")]
# The OpenBao entry under apps/meru/<app>/eso/ holding a secret's value: env when requested with the app (D99),
# env-<yyyymmdd>-<6 hex> when a later change wrote it (D123) - Janus picks the name, the request records it.
Entry = Annotated[str, StringConstraints(pattern=r"^env(-[0-9]{8}-[0-9a-f]{6})?$")]
Size = Literal["small", "medium", "large"]  # small 50m/64Mi/256Mi, medium 200m/256Mi/1Gi, large 1/1Gi/4Gi (approval)
StorageClass = Literal["ssd", "hdd"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Metadata(_Model):
    """Who the request is for, and who approved or removed it."""

    name: Name
    approved_by: str | None = Field(
        default=None, alias="approvedBy", description="Written by Xolotl after an atlas approval."
    )
    removed_by: str | None = Field(
        default=None, alias="removedBy", description="Written by Xolotl when a removal is approved."
    )
    removed_at: datetime | None = Field(
        default=None, alias="removedAt", description="Written by Xolotl with removedBy; the retention counts from here."
    )


class Route(_Model):
    """How the app is reached from outside."""

    host: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\.clockmost\.com$",
        description="Default <name>.clockmost.com.",
    )
    auth: Literal["authentik", "oidc", "none"] = Field(
        default="authentik",
        description="authentik = forward-auth at Caddy; oidc = the app logs in itself; none: approval.",
    )
    access: list[Owner] = Field(default=["clockmost-admins"], min_length=1, description="Groups; others: approval.")
    enabled: bool = True


class Volume(_Model):
    """A persistent volume mounted into the app."""

    name: Name
    size: Quantity
    # A file may say `class` or `storage_class`, and only `mountPath`: so no populate_by_name here.
    storage_class: StorageClass = Field(
        validation_alias=AliasChoices("class", "storage_class"), serialization_alias="class"
    )
    mount_path: Annotated[str, StringConstraints(pattern=r"^/\S*$")] = Field(alias="mountPath")


class KyvernoException(_Model):
    """A Kyverno exception for this app's pods. Always needs approval."""

    kind: Literal["runAsRoot", "privileged", "hostNetwork", "hostPath"]
    reason: Annotated[str, StringConstraints(min_length=10, max_length=300)]


class Secret(_Model):
    """One environment variable from OpenBao (D99): `discord-token` -> DISCORD_TOKEN.

    Only the name lives here; Janus writes the value to OpenBao (apps/meru/<app>/eso/env) when the app is requested -
    typed in, or made by Janus. A later change writes a new entry and the request points the name at it (D123).
    """

    name: Name
    generate: bool = Field(default=False, description="true: Janus makes the value (secrets.token_urlsafe(32)).")
    entry: Entry = Field(
        default="env", description="The OpenBao entry with the value: env, or env-<date>-<id> from a change (D123)."
    )


class AppSpec(_Model):
    """What an app asks for."""

    owner: Owner
    description: Annotated[str, StringConstraints(min_length=3, max_length=120, pattern=r"^[^\n]*$")]
    template: str = Field(
        default="nuwa/python",
        pattern=r"^(nuwa/[a-z][-a-z0-9]*|https://\S+)$",
        description="nuwa/<name>; nuwa/upstream needs image; a git URL only seeds the repo (approval).",
    )
    image: str | None = Field(default=None, description="Someone else's image, for template nuwa/upstream.")
    size: Size = "small"
    route: Route = Route()
    storage: list[Volume] = []
    secrets: list[Secret] = Field(default=[], description="Names only; the values go to OpenBao, never here (D99).")
    replicas: Annotated[int, Field(ge=1, le=3)] = 1
    port: Annotated[int, Field(ge=1024, le=65535)] = 8080
    heartbeat: Annotated[str, StringConstraints(pattern=r"^/\S*$")] = "/heartbeat"
    exceptions: list[KyvernoException] = []
    retention: Days = "30d"
    removed: bool = Field(
        default=False, description="true = retire (D83): stopped and revoked now, purged after the retention."
    )

    @model_validator(mode="after")
    def _secret_names_unique(self) -> Self:
        names = [s.name for s in self.secrets]
        if len(names) != len(set(names)):
            raise ValueError("each secret name only once")
        return self

    @model_validator(mode="after")
    def _image_only_for_upstream(self) -> Self:
        if (self.template == "nuwa/upstream") != (self.image is not None):
            raise ValueError("image is required with template nuwa/upstream, and only there")
        return self


def _removal_recorded_only_when_removed(metadata: Metadata, removed: bool) -> None:
    if not removed and (metadata.removed_by or metadata.removed_at):
        raise ValueError("removedBy/removedAt only on a request with spec.removed: true")


class App(_Model):
    """A request for an app: apps/<name>.yaml."""

    api_version: Literal["meru.clockmost.com/v1"] = Field(alias="apiVersion")
    kind: Literal["App"]
    metadata: Metadata
    spec: AppSpec

    @model_validator(mode="after")
    def _removal(self) -> Self:
        _removal_recorded_only_when_removed(self.metadata, self.spec.removed)
        return self


class DatabaseStorage(_Model):
    """The disk a database gets."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    size: Quantity
    storage_class: StorageClass = Field(alias="class")


class DatabaseSpec(_Model):
    """What a database asks for."""

    owner: Owner
    engine: Literal["postgres"] = "postgres"  # a CNPG Cluster
    size: Size = "small"
    storage: DatabaseStorage
    app: Name = Field(description="The one app that gets the credentials: no shared databases.")
    retention: Days = "30d"
    removed: bool = Field(default=False, description="true = retire (D83): purged after the retention.")


class Database(_Model):
    """A request for a database: databases/<name>.yaml."""

    api_version: Literal["meru.clockmost.com/v1"] = Field(alias="apiVersion")
    kind: Literal["Database"]
    metadata: Metadata
    spec: DatabaseSpec

    @model_validator(mode="after")
    def _removal(self) -> Self:
        _removal_recorded_only_when_removed(self.metadata, self.spec.removed)
        return self


Request = App | Database
