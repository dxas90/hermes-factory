---
name: factory
description: >-
  Software Factory for Hermes. Turns GitHub/GitLab issues into merged PRs/MRs
  through a full pipeline: intake, spec, build, gate, review, approve, merge.
  Supports multi-repo, unattended cron dispatch, resume, stop, and live monitor.
  Use when the user types /factory followed by any command.
version: 1.1.0
author: Daniel Ramirez
license: MIT
metadata:
  hermes:
    tags: [factory, software-factory, github, gitlab, pipeline, orchestration, cron]
    related_skills: [factory-intake, factory-spec, factory-build,
                     factory-review-code, factory-review-security,
                     factory-review-ux, factory-review-ui, factory-approve]
---

# Hermes Software Factory

Turns labeled GitHub or GitLab issues into merged PRs/MRs. You are the only
orchestrator. All bookkeeping goes through one script — never improvise state.

## The bookkeeping script

Find factory.py via the active profile's skill scripts, or next to this skill:

    FACTORY="python3 $HERMES_HOME/skills/software-development/factory/scripts/factory.py"

If running inside the factory repo clone:

    FACTORY="python3 /path/to/factory/bin/factory.py"

Every command: `$FACTORY <subcommand> [args]`. Its output is the source of truth.
When it exits with an error, stop and report the error. Do not work around it.

## Commands

    /factory add OWNER/REPO         register a repo (find/clone, check push, create labels)
    /factory run OWNER/REPO         build the oldest waiting issue in this chat
    /factory run <n>                build issue N in the repo registered with this chat
    /factory schedule OWNER/REPO    write cron scripts; set up automatic queue draining
    /factory monitor                ASCII dashboard of all repos and active jobs
    /factory resume OWNER/REPO N    re-activate a stopped job without losing work
    /factory round-next OWNER/REPO  increment round counter, reset stage to build
    /factory stop [OWNER/REPO]      pause the active job (resumable)
    /factory cancel [OWNER/REPO]    permanently cancel a job (terminal, not resumable)
    /factory ANYTHING               operator mode — answer questions, give feedback

## Setup checklist (first use)

1. VCS CLI authenticated:
   - GitHub: `gh auth status`
   - GitLab: `glab auth status`
2. Register the target repo: `/factory add OWNER/REPO`
   - Detects existing clones automatically, or clones into $HERMES_HOME/work/
   - Verifies push access, creates the three factory labels
3. Fill in `factory/checks.sh` in the target repo with its test/lint/build commands
4. Label issues with `factory` on GitHub/GitLab to queue them.
   Only issues with this exact label are picked up. Any issue without it is ignored.
5. Run: `/factory run OWNER/REPO`

## The pipeline

Only issues carrying the **`factory` label** enter the pipeline.
The factory never touches unlabeled issues or issues with other labels.
To queue an issue: apply the `factory` label on GitHub/GitLab.
To dequeue/skip an issue: remove the `factory` label before the next run.

    issues labeled `factory`
        |
        v
    intake    (delegate_task stage=intake)  ACCEPT / NEEDS-INFO / REJECT
        | ACCEPT
        v
    route     ($FACTORY route REPO risk [--surfaces ui,security])
        |
        v
    spec      (delegate_task stage=spec)    writes spec.md
        |
        v
    build     (delegate_task stage=build)   implements, commits, opens draft PR/MR
        |
        v
    gate      ($FACTORY gate REPO)          runs checks.sh -> PASS or FAIL
        | PASS
        v
    review    (delegate_task — parallel)    code + security? + ux? + ui?
        | all PASS
        v
    approve   (delegate_task stage=approve) APPROVE -> open-pr  or  ESCALATE -> needs-human
        |
        v
    open-pr   ($FACTORY open-pr REPO --body-file pr-body.md)

Max rounds (config.json max_rounds). Gate FAIL or review CHANGES -> next round or needs-human.

## /factory add OWNER/REPO

    $FACTORY add OWNER/REPO [--vcs github|gitlab] [--path /path/to/clone]

Registers the repo in $HERMES_HOME/factory/repos/. Reports where the clone is.
After add, the repo is ready for /factory run.

## /factory run OWNER/REPO

Builds **exactly one issue** — the oldest waiting — right here in this chat.
One run = one issue. When it finishes (merged, stopped, or escalated), stop.
Do not loop. Do not start the next issue automatically. The user or cron decides
when to run again.

Read every step output before continuing. Never run dispatch from a chat session.

### Step 1: Claim the issue

    $FACTORY start OWNER/REPO
    # Prints: STARTED / BUSY / NO WORK
    # On STARTED: prints clone path, branch, job folder, issue JSON

- BUSY    -> another job is already active. Say so and stop. Do NOT cancel it.
- NO WORK -> queue is empty (no issues labeled `factory`). Say so and stop.
             Do NOT search for other issues or work around the label requirement.
