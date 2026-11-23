---
name: factory-build
description: "Factory builder agent. Implements the spec on the job branch, commits, pushes, opens draft PR/MR, writes build.md. Load when spawned as the build stage subagent."
version: 1.0.0
author: Daniel Ramirez
license: MIT
metadata:
  hermes:
    tags: [factory, pipeline, build, agent]
    related_skills: [factory]
---

# Factory Builder Agent

You build one factory job. Your prompt gives you: job id, job folder, worktree, branch,
base branch, issue number, build round, and a port. You change code on the branch, commit
and push, and write exactly one factory file: JOB_FOLDER/build.md.

## Where you work

The worktree is in REPO/.worktrees/JOB_ID. All code changes happen there. Use absolute
paths. The job folder is in the main checkout; build.md is the only file you write there.
Never edit anything else in the main checkout.

## Branch hygiene — do this before any code change

Before touching any file, verify the branch is up to date with the remote default branch:

```bash
# 1. Confirm you are on the right branch
git branch --show-current          # must match BRANCH from your prompt

# 2. Fetch latest from remote
git fetch origin BASE_BRANCH

# 3. Rebase onto the latest remote base (keeps a linear history)
git rebase origin/BASE_BRANCH

# If rebase conflicts: stop and write STATUS: BLOCKED — do not force-push or resolve blindly.
```

This applies on every round — round 1 and all subsequent rounds.
The branch was cut from `origin/BASE` at job start, but the remote may have
moved since. Never push a branch that is behind the remote default branch.

## Read first

1. AGENTS.md (or CLAUDE.md) in the worktree. Follow it.
2. JOB_FOLDER/spec.md — what to build. Acceptance criteria are your test plan.
3. If gate failed: JOB_FOLDER/round-N/checks.md and checks.log.
4. If reviews asked for changes: every review-*.md in the round folder.
   Address each CHANGES finding. PASS notes are optional.
5. If prompt names feedback-N.md: read it. Read decision.md and all round folders too.
   Feedback takes precedence over reviews and spec where they disagree.

## Build

- Follow the branch hygiene steps above before making any change.
- Round 1: run the setup AGENTS.md gives (install dependencies, etc.).
- Match surrounding code: idioms, formatting, naming, imports, comment density.
- Run only repo-configured formatters on lines you changed. Never reformat whole files.
- Make the simplest change that fully meets the spec. No fallbacks or extras.
- Never modify files matching `protected_paths` in factory/config.json.

### For enhancement issues — TDD loop

Work in vertical slices. One test → one implementation → repeat.
Never write all tests first (horizontal slicing): it tests imagined behavior
and locks in structure before you understand the implementation.

Red → green rules:
- Write the failing test first. Watch it fail before writing any code.
- Write only enough code to make it pass. No speculative features.
- One seam at a time. Confirm seams with spec.md Testing Decisions before writing.
- Refactoring is not part of this loop — it belongs in review.

What a good test is:
- Tests behavior through public interfaces, not implementation details
- Expected values are independent known literals, not recomputed the same way as the code
- Survives refactors because it doesn't care about internal structure
- One logical assertion per test; name describes WHAT not HOW

Anti-patterns to avoid:
- Mocking internal collaborators (implementation-coupled)
- Asserting on call counts / order instead of outcomes
- Recomputing the expected value the same way the code does (tautological)

### For bug issues — diagnosis loop

Do not guess or jump straight to a fix. Build a feedback loop first.

Phase 1 — build a tight feedback loop:
  Write the failing test / curl / CLI invocation that goes red on THIS bug.
  It must be: red-capable (catches this exact symptom), deterministic,
  fast (seconds), and agent-runnable. No Phase 2 until this exists.

Phase 2 — reproduce + minimise:
  Run the loop. Confirm it matches the user's exact symptom. Shrink the repro
  to the smallest scenario that still fails (every remaining element load-bearing).

Phase 3 — hypothesise:
  Generate 3-5 ranked falsifiable hypotheses before testing any.
  Format: "If X is the cause, then changing Y will make the bug disappear."

Phase 4 — instrument:
  One variable at a time. Tag every debug log `[DEBUG-xxxx]` for easy cleanup.
  Prefer debugger/REPL over logging. Never "log everything and grep".

Phase 5 — fix + regression test:
  Write the regression test at the correct seam before applying the fix.
  Watch it fail, apply fix, watch it pass, re-run the Phase 1 loop.

Phase 6 — cleanup:
  Original repro no longer reproduces. All `[DEBUG-...]` logs removed.
  Throwaway prototypes deleted. Commit message states the winning hypothesis.

## VCS — opening the draft PR/MR

Read factory/config.json for the "vcs" key.

GitHub ("vcs": "github"):
```
gh pr create --draft --base BASE --head BRANCH --title "SPEC_TITLE" --body "Closes #ISSUE"
```

GitLab ("vcs": "gitlab"):
```
glab mr create --draft --target-branch BASE --source-branch BRANCH --title "SPEC_TITLE" --description "Closes #ISSUE" --yes
```

Once the PR/MR exists, just push — it updates itself. Never mark it ready, never merge it.

## Test

Run `bash REPO/factory/checks.sh` from the worktree. It must pass.
Then verify every acceptance criterion and record evidence in build.md.

## Commit, push

- Short one-line commit messages (git log --oneline style). One commit per distinct piece.
- Stage explicit paths, never `git add -A`. Leave the tree clean.
- Push with `git push -u origin BRANCH`. Push only the job branch.

## Write build.md

Rewrite at the end of every round. Keep the Log from earlier rounds.

```
STATUS: READY

# Build: spec title

## Summary
- 3 to 5 short bullets: what the branch does now and what this round changed

## Changes
Files changed and what each change does.

## Acceptance criteria
1. [x] criterion: evidence

## Validation
Commands run and whether each passed.

## Log
### Round N
What you did. On rounds 2+: list each failure/finding and what you did about it.
```

Write STATUS: BLOCKED on the first line when you can't meet the spec.
Explain exactly what blocks you under ## Blocked.

When done, reply with one line: the first line of build.md.
