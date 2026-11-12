"""The runner's configuration: one JSON file, written by ``init`` and read at start.

```json
{ "watch_url": "https://ingest.clovelly.io",
  "gateway_url": "https://model.clovelly.io",
  "runner_token_file": "runner-token",
  "listen": "0.0.0.0:8090",
  "sessions": 4,
  "environment": { "verifier_url": "https://verifier.example.com", "env_id": "acme",
                   "ca_bundle": "ca.pem", "subenv": "watch" },
  "repositories": { "repo_0123456789abcdef": { "github": "acme/refunds", "template": "refunds",
                    "webhook_secret_file": "repo_0123456789abcdef/webhook-secret",
                    "payload_key_file": "repo_0123456789abcdef/payload-key" } } }
```

Relative paths are relative to the file's directory. Secrets live in their own files
(mode 0600), never in this one. The sandbox SDK's own settings (the control plane's
address and token, the verifier's token) come from its usual environment variables
(``CLOVELLY_API_URL``, ``CLOVELLY_API_TOKEN``, ``CLOVELLY_VERIFIER_TOKEN``, ...).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ID = re.compile(r"^repo_[0-9a-f]{16}$")
_GITHUB = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Repository:
    id: str
    github: str
    template: str
    webhook_secret_file: Path
    payload_key_file: Path

    def webhook_secret(self) -> bytes:
        return self.webhook_secret_file.read_bytes().strip()


@dataclass(frozen=True)
class EnvironmentConfig:
    verifier_url: str
    env_id: str
    ca_bundle: Path
    subenv: str


@dataclass(frozen=True)
class Config:
    watch_url: str
    gateway_url: str
    runner_token_file: Path
    listen: tuple[str, int]
    environment: EnvironmentConfig
    repositories: dict[str, Repository] = field(default_factory=dict)
    sessions: int = 4

    def runner_token(self) -> str:
        return self.runner_token_file.read_text().strip()

    def repository(self, repo_id: str) -> Repository | None:
        return self.repositories.get(repo_id)


def _need(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"{where}{key} is required")
    return d[key]


def parse(doc: dict[str, Any], base: Path) -> Config:
    def path(p: str) -> Path:
        return (base / p).resolve()

    if not isinstance(doc, dict):
        raise ConfigError("the configuration is a JSON object")
    host, _, port = str(doc.get("listen", "0.0.0.0:8090")).rpartition(":")
    if not port.isdigit():
        raise ConfigError("listen is host:port")
    e = _need(doc, "environment", "")
    env = EnvironmentConfig(
        verifier_url=str(_need(e, "verifier_url", "environment.")),
        env_id=str(_need(e, "env_id", "environment.")),
        ca_bundle=path(str(_need(e, "ca_bundle", "environment."))),
        subenv=str(e.get("subenv", "watch")),
    )
    repos: dict[str, Repository] = {}
    for rid, r in (doc.get("repositories") or {}).items():
        if not _REPO_ID.match(rid):
            raise ConfigError(f"repositories: {rid!r} is not a repository id")
        gh = str(_need(r, "github", f"repositories.{rid}."))
        if not _GITHUB.match(gh):
            raise ConfigError(f"repositories.{rid}.github is owner/name")
        repos[rid] = Repository(
            id=rid,
            github=gh,
            template=str(_need(r, "template", f"repositories.{rid}.")),
            webhook_secret_file=path(str(_need(r, "webhook_secret_file", f"repositories.{rid}."))),
            payload_key_file=path(str(_need(r, "payload_key_file", f"repositories.{rid}."))),
        )
    sessions = doc.get("sessions", 4)
    if not isinstance(sessions, int) or not 1 <= sessions <= 64:
        raise ConfigError("sessions is 1 to 64")
    return Config(
        watch_url=str(_need(doc, "watch_url", "")),
        gateway_url=str(_need(doc, "gateway_url", "")),
        runner_token_file=path(str(_need(doc, "runner_token_file", ""))),
        listen=(host or "0.0.0.0", int(port)),
        environment=env,
        repositories=repos,
        sessions=sessions,
    )


def load(file: str | os.PathLike[str]) -> Config:
    p = Path(file)
    return parse(json.loads(p.read_text()), p.parent)


def write_secret(path: Path, value: bytes) -> None:
    """Writes a secret file readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(value)


def make_environment(cfg: Config) -> Any:
    """The customer's environment, through the published sandbox SDK."""
    import clovelly  # the sandbox SDK; imported here so the runner's other code needs no gRPC

    return clovelly.Environment(cfg.environment.verifier_url, cfg.environment.env_id, cfg.environment.ca_bundle.read_text())