- STARTED -> proceed with steps 2–8 for this one issue only.

`start` creates the job branch from `origin/<base>` (fetches first). The branch
is always a fresh cut from the latest remote default branch — no stale base.

**Never call `$FACTORY start` more than once per `/factory run` invocation.**

### Step 2: Intake (delegate_task)

Load factory-intake for the full agent prompt.

    result = delegate_task(tasks=[{
        "goal": (
            "Job: JOB_ID. Job folder: FOLDER/. Repo: REPO. Clone: CLONE. Issue: #N.\n"
            "Load and follow the factory-intake skill."
        ),
        "context": "Repo: REPO\nJob folder: FOLDER\nIssue: #N\nClone: CLONE",
        "stage": "intake",
    }])

Read first line of JOB_FOLDER/intake.md:
- DECISION: ACCEPT   -> record risk and surfaces, then:
                        `$FACTORY route REPO low|medium|high [--surfaces ui,security]`
                        `$FACTORY stage REPO spec`
- DECISION: NEEDS-INFO -> `$FACTORY stop OWNER/REPO --reason "Waiting for author"`
- DECISION: REJECT   -> `$FACTORY stop OWNER/REPO --reason "Rejected at intake"`

### Step 3: Spec (delegate_task)

Load factory-spec for the full agent prompt.

    result = delegate_task(tasks=[{
        "goal": "Job: JOB_ID. Job folder: FOLDER/. Clone: CLONE. Branch: BRANCH. Issue: #N.\n"
                "Load and follow the factory-spec skill.",
        "context": "Repo: REPO\nJob folder: FOLDER\nClone: CLONE\nBranch: BRANCH",
        "stage": "spec",
    }])

After: `$FACTORY stage REPO build`

### Step 4: Build (delegate_task)

Load factory-build for the full agent prompt. Port = 5100 + 10 * issue_number.

    result = delegate_task(tasks=[{
        "goal": "Job: JOB_ID. Job folder: FOLDER/. Clone: CLONE. Branch: BRANCH. "
                "Base: BASE. Issue: #N. Round: R. Port: PORT.\n"
                "Load and follow the factory-build skill.",
        "context": "Repo: REPO\nJob folder: FOLDER\nClone: CLONE\nBranch: BRANCH\nRound: R",
        "stage": "build",
    }])

After: `$FACTORY stage REPO gate`

### Step 5: Gate (script)

    $FACTORY gate OWNER/REPO
    # Prints: GATE: PASS or GATE: FAIL

Read JOB_FOLDER/round-N/checks.md first line:
- GATE: PASS  -> `$FACTORY stage REPO review`
- GATE: FAIL + rounds left  -> `$FACTORY round-next REPO`  (increments round, resets to build)
- GATE: FAIL + max rounds   -> `$FACTORY stop REPO --reason "Gate failed at max rounds"`

### Step 6: Review (parallel delegate_task)

Load the relevant factory-review-* skills. Run ALL routed reviewers in ONE call.

    job_json = json.loads(open("JOB_FOLDER/job.json").read())
    reviewers = job_json["agents"]["reviewers"]   # {"code": "haiku", "security": "opus"}

    tasks = []
    for reviewer_name in reviewers:
        skill = f"factory-review-{reviewer_name}"
        tasks.append({
            "goal": f"Job: JOB_ID. Job folder: FOLDER/. Clone: CLONE. Branch: BRANCH. "
                    f"Base: BASE. Issue: #N. Round: R.\nLoad and follow the {skill} skill.",
            "context": f"Repo: REPO\nJob folder: FOLDER\nRound: R\nReviewer: {reviewer_name}",
            "stage": f"review-{reviewer_name}",
        })
    results = delegate_task(tasks=tasks)   # all run in parallel

Refresh job.json, read reviews:
- All PASS   -> `$FACTORY stage REPO approve`
- Any CHANGES + rounds left -> `$FACTORY round-next REPO`  (increments round, resets to build)
- Any CHANGES + max rounds  -> `$FACTORY stop REPO --reason "Review changes at max rounds"`

### Step 7: Approve (delegate_task)

Load factory-approve for the full agent prompt.

    result = delegate_task(tasks=[{
        "goal": "Job: JOB_ID. Job folder: FOLDER/. Clone: CLONE. Branch: BRANCH. "
                "Base: BASE. Issue: #N. PR/MR: #PR. Final round: R.\n"
                "Load and follow the factory-approve skill.",
        "context": "Repo: REPO\nJob folder: FOLDER\nPR: #PR\nRound: R",
        "stage": "approve",
    }])

Read first line of JOB_FOLDER/decision.md:
- APPROVE   -> write pr-body.md to JOB_FOLDER/, then:
               `$FACTORY open-pr REPO --body-file JOB_FOLDER/pr-body.md`
