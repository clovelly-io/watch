"""The runner's setup commands (``SPEC-watch.md``, "The runner"): ``init``, ``setup``,
``template build`` and ``check``. Each takes the customer's environment from the sandbox
SDK as an argument, so the same code runs against a verifier or a test's stand-in.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as config_mod
from .api import ApiError, WatchApi
from .config import Config, Repository
from .payload import PrivateKey, generate

#: The two GitHub credentials ``setup`` seals, as the sandbox's policy names them.
GIT_CREDENTIAL = "github-git"
API_CREDENTIAL = "github-api"
HARNESS = "clovelly-watch-harness"


def init(
    directory: Path, *, repo: str, github: str, watch_url: str, gateway_url: str, verifier_url: str, env_id: str, ca_bundle: str
) -> dict[str, Any]:
    """Adds a repository to the runner's configuration in ``directory`` (made if new):
    a payload key pair and a webhook secret of its own, each in a file only its owner
    reads. Returns what the customer needs next: the service's ``CLOVELLY_WATCH_KEY``,
    the webhook's path and the file holding its secret."""
    directory.mkdir(parents=True, exist_ok=True)
    cfg_file = directory / "config.json"
    doc: dict[str, Any] = (
        json.loads(cfg_file.read_text())
        if cfg_file.exists()
        else {
            "watch_url": watch_url,
            "gateway_url": gateway_url,
            "runner_token_file": "runner-token",
            "listen": "0.0.0.0:8090",
            "sessions": 4,
            "environment": {"verifier_url": verifier_url, "env_id": env_id, "ca_bundle": ca_bundle, "subenv": "watch"},
            "repositories": {},
        }
    )
    key_file = directory / repo / "payload-key"
    secret_file = directory / repo / "webhook-secret"
    if key_file.exists():
        key = PrivateKey.parse(key_file.read_text())
    else:
        key = generate()
        config_mod.write_secret(key_file, key.text().encode())
    if not secret_file.exists():
        config_mod.write_secret(secret_file, secrets.token_hex(32).encode())
    doc["repositories"][repo] = {
        "github": github,
        "template": repo.replace("_", "-"),
        "webhook_secret_file": f"{repo}/webhook-secret",
        "payload_key_file": f"{repo}/payload-key",
    }
    cfg_file.write_text(json.dumps(doc, indent=2) + "\n")
    config_mod.parse(doc, directory)  # what was written must load
    return {
        "repo": repo,
        "CLOVELLY_WATCH_KEY": key.public_text(),
        "webhook_path": f"/github/{repo}",
        "webhook_secret_file": str(secret_file),
    }


def policy_destinations(gateway_url: str, repositories: list[str]) -> list[dict[str, Any]]:
    """The destinations a Watch sub-environment's policy bundle needs: GitHub's git host
    with the basic adapter, its API with the bearer adapter, and the model gateway, each
    held by path rules to the runner's own repositories (``owner/name``) and the Messages
    API, so a session can reach no other repository on GitHub, with the customer's token
    or anyone's. Not ``api.anthropic.com``: a session reaches the model only through the
    gateway."""
    gateway = gateway_url.split("://", 1)[-1].split("/", 1)[0]
    host, _, port = gateway.partition(":")
    return [
        {
            "host": "github.com",
            "port": 443,
            "paths": [f"/{r}.git/" for r in repositories],
            "adapter": {"type": "basic", "credential": GIT_CREDENTIAL},
        },
        {
            "host": "api.github.com",
            "port": 443,
            "paths": [f"/repos/{r}/" for r in repositories],
            "adapter": {"type": "bearer", "credential": API_CREDENTIAL},
        },
        {"host": host, "port": int(port or 443), "paths": ["/v1/messages", "/api/hello"]},
    ]


def setup(cfg: Config, environment: Any, token: str, out: Path) -> Path:
    """Seals the repository's fine-grained GitHub token, once, as the two credentials the
    egress proxy substitutes (``github-git``, basic, for ``github.com``; ``github-api``,
    bearer, for ``api.github.com``), and writes the policy destinations to ``out`` for the
    customer to add to their sub-environment's bundle and sign. The runner keeps no copy."""
    token = token.strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("the token is one line with no spaces")
    sub = cfg.environment.subenv
    environment.put_credential(sub, GIT_CREDENTIAL, token, adapter="basic", destination="github.com")
    environment.put_credential(sub, API_CREDENTIAL, token, adapter="bearer", destination="api.github.com")
    repos = sorted({r.github for r in cfg.repositories.values()})
    out.write_text(json.dumps({"destinations": policy_destinations(cfg.gateway_url, repos)}, indent=2) + "\n")
    return out


