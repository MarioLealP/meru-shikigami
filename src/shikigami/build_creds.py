"""An app's build credentials (D32, D66): the Zot push login its builds use and the cosign key that signs them. The
shapes match what Sindri build jobs read (apps/meru/<app>-build/build, base64 fields) and what Zot reads (one
htpasswd file, a line per app)."""

import asyncio
import base64
import json
import os
import secrets
import tempfile
from pathlib import Path

import bcrypt

BUILD_FIELDS = ("zot_config_b64", "cosign_key_b64", "cosign_password_b64")


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def unb64(text: str) -> str:
    return base64.b64decode(text).decode()


def builder(app: str) -> str:
    return f"{app}-builder"


def zot_config(registry: str, user: str, password: str) -> str:
    """A docker config.json that logs buildctl + cosign in to Zot."""
    return json.dumps({"auths": {registry: {"auth": b64(f"{user}:{password}")}}})


def zot_login(config: str, registry: str) -> tuple[str, str]:
    user, _, password = unb64(json.loads(config)["auths"][registry]["auth"]).partition(":")
    return user, password


def htpasswd_with(text: str, user: str, password: str) -> str | None:
    """`text` with `user`'s line set to `password`, or None when the line already matches (nothing to write).
    Every other line is kept as it is."""
    lines = text.splitlines()
    for line in lines:
        name, _, hashed = line.partition(":")
        if name == user and bcrypt.checkpw(password.encode(), hashed.encode()):
            return None
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    kept = [line for line in lines if line.partition(":")[0] != user]
    return "\n".join([*kept, f"{user}:{hashed}"]) + "\n"


async def _cosign(cosign: str, password: str, *args: str, cwd: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        cosign,
        *args,
        cwd=cwd,
        env={**os.environ, "COSIGN_PASSWORD": password},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode:
        raise RuntimeError(f"cosign {args[0]} failed: {err.decode().strip()}")
    return out


async def new_cosign_key(cosign: str) -> tuple[str, str, str]:
    """A fresh key pair: (encrypted private key, its password, public key)."""
    password = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory() as tmp:
        await _cosign(cosign, password, "generate-key-pair", cwd=tmp)
        return (Path(tmp, "cosign.key").read_text(), password, Path(tmp, "cosign.pub").read_text())


async def cosign_public_key(cosign: str, key: str, password: str) -> str:
    """The public half of an existing key (Echo's and Thoth's entries don't store it)."""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "cosign.key").write_text(key)
        return (await _cosign(cosign, password, "public-key", "--key", "cosign.key", cwd=tmp)).decode()
