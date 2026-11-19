#!/usr/bin/env python3
"""Check blueprint commands against the *installed* CLIs' own --help text.

This is how a blueprint earns ``verification_status: syntax-checked``:
every gcloud / firebase / clasp subcommand used must exist, and every
``--flag`` must appear in that subcommand's help (or the tool's global
flags) for the versions recorded in ``verified_versions``.

It makes no network calls and changes nothing: it only runs ``<tool> ... --help``
and ``<tool> --version``. It does NOT prove a blueprint works end to end;
only a run against a real throwaway project (status ``live``) does that.

Usage:
    python scripts/check_cli_syntax.py [--blueprints-dir blueprints] [--json]
Exit code 0 = all checked commands passed, 1 = problems found, 2 = a tool is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from doogle import engine as E  # noqa: E402

CHECKED_TOOLS = ("gcloud", "firebase", "clasp")
ENV = dict(os.environ, CLOUDSDK_CORE_DISABLE_PROMPTS="1", NO_COLOR="1", FORCE_COLOR="0")


@lru_cache(maxsize=None)
def help_text(argv: tuple[str, ...]) -> tuple[int, str]:
    try:
        proc = subprocess.run([*argv, "--help"], capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL, env=ENV)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tool_version(tool: str) -> str:
    try:
        out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL, env=ENV).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    first = out.strip().splitlines()[0] if out.strip() else "?"
    return first.replace("Google Cloud SDK ", "").strip()


def command_path(tokens: list[str]) -> list[str]:
    path = []
    for tok in tokens[1:]:
        if tok.startswith("-") or "{{" in tok:
            break
        path.append(tok)
    return path


def resolve_help(tool: str, path: list[str]) -> tuple[list[str], str] | None:
    for n in range(len(path), -1, -1):
        rc, text = help_text((tool, *path[:n]))
        if rc == 0 and text.strip():
            if n == 0 and path:
                return None  # not even the first subcommand exists
            return path[:n], text
    return None


def flag_ok(flag: str, text: str, global_text: str) -> bool:
    name = flag.split("=", 1)[0]
    candidates = {name}
    if name.startswith("--no-"):
        base = name[5:]
        candidates |= {f"--[no-]{base}", f"--{base}"}
    haystack = text + "\n" + global_text
    return any(c in haystack for c in candidates)


def check(blueprints_dir: str | None) -> dict:
    root, _ = E.resolve_blueprints_dir(blueprints_dir)
    catalog = E.Catalog(root)
    report = {"tools": {}, "blueprints": {}, "invalid": catalog.errors}
    for tool in CHECKED_TOOLS:
        report["tools"][tool] = tool_version(tool) if shutil.which(tool) else None
    for bp in catalog:
        problems, checked = [], 0
        cmds = []
        for s in bp.steps:
            cmds += [(s.id, c) for c in (s.command, s.verify_command, s.skip_if) if c]
        cmds += [(t.id, t.command) for t in bp.teardown if t.command]
        for where, cmd in cmds:
            toks = shlex.split(cmd)
            tool = toks[0]
            if tool not in CHECKED_TOOLS:
                continue
            if not report["tools"].get(tool):
                problems.append(f"{where}: {tool} not installed, cannot check")
                continue
            checked += 1
            path = command_path(toks)
            resolved = resolve_help(tool, path)
            if resolved is None:
                problems.append(f"{where}: '{tool} {' '.join(path)}' is not a known command")
                continue
            used_path, text = resolved
            if len(used_path) < len(path) and re.fullmatch(r"[a-z][a-z0-9-]*", path[len(used_path)]):
                problems.append(f"{where}: '{tool} {' '.join(path[:len(used_path) + 1])}' is not a known command")
                continue
            _, global_text = help_text((tool,))
            for tok in toks:
                if tok.startswith("--") and not flag_ok(tok, text, global_text):
                    problems.append(f"{where}: flag {tok.split('=', 1)[0]} not found in "
                                    f"'{tool} {' '.join(used_path)} --help'")
        report["blueprints"][bp.id] = {"commands_checked": checked, "problems": problems,
                                       "declared": bp.verified_versions,
                                       "status": bp.verification_status}
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blueprints-dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = check(args.blueprints_dir)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Installed CLI versions: " + ", ".join(f"{k}={v or 'missing'}" for k, v in report["tools"].items()))
        for bp_id, r in report["blueprints"].items():
            state = "ok" if not r["problems"] else "PROBLEMS"
            print(f"\n{bp_id}: {state} ({r['commands_checked']} commands checked; declared {r['status']})")
            for p in r["problems"]:
                print(f"  - {p}")
    if any(not v for v in report["tools"].values()) and any(
            "not installed" in p for r in report["blueprints"].values() for p in r["problems"]):
        return 2
    return 1 if any(r["problems"] for r in report["blueprints"].values()) or report["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
