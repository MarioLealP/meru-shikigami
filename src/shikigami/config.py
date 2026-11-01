"""Shikigami's settings, from SHIKIGAMI_* environment variables (Nüwa's env + secretEnv)."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHIKIGAMI_")

    # Temporal's front door (D70): mutual TLS with the pod's pki_meru certificate + an Authentik token.
    temporal_address: str = "temporal-frontend.temporal.svc:7233"
    temporal_server_name: str = "temporal-frontend.temporal.svc"
    temporal_namespace: str = "meru"
    task_queue: str = "shikigami"
    tls_dir: Path = Path("/var/run/meru/tls")  # tls.crt, tls.key, ca.crt (nuwa-app's certificate)

    # Authentik client_credentials (thoth.yaml / shikigami.yaml): one service account, a token per provider.
    token_url: str = "https://auth.clockmost.com/application/o/token/"
    authentik_username: str = "shikigami"
    authentik_password: SecretStr
    temporal_client_id: str = "temporal"
    thoth_client_id: str = "thoth"

    # Where requests come from (D35) and what exists (Thoth).
    gitea_url: str = "https://git.clockmost.com"
    requests_repo: str = "Meru/requests"
    apps_org: str = "Meru"  # where app repos and their templates live (D76)
    requests_branch: str = "main"
    gitea_token: SecretStr
    gitea_user: str = "shikigami"  # the token's account - git's Basic auth names it (AGit PRs, D82)
    merger_user: str = "xolotl"  # gets Write on each app repo as Shikigami steps down to Read (D87, D89)
    merger_ping_url: str = "http://xolotl.xolotl.svc/ping"  # "trust PRs opened, sweep now" (D87)
    first_build_reruns: int = 2  # D90: a failed first build is re-run this many times before Shikigami leaves
    thoth_url: str = "http://thoth.thoth.svc"

    # Making an app's build credentials (D32, D66): the registry its builds push to, and the cosign binary.
    zot_registry: str = "zot.clockmost.com"
    cosign: str = "cosign"
    # ... written to OpenBao with Shikigami's own login (D79): the pod's token + CA from nuwa-app's openbaoLogin.
    openbao_addr: str = "https://openbao.clockmost.internal:8204"
    openbao_role: str = "meru-shikigami-onboard"
    openbao_dir: Path = Path("/var/run/meru/openbao")  # token, ca.crt

    # Reading Argo CD Applications (the retire pass, D85): the pod's API token + cluster CA (nuwa-app kubernetesApi).
    kubernetes_url: str = "https://kubernetes.default.svc"
    kubernetes_dir: Path = Path("/var/run/meru/kubernetes")  # token, ca.crt
    argocd_namespace: str = "argocd"

    # How many app workflows' activities one worker runs at once (D75): replicas x this = the ceiling.
    max_concurrent_activities: int = 4
    # The nightly re-read that catches missed pings (D65), Temporal's cron syntax, UTC.
    nightly_cron: str = "0 3 * * *"
    # Gitea signs its webhook with this (HMAC-SHA256); unset = pings aren't checked (in-cluster only until wired).
    webhook_secret: SecretStr | None = None
    # Not `port`: Kubernetes puts SHIKIGAMI_PORT=tcp://... (a service link for the Service named shikigami) in the env.
    http_port: int = 8080
