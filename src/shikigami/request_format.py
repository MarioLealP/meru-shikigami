"""The request format v1 (D34): what someone asks Meru for, one YAML file per thing in Meru/requests.

`App` (apps/<name>.yaml) and `Database` (databases/<name>.yaml). A request says what is wanted, never how: Shikigami
turns it into repos, charts, OpenBao entries and PRs. Status lives in Thoth, not here; the only thing written back
into a file is `metadata.approvedBy`, by Shikigami's bot after an approval in the atlas.

Fields marked "approval" in the docs pause the workflow for Mario's yes in the atlas (D11); this module only checks
the shape, the workflow decides what is risky.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

API_VERSION = "meru.clockmost.com/v1"

# The app's name is its namespace, its repo (Meru/<name>), its default host and its Thoth key: a DNS label, short
# enough that "<name>-build" and "meru-<name>-build-eso" still fit, and it never changes.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z]([-a-z0-9]{0,28}[a-z0-9])?$")]
Owner = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9_.]{0,61}[a-z0-9])?$")]
Quantity = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,4}(Mi|Gi)$")]
Days = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,3}d$")]
Size = Literal["small", "medium", "large"]  # small 50m/64Mi/256Mi, medium 200m/256Mi/1Gi, large 1/1Gi/4Gi (approval)
StorageClass = Literal["ssd", "hdd"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Metadata(_Model):
    name: Name
    approvedBy: str | None = Field(default=None, description="Written by Shikigami's bot after an atlas approval.")


class Route(_Model):
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
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: Name
    size: Quantity
    storage_class: StorageClass = Field(alias="class")
    mountPath: Annotated[str, StringConstraints(pattern=r"^/\S*$")]


class Exception_(_Model):
    """A Kyverno exception for this app's pods. Always needs approval."""

    kind: Literal["runAsRoot", "privileged", "hostNetwork", "hostPath"]
    reason: Annotated[str, StringConstraints(min_length=10, max_length=300)]


class AppSpec(_Model):
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
    secrets: list[Name] = Field(default=[], description="Names only; the values go to OpenBao, never here.")
    replicas: Annotated[int, Field(ge=1, le=3)] = 1
    port: Annotated[int, Field(ge=1024, le=65535)] = 8080
    heartbeat: Annotated[str, StringConstraints(pattern=r"^/\S*$")] = "/heartbeat"
    exceptions: list[Exception_] = []
    retention: Days = "30d"

    @model_validator(mode="after")
    def _image_only_for_upstream(self) -> Self:
        if (self.template == "nuwa/upstream") != (self.image is not None):
            raise ValueError("image is required with template nuwa/upstream, and only there")
        return self


class App(_Model):
    apiVersion: Literal["meru.clockmost.com/v1"]
    kind: Literal["App"]
    metadata: Metadata
    spec: AppSpec


class DatabaseStorage(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    size: Quantity
    storage_class: StorageClass = Field(alias="class")


class DatabaseSpec(_Model):
    owner: Owner
    engine: Literal["postgres"] = "postgres"  # a CNPG Cluster
    size: Size = "small"
    storage: DatabaseStorage
    app: Name = Field(description="The one app that gets the credentials: no shared databases.")
    retention: Days = "30d"


class Database(_Model):
    apiVersion: Literal["meru.clockmost.com/v1"]
    kind: Literal["Database"]
    metadata: Metadata
    spec: DatabaseSpec


Request = App | Database
