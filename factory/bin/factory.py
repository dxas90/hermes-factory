#!/usr/bin/env python3
"""Hermes Software Factory — unified state machine and bookkeeping.

Hermes (or its subagents) does the judgment work: reading issues, writing specs,
editing code, running checks, reviewing, approving. This script does the parts
that must not drift: where each repository lives, which issue is next, which
stage the pipeline is in, VCS operations, multi-repo dispatch, cron pre-checks,
and the monitor's picture of everything.

State lives in $HERMES_HOME/factory/ — separate from any repository:

  repos/<owner>:<repo>.json          clone location, VCS backend, queue
  jobs/<owner>:<repo>--<N>/job.json  one record per issue, plus stage files
  <owner>:<repo>.dispatch.lock       prevents overlapping dispatchers

VCS backends: "github" (gh CLI, default), "gitlab" (glab CLI), or
"gitea" / "forgejo" (gt CLI, self-hosted).
Set "vcs" in factory/config.json inside the target repository.
For Gitea/Forgejo also set "gitea_host" (e.g. "https://gitea.example.com")
or export GITEA_CLI_HOST / GITEA_INSTANCE_URL.

Commands
--------
  add OWNER/REPO          find or clone a repo, verify push access, register it
  run OWNER/REPO          build the oldest waiting issue in this chat
  dispatch OWNER/REPO     batch-process all waiting issues (one worker per issue)
  start OWNER/REPO        claim the oldest issue and open its job (used by dispatch)
  stage OWNER/REPO NAME   advance the active job to the next pipeline stage
  note OWNER/REPO TEXT    record a one-line progress note
  open-pr OWNER/REPO      push the branch and open the PR/MR
  resume OWNER/REPO N     re-activate a stopped job without losing its work
  stop [OWNER/REPO]       stop the active job without opening a PR/MR
  monitor                 ASCII dashboard of all registered repos and active jobs
  precheck OWNER/REPO     cron pre-check: print {wakeAgent: bool}
  install-precheck OWNER/REPO  write a cron script into $HERMES_HOME/scripts/
"""

import argparse
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ── constants ────────────────────────────────────────────────────────────────

LABEL          = "factory"
LABEL_INFO     = "factory:needs-info"
LABEL_HUMAN    = "factory:needs-human"
STAGES         = ["ingest", "spec", "build", "gate", "review", "approve", "pr"]
ACTIVE_STAGES  = {"ingest", "spec", "build", "gate", "review", "approve"}
WRITE_ACCESS   = {"ADMIN", "MAINTAIN", "WRITE"}
SEARCH_DEPTH   = 3
WIDTH          = 72

# Session env vars that must not leak from the dispatcher into worker processes.
PARENT_SESSION_VARS = (
    "HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_SESSION_TOKEN__",
    "HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_SOURCE_EXPLICIT", "HERMES_SESSION_PROFILE",
    "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_CHAT_TYPE", "HERMES_SESSION_PARENT_CHAT_ID",
    "HERMES_SESSION_THREAD_ID", "HERMES_SESSION_MESSAGE_ID",
    "HERMES_SESSION_SCOPE_ID", "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_USER_ID_ALT", "HERMES_SESSION_USER_NAME",
    "HERMES_SESSION_ASYNC_DELIVERY", "HERMES_SESSION_HISTORY_DELIVERY",
    "HERMES_SESSION_STALL_TIMEOUT", "HERMES_CRON_SESSION",
    "HERMES_UI_SESSION_ID",
)


# ── core helpers ─────────────────────────────────────────────────────────────

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hermes_home():
    home = os.environ.get("HERMES_HOME")
    if not home:
        raise SystemExit("HERMES_HOME is not set. Run this from inside Hermes.")
    return Path(home)


def state_dir():
    return hermes_home() / "factory"


def check_slug(slug):
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", slug):
        raise SystemExit(f"Name the repository as OWNER/REPO, not {slug!r}.")
    return slug


def key(slug):
    # Use ":" as separator — illegal in GitHub/GitLab owner and repo names,
    # so "owner:repo" is always collision-free (unlike "--" which "--" could
    # appear in either segment, causing key("a/b--c") == key("a--b/c")).
    return slug.replace("/", ":")


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def repo_file(slug):
    return state_dir() / "repos" / f"{key(slug)}.json"


def load_repo(slug):
    p = repo_file(check_slug(slug))
    if not p.exists():
        raise SystemExit(f"{slug} isn't registered. Run `/factory add {slug}` first.")
    return json.loads(p.read_text())


def job_dir(slug, number):
    return state_dir() / "jobs" / f"{key(slug)}--{number}"


def all_jobs():
    jobs = []
    for p in sorted(state_dir().glob("jobs/*/job.json")):
        try:
            jobs.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: skipping corrupt job file {p}: {exc}", file=sys.stderr)
    return jobs


def active_jobs(slug=None):
    jobs = [j for j in all_jobs() if j["state"] == "active"]
    return [j for j in jobs if j["repo"] == slug] if slug else jobs


def active_job(slug):
    return next(iter(active_jobs(slug)), None)


def require_active(slug):
    job = active_job(check_slug(slug))
    if not job:
        raise SystemExit(f"No active factory job for {slug}.")
    return job


def log(job, text):
    job["updated"] = now()
    job["events"].append({"at": job["updated"], "stage": job["stage"], "text": text})
    save(job_dir(job["repo"], job["issue"]) / "job.json", job)


# ── VCS abstraction ───────────────────────────────────────────────────────────

def _vcs(repo_or_slug):
    """Return the vcs backend ('github', 'gitlab', or 'gitea') for a repo record or slug."""
    if isinstance(repo_or_slug, dict):
        return repo_or_slug.get("vcs", "github")
    # Try to read from the repo's factory/config.json if it exists
    try:
        rec = load_repo(repo_or_slug)
        return rec.get("vcs", "github")
    except SystemExit:
        return "github"


def _cli(vcs):
    if vcs == "gitlab":
        return "glab"
    if vcs == "gitea":
        return "gt"
    return "gh"


