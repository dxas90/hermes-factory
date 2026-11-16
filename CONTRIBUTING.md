# Contributing to the Hermes Software Factory

## Prerequisites

```bash
pip install pyyaml   # for validation scripts only
```

## Validation — run before every PR

```bash
# 1. All SKILL.md frontmatter valid
python3 -c "
import yaml, pathlib, sys
errors = []
for p in pathlib.Path('skills').rglob('SKILL.md'):
    try:
        fm = next(yaml.safe_load_all(p.read_text()))
        for f in ['name','description','version','author','license']:
            if not fm.get(f): errors.append(f'{p}: missing {f}')
        if not fm.get('metadata',{}).get('hermes',{}).get('tags'):
            errors.append(f'{p}: missing metadata.hermes.tags')
        if fm.get('tags'):
            errors.append(f'{p}: has top-level tags: (move to metadata.hermes.tags)')
        if fm.get('name') != p.parent.name:
            errors.append(f'{p}: name does not match directory name')
        n = len(p.read_text().splitlines())
        if n > 500: errors.append(f'{p}: {n} lines > 500 limit')
    except Exception as e:
        errors.append(f'{p}: {e}')
for e in errors: print('FAIL:', e)
sys.exit(1 if errors else 0)
"

# 2. Config files parse cleanly
python3 -c "
import yaml, json
yaml.safe_load(open('config.yaml'))
json.load(open('factory/config.json'))
print('OK: config.yaml and factory/config.json parse cleanly')
"

# 3. distribution.yaml has all required fields
python3 -c "
import yaml, sys
d = yaml.safe_load(open('distribution.yaml'))
required = ['name','version','description','hermes_requires','author','license','source']
missing = [k for k in required if not d.get(k)]
if missing: print('FAIL: distribution.yaml missing:', missing); sys.exit(1)
print('OK:', d['name'], 'v' + d['version'])
"

# 4. State machine tests
python3 -m unittest discover -s tests -q
# Should print: Ran N tests ... OK
```

## Contribution rules

**Version discipline**: any change to skills, SOUL.md, config.yaml, skins/, or
plugins/ requires:
1. A version bump in `distribution.yaml` (semver: patch for fixes, minor for
   features, major for breaking changes).
2. A new entry in `CHANGELOG.md` under the new version heading.

**Agent instruction skills** (`factory-*`) contain the full agent prompt.
When changing pipeline behaviour, update the corresponding SKILL.md — the skill
is what subagents load at runtime.

**factory.py** is the only writer of job state. All new bookkeeping (new
commands, new state transitions) goes through `factory.py`. Never write job
state from skill prompts directly.

**Commits**: short one-line messages. Branch naming: `feat/<desc>` or
`fix/<desc>`. PRs against `main`. Squash merge.

**Do not commit**: `memories/`, `sessions/`, `logs/`, `.env`, `state.db*`,
`factory/jobs/`, `.worktrees/`, `__pycache__/`, `*.pyc`.

## Testing a skill change

Load the updated skill in a live session:

```
hermes -p factory chat
> /skill factory
```

Run a one-shot smoke test:

```bash
hermes -p factory chat -q "What pipeline stages are configured? List them."
```

## File structure contract

| Path | Purpose |
|------|---------|
| `factory/bin/factory.py` | State machine + VCS abstraction. Only writer of job state. |
| `plugins/factory-router/` | Delegation provider. Routes `delegate_task(stage=X)`. |
| `skills/software-development/factory/` | Orchestrator. Runs `/factory run`. |
| `skills/software-development/factory-*/` | One agent prompt per pipeline stage. |
| `factory/config.json` | `"vcs"` key switches GitHub ↔ GitLab. |
| `config.yaml` | Clean installer template — no credentials, no runtime state. |
| `tests/` | Tests for `factory.py`. Run with `python3 -m unittest discover -s tests`. |

## Adding a new pipeline stage

1. Create `skills/software-development/factory-<stage>/SKILL.md` with
   the full agent prompt, following the frontmatter rules.
2. Add `<stage>` to `_ALL_STAGES` in `plugins/factory-router/__init__.py`.
3. Add a `<stage>` entry under `delegation.backends.factory-router.stages`
   in `config.yaml` (provider, model, base_url, api_key, timeout).
4. Update `factory` orchestrator skill to dispatch the new stage.
5. Add a `stage_cmd` call to the relevant part of the pipeline in `factory.py`
   if the new stage needs a strict position in the order.
6. Bump `distribution.yaml` version and update `CHANGELOG.md`.

## Testing factory.py changes

Tests live in `tests/`. Load the module via `importlib`:

```python
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "factory", pathlib.Path("factory/bin/factory.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
```

Always use `tempfile.TemporaryDirectory()` and `patch.dict(os.environ, HERMES_HOME=tmp)`
so tests never touch the real `$HERMES_HOME`. Mock VCS calls with
`patch.object(F, "cli_run", return_value="")`.

Run all tests:

```bash
python3 -m unittest discover -s tests -v
```
