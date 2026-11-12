# The Watch runner

The runner is the part of Clovelly Watch you run yourself, beside the verifier you already run for your sandboxes
(customer-held mode). It receives GitHub's webhooks for your repositories, claims Watch's jobs, runs each job in a
sandbox of your own environment, and reports to Watch only a result that fits a closed schema. In hosted mode we run
the same image for you.

## What it holds, and what it does not

| | The runner | The sandbox | Watch (us) |
|---|---|---|---|
| Sandbox access keys | yes, in memory | no | no |
| Each repository's payload private key | yes | no | no |
| Your GitHub token | no: `setup` seals it once through your verifier | a placeholder only | sealed ciphertext |
| The model key | no | a token for one job | in the model gateway only |
| Source code | no | yes | only what the agent sends the model, passing through the gateway |

The sandbox reaches three hosts and nothing else: `github.com` (git, with the `github-git` credential put in by your
cell's egress proxy), `api.github.com` (with `github-api`) and `model.clovelly.io`, Watch's model gateway. It has no
route to Watch's API and none to `api.anthropic.com`.

**What the gateway sees.** The agent in the session is the Claude Agent SDK (which runs Claude Code, unmodified). It
sends its prompts, which hold the code it reads and the diff, to `model.clovelly.io` with the job's token. The
gateway checks the token, the model and the job's budget, forwards the request to Anthropic with our key, and passes
the reply back as it streams. It keeps, per job, the tokens in, cached and out, the model and the cost, and writes no
request or reply anywhere, logs included. Prompts do pass through it in the clear, in both modes: that is our word,
not a proof.

## Setting it up

```bash
pip install "clovelly-watch-runner[sandbox] @ git+https://github.com/clovelly-io/watch#subdirectory=runner"
#   or run the published image beside your verifier: runner/deploy/compose.yaml, or on Google Cloud
#   runner/deploy/gcp.sh, or on AWS runner/deploy/aws.yaml
clovelly-watch-runner init ./watch-runner --repo REPO_ID --github acme/refunds \
  --watch-url https://ingest.clovelly.io --gateway-url https://model.clovelly.io \
  --verifier-url https://verifier.acme.example --env-id acme --ca-bundle ca.pem
```

`init` makes the repository's payload key pair and webhook secret, each in a file only you can read, and prints the
service's `CLOVELLY_WATCH_KEY`. Put your runner token (from the console) in `./watch-runner/runner-token`.

```bash
clovelly-watch-runner setup ./watch-runner/config.json < token.txt     # a fine-grained token for the repository
```

`setup` reads the token on standard input and seals it, once, as two credentials of your Watch sub-environment
(`watch` by default): `github-git` (the basic adapter, for `github.com`) and `github-api` (bearer, for
`api.github.com`). The runner keeps no copy. It writes `policy-destinations.json`, each destination held by path rules to the runner's own repositories
(`/OWNER/NAME.git/` on `github.com`, `/repos/OWNER/NAME/` on `api.github.com`) and the gateway's Messages API, so a
session reaches no other repository with your token or anyone's: add those destinations to the
sub-environment's policy bundle and sign it with your policy key ([the verifier guide](https://github.com/clovelly-io/clovelly/blob/main/docs/verifier-deploy.md),
"Publishing a policy").

The token needs, on that repository only: Contents read and write, Pull requests read and write, Issues read and
write, Metadata read. Protect your default branch: the agent can push to any branch the token allows.

```bash
clovelly-watch-runner template build ./watch-runner/config.json ./template      # a Dockerfile FROM the base image
clovelly-watch-runner check ./watch-runner/config.json
```

`template build` builds your repository's template (a Dockerfile that starts `FROM` our base image, with whatever
your tests need) and uploads it; it refuses an image without the harness. `.clovelly/watch.toml` in the repository
names its `test` command and an optional `setup` command. `check` runs everything end to end and names the cause of
each failure: the runner token, the repository's secrets, a session from the template reaching GitHub with each
credential and reaching the gateway (a wrong token, a destination missing from the policy, a template without the
harness).

