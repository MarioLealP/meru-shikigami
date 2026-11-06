"""GlitchTip: each app's project and its DSN (D125).

With Shikigami's token (`project:read` + `project:write`, on Mario's GlitchTip user - GlitchTip has no service
accounts): find the app's project in the Meru organization, create it under the Meru team if it isn't there, and read
its DSN. The DSN is plain config, not a secret: its key only sends reports, and GlitchTip is LAN + VPN only (D122).
"""

import httpx2
from pydantic import Field, SecretStr

from shikigami.incoming import Incoming


class _Dsn(Incoming):
    public: str


class _Key(Incoming):
    is_active: bool = Field(alias="isActive")
    dsn: _Dsn


class _Project(Incoming):
    slug: str


async def project_dsn(
    http: httpx2.AsyncClient, url: str, token: SecretStr, org: str, team: str, app: str
) -> tuple[str, bool]:
    """The app's DSN, and whether this call made its project. Kept: a project with the app's slug already there."""
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
    api = f"{url}/api/0"
    found = await http.get(f"{api}/projects/{org}/{app}/", headers=headers)
    created = found.status_code == 404
    if created:
        made = await http.post(
            f"{api}/teams/{org}/{team}/projects/", headers=headers, json={"name": app, "platform": "python"}
        )
        made.raise_for_status()
        slug = _Project.model_validate_json(made.content).slug
        if slug != app:  # GlitchTip suffixes a taken slug: never wire an app to someone else's project
            raise ValueError(f"GlitchTip named {app}'s project {slug!r}")
    else:
        found.raise_for_status()
    keys = await http.get(f"{api}/projects/{org}/{app}/keys/", headers=headers)
    keys.raise_for_status()
    active = [key for key in (_Key.model_validate(k) for k in keys.json()) if key.is_active]
    if not active:
        made = await http.post(f"{api}/projects/{org}/{app}/keys/", headers=headers, json={"name": "meru"})
        made.raise_for_status()
        active = [_Key.model_validate_json(made.content)]
    return active[0].dsn.public, created
