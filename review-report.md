# Hermes Software Factory — Code Review

**Reviewer**: Senior Software Engineer  
**Date**: 2026-11-15  
**Scope**: Full codebase at `/Users/C5401338/C0D3/Extra/software-factory-claude-code-v2`  
**Passes**: Three review passes — Pass 1 (original findings), Pass 2 (fix verification + new findings), Pass 3 (deep re-scan after Pass 2 fixes).

---

## Pass 1 Findings

### 1. Correctness — FAIL → WARN

#### 1.1 `key()` collision

**Status: FIXED** — separator changed from `--` to `:`. `key("a/b--c")` and `key("a--b/c")` now return distinct values.  
**Regression test added** in `test_factory_extended.py`.

#### 1.2 `open_pr` state set before push

**Status: FIXED** — VCS calls wrapped in `try/except BaseException`. `state="done"` only written after both succeed. On failure, stage rolls back to `"approve"` and the error re-raises.

#### 1.3 `dispatch` ignores `concurrency` config key

**Status: FIXED** — `"concurrency": 3` removed from `config.json`. Dispatch is correctly documented as sequential.

#### 1.4 `round` counter orphaned

**Status: FIXED** — `round-next` command added. Increments `job["round"]`, resets stage to `"build"`, enforces `round_limit`, and includes a stage guard.

#### 1.5 `gate_cmd` no stage guard

**Status: FIXED** — rejects if `job["stage"]` not in `("build", "gate")`.

---

### 2. Robustness / Error Handling — WARN

#### 2.1 `all_jobs()` crashes on corrupt JSON

**Status: FIXED** — per-file `try/except (json.JSONDecodeError, OSError)` with stderr warning.

#### 2.2 `checks.sh` stdin blocks + no timeout

**Status: FIXED** — `stdin=subprocess.DEVNULL` on all `subprocess.run` calls. `checks_timeout_seconds` (default 300) added to `config.json`. `subprocess.TimeoutExpired` caught and written as `GATE: FAIL`.

#### 2.3 `git_push` failure no recovery

**Status: FIXED** — see 1.2.

---

### 3. Security — PASS (no regressions across all passes)

No shell injection vectors. All subprocess calls use list arguments. Slug validated before use in filenames or git commands. Worker `env -u` isolation correct. No changes introduced new injection surfaces.

---

### 4. Concurrency / Atomicity — WARN (improved)

#### 4.1 `save()` atomic on POSIX

**Status: CORRECT** — `os.replace()` on Linux/macOS is atomic. Unchanged.

#### 4.2 `flock` not thread-safe

**Status: UNCHANGED** — acceptable given sequential dispatch. Note for if threading is ever added.

#### 4.3 `start()` TOCTOU window

**Status: FIXED** — `job_dir.mkdir(exist_ok=False)` atomically claims the issue slot. Concurrent `start()` calls get a `FileExistsError` with a clear message.

---

### 5. Test Quality — WARN (improved)

#### Fixed
- Dead assertion in `test_renamed_away_from_protected_not_caught` now asserts `assertIn("factory/checks.sh", result)`.
- `cancel`, `round-next`, and TOCTOU guard tests present.
- Key collision regression test added.

#### Remaining gaps (see Pass 3 findings)

---

### 6. Distribution / Install — WARN (improved)

#### Fixed
- `vcs.py` deleted.
- `concurrency` removed from `config.json`.
- `FACTORY` path uses `$HERMES_HOME`.
- `hermes_requires` bounded to `'>=0.1.0,<2.0'`.
- SKILL.md job path updated to `OWNER:REPO--N`.

---

### 7. What Was Missing (Pass 1)

| Gap | Status |
|---|---|
| `round-next` command | **ADDED** |
| `cancel` command | **ADDED** |
| `checks_timeout_seconds` | **ADDED** |
| `merge` / `close` command | still absent |
| Worker dispatch timeout | still absent (see Pass 3, finding 3.5) |
| `--json` flag on `list` / dashboard data feed | still absent |

---

## Pass 2 Findings

All five Pass 2 findings are **FIXED** in the current code:

| # | Finding | Status |
|---|---|---|
| 2.1 | `stage_cmd` allowed advancing to `"pr"` directly | **FIXED** — `choices=STAGES[1:-1]` |
| 2.2 | `open_pr` rollback caught only `SystemExit` | **FIXED** — broadened to `BaseException` |
| 2.3 | `monitor()` crashed on corrupt repo files | **FIXED** — per-file try/except added |
| 2.4 | `round_next_cmd` had no stage guard | **FIXED** — guard requires stage in `("review", "approve")` |
| 2.5 | SKILL.md documented stale `OWNER--REPO--N` path | **FIXED** — updated to `OWNER:REPO--N` |

