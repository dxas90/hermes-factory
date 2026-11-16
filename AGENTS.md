# Factory Profile — Repository Agent Instructions

This repository is an installable Hermes profile distribution for the Software
Factory. Keep it accurate, installable, and free of credentials.
Do not describe planned capabilities as implemented.

## Hard rules

1. Never commit secrets. `.env` is forbidden. `.env.example` is allowed.
2. Keep `distribution.yaml` at the repository root with all required fields.
3. Keep the profile installable:
   `hermes profile install github.com/dxas90/factory`
4. Do not commit gitignored paths: `memories/`, `sessions/`, `logs/`,
   `.env`, caches, or `factory/jobs/`.
5. SKILL.md frontmatter must have: `name`, `description`, `version`, `author`,
   `license`, and `metadata.hermes.tags`. The directory name must match `name`.
   No top-level `tags:` field.

## Repository structure

```
distribution.yaml                    Profile manifest (name, version, hermes_requires)
config.yaml                          Installer template — no credentials, no runtime state
SOUL.md                              Factory persona (system prompt)
AGENTS.md                            This file
.hermes.md                           Per-turn routing rules loaded by Hermes
CHANGELOG.md                         Version history
CONTRIBUTING.md                      Validation scripts and contribution rules
SECURITY.md                          Trust model, protected paths, approval mode

skins/factory.yaml                   Dark forge + amber theme
plugins/
  factory-router/             Delegation provider plugin
    __init__.py                      Routes delegate_task(stage=X) to configured model
    plugin.yaml                      Plugin manifest

skills/software-development/
  factory/                    Orchestrator skill — runs the full pipeline loop
  factory-intake/             Intake agent: ACCEPT / NEEDS-INFO / REJECT
  factory-spec/               Spec writer: requirements + acceptance criteria
  factory-build/              Builder: implements spec, commits, opens PR/MR
  factory-review-code/        Code reviewer: correctness, simplicity, tests
  factory-review-security/    Security reviewer: auth, input, secrets, deps
  factory-review-ux/          UX reviewer: flows, states, copy, accessibility
  factory-review-ui/          UI reviewer: visual consistency, styling, layout
  factory-approve/            Approver: APPROVE or ESCALATE

factory/
  bin/factory.py                     State machine + VCS abstraction (GitHub & GitLab)
  checks.sh                          Repo-specific checks (operators fill this in)
  config.json                        Routing, max_rounds, VCS backend, timeout, prices
  dashboard.html                     Job board (open with python3 -m http.server)

docs/factory-pipeline.md             Full Mermaid flowchart of the pipeline
examples/jobs/                       Example job outputs (4 real runs)
tests/                               Tests for factory.py (run with python3 -m unittest)
```

## factory.py — the state machine

`factory/bin/factory.py` is the only writer of job state. All bookkeeping goes
through it. Do not edit `job.json` directly.

Key commands:

```
factory.py add OWNER/REPO [--vcs github|gitlab]  register a repo
factory.py start OWNER/REPO                       claim the oldest queued issue
factory.py stage OWNER/REPO NAME [note]           advance to the next stage
factory.py route OWNER/REPO risk [--surfaces s]   set risk + reviewer set
factory.py gate OWNER/REPO                        run checks.sh, write checks.md
factory.py open-pr OWNER/REPO --body-file FILE    push branch, open PR/MR
factory.py round-next OWNER/REPO                  next build round after CHANGES
factory.py resume OWNER/REPO N                    re-activate a stopped job
factory.py stop [OWNER/REPO] --reason TEXT        pause active job
factory.py cancel [OWNER/REPO] --reason TEXT      permanently cancel active job
factory.py monitor                                ASCII pipeline dashboard
factory.py precheck OWNER/REPO                    cron pre-check (wakeAgent JSON)
factory.py install-precheck OWNER/REPO [--dispatch]  write cron script
```

## State file layout

State lives in `$HERMES_HOME/factory/`, not inside any repo:

```
$HERMES_HOME/factory/
  repos/owner:repo.json              registered repo record
  jobs/owner:repo--N/                one directory per issue
    job.json                         canonical state record
    intake.md, spec.md, build.md     stage output files
    round-1/checks.md                gate result per round
    round-1/review-code.md           code reviewer verdict
    round-1/review-security.md       security reviewer verdict
    decision.md                      approver decision
    pr-body.md                       PR/MR body
  owner:repo.dispatch.lock           cron dispatch lock
```

Job directory names use `:` as the owner/repo separator:
`owner:repo--ISSUE_NUMBER` (not `owner--repo--N`).

## Skill authoring rules

- `name:` in frontmatter must match the skill directory name exactly.
- Tags live under `metadata.hermes.tags` (list) — not at the top level.
- `related_skills` entries must exist in this repo.
- Body: under 500 lines per skill.
- Agent instruction skills (`factory-*`) contain the full agent prompt.
  When updating pipeline behaviour, update the corresponding SKILL.md.

## Version discipline

Any change to skills, SOUL.md, config.yaml, skins/, or plugins/ requires:

1. A version bump in `distribution.yaml` (semver patch/minor/major).
2. A new entry in `CHANGELOG.md` under the new version heading.

## Adding a new pipeline stage

1. Create `skills/software-development/factory-<stage>/SKILL.md`.
2. Add `<stage>` to `_ALL_STAGES` in `plugins/factory-router/__init__.py`.
3. Add a `<stage>` entry under `delegation.backends.factory-router.stages`
   in `config.yaml`.
4. Update the `factory` orchestrator skill to dispatch the new stage.
5. Bump `distribution.yaml` version and update `CHANGELOG.md`.

## Validation before pushing

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
        if n > 500: errors.append(f'{p}: {n} lines exceeds 500 limit')
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
if missing: print('FAIL: missing:', missing); sys.exit(1)
print('OK:', d['name'], 'v' + d['version'])
"

# 4. Run the test suite
python3 -m unittest discover -s tests -q
```
