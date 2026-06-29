#!/usr/bin/env python3
"""PreToolUse hook: enforce a project's PR workflow.

Gates ``git commit`` / ``git push`` (to an open PR) / ``gh pr create`` against
the per-repo rules in ``<repo-root>/.claude/pr.json``, returning allow / ask /
deny. Fails OPEN on internal errors and unreachable lookups; fails CLOSED on
the intentional checks. Config schema, behaviour, and design notes live in the
project README — ``normalize_config`` is the single source for the keys.
"""
import json
import os
import re
import shlex
import subprocess
import sys


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def ask(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def allow():
    sys.exit(0)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", root, *args],
        capture_output=True, text=True, timeout=90,
    )


AI_PATTERNS = [
    re.compile(r"co-authored-by:\s*claude", re.I),
    re.compile(r"generated with .{0,25}claude", re.I),
    re.compile(r"claude\s+code", re.I),
]


def has_ai_trailer(text):
    if not text:
        return False
    if "🤖" in text and "claude" in text.lower():
        return True
    return any(p.search(text) for p in AI_PATTERNS)


# flags whose value is an inline string we should scan, and file-valued flags
STR_FLAGS = {"-m", "--message", "-b", "--body", "-t", "--title"}
FILE_FLAGS = {"-F", "--file", "--body-file"}


def collect_message_text(tokens, workdir):
    """Pull title/body/message text out of a git/gh command's tokens."""
    parts = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" in tok and tok.startswith("--"):
            flag, val = tok.split("=", 1)
            if flag in STR_FLAGS:
                parts.append(val)
            elif flag in FILE_FLAGS:
                parts.append(read_file(workdir, val))
            i += 1
            continue
        if tok in STR_FLAGS and i + 1 < len(tokens):
            parts.append(tokens[i + 1])
            i += 2
            continue
        if tok in FILE_FLAGS and i + 1 < len(tokens):
            parts.append(read_file(workdir, tokens[i + 1]))
            i += 2
            continue
        i += 1
    return "\n".join(p for p in parts if p)


def read_file(workdir, path):
    try:
        p = path if os.path.isabs(path) else os.path.join(workdir, path)
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


SHELL_OPS = {"&&", "||", ";", "|", "|&", "&"}


def _segments(tokens):
    """Split a token list into command segments on top-level shell operators."""
    seg, out = [], []
    for t in tokens:
        if t in SHELL_OPS:
            if seg:
                out.append(seg)
                seg = []
        else:
            seg.append(t)
    if seg:
        out.append(seg)
    return out


def _strip_env(seg):
    """Drop leading ``VAR=value`` assignments (e.g. ``PR_CONFIRM_ACK=1 git ...``)."""
    i = 0
    while i < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]):
        i += 1
    return seg[i:]


def _git_subcommand(seg):
    """First non-option token after ``git``, skipping ``-C dir`` / ``-c k=v``."""
    i = 1
    while i < len(seg):
        t = seg[i]
        if t in ("-C", "-c"):
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return None