---

## Pass 3 Findings (New Issues)

### 3.1 Test suite has one ERROR: `yaml` module not installed — TEST INFRASTRUCTURE

**`tests/test_factory_extended.py:2150`**

```
ERROR: test_distribution_yaml_has_bounded_version
ModuleNotFoundError: No module named 'yaml'
```

The test that validates `hermes_requires` has a bounded version imports `yaml` (PyYAML), which is not installed in the default Python environment. The test is skipped with `ERROR` rather than `FAIL`, meaning the validation it provides is never actually run.

**Fix**: Either add `pyyaml` to a `requirements-dev.txt` / test dependency list, or rewrite the test using only stdlib (the YAML frontmatter in `distribution.yaml` is simple enough to parse with a regex):

```python
req = re.search(r"hermes_requires:\s*'([^']+)'", content).group(1)
self.assertIn("<", req)
```

---

### 3.2 `cancel` permanently blocks an issue from re-queuing — DESIGN GAP

**`factory/bin/factory.py:386–388`**

```python
built = {j["issue"] for j in all_jobs() if j["repo"] == slug}
waiting = [i for i in issues if i["number"] not in built]
```

`check_queue` excludes any issue number that appears in any job for this repo — regardless of that job's state. A `cancelled` job means its issue number is in `built` forever. The issue still has the `factory` label on GitHub/GitLab, but `check_queue` will silently skip it on every future run. There is no way to re-process a cancelled issue without manually deleting its `job.json`.

`resume` only works on `"stopped"` jobs, not `"cancelled"` ones, so there is no supported recovery path.

**Fix**: Exclude `"cancelled"` (and optionally `"done"`) jobs from the `built` set, or add a `factory requeue OWNER/REPO N` command that deletes the job record and removes the issue from `built`. At minimum, document the limitation prominently: "Cancelling a job permanently skips that issue. Remove the job folder to re-queue it."

---

### 3.3 Plugin: provider-only config returns a partial route — BUG

**`plugins/hermes-factory-router/__init__.py:104–128`**

When a stage is configured with a provider but an empty model (e.g. `{provider: "SomeProvider", model: ""}`), the plugin adds `provider` to the route dict before reaching the fallthrough guard:

```python
if provider:          # True
    route["provider"] = provider   # added
if model:             # False — skipped
    route["model"] = model

if not provider and not model:   # False — not triggered
    return dict(config)          # fallthrough never reached

return route  # {"label": "intake", "provider": "SomeProvider"}  — partial!
```

The function returns `{"label": "...", "provider": "SomeProvider"}` with no `model`. Hermes receives a partial route: a provider with no model to use. The behaviour is undefined and depends on whether the Hermes runtime treats a missing `model` as "use default" or as an error.

The docstring and the test at `test_plugin.py:231` say "fall through", but the code does not fall through — it returns a partial route. The test passes only because it asserts `assertNotIn("model", result)` without asserting `assertNotIn("provider", result)`.

**Fix**: Move the `provider`/`model` assignment inside the fallthrough guard, or check both before adding either:

```python
if provider and model:
    route["provider"] = provider
    route["model"]    = model
elif not provider and not model:
    return dict(config)  # true fallthrough
else:
    # one is set, the other isn't — treat as misconfiguration
    raise DelegationRouteError(
        f"Stage {stage!r}: both 'provider' and 'model' must be set, or neither.")
```

**Fix test**: Add `self.assertNotIn("provider", result)` to `test_provider_only_empty_model_falls_through`.

---

### 3.4 `dispatch` worker has no timeout — ROBUSTNESS

**`factory/bin/factory.py:839–845`**

```python
result = subprocess.run(
    ["env", *unset, "hermes", "chat", "--oneshot", "--quiet",
     "--skills", "hermes-factory", "-q", prompt],
    cwd=repo["path"],
    stdin=subprocess.DEVNULL,
    stdout=output, stderr=subprocess.STDOUT)
```

No `timeout=` is set. A worker that stalls (LLM network timeout, hung subprocess inside the pipeline, infinite retry loop in the skill) blocks the entire dispatch batch indefinitely. The dispatch lock is held for the duration, so no other dispatcher can start on this repo.

`checks.sh` correctly got a timeout (`checks_timeout_seconds`), but the outer worker invocation that runs the entire pipeline has no bound.

**Fix**: Add a `worker_timeout_seconds` key to `config.json` (e.g. default 3600) and pass it to `subprocess.run`. On `TimeoutExpired`, mark the job as `stopped` before raising:

