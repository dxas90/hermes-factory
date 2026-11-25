## Unreleased (2026-11-16)

### Fixed

- `start` no longer leaves a claim behind when it fails. It claimed the issue
  (created the job folder) before fetching the issue and creating the branch;
  if either failed, the folder stayed without `job.json`. `monitor` ignored it
  and showed the repo idle, while every later `start` refused with "claimed by
  a concurrent process". Now `start` releases the claim on failure, reclaims a
  claim folder without `job.json` older than two minutes, and refuses a
  leftover `factory/issue-N` branch before claiming, saying how to fix it.
- Gitea/Forgejo (`vcs: gitea`) now works with gitea-cli (`gt`) 2.x. The
  backend assumed `gt` prints GitHub-style JSON; it prints Gitea's field names:
  - `repo view` asked for `nameWithOwner,defaultBranchRef,isArchived`, which
    `gt` rejects ("unknown JSON field"), so `add` failed. It now asks for
    `full_name,default_branch,archived` and maps them.
  - `label create` passed `--name NAME` and `--color #RRGGBB`; `gt` takes the
    name as an argument and the colour without `#`. The error was swallowed,
    so `add` reported success without creating labels, and every later
    `issue list --label factory` failed because the label did not exist.
    Labels now get their description too, and real failures are raised.
  - `issue view` normalises `author` (object or string) to `{"login": ...}`
    and `null` values to empty strings.
  - `pr create` reads the URL with `--json url --jq .url` instead of the last
    line of human-readable output.
  The Gitea tests mocked `gt` with GitHub-shaped JSON and now use gitea-cli's
  documented fields.
- `gate` now advances the job from `build` to `gate` after every verdict
  (PASS, FAIL, protected-path, timeout), so the documented flow works:
  PASS → `stage review`, FAIL → `round-next` starts the next build round.
  Previously the job was stranded at `build` and both follow-ups raised.
- `round-next` is now valid from `gate` (gate FAIL) in addition to
  `review` and `approve` (CHANGES).
- `protected_paths_check` reads the protected-paths list from the BASE
  version of `factory/config.json` instead of the job branch's working
  tree — a job branch can no longer disable protection by rewriting the
  config file that defines it.
- `precheck` now prints only JSON on stdout; the human-readable status
  line goes to stderr, matching what the cron harness parses.
- `route` accepts `surfaces` as a list/tuple as well as a comma-separated
  string.

### Changed

- `factory/checks.sh` now runs the real test suite
  (`uv run --no-project --with pytest,pyyaml python -m pytest tests/ -q`)
  instead of the `exit 1` stub.
- `distribution.yaml` bounds the Hermes requirement to `>=0.1.0,<2`.
- Re-synced `skills/.../factory/scripts/factory.py` with
  `factory/bin/factory.py` (the two had drifted when the gate fixes went
  into `bin` only — the orchestrator runs the skill copy).
- New test `test_skill_script_matches_bin` fails the suite if the skill
  script and `factory/bin/factory.py` ever drift again.

## 1.3.0 — 2026-11-15

### Added

- **Gitea / Forgejo VCS backend** (`"vcs": "gitea"`): full VCS abstraction via
  the `gt` CLI (Gitea CLI, v1.2+). Forgejo instances are compatible — gt works
  against Forgejo too. Covers all factory operations:
  - `repo_view`: `gt repo view --json`
  - `issue_list_labeled`: `gt issue list --json number,title`
  - `issue_view_full`: `gt issue view --json number,title,body,url,author`
  - `issue_comment`: `gt issue comment --body`
  - `label_create`: `gt label create --color #RRGGBB --force`
  - `pr_create`: `gt pr create --yes [--draft]`
  - `origin_slug`: host-aware regex matching any Gitea/Forgejo instance URL
- `_gt_host()` helper: resolves the Gitea instance URL from `GITEA_CLI_HOST`
  env, `GITEA_INSTANCE_URL` env, or `gitea_host` in `factory/config.json`.
- `gitea_host` key in `factory/config.json` (empty default).
- `--vcs gitea` choice added to `factory.py add` argparser.
- 41 tests for the Gitea backend in `tests/test_factory_gitea.py`.
- `gitea` skill installed: `skills/software-development/gitea/` with 8
  reference files covering auth, PRs, issues, reviews, repo management, API
  cheatsheet, CI troubleshooting, and bulk merge. Covers both `gt` porcelain
  and raw `curl /api/v1/` fallbacks. Works for Gitea and Forgejo instances.

### Changed

- `_cli()`, `_vcs()`, `origin_slug()`, `repo_view()`, `issue_list_labeled()`,
  `issue_view_full()`, `issue_comment()`, `label_create()`, `pr_create()`,
  `add()`, `check_queue()` — all accept `vcs="gitea"` and route to `gt`.
- `origin_slug()` gains a `config` parameter. For Gitea, host verification
  prevents false-positive matches against github.com/gitlab.com remotes when
  a Gitea host is configured.
- `check_queue()` now loads and passes `config` to `issue_list_labeled()` so
  the `gitea_host` key reaches the VCS call.

---

# Changelog

All notable changes to the Hermes Software Factory profile distribution.

Format: `## <version> — <date>`
Entries: Added / Changed / Fixed / Removed

---

## 1.2.0 — 2026-11-15

### Added

- `factory.py round-next OWNER/REPO` — increment round counter and reset
  stage to `build` after review CHANGES. Only valid from `review` or `approve`
  stage (enforced by guard). Raises if already at `round_limit`.
- `factory.py cancel [OWNER/REPO] --reason TEXT` — permanently cancel a job
  in a terminal `cancelled` state. Unlike `stop`, cancelled jobs cannot be
  resumed. Branch is preserved.