Then run it (`clovelly-watch-runner serve ./watch-runner/config.json`, the compose file, or on Google Cloud
`runner/deploy/gcp.sh deploy PROJECT REGION ./watch-runner`, which enables the APIs, lets the service's account read
its three secrets and deploys one always-on Cloud Run service open to GitHub's webhooks; an organization policy that
restricts IAM members to your domains refuses that last step, as the script's header says) and add one webhook to
the repository: `https://YOUR-HOST/github/REPO_ID`, content type `application/json`, the secret from
`./watch-runner/REPO_ID/webhook-secret`, events "Pull requests" and "Issues".

`serve` keeps one runner up and claiming, which is the simplest and costs a small always-on instance. To pay only
while there is work, split it: `clovelly-watch-runner serve CONFIG --webhooks-only` answers only GitHub's webhooks
(short requests, so a service at zero instances that has CPU only during a request will do; in `gcp.sh`, change
`--min-instances 1 --no-cpu-throttling` to `--min-instances 0` and add `--webhooks-only` to the command), and
`clovelly-watch-runner work CONFIG` claims one job, runs it and exits, or exits at once when there is none. Start
`work` on a schedule, for example as a Cloud Run job (`gcloud run jobs deploy` with the same image, secrets and
`work` command, a task timeout of two hours, since an alert job's session alone may take an hour) run every few
minutes by Cloud Scheduler; a job waits at most one interval. Hosted mode runs this way, with Watch starting an
execution whenever the organization has work.

## A job

A merged pull request into the default branch is reported to Watch, which queues its **monitors job** once. The runner
claims it (a ten-minute lease, renewed while it works), makes a sandbox from the repository's template (2 vCPU,
4 GiB, a 15-minute limit), writes the job to `/job/job.json` and runs the harness. The harness clones the repository
(a blobless partial clone) at the merge commit, gives the agent the diff, the code and your existing monitors with
read-only tools, and writes `/job/result.json`:

```json
{ "kind": "monitors",
  "create": [ monitor, ... ],                    // at most 10
  "change": [ { "id": "mon_…", "monitor": monitor } ],
  "delete": [ "mon_…" ],
  "merge_position": 42 }                         // the merge commit's first-parent position
```

Each monitor is in the schema of [watch-monitors.md](watch-monitors.md), its source the job's pull request and merge
commit. The runner parses the file itself (`vectors/monitors.json` holds the rules it shares with Watch) and refuses
the whole result on anything else: an unknown field, a name that is not a slug, a string with spaces, an eleventh
monitor, a monitor of another pull request or repository. A refused result is reported as `failed`, and nothing of it
leaves the runner. When the model budget runs out, the gateway refuses further requests (402, `budget_spent`), the
harness ends the session, and the job ends `budget_spent`.

The merge's position the runner reports after a monitors job is read from a file in the sandbox
(`/job/position.json`), which the session could rewrite, as it could the result: the runner checks both against its
schema, not against the truth.

If the runner dies mid-job, the lease lapses and the job is claimed again; the orphaned sandbox ends at its limit with
a final receipt, and the job's monitors are written once, by whichever claim finishes it.

## The alert job

When a monitor fires, Watch queues an **alert job** (unless the monitor's fix is already open: a pull request or issue
the last job opened, in which case the alert is a `duplicate` and nothing is queued). The runner claims it, fetches the
alert's records (up to 20 failing requests, exceptions or log lines) and opens their sealed payloads with the
repository's payload key. The plaintext goes into the sandbox and nowhere else. The session has 60 minutes, and the
agent has `Read`, `Grep`, `Glob`, `Bash`, `Edit` and `Write`.

Anyone who can send your service a request chooses what those payloads say. The harness hands them to the model as
quoted data under a standing instruction that nothing in them is an instruction. The session's policy reaches GitHub
only for this repository (path rules) and the model only through the gateway, which refuses server-side tools (web
fetch or search, code execution, MCP servers) and URL sources. The session holds no credential: the egress proxy
puts the token in place on the way out. The agent can still use the token on this repository through the proxy, as
the harness does, so **protect your default branch and your release tags**.

The harness runs the job in one session:

1. **Triage.** The agent reads the alert and its records at the deployed commit (the release the records carry). If
   it is noise, the job ends with the monitor kept, muted, deleted or replaced by a looser one.
2. **Reproduce.** The agent writes a test, in a new file. The harness runs it on the deployed commit itself, in a
   fresh clone, and it must fail with your test runner's "a test failed" status (1 for pytest, jest, vitest and
   `node --test`; set `test_failed_exit` in `.clovelly/watch.toml` if yours uses another, from 6 to 123). An error
   (pytest's 2 to 5, a timeout, a missing command, a signal) shows nothing. Jest, Vitest, `node --test` and `cargo
   test` exit with the same status for a failed assertion and for a test file that does not load (an import of
   something only the fix adds, a compile error), so for them the status alone shows less: with Jest or Vitest, set
   `test_report = "jest-json"` or `"vitest-json"` and the harness reads the runner's JSON report, counting a failure
   only where it shows a failed assertion and no file that did not load. The `test` command must then invoke Jest or
   Vitest itself (`npx jest`, `npx vitest run`), not `npm test`, which takes the report's flags as its own: no report
   is written and the gate never passes. For any other runner but pytest the pull request says the gate could not
   tell the two apart. Two things no runner tells the gate: a test that fails because the fix adds a name the test
   calls (pytest's `AttributeError`, or calling `undefined` in Jest) fails just as a test of the bug does; and Jest
   takes a test's path as a pattern, so a file whose path is a prefix of another's runs with it. If no test fails there, the harness opens an issue with what
   the agent found. The test's inputs are made up, of the same shape as the records' payloads: the harness refuses a
   test, issue, commit message or pull request that copies 24 characters of an opened payload's strings, numbers or
   keys, or a shorter value with an `@` or six digits, or a number of eight digits or more by its digits alone,
   comparing after folding case and whitespace and undoing the common escapes, with timestamps and UUIDs left out;
   a match counts only if it holds a word that neither the records' cleartext metadata nor your repository holds (a
   route's path, a field name or an error your code raises is no copy). It then asks the agent again, and tells it
   nothing of what matched. Nothing from a payload goes to GitHub. The check catches copies, verbatim or nearly: it
   stops a copy by accident, not an agent set on hiding one (in base64, say, or split in two).
3. **Fix.** The working tree becomes a fresh clone of your default branch's head with the test. If the test already
   passes there, the job ends `already_fixed` and the monitor waits for that commit's release. Otherwise the agent fixes
   the code. It may not change the test, add or delete any test, or change a file that decides how tests run
   (`conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`, `noxfile.py`, `package.json`, a jest, vitest
   or vite config, `jest.setup.*`, `setupTests.*`, `vitest.setup.*`, `vitest.workspace.*`, `tsconfig*`, a babel or swc config,
   `.npmrc`, `.yarnrc*`, `bunfig.toml`, `.mocharc*`,
   a `Makefile`, `sitecustomize.py`, `.gitattributes`, a `.pth` file): such a patch is refused whole. A setup file
   your test config names under another name is not on that list, and code a test imports can still patch the test
   framework itself: the list guards against mistakes, it does not prove a fix honest.
4. **The gate.** The harness ends every process the session left (and refuses if any keeps appearing), takes only the
   files the agent named, and refuses the whole patch if it touches `.github/`, `.clovelly/` or another CI
   configuration, by comparing those files' bytes, not by asking git. Each run is in a fresh clone made just before it:
   each new test file alone on the deployed commit (each passes or fails, at least one fails), each on the head with
   the patch (all pass), then your `test` command in another clone. Each run gets an empty home and temporary
   directory, no Python user site, no npm user configuration and a `PATH` without the session's own directories, so
   nothing the session left in its home runs there. Your `test` command must take a test file's path as its last
   argument (`python -m pytest -q`, `npx vitest run` and `node --test` do); with `test_report`, the report's flags
   follow it. The session and the harness run as one user in one sandbox, so the gate is the harness's report of what
   it ran, not a proof: a determined agent could still tamper with what the harness runs (decided on
   2026-11-11: one sandbox, said plainly).
5. **The pull request.** The harness builds the commit from the patch it holds, pushes branch
   `clovelly/fix-<monitor>-<alert>`, and opens the pull request with the gate's record at the end of its
   description, as the harness reported it. Before anything is sent, the harness refuses a file or text holding the
   job's gateway token or a credential placeholder; that check is verbatim too. The session can still use the
   repository's token through the egress proxy itself, so protect your default branch.

The result is one of four, parsed by the runner with the same rules as Watch (`vectors/alert-outcomes.json`):

```json
{ "kind": "alert", "outcome": "noise", "monitor": "keep" | "mute" | "delete" | { "replace": monitor } }
{ "kind": "alert", "outcome": "already_fixed", "head_sha": "…", "head_position": 42 }
{ "kind": "alert", "outcome": "issue", "number": 17 }
{ "kind": "alert", "outcome": "pr", "number": 18,
  "gate": { "deployed_sha": "…", "head_sha": "…", "tests": [ "tests/test_refunds.py" ], "failed_exit": 1 } }
```

A gate whose tests did not fail on the deployed commit with the test runner's "a test failed" status (`failed_exit`
1, or 6 to 123) has no form, so Watch records a fix pull request only with a gate that passed. A fix branch already
on GitHub when a job is claimed again is reported only if it is the harness's own: one commit by Clovelly Watch on a
commit of your default branch, adding the tests its trailer names and passing the same rules. When the pull request merges, its monitor waits for that commit's release and then arms again. When it
is closed without merging, the monitor is muted until a person unmutes it. When the issue is closed, the monitor fires
again if the failures go on.