def _docker(*args: str, capture: bool = False) -> str:
    r = subprocess.run(["docker", *args], check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return r.stdout or ""


def template_build(
    cfg: Config, environment: Any, repo: Repository, directory: Path, *, platform: str = "linux/amd64", upload: bool = True
) -> dict[str, Any]:
    """Builds a repository's template from ``directory`` (a Dockerfile ``FROM`` the base
    image), refuses it if it lacks the harness, and uploads its root as the repository's
    template. Returns the template's name and digest (or the local image, without upload)."""
    tag = f"clovelly-watch-template-{repo.id}".lower()
    _docker("build", "--platform", platform, "-t", tag, str(directory))
    found = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", tag, "-c", f"command -v {HARNESS}"],
        text=True,
        capture_output=True,
    )
    if found.returncode != 0:
        raise RuntimeError(f"the template lacks the harness: {HARNESS} is not on its PATH (build it FROM the base image)")
    if not upload:
        return {"template": repo.template, "image": tag}
    work = Path(tempfile.mkdtemp(prefix="clovelly-template-"))
    try:
        container = _docker("create", "--platform", platform, tag, capture=True).strip()
        tarball = work / "root.tar.gz"
        try:
            with open(tarball, "wb") as f:
                export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
                gz = subprocess.run(["gzip", "-c"], stdin=export.stdout, stdout=f, check=True)
                export.wait()
                if export.returncode or gz.returncode:
                    raise RuntimeError("exporting the template's root failed")
        finally:
            _docker("rm", "-f", container, capture=True)
        t = environment.upload_template(cfg.environment.subenv, repo.template, tarball)
        return {"template": repo.template, "digest": t.digest}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    cause: str = ""


def check(cfg: Config, environment: Any, api: WatchApi, repo: Repository) -> list[Check]:
    """Everything a job needs, end to end, each failure naming its cause: the runner
    token, the repository's secrets, and a session from the template that reaches GitHub
    with both credentials and the model gateway."""
    out: list[Check] = []
    try:
        mine = {r["id"] for r in api.repositories()}
        out.append(
            Check(
                "runner token",
                repo.id in mine,
                "" if repo.id in mine else f"{repo.id} is not one of this runner token's repositories",
            )
        )
    except ApiError as e:
        out.append(Check("runner token", False, f"Watch refused the runner token: {e}"))
    except OSError as e:
        out.append(Check("runner token", False, f"Watch is not reachable at {cfg.watch_url}: {e}"))
    try:
        PrivateKey.parse(repo.payload_key_file.read_text())
        out.append(Check("payload key", True))
    except (OSError, ValueError) as e:
        out.append(Check("payload key", False, f"{repo.payload_key_file}: {e}"))
    out.append(
        Check(
            "webhook secret",
            repo.webhook_secret_file.exists(),
            "" if repo.webhook_secret_file.exists() else f"{repo.webhook_secret_file} is missing",
        )
    )

    sb = environment.sandbox(cfg.environment.subenv, template=repo.template, timeout=300, cpu=1, memory=2)
    try:
        sb.files.write("/job/check.json", json.dumps({"github": repo.github, "gateway_url": cfg.gateway_url}))
        r = sb.exec([HARNESS, "check", "/job/check.json"], cwd="/job", timeout=240)
    finally:
        sb.close()
    if r.exit_code == 127 or (b"not found" in r.stderr.lower() and not r.stdout):
        out.append(Check("template", False, f"the template lacks the harness: {HARNESS} is not on its PATH"))
        return out
    out.append(Check("template", True))
    for line in r.stdout.decode(errors="replace").splitlines():
        try:
            c = json.loads(line)
            out.append(Check(str(c["check"]), bool(c["ok"]), str(c.get("cause", ""))))
        except (ValueError, KeyError, TypeError):
            continue
    if not any(c.name == GIT_CREDENTIAL for c in out):
        out.append(Check("session", False, f"the harness's check said nothing (exit {r.exit_code})"))
    return out


def read_token_from_stdin() -> str:
    """The token, from standard input (never an argument a process listing would show)."""
    import sys

    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("GitHub fine-grained token: ")
    return sys.stdin.readline()


def environment_from(cfg: Config) -> Any:
    return config_mod.make_environment(cfg)
