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
  to load `hermes-factory-review-haiku` instead of `hermes-factory-review-code`.
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

- `hermes-factory` orchestrator skill — full pipeline loop using `delegate_task`
  with the `stage` routing parameter.
- `hermes-factory-router` delegation provider plugin — adds `stage` enum to
  `delegate_task`; maps each stage to a configurable provider/model/timeout.
- Eight agent instruction skills (full agent prompts):
  `hermes-factory-intake`, `hermes-factory-spec`, `hermes-factory-build`,
  `hermes-factory-review-code`, `hermes-factory-review-security`,
  `hermes-factory-review-ux`, `hermes-factory-review-ui`,
  `hermes-factory-approve`.
- `factory/dashboard.html` — job board with per-job cost, timing, and file explorer.
- `factory/config.json` — routing tables, VCS backend, cost prices.
- `skins/factory.yaml` — dark forge + molten amber theme.
- `config.yaml` — clean installer template with all 8 stage slots, no credentials.
- `docs/factory-pipeline.md` — full Mermaid flowchart of the pipeline.
- `SOUL.md`, `AGENTS.md`, `.hermes.md`, `SECURITY.md`, `CONTRIBUTING.md`.
