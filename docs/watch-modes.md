# Getting started with Clovelly Watch

Watch writes monitors for each pull request you merge, and when one fires, an agent in a confidential sandbox decides
whether the alert is real, reproduces it as a failing test, fixes it and opens a pull request for you to review. It
runs in one of two modes. Pick one per organization; you can move later.

| | Hosted | Customer-held |
|---|---|---|
| Who runs the verifier, the key manager and the runner | we do, in a project of ours that holds nothing else | you do, beside the verifier you already run for your sandboxes |
| Where sessions run | an environment of ours for your organization alone (its own verifier, keys and API key), sub-environment `watch` | your environment's sub-environment `watch` |
| Setup | the console only: paste a token, add a webhook and three variables | deploy the runner, seal your GitHub token, add the webhook and the SDK |
| Can we read your code and your telemetry's payloads? | **yes**: we hold the keys, as any vendor does | not in storage. In a session, only by misusing our own control plane: it decides who may create a sandbox in your sub-environment, so we could create one there that reaches what your policy releases to any sandbox, your sealed GitHub token included, and so your code ([the threat model](https://github.com/clovelly-io/clovelly/blob/main/docs/threat-model.md), "Who may create a sandbox"). What the agent sends the model passes through our gateway, unstored |
| Revocation | ours | yours, within one lease |

In both modes the sandbox, the SDK, the monitors and the jobs are the same. The machine time the sessions use is
yours, on its own line: in customer-held mode your sub-environment's cells are billed to you as any cell is; in hosted
mode Watch charges your balance what the runtime billed your environment, at the runtime's prices, day by day.

## What it costs

Watch spends credits from your organization's prepaid balance, a credit being one dollar, only for completed work: 1 credit for a merge that changes your monitors, 5 credits for an alert investigated with an issue opened, 20 credits for a fix pull request, and nothing else costs a credit ([watch-pricing.md](watch-pricing.md)). These prices are provisional: they are charged as listed, and may change before Watch is generally available.

In hosted mode Watch also charges a hosted fee of $1.63 for each UTC day your organization is set up and Watch is on, about $49 a month, charged once the day has ended and never for the day you turn Watch off or the days it is off; customer-held mode has no hosted fee.

It is the balance your sandboxes draw on, bought on the console's Billing tab. At zero, no job starts and new alerts
are held until credit arrives; telemetry is still received and monitors still evaluated. The console's Watch section
shows your balance with this month's sandbox usage and Watch's spend apart, by kind of work.

Credits pay for the work only. The sessions' machine time is charged apart and whatever a job's outcome: in
customer-held mode it is your sub-environment's cells, on your sandbox bill like any others; in hosted mode it is what
the runtime billed the environment we hold for you, passed through at the runtime's own prices once each UTC day has
ended, and shown as **Machine time** on the Spend page. The hosted fee is its own line there too, **Hosted fee**.

## Hosted: the console only