def classify_command(cmd):
    """Parse ``cmd`` and inspect the real argv of each segment.

    Returns ``(is_commit, is_pr_create, is_push)``. A git verb inside a quoted
    string, an ``echo`` argument, or a heredoc body is NOT the leading command
    of its segment, so it no longer false-matches. Falls back to whole-string
    regex on a shlex parse failure (fail toward enforcement, never under it).
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return (
            re.search(r"\bgit\s+commit\b", cmd) is not None,
            re.search(r"\bgh\s+pr\s+create\b", cmd) is not None,
            re.search(r"\bgit\s+push\b", cmd) is not None,
        )

    is_commit = is_pr = is_push = False
    for seg in _segments(tokens):
        seg = _strip_env(seg)
        if not seg:
            continue
        cmd0 = os.path.basename(seg[0])
        if cmd0 == "git":
            sub = _git_subcommand(seg)
            if sub == "commit":
                is_commit = True
            elif sub == "push":
                is_push = True
        elif cmd0 == "gh":
            rest = [t for t in seg[1:] if not t.startswith("-")]
            if rest[:2] == ["pr", "create"]:
                is_pr = True
    return is_commit, is_pr, is_push


def find_workdir(cmd, payload_cwd):
    """Honour a leading `cd <dir> && ...` (the form git -C rewrites into)."""
    m = re.match(r"\s*cd\s+('[^']*'|\"[^\"]*\"|[^\s&;|]+)\s*&&", cmd)
    if m:
        raw = m.group(1)
        if raw and raw[0] in "'\"":
            raw = raw[1:-1]
        raw = os.path.expanduser(raw)
        if os.path.isdir(raw):
            return raw
    return payload_cwd or os.getcwd()


def repo_root(workdir):
    r = git(workdir, "rev-parse", "--show-toplevel")
    return r.stdout.strip() if r.returncode == 0 else None


def current_branch(root):
    r = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def gh_pr_state(root, branch):
    """Return the PR state for ``branch`` (OPEN/CLOSED/MERGED), or None.

    None means "can't tell" — no PR, gh missing, offline, or unauthed — and
    the caller treats that as fail-open (do nothing).
    """
    try:
        r = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "state", "-q", ".state"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def load_config(root):
    path = os.path.join(root, ".claude", "pr.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_config(cfg):
    """Collapse the friendly flat schema (and the legacy schema) into one
    canonical settings dict. A check is ON simply because its config is
    present — no separate ``checks`` toggle map, no implicit defaults to
    reason about. The legacy keys (``checks``, ``version_files``) are still
    honoured so existing repos keep working unchanged.

    Friendly schema:
      main_branch          str   default "master"
      block_ai_trailer     bool  default true
      refresh_branch       bool  default true
      require_version_bump [str] files that must change; omit/[] = off
      lint                 str   lint command; omit/null = off
      confirm              [str] judgment-call prompts (hard-gated)
      bump_hint            str   pointer to the fix, printed in the report
      verbose              bool  default true
    """
    legacy = cfg.get("checks") or {}

    def legacy_on(name):
        return legacy.get(name, True) is not False

    ai = cfg.get("block_ai_trailer")
    if ai is None:
        ai = legacy_on("ai_trailer")

    branch = cfg.get("refresh_branch")
    if branch is None:
        branch = legacy_on("branch_stale")

    vfiles = cfg.get("require_version_bump")
    if vfiles is None:
        vfiles = cfg.get("version_files") or []
        if not legacy_on("version_bump"):
            vfiles = []

    lint_cmd = cfg.get("lint") or ""
    if not legacy_on("lint"):
        lint_cmd = ""

    # on a clean, acknowledged run: ask the human (true) or let the agent
    # proceed silently (false). The clean-pass report is read by no one (it's
    # not injected into the agent context and the user doesn't see it), so
    # this is purely an ask-vs-proceed switch. Legacy alias: "verbose".
    ask_on_pass = cfg.get("ask_on_pass")
    if ask_on_pass is None:
        ask_on_pass = cfg.get("verbose", True)

    return {
        "main_branch": cfg.get("main_branch") or "master",
        "ai_trailer": bool(ai),
        "branch_stale": bool(branch),
        "version_files": list(vfiles),
        "lint_cmd": lint_cmd,
        "confirm": cfg.get("confirm") or [],
        "bump_hint": cfg.get("bump_hint"),
        "ask_on_pass": ask_on_pass is not False,
    }


PASS, FAIL, OFF, SKIP = "✔", "✗", "⊘", "⚠"

_VERSION_RE = re.compile(
    r'(?:^|[^\w])_*version_*["\']?\s*[=:]\s*["\']?v?(\d+\.\d+[\w.\-]*)', re.I | re.M)


def _version_token(text):
    """First version value in a file (pyproject ``version =``, package.json
    ``"version":``, python ``__version__ =``). None if no version line."""
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def version_bumped(root, main, vfiles):
    """Compare the version VALUE (not just the file) across origin/main..HEAD.

    Returns ``(bumped, readable)``: ``bumped`` lists ``"file old->new"`` for
    files whose version token actually changed; ``readable`` is True if at
    least one file was readable on both sides. An unrelated edit to a version
    file (e.g. a dependency change) does NOT count — only a changed version.
    """
    bumped, readable = [], False
    for vf in vfiles:
        old = git(root, "show", "origin/{}:{}".format(main, vf))
        new = git(root, "show", "HEAD:{}".format(vf))
        if old.returncode != 0 or new.returncode != 0:
            continue
        readable = True
        ov, nv = _version_token(old.stdout), _version_token(new.stdout)
        if ov and nv and ov != nv:
            bumped.append("{} {}->{}".format(vf, ov, nv))
    return bumped, readable


def check_pr(root, cfg, tokens, workdir):
    """Run every configured check, accumulate a per-step report, and ALWAYS
    surface it. ``cfg`` is the normalized settings dict (see
    ``normalize_config``); a step is ON when its config is present, else it is
    reported OFF — never silently skipped. Failures hard-deny (with the full
    report); otherwise the report is returned as an ``ask`` so the active/off
    settings (and the ``confirm`` prompts, e.g. a dependency-bump question) are
    loud on every PR / push.
    """
    main = cfg["main_branch"]

    report = []  # (symbol, name, detail)
    failures = []

    # ai-trailer
    if cfg["ai_trailer"]:
        if has_ai_trailer(collect_message_text(tokens, workdir)):
            report.append((FAIL, "ai-trailer", "AI/Claude trailer in title/body — remove it"))
            failures.append("ai-trailer")
        else:
            report.append((PASS, "ai-trailer", "no AI/Claude trailer"))
    else:
        report.append((OFF, "ai-trailer", "OFF (not configured)"))

    if cfg["branch_stale"] or cfg["version_files"]:
        git(root, "fetch", "origin", main)  # refresh remote view once

    # branch-stale
    if cfg["branch_stale"]:
        rev = git(root, "rev-list", "--count", "HEAD..origin/{}".format(main))
        n = rev.stdout.strip()
        if rev.returncode != 0:
            report.append((SKIP, "branch-stale", "SKIP — can't reach origin/{}".format(main)))
        elif n.isdigit() and int(n) > 0:
            report.append((FAIL, "branch-stale", "{} commit(s) behind origin/{} — rebase/merge first".format(n, main)))
            failures.append("branch-stale")
        else:
            report.append((PASS, "branch-stale", "up to date with origin/{}".format(main)))
    else:
        report.append((OFF, "branch-stale", "OFF (not configured)"))

    # version-bump — the version VALUE must change, not merely the file
    if cfg["version_files"]:
        bumped, readable = version_bumped(root, main, cfg["version_files"])
        if not readable:
            report.append((SKIP, "version-bump", "SKIP — can't read version files vs origin/{}".format(main)))
        elif bumped:
            report.append((PASS, "version-bump", "bumped ({})".format(", ".join(bumped))))
        else:
            report.append((FAIL, "version-bump", "version unchanged in {} vs origin/{} — bump the value, not just the file".format(cfg["version_files"], main)))
            failures.append("version-bump")
    else:
        report.append((OFF, "version-bump", "OFF (not configured)"))

    # lint
    if cfg["lint_cmd"]:
        lint = cfg["lint_cmd"]
        r = subprocess.run(lint, shell=True, cwd=root, capture_output=True, text=True, timeout=180)
        if r.returncode == 127:
            report.append((FAIL, "lint", "`{}` not found (127) — fix the lint command in pr.json".format(lint)))
            failures.append("lint")
        elif r.returncode != 0:
            tail = "\n      ".join((r.stdout + r.stderr).strip().splitlines()[-12:])
            report.append((FAIL, "lint", "`{}` reported issues:\n      {}".format(lint, tail)))
            failures.append("lint")
        else:
            report.append((PASS, "lint", "clean (`{}`)".format(lint)))
    else:
        report.append((OFF, "lint", "OFF (not configured)"))

    # ---- loud report ----
    lines = ["PR checks — {} (vs origin/{}):".format(os.path.basename(root), main)]
    lines += ["  {} {}: {}".format(s, n, d) for (s, n, d) in report]
    not_enforced = [n for (s, n, d) in report if s in (OFF, SKIP)]
    if not_enforced:
        lines.append("  ⚑ NOT ENFORCED: " + ", ".join(not_enforced))
    # optional per-repo pointer to the canonical fix (e.g. a bump skill). Kept
    # in pr.json so this global hook stays repo-agnostic.
    hint = cfg["bump_hint"]
    if hint:
        lines.append("  → " + hint)
    body = "\n".join(lines)

    if failures:
        deny(body + "\n\nBLOCKED — failed: " + ", ".join(failures))

    # confirm prompts (e.g. a dependency bump) are judgment calls that MUST be
    # answered. They are a hard gate, not a soft `ask`: a permission `ask`
    # only gates the command and gets cleared by the session's auto-accept
    # without the question ever being answered. So deny until the SAME command
    # is re-run with an explicit PR_CONFIRM_ACK=1 prefix — that re-run only
    # happens after the question has actually been put to the user.
    prompts = cfg["confirm"]
    acked = any(re.match(r"PR_CONFIRM_ACK=\S", t) for t in tokens)
    if prompts and not acked:
        deny(body
             + "\n\nMUST CONFIRM — blocked until acknowledged:\n- "
             + "\n- ".join(prompts)
             + "\n\nPut these to the user and get an explicit answer. Once "
               "addressed (bump made if needed), re-run the SAME command "
               "prefixed with `PR_CONFIRM_ACK=1` to record it.")

    # no failures; confirms acknowledged (or none). ask_on_pass (default ON)
    # gates on a human ask; false lets the agent proceed silently.
    if not cfg["ask_on_pass"]:
        allow()
    ask(body + "\n\nProceed?")


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        allow()

    if data.get("tool_name") != "Bash":
        allow()

    cmd = (data.get("tool_input") or {}).get("command", "")
    if not cmd.strip():
        allow()

    is_commit, is_pr, is_push = classify_command(cmd)
    if not (is_commit or is_pr or is_push):
        allow()

    try:
        workdir = find_workdir(cmd, data.get("cwd"))
        root = repo_root(workdir)
        if not root:
            allow()  # not a git repo — nothing to enforce

        raw_cfg = load_config(root)
        if raw_cfg is None:
            deny("No {}/.claude/pr.json — declare this repo's PR tooling "
                 "(main_branch, lint, require_version_bump) before committing "
                 "or opening a PR. Hard-blocked by the PR-workflow hook."
                 .format(os.path.basename(root)))
        cfg = normalize_config(raw_cfg)

        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()

        if is_commit:
            text = collect_message_text(tokens, workdir)
            if has_ai_trailer(text):
                deny("Commit message contains an AI/Claude trailer — "
                     "project rule forbids crediting the AI. Remove it.")

        # A commit chained with a push / PR-create in one command can't be
        # gated reliably: at PreToolUse the new commit isn't applied yet, so
        # the push/PR checks would read the pre-commit HEAD — and the old
        # is_commit short-circuit skipped them entirely. Force separation.
        if is_commit and (is_push or is_pr):
            deny("Don't chain `git commit` with `git push` / `gh pr create` "
                 "in one command — the commit isn't applied yet when this "
                 "hook runs, so the PR gate can't see it (version/branch "
                 "checks read stale state). Commit first, then push / create "
                 "as a separate command.")

        if is_commit:
            allow()  # commit-only: trailer already cleared above

        if is_push:
            branch = current_branch(root)
            if not branch or branch == "HEAD":
                allow()  # detached/unknown — nothing to enforce
            if gh_pr_state(root, branch) != "OPEN":
                allow()  # no open PR (or can't tell) — fail open, do nothing
            check_pr(root, cfg, tokens, workdir)
            allow()

        # is_pr
        check_pr(root, cfg, tokens, workdir)
        allow()
    except Exception:
        # never brick git on an internal hook error
        allow()


if __name__ == "__main__":
    main()