def _gt_host(config_or_path=None):
    """Return the Gitea host URL from factory/config.json or env."""
    host = os.environ.get("GITEA_CLI_HOST") or os.environ.get("GITEA_INSTANCE_URL", "")
    if not host and config_or_path:
        cfg = config_or_path if isinstance(config_or_path, dict) else load_config(config_or_path)
        host = cfg.get("gitea_host", "")
    return host.rstrip("/")


def cli_run(*args, cwd=None, check=True):
    """Run a shell command, returning stdout. Raises SystemExit on failure."""
    try:
        r = subprocess.run(list(args), cwd=cwd, text=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise SystemExit(f"`{args[0]}` is not installed.")
    if check and r.returncode:
        detail = (r.stderr or r.stdout).strip()
        raise SystemExit(f"`{' '.join(str(a) for a in args[:3])}` failed: {detail}")
    return r.stdout.strip()


def git_push(path, *args):
    return cli_run("env", "GIT_TERMINAL_PROMPT=0", "git", "push", *args, cwd=path)


def origin_slug(path, vcs="github", config=None):
    """The OWNER/REPO of a clone's origin remote, or None.

    For gitea/forgejo (self-hosted), the host is read from config or env
    so the regex matches any instance URL, not just a fixed domain.
    """
    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                       text=True, capture_output=True, stdin=subprocess.DEVNULL)
    url = r.stdout.strip()
    if vcs == "gitlab":
        m = re.search(r"gitlab\.com[:/]([\w.\-]+/[\w.\-]+?)(?:\.git)?/?$", url)
    elif vcs == "gitea":
        # Self-hosted: match any host. Extract last two path segments (owner/repo).
        # Handles https://host/owner/repo.git and git@host:owner/repo.git
        m = re.search(r"[:/]([\w.\-]+/[\w.\-]+?)(?:\.git)?/?$", url)
        # If the host is known, verify the URL actually contains it to avoid
        # false-positive matches against github.com or gitlab.com remotes.
        host = _gt_host(config)
        if m and host:
            host_plain = re.sub(r"^https?://", "", host).rstrip("/")
            if host_plain not in url:
                return None
    else:
        m = re.search(r"github\.com[:/]([\w.\-]+/[\w.\-]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def repo_view(slug, vcs, config=None):
    """Return {nameWithOwner, viewerPermission, hasIssuesEnabled, isArchived,
    defaultBranchRef.name} for a repository."""
    if vcs == "gitlab":
        raw = json.loads(cli_run(
            "glab", "repo", "view", slug, "--output", "json"))
        # glab returns: name_with_namespace, visibility, default_branch, ...
        # Permissions: we check by attempting a dry-run push later
        return {
            "nameWithOwner": raw.get("path_with_namespace", slug),
            "viewerPermission": "WRITE",  # validated by push dry-run
            "hasIssuesEnabled": True,
            "isArchived": raw.get("archived", False),
            "defaultBranchRef": {"name": raw.get("default_branch", "main")},
        }
    elif vcs == "gitea":
        # gt repo view --json returns GitHub-compatible JSON fields
        host = _gt_host(config)
        cmd = ["gt", "repo", "view", slug,
               "--json", "nameWithOwner,defaultBranchRef,isArchived"]
        if host:
            cmd += ["--host", host]
        raw = json.loads(cli_run(*cmd))
        return {
            "nameWithOwner": raw.get("nameWithOwner", slug),
            "viewerPermission": "WRITE",  # validated by push dry-run
            "hasIssuesEnabled": True,
            "isArchived": raw.get("isArchived", False),
            "defaultBranchRef": raw.get("defaultBranchRef", {"name": "main"}),
        }
    else:
        return json.loads(cli_run(
            "gh", "repo", "view", slug, "--json",
            "nameWithOwner,viewerPermission,hasIssuesEnabled,isArchived,defaultBranchRef"))


def issue_list_labeled(slug, vcs, label, limit=100, config=None):
    """Return [{number, title}] for open issues with the given label."""
    if vcs == "gitlab":
        raw = json.loads(cli_run(
            "glab", "issue", "list", "--repo", slug,
            "--label", label, "--per-page", str(limit), "--output", "json"))
        return [{"number": i["iid"], "title": i["title"]} for i in (raw or [])]
    elif vcs == "gitea":
        # gt issue list mirrors gh: --json number,title, --label, --state, --limit
        host = _gt_host(config)
        cmd = ["gt", "issue", "list", "--repo", slug,
               "--label", label, "--state", "open",
               "--json", "number,title", "--limit", str(limit)]
        if host:
            cmd += ["--host", host]
        return json.loads(cli_run(*cmd))
    else:
        return json.loads(cli_run(
            "gh", "issue", "list", "--repo", slug, "--label", label,
            "--state", "open", "--json", "number,title", "--limit", str(limit)))


def issue_view_full(slug, vcs, number, config=None):
    """Return {number, title, body, url, author{login}}."""
    if vcs == "gitlab":
        raw = json.loads(cli_run(
            "glab", "issue", "view", str(number), "--repo", slug, "--output", "json"))
        return {
            "number": raw["iid"],
            "title": raw.get("title", ""),
            "body": raw.get("description", ""),
            "url": raw.get("web_url", ""),
            "author": {"login": raw.get("author", {}).get("username", "")},
        }
    elif vcs == "gitea":
        host = _gt_host(config)
        cmd = ["gt", "issue", "view", str(number), "--repo", slug,
               "--json", "number,title,body,url,author"]
        if host:
            cmd += ["--host", host]
        return json.loads(cli_run(*cmd))
    else:
        return json.loads(cli_run(
            "gh", "issue", "view", str(number), "--repo", slug,
            "--json", "number,title,body,url,author"))


def issue_comment(slug, vcs, number, body, config=None):
    if vcs == "gitlab":
        cli_run("glab", "issue", "note", str(number), "--repo", slug,
                "--message", body)
    elif vcs == "gitea":
        host = _gt_host(config)
        cmd = ["gt", "issue", "comment", str(number), "--repo", slug, "--body", body]
        if host:
            cmd += ["--host", host]
        cli_run(*cmd)
    else:
        cli_run("gh", "issue", "comment", str(number), "--repo", slug,
                "--body", body)


def label_create(slug, vcs, name, color, description, config=None):
    if vcs == "gitlab":
        cli_run("glab", "label", "create", name, "--repo", slug,
                "--color", f"#{color}", "--force")
    elif vcs == "gitea":
        # gt label create --color requires the hash prefix (like glab)
        host = _gt_host(config)
        cmd = ["gt", "label", "create", "--repo", slug,
               "--name", name, "--color", f"#{color}", "--force"]
        if host:
            cmd += ["--host", host]
        try:
            cli_run(*cmd)
        except SystemExit:
            pass  # label already exists
    else:
        cli_run("gh", "label", "create", name, "--repo", slug,
                "--color", color, "--description", description, "--force")


def pr_create(slug, vcs, base, branch, title, body_file, draft, config=None):
    """Open a PR/MR and return its URL."""
    if vcs == "gitlab":
        args = ["glab", "mr", "create",
                "--repo", slug,
                "--target-branch", base,
                "--source-branch", branch,
                "--title", title,
                "--description-file", body_file,
                "--yes"]
        if draft:
            args.append("--draft")
        out = cli_run(*args)
        # glab mr create prints the MR URL on the last line
        return out.splitlines()[-1]
    elif vcs == "gitea":
        # gt pr create mirrors gh; use --yes to suppress interactive confirmation
        host = _gt_host(config)
        args = ["gt", "pr", "create",
                "--repo", slug,
                "--base", base,
                "--head", branch,
                "--title", title,
                "--body-file", body_file,
                "--yes"]
        if draft:
            args.append("--draft")
        if host:
            args += ["--host", host]
        return cli_run(*args).splitlines()[-1]
    else:
        args = ["gh", "pr", "create",
                "--repo", slug,
                "--base", base,
                "--head", branch,
                "--title", title,
                "--body-file", body_file]
        if draft:
            args.append("--draft")
        return cli_run(*args).splitlines()[-1]


# ── config.json helpers ───────────────────────────────────────────────────────

def load_config(repo_path):
    """Load factory/config.json from the repository clone."""
    p = Path(repo_path) / "factory" / "config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def routing_for(config, risk, surfaces):
    """Return {reviewer_name: model_hint} based on risk and surfaces.

    config.json shape:
      routing:        {low: {code: haiku}, medium: {code: sonnet, security: opus}}
      surface_reviewers: {security: {security: opus}, ui: {ui: haiku, ux: sonnet}}

    Returns e.g. {"code": "haiku", "security": "opus"} — keys are reviewer names
    (used to select the hermes-factory-review-<name> skill), values are model hints.
    """
    routing = config.get("routing", {})
    surface_reviewers = config.get("surface_reviewers", {})
    # base reviewers from risk level: {reviewer_name: model_hint}
    base = routing.get(risk, {})
    reviewers = dict(base) if base else {"code": "haiku"}
    # merge in surface-specific reviewers
    for surface in (surfaces or []):
        extra = surface_reviewers.get(surface, {})
        reviewers.update(extra)
    return reviewers


def protected_paths_check(repo_path, config, base_branch):
    """Return list of protected files that were modified. Empty = clean.

    The protected list is read from the BASE version of factory/config.json
    (origin/<base_branch>), not the job branch's working tree.  Otherwise a
    job branch could disable protection by rewriting the config file that
    defines it, and the diff would show that very rewrite as clean.
    Falls back to the working-tree config if the base file is unavailable.
    """
    base_config = config
    try:
        base_raw = cli_run("git", "show",
                           f"origin/{base_branch}:factory/config.json",
                           cwd=repo_path, check=False)
        if base_raw.strip():
            base_config = json.loads(base_raw)
    except (ValueError, SystemExit):
        pass  # base config unavailable — keep the working-tree config
    patterns = base_config.get("protected_paths", [])
    if not patterns:
        return []
    diff = cli_run("git", "diff", "--name-status",
                   f"origin/{base_branch}...HEAD", cwd=repo_path, check=False)
    hits = []
    for line in diff.splitlines():
        parts = line.split("\t")
        if not parts or parts[0].startswith("A"):
            continue
        # Rename lines: R100\tsrc\tdst — check both src and dst
        candidates = [parts[1]] if len(parts) > 1 else []
        if parts[0].startswith("R") and len(parts) > 2:
            candidates.append(parts[2])
        for path in candidates:
            if any(fnmatch.fnmatch(path, pat) for pat in patterns):
                if path not in hits:
                    hits.append(path)
    return hits


# ── clone discovery ───────────────────────────────────────────────────────────

def find_clones(slug, vcs="github"):
    roots = []
    for root in (hermes_home(), Path.home()):
        r = root.resolve()
        if r.is_dir() and r not in roots:
            roots.append(r)
    found = []
    for root in roots:
        for current, dirs, files in os.walk(root):
            here = Path(current)
            if ".git" in dirs or ".git" in files:
                if (origin_slug(here, vcs) or "").lower() == slug.lower():
                    if here not in found:
                        found.append(here)
                dirs[:] = []
                continue
            depth = len(here.relative_to(root).parts)
            dirs[:] = [] if depth >= SEARCH_DEPTH else [
                d for d in dirs
                if not d.startswith(".") and d not in ("node_modules", ".worktrees")]
    return found


def check_queue(repo):
    """Open labeled issues with no active or done job, oldest first."""
    slug, vcs = repo["repo"], repo.get("vcs", "github")
    cfg = load_config(repo.get("path", ""))
    issues = issue_list_labeled(slug, vcs, LABEL, limit=200, config=cfg)
    built = {j["issue"] for j in all_jobs() if j["repo"] == slug}
    waiting = sorted(
        (i for i in issues if i["number"] not in built),
        key=lambda i: i["number"])
    repo.update(checked=now(), queue=waiting)
    save(repo_file(slug), repo)
    return waiting


# ── add ───────────────────────────────────────────────────────────────────────

def add(args):
    slug = check_slug(args.repo)
    # Determine VCS from env hint or flag
    vcs = getattr(args, "vcs", None) or os.environ.get("FACTORY_VCS", "github")
    # Load config from --path if provided, to get gitea_host etc.
    cfg = load_config(args.path) if getattr(args, "path", None) else {}

    info = repo_view(slug, vcs, config=cfg)
    slug = info["nameWithOwner"]

    if info.get("isArchived") or not info.get("hasIssuesEnabled", True):
        raise SystemExit(f"{slug} must be unarchived with issues enabled.")

    if args.path:
        path = Path(args.path).expanduser().resolve()
        if (origin_slug(path, vcs, config=cfg) or "").lower() != slug.lower():
            raise SystemExit(f"{path} isn't a clone of {slug}.")
        how = "Using the clone at"
    else:
        clones = find_clones(slug, vcs)
        if len(clones) > 1:
            raise SystemExit(
                f"Found {len(clones)} clones of {slug}: "
                + ", ".join(map(str, clones))
                + ". Choose one with --path.")
        if clones:
            path, how = clones[0], "Found a clone at"
        else:
            path = hermes_home() / "work" / slug.split("/")[1]
            if path.exists():
                raise SystemExit(f"{path} already exists and isn't a clone of {slug}.")
            path.parent.mkdir(parents=True, exist_ok=True)
            clone_cmd = ["gt", "repo", "clone", slug, str(path)] if vcs == "gitea" else \
                        [_cli(vcs), "repo", "clone", slug, str(path)]
            if vcs == "gitea":
                host = _gt_host(cfg)
                if host:
                    clone_cmd += ["--host", host]
            cli_run(*clone_cmd)
            how = "Cloned into"
        # Reload config from discovered clone path
        cfg = load_config(str(path))

    base = info["defaultBranchRef"]["name"]

    # Verify push access via dry-run
    git_push(path, "--dry-run", "origin", f"HEAD:refs/heads/{LABEL}/access-check")

    # Create labels
    for name, color, desc in [
        (LABEL,       "5319E7", "Queued for the Hermes software factory"),
        (LABEL_INFO,  "FBCA04", "Factory asked the author a question"),
        (LABEL_HUMAN, "D93F0B", "Factory needs a human decision"),
    ]:
        try:
            label_create(slug, vcs, name, color, desc, config=cfg)
        except SystemExit:
            pass  # label already exists — that's fine

    previous = json.loads(repo_file(slug).read_text()) if repo_file(slug).exists() else {}
    save(repo_file(slug), {
        "repo": slug, "path": str(path), "base": base, "vcs": vcs,
        "added": now(), "checked": previous.get("checked"), "queue": previous.get("queue", []),
    })
    print(f"{how} {path}")
    print(f"Push access to {slug} confirmed. Default branch: {base}. VCS: {vcs}.")
    print(f"Label an issue `{LABEL}` to queue it. Build with `/factory run {slug}`.")


# ── start (claim a job) ────────────────────────────────────────────────────────

def start(args):
    repo = load_repo(args.repo)
    slug, path, vcs = repo["repo"], repo["path"], repo.get("vcs", "github")

    busy = active_job(slug)
    if busy:
        print(f"BUSY: #{busy['issue']} is at {busy['stage']} "
              f"(updated {busy['updated']}). Nothing started.")
        return

    if cli_run("git", "status", "--porcelain", cwd=path, check=False):
        raise SystemExit(f"{path} has uncommitted changes. Commit, stash, or discard first.")

    waiting = check_queue(repo)
    if not waiting:
        print(f"NO WORK: {slug} has no open issues labeled `{LABEL}`.")
        return

    requested = getattr(args, "issue", None)
    if requested is not None and waiting[0]["number"] != requested:
        raise SystemExit(
            f"Queue changed: expected #{requested}, found #{waiting[0]['number']}. "
            "Inspect before continuing.")

    number = waiting[0]["number"]

    # Hard label re-verification: confirm the issue STILL carries the factory
    # label right now, directly from the VCS API — not from the local snapshot.
    # This prevents acting on an issue whose label was removed after check_queue
    # ran, and ensures no code path can start a job on an unlabeled issue.
    cfg = load_config(path)
    live_labeled = {i["number"] for i in issue_list_labeled(slug, vcs, LABEL, limit=200, config=cfg)}
    if number not in live_labeled:
        raise SystemExit(
            f"LABEL GUARD: issue #{number} no longer carries the `{LABEL}` label. "
            "Remove it from the queue snapshot and re-run to pick the next issue."
        )

    # TOCTOU guard: claim the issue number by creating the job dir exclusively.
    # If two concurrent start() calls race, only one gets O_EXCL; the other fails.
    claim_path = job_dir(slug, number) / "job.json"
    try:
        claim_path.parent.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SystemExit(
            f"#{number} was claimed by a concurrent process. "
            "Run `factory.py monitor` to check current state.")

    issue  = issue_view_full(slug, vcs, number, config=load_config(path))
    branch = f"{LABEL}/issue-{number}"
    config = load_config(path)

    cli_run("git", "fetch", "origin", repo["base"], cwd=path)
    cli_run("git", "switch", "-c", branch, f"origin/{repo['base']}", cwd=path)

    job = {
        "repo": slug, "path": path, "vcs": vcs, "issue": number,
        "title": issue["title"], "url": issue["url"],
        "branch": branch, "base": repo["base"],
        "stage": "ingest", "state": "active", "result": None,
        "pr": None, "pr_url": None,
        "risk": None, "surfaces": [], "agents": {},
        "round": 0, "round_limit": config.get("max_rounds", 3),
        "reviews": {}, "started": now(), "updated": now(),
        "stages": {"ingest": now()}, "events": [],
    }
    log(job, f"Picked #{number} from {len(waiting)} queued issue(s)")

    # Dequeue this issue
    repo["queue"] = waiting[1:]
    save(repo_file(slug), repo)

    print(f"STARTED issue #{number} in {slug}")
    print(f"Clone:      {path}")
    print(f"Branch:     {branch} (from origin/{repo['base']})")
    print(f"Job folder: {job_dir(slug, number)}")
    print("Issue:")
    print(json.dumps({
        "title": issue["title"], "author": issue["author"]["login"],
        "url": issue["url"], "body": issue.get("body", ""),
    }, indent=2))


# ── stage advance ──────────────────────────────────────────────────────────────

def stage_cmd(args):
    job = require_active(args.repo)
    name = args.name
    if name not in STAGES:
        raise SystemExit(f"Unknown stage {name!r}. Valid: {', '.join(STAGES)}")
    current_idx = STAGES.index(job["stage"])
    target_idx  = STAGES.index(name)
    if target_idx != current_idx + 1:
        raise SystemExit(
            f"#{job['issue']} is at {job['stage']}; "
            f"the next stage is {STAGES[current_idx + 1]}, not {name}.")
    job["stage"] = name
    job["stages"][name] = now()
    note_text = getattr(args, "note", None) or f"Started {name}"
    log(job, note_text)
    print(f"#{job['issue']} advanced to {name}.")


# ── note ───────────────────────────────────────────────────────────────────────

def note_cmd(args):
    job = require_active(args.repo)
    log(job, args.text)
    print(f"Noted at {job['stage']}.")


# ── route (risk + surfaces → reviewer set) ────────────────────────────────────

def route_cmd(args):
    job = require_active(args.repo)
    config = load_config(job["path"])
    risk     = args.risk
    raw_surfaces = args.surfaces or ""
    if isinstance(raw_surfaces, (list, tuple)):
        surfaces = [str(s).strip() for s in raw_surfaces if str(s).strip()]
    else:
        surfaces = [s.strip() for s in str(raw_surfaces).split(",") if s.strip()]

    job["risk"]     = risk
    job["surfaces"] = surfaces
    job["agents"]   = {"reviewers": routing_for(config, risk, surfaces)}
    log(job, f"Routed: risk={risk}, surfaces={surfaces or ['none']}, "
             f"reviewers={list(job['agents']['reviewers'])}")
    print(f"#{job['issue']} routed: risk={risk}, "
          f"reviewers={list(job['agents']['reviewers'])}")


# ── gate (run checks) ──────────────────────────────────────────────────────────

def gate_cmd(args):
    """Run factory/checks.sh in the job's clone and record the result."""
    job    = require_active(args.repo)
    path   = job["path"]
    config = load_config(path)

    def _to_gate():
        # Record that the gate ran: build -> gate, so the orchestrator can
        # continue from there (PASS: stage review; FAIL: round-next).
        if job["stage"] == "build":
            job["stage"] = "gate"
            job["stages"]["gate"] = now()

    # Stage guard: gate only makes sense while building or re-checking
    if job["stage"] not in ("build", "gate"):
        raise SystemExit(
            f"#{job['issue']} is at {job['stage']}, not build or gate. "
            "Advance to build first.")

    # Protected path check
    protected = protected_paths_check(path, config, job["base"])
    if protected:
        _to_gate()
        msg = f"GATE: FAIL — protected paths modified: {', '.join(protected)}"
        log(job, msg)
        print(msg)
        return

    checks_sh = Path(path) / "factory" / "checks.sh"
    if not checks_sh.exists():
        raise SystemExit(f"factory/checks.sh not found in {path}. Fill it in first.")

    checks_timeout = config.get("checks_timeout_seconds", 300)
    start_t = time.monotonic()
    try:
        r = subprocess.run(["bash", str(checks_sh)], cwd=path,
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL,
                           timeout=checks_timeout)
    except subprocess.TimeoutExpired:
        elapsed = int(time.monotonic() - start_t)
        _to_gate()
        msg = f"GATE: FAIL — checks.sh timed out after {elapsed}s"
        log(job, msg)
        print(msg)
        rnd = job.get("round", 0)
        rnd_dir = job_dir(job["repo"], job["issue"]) / f"round-{rnd}"
        rnd_dir.mkdir(parents=True, exist_ok=True)
        (rnd_dir / "checks.md").write_text(f"GATE: FAIL\n\n## Summary\n- checks.sh timed out after {elapsed}s\n")
        return
    elapsed = int(time.monotonic() - start_t)

    verdict  = "PASS" if r.returncode == 0 else "FAIL"
    commit   = cli_run("git", "rev-parse", "HEAD", cwd=path)
    out_text = (r.stdout + r.stderr).rstrip()
    tail     = "\n".join(out_text.splitlines()[-80:])

    # Write round-N/checks.md
    rnd = job.get("round", 0)
    rnd_dir = job_dir(job["repo"], job["issue"]) / f"round-{rnd}"
    rnd_dir.mkdir(parents=True, exist_ok=True)
    md = (f"GATE: {verdict}\nCOMMIT: {commit}\n\n"
          f"## Summary\n- checks.sh {'passed' if r.returncode == 0 else 'failed'} in {elapsed}s\n"
          f"- Protected paths: {'none changed' if not protected else ', '.join(protected)}\n\n"
          f"## Output (last 80 lines)\n```\n{tail}\n```\n")
    (rnd_dir / "checks.md").write_text(md)
    (rnd_dir / "checks.log").write_text(out_text)

    _to_gate()
    log(job, f"Gate {verdict} in {elapsed}s (round {rnd})")
    print(f"GATE: {verdict}  commit={commit[:7]}  {elapsed}s")
    if verdict == "FAIL":
        print(tail[-2000:])


# ── open-pr ────────────────────────────────────────────────────────────────────

def open_pr(args):
    job  = require_active(args.repo)
    path = job["path"]
    slug = job["repo"]
    vcs  = job.get("vcs", "github")

    if job["stage"] not in ("review", "approve", "pr"):
        raise SystemExit(
            f"#{job['issue']} is at {job['stage']}. "
            "Open the PR/MR after review passes and approve decides APPROVE.")

    if cli_run("git", "branch", "--show-current", cwd=path) != job["branch"]:
        raise SystemExit(f"Check out {job['branch']} in {path} first.")
    if cli_run("git", "status", "--porcelain", cwd=path, check=False):
        raise SystemExit("Commit or discard remaining changes first.")
    if cli_run("git", "rev-list", "--count",
               f"origin/{job['base']}..HEAD", cwd=path) == "0":
        raise SystemExit("No commits on the branch. Use `stop` if there's nothing to open.")

    folder    = job_dir(slug, job["issue"])
    body_file = args.body_file
    draft     = getattr(args, "draft", False)

    # Append standard footer
    body = Path(body_file).read_text().rstrip()
    final_body = Path(body_file).parent / "pr-body-final.md"
    final_body.write_text(
        f"{body}\n\nCloses #{job['issue']}\n\n_Built by the Hermes Software Factory._\n")

    job["stage"]        = "pr"
    job["stages"]["pr"] = now()
    log(job, "Pushing branch and opening PR/MR")

    try:
        git_push(path, "--set-upstream", "origin", job["branch"])
        url = pr_create(slug, vcs, job["base"], job["branch"],
                        job["title"], str(final_body), draft,
                        config=load_config(path))
    except BaseException as exc:
        # Roll back stage to "approve" on any failure — SystemExit (cli_run),
        # KeyboardInterrupt (SIGTERM/Ctrl-C mid-push), or unexpected OSError.
        # State stays "active"; branch is preserved; job can be retried.
        job["stage"] = "approve"
        job["stages"].pop("pr", None)
        log(job, f"PR/MR open failed: {exc}. Rolled back to approve.")
        raise

    job.update(pr=url, pr_url=url, state="done",
               result="draft" if draft else "ready")
    log(job, f"Opened {'draft ' if draft else ''}PR/MR: {url}")

    # Switch back to base branch
    cli_run("git", "switch", job["base"], cwd=path, check=False)

    print(f"{'Draft PR/MR' if draft else 'PR/MR'} for #{job['issue']}: {url}")


# ── resume ────────────────────────────────────────────────────────────────────

def resume(args):
    repo = load_repo(args.repo)
    slug = repo["repo"]

    if active_job(slug):
        raise SystemExit(f"A factory job is already active for {slug}.")

    target = job_dir(slug, args.issue) / "job.json"
    if not target.exists():
        raise SystemExit(f"No recorded job for {slug} #{args.issue}.")
    job = json.loads(target.read_text())

    if job["state"] != "stopped":
        raise SystemExit(f"Job #{args.issue} is {job['state']!r}, not stopped.")
    if job["path"] != repo["path"]:
        raise SystemExit("The recorded clone path changed; inspect before resuming.")
    if cli_run("git", "branch", "--show-current", cwd=job["path"]) != job["branch"]:
        raise SystemExit(f"Check out {job['branch']} in {job['path']} before resuming.")

    job["state"] = "active"
    log(job, "Resumed by operator after blocker fixed")
    print(f"RESUMED #{job['issue']} in {slug} at stage {job['stage']}. Work preserved.")


# ── round-next ────────────────────────────────────────────────────────────────

def round_next_cmd(args):
    """Increment the round counter and reset stage to 'build'.

    Called by the orchestrator when a round ends without a PASS: after a
    gate FAIL (job at 'gate') or when review produces CHANGES (job at
    'review' or 'approve').  Raises if the job is already at max rounds,
    or if the current stage predates the gate — a new round must never
    start before spec and the first gate have run.
    """
    job = require_active(args.repo)
    if job["stage"] not in ("gate", "review", "approve"):
        raise SystemExit(
            f"#{job['issue']} is at {job['stage']}; round-next is only valid "
            "after the gate (FAIL) or after review/approve (CHANGES).")
    if job["round"] >= job["round_limit"]:
        raise SystemExit(
            f"#{job['issue']} is already at round {job['round']}/{job['round_limit']}. "
            "Stop the job or increase max_rounds in factory/config.json.")
    job["round"] += 1
    job["stage"] = "build"
    job["stages"]["build"] = now()
    log(job, f"Advanced to round {job['round']} (stage reset to build)")
    print(f"#{job['issue']} now in round {job['round']}/{job['round_limit']}. "
          f"Stage reset to build.")


# ── cancel ────────────────────────────────────────────────────────────────────

def cancel(args):
    """Permanently cancel a job, recording the reason in its history.

    Unlike stop, cancel transitions the job to the terminal 'cancelled' state.
    The branch is preserved but the job will never be retried automatically.
    """
    if getattr(args, "repo", None):
        job = require_active(args.repo)
    else:
        jobs = active_jobs()
        if len(jobs) != 1:
            raise SystemExit("No active job." if not jobs else
                             "Several jobs active: "
                             + ", ".join(j["repo"] for j in jobs)
                             + ". Name the repository.")
        job = jobs[0]
    job["state"] = "cancelled"
    log(job, f"Cancelled: {args.reason}")
    print(f"Cancelled #{job['issue']} in {job['repo']} at {job['stage']}. "
          f"Branch {job['branch']} preserved in {job['path']}.")


# ── stop ──────────────────────────────────────────────────────────────────────

def stop(args):
    if getattr(args, "repo", None):
        job = require_active(args.repo)
    else:
        jobs = active_jobs()
        if len(jobs) != 1:
            raise SystemExit("No active job." if not jobs else
                             "Several jobs active: "
                             + ", ".join(j["repo"] for j in jobs)
                             + ". Name the repository.")
        job = jobs[0]
    job["state"] = "stopped"
    log(job, f"Stopped: {args.reason}")
    print(f"Stopped #{job['issue']} in {job['repo']} at {job['stage']}. "
          f"Branch {job['branch']} preserved in {job['path']}.")


# ── dispatch (unattended batch) ────────────────────────────────────────────────

def dispatch(args):
    """Drain the queue snapshot with one fresh hermes --oneshot process per issue."""
    slug = check_slug(args.repo)
    lock_path = state_dir() / f"{key(slug)}.dispatch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"BUSY: dispatcher already running for {slug}.")
            return

        repo = load_repo(slug)
        if active_job(slug):
            print(f"BUSY: a factory job is already active for {slug}.")
            return

        waiting = check_queue(repo)
        if not waiting:
            print(f"NO WORK: {slug} has no eligible issues.")
            return

        completed = []
        for issue in waiting:
            number = issue["number"]
            start(argparse.Namespace(repo=slug, issue=number))

            job = active_job(slug)
            if not job or job["issue"] != number:
                raise SystemExit(f"Could not claim #{number}; batch halted.")

            folder = job_dir(slug, number)
            prompt = (
                f"Factory worker for {slug} issue #{number}. "
                f"The dispatcher already ran start; the active job is at ingest in {folder}, "
                f"clone {repo['path']}. "
                "Do NOT run start or dispatch. Load the factory skill and follow the "
                "pipeline from ingest through spec, build, gate, review, approve, and open-pr "
                "for ONLY this issue. "
                f"Use `python3 {Path(__file__).resolve()}` for all bookkeeping. "
                "Do not merge. Stop on factory.py errors. "
                "End after this issue; the dispatcher handles the queue."
            )

            # Unset parent session vars so the worker starts a clean session
            unset = [arg for name in PARENT_SESSION_VARS for arg in ("-u", name)]
            with (folder / "worker-output.log").open("w") as output:
                result = subprocess.run(
                    ["env", *unset, "hermes", "chat", "--oneshot", "--quiet",
                     "--skills", "hermes-factory", "-q", prompt],
                    cwd=repo["path"],
                    stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT)

            saved = json.loads((folder / "job.json").read_text())
            if result.returncode or saved["state"] != "done":
                raise SystemExit(
                    f"Worker #{number} did not complete; batch halted. "
                    f"See {folder}/worker-output.log")

            completed.append(number)
            print(f"Completed #{number}: {saved.get('pr_url') or saved.get('pr')}", flush=True)

        print(f"Batch complete: {len(completed)} issue(s). New arrivals wait for the next tick.")


# ── precheck / install-precheck ───────────────────────────────────────────────

def precheck(args):
    """Cron pre-check: print wakeAgent JSON. True only when work is waiting.

    stdout carries ONLY the JSON object (the cron harness parses it);
    human-readable status goes to stderr.
    """
    repo  = load_repo(args.repo)
    busy  = active_job(repo["repo"])
    if busy:
        print(f"{repo['repo']}: #{busy['issue']} is already at {busy['stage']}.",
              file=sys.stderr)
        wake = False
    else:
        waiting = check_queue(repo)
        if waiting:
            print(f"{repo['repo']}: {len(waiting)} issue(s) labeled '{LABEL}'. "
                  f"Next: #{waiting[0]['number']}.", file=sys.stderr)
        else:
            print(f"{repo['repo']}: no issues labeled '{LABEL}'.",
                  file=sys.stderr)
        wake = bool(waiting)
    print(json.dumps({"wakeAgent": wake}))


def install_precheck(args):
    """Write a cron pre-check (or dispatch) script into $HERMES_HOME/scripts/."""
    slug    = load_repo(args.repo)["repo"]
    command = "dispatch" if getattr(args, "dispatch", False) else "precheck"
    prefix  = "factory-dispatch" if command == "dispatch" else "factory-precheck"
    target  = hermes_home() / "scripts" / f"{prefix}-{key(slug)}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'"""Hermes cron factory {command} for {slug}."""\n'
        "import runpy\nimport sys\n\n"
        f'sys.argv = ["factory.py", {command!r}, {slug!r}]\n'
        f'runpy.run_path({str(Path(__file__).resolve())!r}, run_name="__main__")\n')
    print(f"Script written: {target}")
    print(f"Script name:    {target.name}")


# ── monitor ────────────────────────────────────────────────────────────────────

def _parse(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _clock(stamp):
    return _parse(stamp).strftime("%H:%M")


def _age(start_stamp, end=None):
    minutes = int(
        ((end or datetime.now(timezone.utc)) - _parse(start_stamp)).total_seconds() // 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def _fit(text, width):
    return text if len(text) <= width else text[:width - 3] + "..."


def _stage_status(job, name):
    stages   = job.get("stages", {})
    current  = job["stage"]
    state    = job["state"]
    cur_idx  = STAGES.index(current) if current in STAGES else -1
    this_idx = STAGES.index(name) if name in STAGES else -1

    if state == "done" or this_idx < cur_idx:
        if name == STAGES[-1]:
            return job.get("result") or "done"
        nxt = STAGES[this_idx + 1]
        if name in stages and nxt in stages:
            return "done " + _age(stages[name], _parse(stages[nxt]))
        return "done"
    if this_idx == cur_idx:
        if state == "active" and name in stages:
            return f"working {_age(stages[name])}"
        return "STOPPED" if state == "stopped" else ("CANCELLED" if state == "cancelled" else "")
    return ""


def _diagram(job):
    display_stages = ["ingest", "spec", "build", "gate", "review", "approve", "pr"]
    boxes   = [f"| {s.upper()} |" for s in display_stages]
    arrow   = "-->"
    starts, x = [], 1
    for box in boxes:
        starts.append(x)
        x += len(box) + len(arrow)
    border  = " " + (" " * len(arrow)).join("+" + "-" * (len(b) - 2) + "+" for b in boxes)
    status  = [" "] * (x + 10)
    for i, (box, name) in enumerate(zip(boxes, display_stages)):
        word = _stage_status(job, name)
        left = max(starts[i] + (len(box) - len(word)) // 2, 0)
        status[left:left + len(word)] = list(word)
    lines = [border, " " + arrow.join(boxes), border, "".join(status).rstrip()]
    if job["state"] == "active" and job["stage"] in display_stages:
        idx    = display_stages.index(job["stage"])
        centre = starts[idx] + len(boxes[idx]) // 2
        lines += [" " * centre + "^", " " * (centre - 3) + "HERMES"]
    return lines


def _card(job, heading):
    lines = [
        f" {heading}",
        f" {job['repo']}  #{job['issue']}  "
        + _fit(job["title"], WIDTH - len(job["repo"]) - 10),
        f" {job['branch']}  |  started {_clock(job['started'])} UTC  |  "
        f"updated {_clock(job['updated'])} UTC ({_age(job['updated'])} ago)",
        "",
    ]
    lines += _diagram(job)
    if job.get("pr") or job.get("pr_url"):
        pr = job.get("pr_url") or job.get("pr")
        lines += ["", f" PR/MR ({job.get('result', '')}): {pr}"]
    lines += ["", " Log"]
    for event in job.get("events", [])[-6:]:
        lines.append(
            f"  {_clock(event['at'])}  {event['stage']:<9} "
            + _fit(event["text"], WIDTH - 19))
    return lines


def monitor(_args=None):
    jobs  = sorted(all_jobs(), key=lambda j: j["started"])
    repos = []
    for p in sorted(state_dir().glob("repos/*.json")):
        try:
            repos.append(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: skipping corrupt repo file {p}: {exc}", file=sys.stderr)
    repos.sort(key=lambda r: r["repo"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f" HERMES SOFTWARE FACTORY{stamp:>{WIDTH - 23}}", " " + "=" * (WIDTH - 1)]

    if not repos:
        lines += ["", " No repositories registered. Add one with `/factory add OWNER/REPO`."]
        print("\n".join(lines))
        return

    active = [j for j in jobs if j["state"] == "active"]
    for job in active:
        lines += [""] + _card(job, "NOW BUILDING")
    if not active:
        lines += ["", " IDLE: no issue in progress."]
        if jobs:
            lines += [""] + _card(jobs[-1], "LAST JOB")

    for repo in repos:
        checked = (f"checked {_clock(repo['checked'])} UTC, {_age(repo['checked'])} ago"
                   if repo.get("checked") else "not checked yet")
        vcs_tag = repo.get("vcs", "github")
        lines += ["", f" Queue  {repo['repo']}  [{vcs_tag}]  ({checked})"]
        queue = repo.get("queue", [])
        lines += [f"  #{i['number']:<5} " + _fit(i["title"], WIDTH - 9)
                  for i in queue] or ["  empty"]

    finished = list(reversed([j for j in jobs if j["state"] != "active"]))[:5]
    if finished:
        lines += ["", " Finished (last 5)"]
        for job in finished:
            pr = job.get("pr_url") or job.get("pr", "")
            outcome = f"{job.get('result', 'stopped'):<8} {pr}" if pr else f"stopped at {job['stage']}"
            lines.append(f"  {job['repo']}  #{job['issue']:<5}  {outcome}")

    print("\n".join(lines))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add_repo(sp, pos=True):
        if pos:
            sp.add_argument("repo")
        else:
            sp.add_argument("repo", nargs="?")

    s = sub.add_parser("add", help="register a repository with the factory")
    add_repo(s)
    s.add_argument("--path", help="force this local clone path")
    s.add_argument("--vcs", choices=["github", "gitlab", "gitea"], default=None,
                   help="VCS backend (default: github)")
    s.set_defaults(func=add)

    s = sub.add_parser("start", help="claim the oldest queued issue")
    add_repo(s)
    s.set_defaults(func=start)

    s = sub.add_parser("stage", help="advance the active job to the next stage")
    add_repo(s)
    s.add_argument("name", choices=STAGES[1:-1])  # "pr" only reachable via open-pr
    s.add_argument("note", nargs="?")
    s.set_defaults(func=stage_cmd)

    s = sub.add_parser("note", help="record a progress note")
    add_repo(s)
    s.add_argument("text")
    s.set_defaults(func=note_cmd)

    s = sub.add_parser("route", help="set risk, surfaces, and reviewer set")
    add_repo(s)
    s.add_argument("risk", choices=["low", "medium", "high"])
    s.add_argument("--surfaces", default="",
                   help="comma-separated surfaces: ui, security")
    s.set_defaults(func=route_cmd)

    s = sub.add_parser("gate", help="run checks.sh and record the result")
    add_repo(s)
    s.set_defaults(func=gate_cmd)

    s = sub.add_parser("open-pr", help="push the branch and open the PR/MR")
    add_repo(s)
    s.add_argument("--body-file", required=True)
    s.add_argument("--draft", action="store_true")
    s.set_defaults(func=open_pr)

    s = sub.add_parser("resume", help="re-activate a stopped job")
    add_repo(s)
    s.add_argument("issue", type=int)
    s.set_defaults(func=resume)

    s = sub.add_parser("stop", help="stop the active job without a PR/MR")
    add_repo(s, pos=False)
    s.add_argument("--reason", required=True)
    s.set_defaults(func=stop)

    s = sub.add_parser("round-next",
                       help="increment round counter and reset stage to build")
    add_repo(s)
    s.set_defaults(func=round_next_cmd)

    s = sub.add_parser("cancel", help="permanently cancel a job (terminal state)")
    add_repo(s, pos=False)
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cancel)

    s = sub.add_parser("dispatch", help="batch-process the queue (one worker per issue)")
    add_repo(s)
    s.set_defaults(func=dispatch)

    s = sub.add_parser("monitor", help="ASCII dashboard of all repos and jobs")
    s.set_defaults(func=monitor)

    s = sub.add_parser("precheck", help="cron pre-check: is work waiting?")
    add_repo(s)
    s.set_defaults(func=precheck)

    s = sub.add_parser("install-precheck",
                       help="write a cron script into $HERMES_HOME/scripts/")
    add_repo(s)
    s.add_argument("--dispatch", action="store_true",
                   help="install a no-agent dispatch script instead")
    s.set_defaults(func=install_precheck)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