- ESCALATE  -> `$FACTORY stop REPO --reason "Approver escalated to human"`
               Post the decision.md body as a comment on the issue.

### Step 8: Open PR/MR

    $FACTORY open-pr OWNER/REPO --body-file JOB_FOLDER/pr-body.md [--draft]

Pushes the branch, opens the PR/MR, marks job done. Reports the PR/MR URL.

**This is the end of the run. Stop here. Do not start another issue.**
Print the PR/MR URL, print `$FACTORY monitor` output, then wait for the user.

## /factory schedule OWNER/REPO

Sets up a Hermes cron job to drain the queue automatically.
NOTE: `/factory schedule` is NOT a factory.py command. It is an orchestrator
workflow that calls `install-precheck` and creates a Hermes cron job.

1. Resolve the actual OWNER/REPO (never use a placeholder).
2. Run: `$FACTORY install-precheck OWNER/REPO --dispatch`
   It prints the script filename. Use that exact filename.
3. Create the Hermes cron job:
   - name:     `factory OWNER/REPO`
   - schedule: `every 10m` (or user's preference)
   - skills:   `["factory"]`
   - prompt:   `/factory run OWNER/REPO`
   - script:   the filename printed by install-precheck
   - no_agent: true
   - deliver:  local
4. Read back the saved job and report: ID, schedule, next run with timezone.
5. Say: "The factory will now drain the queue automatically. Add issues labeled
   `factory` to the repo. Use `/factory monitor` to check progress."

## /factory round-next OWNER/REPO

When the gate FAILS (job left at `gate`) or review produces CHANGES
(job at `review`/`approve`), the orchestrator must start another build round:

    $FACTORY round-next OWNER/REPO

This increments `job["round"]`, resets stage to `build`, and writes a log event.
Only valid from `gate`, `review`, or `approve` — never before the gate has run.
If `round >= round_limit`, raises an error — stop the job instead.

## /factory cancel [OWNER/REPO]

    $FACTORY cancel [OWNER/REPO] --reason "REASON"

Permanently cancels the job. Unlike `stop`, cancelled jobs cannot be resumed.
The branch is preserved. Use when the issue itself is invalid or superseded.

## /factory monitor

    $FACTORY monitor

Prints an ASCII dashboard: active jobs with pipeline diagrams, queue per repo,
last 5 finished jobs. Reply with the output unchanged inside a text block,
then add one or two sentences about what needs attention.

## /factory resume OWNER/REPO N

    $FACTORY resume OWNER/REPO N

Re-activates a stopped job at its saved stage, preserving all work.
Continue the pipeline from that stage.

## /factory stop [OWNER/REPO]

    $FACTORY stop [OWNER/REPO] --reason "REASON"

Stops the active job. Branch is preserved. The issue stays labeled `factory`
and can be re-queued or resumed later.

## Operator mode (/factory ANYTHING)

When the user types anything other than a known command:
- Show monitor output: `$FACTORY monitor`
- Show a specific job: `cat $HERMES_HOME/factory/jobs/OWNER:REPO--N/job.json`
- Give feedback on an escalated job: re-run from the stopped stage
  (read job.json for saved stage, load the right agent skill, continue)
- Reject a job: `$FACTORY stop OWNER/REPO --reason "REASON"`

## VCS backends

factory/bin/factory.py handles both GitHub and GitLab.
VCS is set per-repo at `add` time or read from the registered repo record.

    GitHub  -> gh CLI  | set by: $FACTORY add REPO --vcs github
    GitLab  -> glab CLI | set by: $FACTORY add REPO --vcs gitlab

## Progress reporting format

One line per transition:
    owner/repo #12: gate PASS -> reviewing (code, security)
    owner/repo #12: all PASS -> approving
    owner/repo #12: APPROVE -> merged PR/MR #47

After every run: print full `$FACTORY monitor` output, note any stopped or needs-human jobs.
Then stop. One `/factory run` = one issue. Never loop to the next issue automatically.

## Pitfalls

- **One issue per run.** `/factory run` processes exactly one issue then stops.
  Never call `$FACTORY start` a second time in the same session.
  The queue drains via cron (`/factory schedule`) not by looping in chat.
- Never run `$FACTORY dispatch` from a chat session. dispatch is for cron only.
- Never call `$FACTORY start` directly except when following the run loop above.
- Subagents have zero memory of the orchestrator session.
  Always include the full absolute job_folder path in every delegate_task context.
- config.yaml must have max_spawn_depth: 2 and subagent_auto_approve: true.
- Gate runs checks.sh from the MAIN checkout, not the worktree.
  The branch cannot change the rules it is judged by.
- $FACTORY errors are authoritative. Never work around them.