- `checks_timeout_seconds` in `factory/config.json` (default 300) — hard
  wall-clock limit for `checks.sh`. On timeout: writes GATE: FAIL to
  `round-N/checks.md` and returns cleanly.

### Changed

- `key()` separator changed from `--` to `:`. Job and repo state directories
  now use `owner:repo` format (e.g. `owner:repo--7`). Colon is illegal in
  GitHub/GitLab owner and repo names, making the separator collision-free.
  Old `--` separator allowed `key("a/b--c") == key("a--b/c")` which could
  silently overwrite state files for two different repos.
- `FACTORY` path in SKILL.md now uses `$HERMES_HOME` instead of
  `git rev-parse --show-toplevel`. Works outside git repos.
- `config.yaml` installer template: `approvals.mode` now documented more
  clearly with the distinction between `manual` (default) and `off`.
- `hermes_requires` in `distribution.yaml` bounded to `>=0.1.0,<2.0`.

### Fixed

- `open_pr`: state is now written as `done` only after both `git_push()` and
  `pr_create()` succeed. On any `BaseException` (SystemExit, KeyboardInterrupt,
  OSError), stage rolls back to `approve` and the job stays active for retry.
- `stage_cmd` argparser choices changed from `STAGES[1:]` to `STAGES[1:-1]`,
  excluding `pr`. The `pr` stage is only reachable via `open-pr`, which
  performs the actual push and PR/MR creation.
- `round_next_cmd`: stage guard added — refuses from `ingest`, `spec`,
  `build`, or `gate`. Only valid from `review` or `approve`.
- `gate_cmd`: stage guard added — refuses unless `job["stage"]` is `build`
  or `gate`. Previously ran `checks.sh` from any stage.
- `gate_cmd`: `stdin=subprocess.DEVNULL` added to `checks.sh` subprocess.
  Previously could block indefinitely on interactive prompts.
- `all_jobs()`: corrupt `job.json` files now print a WARNING to stderr and
  are skipped. Previously crashed the entire function.
- `monitor()`: corrupt `repos/*.json` files now print a WARNING and are
  skipped. Same fix applied as `all_jobs()`.
- `start()`: TOCTOU race fixed. Job directory is created with
  `mkdir(exist_ok=False)` before the git operation to atomically claim the
  issue number. A concurrent `start()` call now fails with a clear message.

### Removed

- `factory/bin/vcs.py` (296 lines) — dead code, never imported by `factory.py`.
  All VCS abstraction is built into `factory.py`. Removes a three-way split
  where bug fixes applied to one layer did not propagate to others.
- `concurrency` key from `factory/config.json` — dispatch is strictly
  sequential (one issue at a time). The key was present but never read,
  creating a false impression of parallel dispatch.

---

## 1.1.0 — 2026-11-14

### Added

- `factory/bin/factory.py` (1000+ lines) — unified state machine merging
  `factory/bin/job`, `factory/bin/gate`, and `factory/bin/route` into one
  Python script with a clean CLI. Adds multi-repo support, dispatch/cron,
  precheck, monitor, resume, and stop commands.
- `factory.py add OWNER/REPO` — discover or clone a repo, verify push access,
  register in `$HERMES_HOME/factory/repos/`.
- `factory.py dispatch OWNER/REPO` — batch-process the queue; spawns a fresh
  `hermes chat --oneshot` per issue with all `HERMES_SESSION_*` env vars unset
  to prevent session leakage.
- `factory.py monitor` — ASCII pipeline dashboard with per-stage timing,
  active job diagram, queue per repo, and last 5 finished jobs.
- `factory.py precheck OWNER/REPO` — cron pre-check; prints
  `{"wakeAgent": bool}`. Wakes the agent only when work is actually waiting.
- `factory.py install-precheck OWNER/REPO [--dispatch]` — writes a cron
  script to `$HERMES_HOME/scripts/`.
- `factory.py resume OWNER/REPO N` — re-activate a stopped job at its saved
  stage with all work preserved.
- `factory.py stop [OWNER/REPO] --reason TEXT` — pause the active job.
  Branch and all work are preserved.
- `routing_for()` fixed: was inverting the dict (`{model: reviewer_type}`
  instead of `{reviewer_type: model}`). Would have caused the orchestrator
  to load `factory-review-haiku` instead of `factory-review-code`.
- VCS abstraction built into `factory.py` — GitHub (`gh`) and GitLab (`glab`)
  supported. Switch with `"vcs"` in `factory/config.json`.
- 358 tests for `factory.py` across 6 test files.
- Example `job.json` files updated to match the `factory.py` schema.

### Changed

- State layout moved to `$HERMES_HOME/factory/` — no longer inside the target
  repo. Enables multi-repo support.

---

## 1.0.0 — 2026-11-14

### Added

- `factory` orchestrator skill — full pipeline loop using `delegate_task`
  with the `stage` routing parameter.
- `factory-router` delegation provider plugin — adds `stage` enum to
  `delegate_task`; maps each stage to a configurable provider/model/timeout.
- Eight agent instruction skills (full agent prompts):
  `factory-intake`, `factory-spec`, `factory-build`,
  `factory-review-code`, `factory-review-security`,
  `factory-review-ux`, `factory-review-ui`,
  `factory-approve`.
- `factory/dashboard.html` — job board with per-job cost, timing, and file explorer.
- `factory/config.json` — routing tables, VCS backend, cost prices.
- `skins/factory.yaml` — dark forge + molten amber theme.
- `config.yaml` — clean installer template with all 8 stage slots, no credentials.
- `docs/factory-pipeline.md` — full Mermaid flowchart of the pipeline.
- `SOUL.md`, `AGENTS.md`, `.hermes.md`, `SECURITY.md`, `CONTRIBUTING.md`.
