"""clovelly-watch-runner, Clovelly Watch's runner.

clovelly-watch-runner init DIR --repo REPO --github OWNER/NAME --watch-url URL --gateway-url URL
                               --verifier-url URL --env-id ENV --ca-bundle PEM
    Adds a repository to the runner in DIR: a payload key pair and a webhook secret.
    Prints the service's CLOVELLY_WATCH_KEY and the webhook's path.
clovelly-watch-runner setup CONFIG [--repo REPO] [--out FILE]
    Reads the repository's fine-grained GitHub token on standard input and seals it,
    once, through your verifier as the credentials github-git and github-api; writes the
    policy destinations your sub-environment's bundle needs (default
    policy-destinations.json beside CONFIG).
clovelly-watch-runner template build CONFIG DIR --repo REPO [--platform P]
    Builds the repository's template from DIR (a Dockerfile FROM the base image) and
    uploads it.
clovelly-watch-runner check CONFIG --repo REPO
    Everything end to end; exits 1 naming each cause that failed.
clovelly-watch-runner serve CONFIG [--webhooks-only]
    Receives GitHub's webhooks and runs jobs until stopped; with --webhooks-only, only
    receives the webhooks (a runner whose jobs run as `work`).
clovelly-watch-runner work CONFIG
    Claims one job, runs it to its outcome and exits; exits at once when there is none.
    Hosted mode runs each execution of the organization's runner job this way.
clovelly-watch-runner keygen
    Prints a new payload key pair as JSON.
clovelly-watch-runner open KEY_FILE
    Opens records (JSON lines from Watch's lookups) on stdin.
clovelly-watch-runner hosted-setup KEY_FILE CONFIG [--out FILE]
    Hosted mode's provisioner: opens a cwh1_ sealed GitHub token on stdin with the
    provisioner's key and seals it as `setup` does, in one process, so the token is
    never written out.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from . import commands
from .api import WatchApi
from .config import Config, load
from .payload import Opener, PrivateKey, generate, open_hosted
from .runner import Runner


def _repo(cfg: Config, repo: str | None):  # noqa: ANN202 (a Repository)
    if repo is None and len(cfg.repositories) == 1:
        return next(iter(cfg.repositories.values()))
    r = cfg.repository(repo or "")
    if r is None:
        raise SystemExit(f"--repo: not one of this runner's repositories ({', '.join(cfg.repositories)})")
    return r


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args == ["keygen"]:
        k = generate()
        print(json.dumps({"private": k.text(), "public": k.public_text()}))
        return
    if len(args) == 2 and args[0] == "open":
        key = PrivateKey.parse(Path(args[1]).read_text())
        openers: dict[str, Opener] = {}
        for line in sys.stdin:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("sealed") and record.get("enc"):
                opener = openers.setdefault(record["enc"], Opener(key, bytes.fromhex(record["enc"])))
                record["payload"] = opener.open(bytes.fromhex(record["sealed"]))
            print(json.dumps(record))
        return

    if args[:1] == ["hosted-setup"] and len(args) in (3, 5) and (len(args) == 3 or args[3] == "--out"):
        # Hosted mode's provisioner: a cwh1_ sealed GitHub token on standard input, opened with
        # the provisioner's key and sealed into the organization's sub-environment in this
        # process (the phase 20 review's A4): it reaches no pipe, log or file.
        key = PrivateKey.parse(Path(args[1]).read_text())
        cfg = load(args[2])
        out = Path(args[4]) if len(args) == 5 else Path(args[2]).parent / "policy-destinations.json"
        token = open_hosted(key, sys.stdin.read().strip()).decode()
        commands.setup(cfg, commands.environment_from(cfg), token, out)
        print(f"sealed {commands.GIT_CREDENTIAL} and {commands.API_CREDENTIAL}; destinations in {out}")
        return

    p = argparse.ArgumentParser(
        prog="clovelly-watch-runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init")
    i.add_argument("dir", type=Path)
    for name in ("repo", "github", "watch-url", "gateway-url", "verifier-url", "env-id", "ca-bundle"):
        i.add_argument(f"--{name}", required=True)
    s = sub.add_parser("setup")
    s.add_argument("config")
    s.add_argument("--repo")
    s.add_argument("--out", type=Path)
    t = sub.add_parser("template")
    t.add_argument("action", choices=["build"])
    t.add_argument("config")
    t.add_argument("dir", type=Path)
    t.add_argument("--repo")
    t.add_argument("--platform", default="linux/amd64")
    c = sub.add_parser("check")
    c.add_argument("config")
    c.add_argument("--repo")
    v = sub.add_parser("serve")
    v.add_argument("config")
    v.add_argument("--webhooks-only", action="store_true")
    w = sub.add_parser("work")
    w.add_argument("config")
    a = p.parse_args(args)

    if a.command == "init":
        print(
            json.dumps(
                commands.init(
                    a.dir,
                    repo=a.repo,
                    github=a.github,
                    watch_url=a.watch_url,
                    gateway_url=a.gateway_url,
                    verifier_url=a.verifier_url,
                    env_id=a.env_id,
                    ca_bundle=a.ca_bundle,
                )
            )
        )
        return
    cfg = load(a.config)
    if a.command == "setup":
        out = a.out or Path(a.config).parent / "policy-destinations.json"
        commands.setup(cfg, commands.environment_from(cfg), commands.read_token_from_stdin(), out)
        print(
            f"sealed {commands.GIT_CREDENTIAL} and {commands.API_CREDENTIAL}; "
            f"add the destinations in {out} to your sub-environment's bundle and sign it"
        )
    elif a.command == "template":
        print(
            json.dumps(
                commands.template_build(cfg, commands.environment_from(cfg), _repo(cfg, a.repo), a.dir, platform=a.platform)
            )
        )
    elif a.command == "check":
        results = commands.check(
            cfg, commands.environment_from(cfg), WatchApi(cfg.watch_url, cfg.runner_token()), _repo(cfg, a.repo)
        )
        for r in results:
            print(f"{'ok  ' if r.ok else 'FAIL'}  {r.name}{'' if r.ok else ': ' + r.cause}")
        if not all(r.ok for r in results):
            sys.exit(1)
    elif a.command == "serve":
        runner = Runner(cfg, WatchApi(cfg.watch_url, cfg.runner_token()), commands.environment_from(cfg))
        server = runner.serve_webhooks()
        signal.signal(signal.SIGTERM, lambda *_: runner.stop())
        try:
            if a.webhooks_only:
                runner.wait_until_stopped()
            else:
                runner.run_forever()
        finally:
            server.shutdown()
    elif a.command == "work":
        runner = Runner(cfg, WatchApi(cfg.watch_url, cfg.runner_token()), commands.environment_from(cfg))
        rec = runner.run_one()
        print(json.dumps({"job": None} if rec is None else {"job": rec.job, "outcome": rec.outcome}))


if __name__ == "__main__":
    main()
