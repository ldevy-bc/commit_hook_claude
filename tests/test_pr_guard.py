#!/usr/bin/env python3
"""Dependency-free tests for the pure logic of pr-guard.py.

Covers the two functions that decide everything: command recognition
(``classify_command``) and config normalization (``normalize_config``).
Run with: ``python3 tests/test_pr_guard.py`` (exit code 0 = all passed).
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "hooks", "pr-guard.py")
spec = importlib.util.spec_from_file_location("pr_guard", HOOK)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

failures = []


def check(name, got, want):
    if got != want:
        failures.append("%s\n    expected %r\n    got      %r" % (name, want, got))


# ---------------------------------------------------------------------------
# classify_command — parse the command, inspect real argv
# (verbs assembled at runtime so this test file can't confuse a reader/tool)
# ---------------------------------------------------------------------------
P = "git" + " push"
CM = "git" + " commit"
GH = "gh" + " pr create"

CASES = [
    (CM + " -m x",                          (True, False, False)),
    (P + " origin b",                       (False, False, True)),
    (GH + " --title x --body y",            (False, True, False)),
    (CM + " -m x && " + P,                  (True, False, True)),    # chained
    ("echo '" + P + "'",                    (False, False, False)),  # verb in a string
    ("python3 t.py '" + P + "'",            (False, False, False)),  # verb as arg
    ("PR_CONFIRM_ACK=1 " + P + " origin b", (False, False, True)),   # env prefix
    ("cd /repo && " + P,                    (False, False, True)),   # cd chain
    ("git -C /repo commit -m x",            (True, False, False)),   # global flag
    ("git -c user.name=x commit -m y",      (True, False, False)),   # -c k=v
    ("git status",                          (False, False, False)),
    ("git log --oneline | grep " + "push",  (False, False, False)),  # push as grep arg
    ('NAME="' + CM + '"',                   (False, False, False)),  # assignment only
    ("gh pr view b",                        (False, False, False)),  # not 'create'
    ("git push-mirror origin",              (False, False, False)),  # not the 'push' subcmd
]
for cmd, want in CASES:
    check("classify_command: %r" % cmd, g.classify_command(cmd), want)

# shlex parse failure → regex fallback (fail toward enforcement)
check("classify_command: unbalanced-quote fallback",
      g.classify_command(P + " 'unclosed"), (False, False, True))


# ---------------------------------------------------------------------------
# normalize_config — friendly flat schema + legacy compatibility
# ---------------------------------------------------------------------------
def enabled(cfg):
    n = g.normalize_config(cfg)
    return (n["ai_trailer"], n["branch_stale"], bool(n["version_files"]),
            bool(n["lint_cmd"]), n["ask_on_pass"])


# friendly schema
check("friendly: all configured",
      enabled({"lint": "x", "require_version_bump": ["v"]}),
      (True, True, True, True, True))
check("friendly: minimal (nothing) -> only the default-on bool checks",
      enabled({}),
      (True, True, False, False, True))
check("friendly: ask_on_pass false",
      g.normalize_config({"ask_on_pass": False})["ask_on_pass"], False)
check("friendly: block_ai_trailer false",
      g.normalize_config({"block_ai_trailer": False})["ai_trailer"], False)

# legacy schema still honored
check("legacy: standard repo (lint + version_files)",
      enabled({"lint": "x", "version_files": ["v"]}),
      (True, True, True, True, True))
check("legacy: checks map disables lint + version_bump",
      enabled({"lint": None, "version_files": [], "checks": {"lint": False, "version_bump": False}}),
      (True, True, False, False, True))
check("legacy: verbose alias maps to ask_on_pass",
      g.normalize_config({"verbose": False})["ask_on_pass"], False)
check("legacy: ask_on_pass overrides verbose",
      g.normalize_config({"ask_on_pass": True, "verbose": False})["ask_on_pass"], True)

# defaults
check("default main_branch", g.normalize_config({})["main_branch"], "master")


# ---------------------------------------------------------------------------
if failures:
    print("FAILED (%d):\n" % len(failures))
    print("\n\n".join(failures))
    sys.exit(1)
print("all tests passed")
