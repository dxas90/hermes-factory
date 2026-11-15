# Hermes Software Factory

A Hermes profile that turns labeled GitHub or GitLab issues into merged pull
requests through a fully automated pipeline. Label an issue `factory`, run
`/factory run`, and the factory handles the rest: reading the issue, writing a
spec, implementing on a branch, running your test suite, reviewing from multiple
angles, and opening a PR/MR for your approval — or auto-merging when it's safe.

## Install

```bash
hermes profile install github.com/dxas90/hermes-factory --alias
```

Start it:

```bash
factory chat            # with --alias
hermes -p factory chat  # without alias
```

## Requirements

- [Hermes Agent](https://hermes-agent.nousresearch.com) ≥ 0.1.0
- An LLM provider configured: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, etc.
- `python3` 3.10+ (the state machine uses stdlib only — no pip install needed)
- `gh` CLI authenticated (GitHub) **or** `glab` CLI authenticated (GitLab)

---

## How it works

The factory runs a fixed eight-stage pipeline. Each stage is a `delegate_task`
subagent routed to a configurable model via the `hermes-factory-router` plugin.
State lives in `$HERMES_HOME/factory/` — entirely separate from your repo.

```
labeled issue
    │
    ▼
intake    decides ACCEPT / NEEDS-INFO / REJECT, rates risk and surfaces
    │ ACCEPT
    ▼
spec      writes spec.md — numbered requirements + acceptance criteria
    │
    ▼
build     implements the spec on a branch, commits, opens a draft PR/MR
    │
    ▼
gate      runs factory/checks.sh  (your tests, lint, build)
    │ FAIL ──► back to build  (up to max_rounds)
    │ PASS
    ▼
review    parallel: code reviewer always; security/ux/ui by risk+surface
    │ CHANGES ──► back to build  (up to max_rounds)
    │ all PASS
    ▼
approve   reads every job file + the diff, decides:
          APPROVE  → open-pr → PR/MR created, factory waits for merge
          ESCALATE → marks PR/MR ready, labels factory:needs-human
```

---

## Quick start (5 minutes)

### 1. Install and configure

```bash
hermes profile install github.com/dxas90/hermes-factory --alias

# Set your provider and model — edit the installed config:
# ~/.hermes/profiles/factory/config.yaml
#   model.default: claude-opus-4-5
#   model.provider: anthropic

factory chat -q "What model am I using?"
```

### 2. Register your target repo

```bash
factory chat
> /factory add owner/your-repo
```

`/factory add` discovers existing clones or clones the repo automatically,
verifies push access, and creates the three factory labels on your VCS host.

For GitLab: set `"vcs": "gitlab"` in `factory/config.json` first.

### 3. Write factory/checks.sh

Copy `factory/checks.sh` from this distribution into your repo:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

python3 -m unittest discover -s tests -v    # Python — adjust to your stack
# npm run lint && npm test                  # Node
# go test ./...                             # Go
# cargo test                               # Rust

echo "All checks passed."
```

The gate runs this script with `stdin` closed and a wall-clock timeout
(default 300s, set `checks_timeout_seconds` in `factory/config.json`).
It must exit 0, must not prompt for input, must not modify files.

### 4. Label an issue and run

```bash
# GitHub:
gh issue edit 7 --add-label factory --repo owner/your-repo

# GitLab:
glab issue update 7 --label factory --repo owner/your-repo

# Then:
factory chat
> /factory run
```

---

## Commands reference

Type these in the factory chat session:

| Command | What it does |
|---------|-------------|
| `/factory add OWNER/REPO` | Register a repo: find/clone it, verify push, create labels |
| `/factory run` | Build the oldest waiting issue |
| `/factory run OWNER/REPO` | Build the oldest issue in a specific repo |
| `/factory schedule OWNER/REPO` | Install a cron job to drain the queue automatically |
| `/factory monitor` | ASCII pipeline dashboard of all repos and jobs |
| `/factory resume OWNER/REPO N` | Re-activate a stopped job without losing work |
| `/factory round-next OWNER/REPO` | Start another build round after review CHANGES |
| `/factory stop [OWNER/REPO]` | Pause the active job (resumable later) |
| `/factory cancel [OWNER/REPO]` | Permanently cancel a job (not resumable) |
| `/factory <anything>` | Operator mode: status, feedback, inspect state |

---

## Configuration

### Models per stage

Edit `~/.hermes/profiles/factory/config.yaml`. Each stage follows the same
pattern as Hermes's `auxiliary:` block. Leave `provider` and `model` empty
to inherit `delegation.provider` / `delegation.model`.

```yaml
delegation:
  provider: anthropic          # fallback for stages with no explicit provider
  model: claude-haiku-4-5      # fallback model
  backend: factory-router
  backends:
    factory-router:
      stages:
        intake:
          provider: anthropic
          model: claude-haiku-4-5    # fast: decides accept/reject
          timeout: 120
        spec:
          provider: anthropic
          model: claude-sonnet-4-5   # capable: writes requirements
          timeout: 300
        build:
          provider: anthropic
          model: claude-opus-4-5     # best available: writes the code
          timeout: 900
        review-code:
          provider: anthropic
          model: claude-haiku-4-5    # fast: code review
          timeout: 120
        review-security:
          provider: anthropic
          model: claude-opus-4-5     # best: security is high-stakes
          timeout: 180
        review-ux:
          provider: anthropic
          model: claude-sonnet-4-5   # mid-tier with vision
          timeout: 120
        review-ui:
          provider: anthropic
          model: claude-haiku-4-5    # fast: visual pattern matching
          timeout: 90
        approve:
          provider: anthropic
          model: claude-opus-4-5     # best: APPROVE triggers PR/MR open
          timeout: 180
```

You can use any Hermes-supported provider: Anthropic, OpenRouter, Gemini,
local models via Ollama, custom proxies — one provider per stage is fine.

### VCS backend

In `factory/config.json` inside your repo:

```json
{ "vcs": "github" }    // uses gh CLI  (default)
{ "vcs": "gitlab" }    // uses glab CLI
```

### Rounds and timeout

```json
{
  "max_rounds": 3,
  "checks_timeout_seconds": 300
}
```

`max_rounds`: how many build→gate→review cycles before escalating to a human.
`checks_timeout_seconds`: hard wall-clock limit for `checks.sh` (kills and
marks GATE: FAIL on timeout).

### Reviewer routing

Code review runs on every issue. Security, UX, and UI reviewers are added by
the risk level and surfaces detected at intake:

```json
{
  "routing": {
    "low":    { "code": "haiku" },
    "medium": { "code": "sonnet" },
    "high":   { "code": "sonnet" }
  },
  "surface_reviewers": {
    "ui":       { "ux": "sonnet", "ui": "haiku" },
    "security": { "security": "opus" }
  }
}
```

### Protected paths

```json
{ "protected_paths": ["factory/checks.sh", "factory/config.json"] }
```

The gate fails any branch that modifies these. Add fnmatch patterns for any
other files that jobs must not be allowed to change.

---

## State layout

All factory state lives in `$HERMES_HOME/factory/` — never inside your repo:

```
$HERMES_HOME/factory/
  repos/
    owner:repo.json              registered repo record
  jobs/
    owner:repo--7/               one directory per issue
      job.json                   canonical state (stage, state, round, agents)
      intake.md                  DECISION: ACCEPT / NEEDS-INFO / REJECT
      spec.md                    numbered requirements + acceptance criteria
      build.md                   STATUS: READY / BLOCKED + evidence
      round-1/checks.md          GATE: PASS / FAIL + output tail
      round-1/review-code.md     VERDICT: PASS / CHANGES + findings
      round-1/review-security.md VERDICT: PASS / CHANGES + findings
      decision.md                APPROVE / ESCALATE + reasoning
      pr-body.md                 PR/MR body before submission
  owner:repo.dispatch.lock       prevents overlapping cron dispatchers
  scripts/
    factory-dispatch-owner:repo.py  cron dispatch script (created by schedule)
```

Job directory name: `OWNER:REPO--ISSUE_NUMBER`

To inspect any job directly:

```bash
# List all jobs:
ls "$HERMES_HOME/factory/jobs/"

# Read a specific job's state:
cat "$HERMES_HOME/factory/jobs/owner:repo--7/job.json"

# See the gate result for round 1:
cat "$HERMES_HOME/factory/jobs/owner:repo--7/round-1/checks.md"
```

---

## Unattended / cron dispatch

```
factory chat
> /factory schedule owner/your-repo
```

Installs a Hermes cron job that checks for labeled issues every 10 minutes
(configurable) and drains them automatically. The precheck script costs nothing
when the queue is empty — it only wakes the agent when work is waiting.

---

## Multi-round review

When reviewers return CHANGES:

```
> /factory round-next owner/your-repo
```

Increments the round counter, resets stage to `build`. The builder reads the
previous round's findings and addresses each one. Only valid from `review` or
`approve` stage. Raises if already at `max_rounds`.

---

## Repo structure

```
distribution.yaml                    Hermes profile manifest
config.yaml                          Installer template (no credentials)
SOUL.md                              Factory persona
AGENTS.md                            Repository agent instructions
.hermes.md                           Per-turn routing rules
CHANGELOG.md                         Version history
CONTRIBUTING.md                      How to extend the factory
SECURITY.md                          Trust model and safety boundaries
skins/factory.yaml                   Dark forge + amber theme

plugins/
  hermes-factory-router/             Delegation provider: per-stage model routing

skills/software-development/
  hermes-factory/                    Orchestrator: runs the full pipeline loop
  hermes-factory-intake/             Intake agent
  hermes-factory-spec/               Spec writer
  hermes-factory-build/              Builder
  hermes-factory-review-code/        Code reviewer
  hermes-factory-review-security/    Security reviewer
  hermes-factory-review-ux/          UX reviewer
  hermes-factory-review-ui/          UI reviewer
  hermes-factory-approve/            Approver

factory/
  bin/factory.py                     State machine + VCS abstraction (1100 lines)
  checks.sh                          Your repo's checks (fill this in)
  config.json                        Routing, rounds, VCS, timeout, prices
  dashboard.html                     Job board

docs/factory-pipeline.md             Full Mermaid flowchart
examples/jobs/                       Example job outputs (4 real runs)
tests/                               358 passing tests for factory.py
```

---

## Dashboard

```bash
cd /path/to/your/repo
python3 -m http.server --directory factory 8000
# open http://localhost:8000/dashboard.html
```

Shows all jobs with stage chips, gate results, reviewer verdicts, issue/PR
links, and timing. Populate with example data: `cp -r examples/jobs factory/jobs`

---

## Safety

- **Trust boundary**: only issues labeled `factory` are processed. The label
  requires triage or write access on GitHub, or Reporter+ on GitLab.
- **Issue text is data**: every agent is explicitly told the issue body is
  quoted data and cannot redirect tools, files, or decisions.
- **Branch isolation**: the factory pushes only `factory/*` branches and opens
  PRs/MRs via the state machine. It never deploys or modifies production data.
- **Protected paths**: `factory/checks.sh` and `factory/config.json` are
  protected — the gate fails any branch that modifies them.
- **Atomic state**: all job state writes use `os.replace()` (atomic on POSIX).
  A crash mid-write never produces corrupt JSON.
- **Approvals**: `approvals.mode: manual` in `config.yaml` (default) requires
  confirmation before every shell command. Set to `off` only after you've
  verified the factory behaves correctly on your repo.

---

## License

MIT