```python
try:
    result = subprocess.run([...], timeout=config.get("worker_timeout_seconds", 3600), ...)
except subprocess.TimeoutExpired:
    # mark the orphaned active job stopped so future dispatches can proceed
    saved = json.loads((folder / "job.json").read_text())
    saved["state"] = "stopped"
    save(folder / "job.json", saved)
    raise SystemExit(f"Worker #{number} timed out. See {folder}/worker-output.log")
```

---

### 3.5 `dispatch` leaves an orphaned active job when `hermes` is not on PATH — ROBUSTNESS

**`factory/bin/factory.py:839`**

`subprocess.run(["env", ..., "hermes", ...])` raises `FileNotFoundError` if `hermes` is not on `PATH`. This propagates uncaught through `dispatch()`, past the flock context manager. The flock is released but the job created by `start()` remains in `"active"` state with no worker running.

On the next dispatch call, `active_job(slug)` returns the orphaned job and dispatch prints `"BUSY: a factory job is already active"` and exits. The operator must run `factory cancel OWNER/REPO --reason "..."` to unblock.

**Fix**: Wrap the worker `subprocess.run` in a `try/except FileNotFoundError` and surface a clear error. Optionally auto-stop the orphaned job:

```python
try:
    result = subprocess.run([...])
except FileNotFoundError:
    # hermes not on PATH — stop the job so it's recoverable
    j = json.loads((folder / "job.json").read_text())
    j["state"] = "stopped"
    save(folder / "job.json", j)
    raise SystemExit("`hermes` not found on PATH. Install it and retry.")
```

---

## Summary Verdict Table

| Dimension | Pass 1 | Pass 2 | Pass 3 |
|---|---|---|---|
| Correctness | **FAIL** | **WARN** | **WARN** — plugin partial route (3.3); cancelled issue silently excluded (3.2) |
| Robustness / Error Handling | **WARN** | **WARN** | **WARN** — dispatch worker no timeout (3.4); hermes-not-found orphans job (3.5) |
| Security | **PASS** | **PASS** | **PASS** |
| Concurrency / Atomicity | **WARN** | **WARN** | **WARN** — unchanged |
| Test Quality | **WARN** | **WARN** | **WARN** — 1 test ERROR (yaml missing, 3.1); partial-route test incomplete (3.3) |
| Distribution / Install | **WARN** | **WARN** | **WARN** — yaml dep undeclared (3.1) |

---

## Top 5 Action Items (Current, Post-Pass-3)

### 1. Fix plugin partial route for provider-only config — CORRECTNESS BUG

**`plugins/hermes-factory-router/__init__.py:104–128`**

When a stage has `provider` set but `model` empty (or vice versa), the plugin returns a partial route dict rather than falling through. Hermes receives a `provider` with no `model`, which is undefined behaviour. The guard `if not provider and not model` only triggers when both are empty.

Fix the logic to check both atomically, and add `assertNotIn("provider", result)` to `test_provider_only_empty_model_falls_through`.

### 2. Fix `cancelled` jobs permanently blocking re-queue — DESIGN CORRECTNESS

**`factory/bin/factory.py:386–388`**

`check_queue` excludes all jobs from `built` regardless of state. A cancelled issue is skipped forever. Either exclude cancelled jobs from `built`, add a `requeue` command, or document the deletion procedure prominently. Without a fix, operators have a silent footgun: `cancel` is irreversible with no warning.

### 3. Add worker timeout to `dispatch` — ROBUSTNESS

**`factory/bin/factory.py:839`**

A hung worker blocks the entire batch and holds the dispatch lock. Add `worker_timeout_seconds` to `config.json` and pass it as `timeout=` to the worker `subprocess.run`. On timeout, mark the job `stopped` before raising so future dispatches are not blocked.

### 4. Handle `hermes` not on PATH in `dispatch` — ROBUSTNESS

**`factory/bin/factory.py:839`**

`FileNotFoundError` from `subprocess.run(["env", ..., "hermes", ...])` propagates uncaught and leaves the job in `"active"` state with no worker. Catch `FileNotFoundError`, stop the orphaned job, and surface a clear install message.

### 5. Fix test suite ERROR: declare `pyyaml` as a test dependency — TEST INFRASTRUCTURE

**`tests/test_factory_extended.py:2150`**

`import yaml` fails with `ModuleNotFoundError`, causing one test to error rather than pass. The distribution version bound validation this test provides is never run. Add `pyyaml` to a `requirements-dev.txt`, or rewrite the test to use stdlib only (e.g. `re.search` on the raw YAML text).