You need a Clovelly organization with credit (the console's Billing tab), and you must be its owner.

1. **Turn Watch on.** In the console, open **Watch**, then **Setup**, and choose **Turn Watch on**. This lets Watch
   read your organization's balance and spend credits from it; you can turn it off on the same page.
2. **Add a repository.** Choose **Hosted**, enter the repository (`owner/name`) and paste a fine-grained GitHub token
   for that repository only, with Contents, Pull requests and Issues read and write, and Metadata read. The token is
   sealed the moment it arrives, to a key only our hosted runner holds, and is never stored in the clear. The page
   shows `CLOVELLY_WATCH_TOKEN` once: copy it now.
3. **Wait for ready.** Within a few minutes the repository shows **ready**, with its webhook address and its
   `CLOVELLY_WATCH_KEY`. Choose **Show once** for the webhook's secret and copy it: an owner sees it once, and Watch
   keeps no copy after.
4. **Add the webhook.** In the repository's settings on GitHub, add a webhook: the payload URL and secret from the
   page, content type `application/json`, events "Pull requests" and "Issues".
5. **Add the SDK** to the service the repository deploys, with three variables:

   ```bash
   pip install clovelly-watch            # or: npm install @clovelly/watch
   export CLOVELLY_WATCH_TOKEN=cwt_…     # from step 2
   export CLOVELLY_WATCH_KEY=cwk1_…      # from step 3
   export CLOVELLY_WATCH_RELEASE=$GIT_SHA  # the commit the service runs, set by your deploy
   ```

   In Python call `clovelly_watch.init()` at start-up; in Node, `import "@clovelly/watch/register"`.
6. **Add `.clovelly/watch.toml`** to the repository, naming its test command (it must take a test file's path as its
   last argument):

   ```toml
   test = "python -m pytest -q"
   setup = "pip install -r requirements.txt"   # optional, run at the start of each session
   # test_report = "jest-json"                  # with test = "npx jest" (or "npx vitest run" and "vitest-json"),
   #                                            # never "npm test": see watch-runner.md's alert job
   ```

Merge a pull request: within minutes the **Monitors** page lists what the agent wrote for it, armed once that commit
is deployed and reporting.

## Customer-held: from nothing to the first monitors

You keep the keys: your verifier unlocks the cells that run the sessions, your runner holds the payload key and the
sandbox access keys, and the GitHub token is sealed through your verifier. You need:

- a Clovelly organization with credit, of which you are an owner;
- a verifier deployed for an environment of yours, with its SDK token, its CA bundle and its policy key
  ([the verifier guide](https://github.com/clovelly-io/clovelly/blob/main/docs/verifier-deploy.md));
- an API key of that environment with the `read_write` role (the console's **Keys** page);
- Docker, or Python 3.12 with `pip`, where the runner will run, reachable from GitHub over HTTPS.

1. **Turn Watch on and name where sessions run.** In the console, **Watch**, then **Setup**: choose **Turn Watch on**,
   then **Customer-held**, pick your environment and press **Create the watch sub-environment**. This makes
   sub-environment `watch` in your environment (2 vCPU, 4 GiB, sessions up to an hour), an ordinary one: its cells
   are billed to you like any others, and its sizes and timeout are yours to change. With no warm cell each session
   boots its own. The page then shows your repository's id and your **runner token**: copy both.
   Add a pool for the sub-environment on the **Environments** page, in the region you want, with no warm cells.
2. **Install the runner and make its keys:**

   ```bash
   pip install "clovelly-watch-runner[sandbox] @ git+https://github.com/clovelly-io/watch#subdirectory=runner"
   clovelly-watch-runner init ./watch-runner --repo REPO_ID --github acme/refunds \
     --watch-url https://ingest.clovelly.io --gateway-url https://model.clovelly.io \
     --verifier-url https://verifier.acme.example --env-id acme --ca-bundle ca.pem
   ```

   `init` prints `CLOVELLY_WATCH_KEY`. Put the runner token in `./watch-runner/runner-token`. Set the sandbox SDK's
   variables as for any program that uses it: `CLOVELLY_API_URL`, `CLOVELLY_API_TOKEN` (the API key),
   `CLOVELLY_API_CA` if your deployment has its own CA, and `CLOVELLY_VERIFIER_TOKEN`.
3. **Seal your GitHub token** (the same fine-grained token as for hosted mode), once, and publish the policy:

   ```bash
   clovelly-watch-runner setup ./watch-runner/config.json < token.txt
   ```

   Add the destinations it writes (`./watch-runner/policy-destinations.json`) to sub-environment `watch`'s policy
   bundle and sign it with your policy key ([the verifier guide](https://github.com/clovelly-io/clovelly/blob/main/docs/verifier-deploy.md),
   "Publishing a policy").
4. **Build the template and check everything:**

   ```bash
   clovelly-watch-runner template build ./watch-runner/config.json ./template   # a Dockerfile FROM our base image
   clovelly-watch-runner check ./watch-runner/config.json
   ```

   `check` names the cause of each failure.
5. **Run it** (`clovelly-watch-runner serve ./watch-runner/config.json`, or `runner/deploy/compose.yaml` beside your
   verifier's) behind HTTPS, and add the webhook: `https://YOUR-HOST/github/REPO_ID`, content type
   `application/json`, the secret in `./watch-runner/REPO_ID/webhook-secret`, events "Pull requests" and "Issues".
6. **Add the SDK and `.clovelly/watch.toml`** as in steps 5 and 6 of hosted mode, with the `CLOVELLY_WATCH_KEY` that
   `init` printed and the `CLOVELLY_WATCH_TOKEN` from the console's setup page.

Merge a pull request and the monitors appear on the console's **Monitors** page. [watch-runner.md](watch-runner.md)
says what the runner holds and what each job does.

## Turning it off

**Setup**, then **Turn Watch off**. Finished work is charged when you turn Watch off, and nothing after. Turning off
settles first: Watch stops taking work for your organization, drops what is queued, refuses the outcome of any
session still running (so it spends nothing), posts every charge still pending, and only then ends its access to your
balance; from then the runtime refuses any charge from Watch. You can always turn it off: a charge that cannot be
posted at that moment (the runtime unreachable, or hosted mode's machine time for the day you turn it off) is written
off by us. If you turn Watch on again, the days it was off are never charged. Your monitors and alerts stay until you delete the repositories.

In customer-held mode, stop the runner: sub-environment `watch` stays yours, empty and costing nothing without cells,
and goes when you offboard the environment (there is no call to delete one sub-environment). In hosted mode, revoke
the GitHub token and we delete your environment.
