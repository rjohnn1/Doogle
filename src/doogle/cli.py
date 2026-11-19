"""Doogle command-line interface.

Commands: list, explain, plan (dry-run; --apply to execute), resume, teardown,
history, ask, doctor, validate. ``--json`` gives machine-readable output on
list / explain / plan / ask / doctor / history / validate / teardown.

Exit codes: 0 ok, 1 runtime failure, 2 usage/validation error,
3 blocked by a safety gate (nothing was run).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

from . import __version__
from . import docs as D
from . import engine as E

RISK_BADGE = {"low": "low", "medium": "MEDIUM", "high": "HIGH"}


# --------------------------------------------------------------------------- helpers


class Out:
    def __init__(self, as_json: bool):
        self.as_json = as_json

    def json(self, payload: Any) -> None:
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")

    def line(self, text: str = "") -> None:
        if not self.as_json:
            sys.stdout.write(text + "\n")

    def err(self, text: str) -> None:
        sys.stdout.flush()
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_factory(expected: str):
    def confirm(summary: str) -> bool:
        sys.stderr.write(f"\n{summary}\nType '{expected}' to continue (anything else aborts): ")
        sys.stderr.flush()
        try:
            return input().strip() == expected
        except EOFError:
            return False
    return confirm


def _confirm_manual(step: dict) -> bool:
    sys.stderr.write(f"\nMANUAL STEP '{step['id']}':\n  {step['description']}\n"
                     "Type 'done' when finished, or press Enter to pause the run: ")
    sys.stderr.flush()
    try:
        return input().strip().lower() == "done"
    except EOFError:
        return False


def _parse_vars(pairs: Sequence[str], vars_file: Optional[str]) -> dict:
    values: dict = {}
    if vars_file:
        path = Path(vars_file)
        if not path.is_file():
            raise E.VariableError(f"--vars-file '{path}' not found")
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        if not isinstance(data, dict):
            raise E.VariableError("--vars-file must contain a mapping of key: value")
        values.update({str(k): None if v is None else str(v) for k, v in data.items()})
    for pair in pairs or []:
        if "=" not in pair:
            raise E.VariableError(f"--var expects key=value, got '{pair}'")
        key, value = pair.split("=", 1)
        values[key.strip()] = value
    return values


def _prompt_missing(bp: E.Blueprint, values: dict) -> dict:
    _, missing = E.resolve_variables(bp, values)
    for var in missing:
        hint = f" (e.g. {var.example})" if var.example else ""
        sys.stderr.write(f"{var.prompt}{hint}: ")
        sys.stderr.flush()
        values[var.key] = input().strip()
    return values


def _print_plan(out: Out, plan: E.Plan) -> None:
    bp = plan.blueprint
    out.line(f"DRY-RUN PLAN: {bp.name} ({bp.id})")
    out.line("Nothing below has been executed. Review it, then re-run with --apply.\n")
    out.line("Variables:")
    for k, v in plan.variables.items():
        out.line(f"  {k} = {v}")
    fresh = E.freshness(bp)
    out.line(f"\nVerification: {_fresh_label(fresh)}")
    if bp.prerequisites:
        out.line("\nPrerequisites:")
        for p in bp.prerequisites:
            out.line(f"  - {p}")
    out.line("\nSteps:")
    for s in plan.steps:
        flags = [f"risk={RISK_BADGE[s.risk]}"]
        if s.public_exposure:
            flags.append("PUBLIC")
        if s.manual:
            flags.append("MANUAL")
        out.line(f"  {s.index}. [{', '.join(flags)}] {s.id}: {s.description}")
        if s.command:
            out.line(f"       $ {s.command}")
        if s.skip_argv:
            sk = f"   (expect: {s.skip_if_expect})" if s.skip_if_expect else ""
            out.line(f"       skip if already done: {E.display(s.skip_argv)}{sk}")
        if s.verify_command:
            exp = f"   (expect output to contain: {s.verify_expect})" if s.verify_expect else ""
            out.line(f"       verify: {s.verify_command}{exp}")
    out.line("\nTeardown (run later with: doogle teardown <run-id>):")
    for t in plan.teardown:
        tag = "MANUAL" if t.manual else f"risk={RISK_BADGE[t.risk]}"
        out.line(f"  - [{tag}] {t.id}: {t.description}")
        if t.command:
            out.line(f"       $ {t.command}")
    out.line("\nTools:")
    for tool, path in plan.tools.items():
        out.line(f"  {tool}: {path or 'NOT FOUND'}")
    out.line("\nWarnings:")
    for w in plan.warnings():
        out.line(f"  ! {w}")


def _print_run(out: Out, run: dict) -> None:
    out.line(f"\nRun {run['run_id']}: {run['status'].upper()}")
    for s in run["steps"]:
        extra = f" (exit {s['exit_code']})" if s.get("exit_code") not in (None, 0) else ""
        verify = f", verify {s['verify_status']}" if s.get("verify_status") not in (None, "not_defined") else ""
        already = " (already done, skipped)" if s.get("skip_reason") else ""
        out.line(f"  {s['status']:<16} {s['id']}{extra}{verify}{already}")
        if s["status"] in ("failed", "verify_failed") and s.get("output_tail"):
            for ln in s["output_tail"].splitlines()[-8:]:
                out.line(f"      | {ln}")
    hr = " --approve-high-risk" if any(s["risk"] == "high" and s["status"] != "succeeded"
                                      for s in run["steps"]) else ""
    if run["status"] == "paused":
        out.line(f"\nPaused at a manual step. When it is done: doogle resume {run['run_id']}{hr}")
    elif run["status"] == "failed":
        out.line(f"\nFix the cause, then: doogle resume {run['run_id']}{hr}  "
                 f"(or clean up with: doogle teardown {run['run_id']})")
    else:
        out.line(f"\nClean up later with: doogle teardown {run['run_id']}")


def _event_printer(out: Out):
    def on_event(kind: str, payload: dict) -> None:
        if out.as_json:
            return
        if kind == "step_started":
            out.err(f"-> {payload['id']}" + (f": {payload['command']}" if payload.get("command") else " (manual)"))
        elif kind == "step_finished":
            out.err(f"   {payload['status']}")
        elif kind == "teardown_started":
            out.err(f"-> teardown {payload['id']}: {payload['command']}")
        elif kind == "teardown_finished":
            out.err(f"   {payload['status']}")
    return on_event


def _gates(args, expected: str, out: Out) -> E.Gates:
    confirm = None
    if not args.yes and not out.as_json and _interactive():
        confirm = _confirm_factory(expected)
    return E.Gates(yes=args.yes, approve_high_risk=args.approve_high_risk, confirm=confirm)


# --------------------------------------------------------------------------- commands


def _fresh_label(f: dict) -> str:
    age = "never" if f["age_days"] is None else f"{f['age_days']}d ago"
    flag = "  STALE" if f["stale"] else ""
    return f"{f['verification_status']}, {f['last_verified']} ({age}){flag}"


def cmd_list(args, out: Out) -> int:
    cat = E.Catalog.load(args.blueprints_dir)
    items = []
    for b in cat:
        if args.category and b.category.lower() != args.category.lower():
            continue
        fresh = E.freshness(b, stale_days=args.stale_days)
        if args.stale and not fresh["stale"]:
            continue
        items.append({"id": b.id, "name": b.name, "category": b.category, "tagline": b.tagline,
                      "required_tools": b.required_tools(), "maintainer": b.maintainer, "freshness": fresh})
    stale = [i["id"] for i in items if i["freshness"]["stale"]]
    if out.as_json:
        out.json({"blueprints_dir": str(cat.root), "source": cat.source, "stale_after_days": args.stale_days,
                  "blueprints": items, "stale": stale, "invalid": cat.errors})
        return 0 if not cat.errors else 2
    title = f"Stale blueprints (last verified more than {args.stale_days} days ago)" if args.stale else "Blueprints"
    out.line(f"{title} ({cat.root}, from {cat.source}):\n")
    if not items:
        out.line("  (none)")
    width = max([len(i["id"]) for i in items] + [10])
    for i in items:
        out.line(f"  {i['id']:<{width}}  [{i['category']}] {i['tagline']}")
        out.line(f"  {'':<{width}}  verified: {_fresh_label(i['freshness'])}")
    for bp_id, errs in cat.errors.items():
        out.err(f"  INVALID {bp_id}: {'; '.join(errs)}")
    if stale and not args.stale:
        out.err(f"\nwarning: {len(stale)} blueprint(s) not verified in {args.stale_days} days: {', '.join(stale)}. "
                "Commands may have drifted; check them against current docs before applying.")
    out.line("\nNext: doogle explain <id>   |   doogle ask \"<your goal>\"")
    return 0 if not cat.errors else 2


def cmd_explain(args, out: Out) -> int:
    cat = E.Catalog.load(args.blueprints_dir)
    bp = cat.get(args.blueprint)
    if out.as_json:
        data = bp.to_dict()
        if args.guide and bp.guide_path:
            data["guide_markdown"] = bp.guide_path.read_text(encoding="utf-8")
        out.json(data)
        return 0
    if args.guide:
        out.line(bp.guide_path.read_text(encoding="utf-8") if bp.guide_path else "(no GUIDE.md)")
        return 0
    out.line(f"{bp.name}  ({bp.id})\n[{bp.category}] {bp.tagline}\n")
    out.line("Use it when:")
    for w in bp.when_to_use:
        out.line(f"  + {w}")
    out.line("\nDo NOT use it when (consider these first):")
    for w in bp.when_not_to_use:
        out.line(f"  - {w}")
    out.line("\nGoogle services involved:")
    for s in bp.services:
        out.line(f"  * {s['name']}: {s['role']}")
    out.line(f"\nCost: {bp.estimated_cost}")
    fresh = E.freshness(bp)
    out.line(f"\nFreshness: {_fresh_label(fresh)}; maintainer {bp.maintainer}")
    out.line("  versions: " + ", ".join(f"{k} {v}" for k, v in sorted(bp.verified_versions.items())))
    if bp.verification_notes:
        out.line(f"  notes: {bp.verification_notes}")
    if fresh["stale"]:
        out.err(f"warning: '{bp.id}' was last verified {fresh['age_days']} days ago (> {fresh['stale_after_days']}); "
                "CLI flags may have changed. Compare with the official docs before --apply.")
    if bp.prerequisites:
        out.line("\nPrerequisites:")
        for p in bp.prerequisites:
            out.line(f"  - {p}")
    out.line("\nVariables:")
    for v in bp.variables:
        d = f" [default: {v.default}]" if v.default is not None else " [required]"
        out.line(f"  {v.key}{d}: {v.prompt}")
    out.line(f"\nSteps: {len(bp.steps)} ({sum(s.risk == 'high' for s in bp.steps)} high-risk, "
             f"{sum(s.public_exposure for s in bp.steps)} public)   Teardown entries: {len(bp.teardown)}")
    out.line(f"Tools: {', '.join(bp.required_tools())}")
    if bp.docs:
        out.line("\nOfficial docs:")
        for d in bp.docs:
            out.line(f"  {d}")
    out.line(f"\nGuide: {bp.guide_path}  (doogle explain {bp.id} --guide)")
    out.line(f"Next:  doogle plan {bp.id} " + " ".join(f"--var {v.key}=..." for v in bp.variables if v.default is None))
    return 0


def cmd_plan(args, out: Out) -> int:
    cat = E.Catalog.load(args.blueprints_dir)
    bp = cat.get(args.blueprint)
    values = _parse_vars(args.var, args.vars_file)
    if args.apply and not out.as_json and _interactive() and not args.yes:
        values = _prompt_missing(bp, values)
    plan = E.build_plan(bp, values)
    if not args.apply:
        if out.as_json:
            out.json(plan.to_dict())
        else:
            _print_plan(out, plan)
            hr = " --approve-high-risk" if plan.high_risk_steps else ""
            out.line(f"\nTo execute: doogle plan {bp.id} " + " ".join(
                f"--var {k}={E.shlex.quote(v)}" for k, v in plan.variables.items()) + f" --apply{hr}")
        return 0
    if not out.as_json:
        _print_plan(out, plan)
    store = E.StateStore(E.resolve_state_path(args.state_dir))
    manual = _confirm_manual if (not out.as_json and _interactive()) else None
    ex = E.Executor(store, on_event=_event_printer(out), confirm_manual=manual)
    run = ex.apply(plan, _gates(args, bp.id, out))
    if out.as_json:
        out.json(run)
    else:
        _print_run(out, run)
    return 0 if run["status"] in ("succeeded", "paused") else 1


def cmd_resume(args, out: Out) -> int:
    store = E.StateStore(E.resolve_state_path(args.state_dir))
    run = store.get(args.run_id)
    cat = E.Catalog.load(args.blueprints_dir)
    bp = cat.get(run["blueprint_id"])
    plan = E.build_plan(bp, run["variables"])
    manual = _confirm_manual if (not out.as_json and _interactive()) else None
    ex = E.Executor(store, on_event=_event_printer(out), confirm_manual=manual)
    run = ex.resume(run, plan, _gates(args, "resume", out))
    if out.as_json:
        out.json(run)
    else:
        _print_run(out, run)
    return 0 if run["status"] in ("succeeded", "paused") else 1


def cmd_teardown(args, out: Out) -> int:
    store = E.StateStore(E.resolve_state_path(args.state_dir))
    run = store.get(args.run_id)
    if not args.apply:
        if out.as_json:
            out.json({"run_id": run["run_id"], "mode": "dry-run", "teardown": run["teardown"]})
            return 0
        out.line(f"TEARDOWN DRY-RUN for {run['run_id']} ({run['blueprint_id']}, status {run['status']})")
        out.line("Nothing has been executed.\n")
        for t in run["teardown"]:
            tag = "MANUAL" if not t.get("argv") else f"risk={RISK_BADGE[t['risk']]}"
            out.line(f"  - [{tag}] {t['id']}: {t['description']}")
            if t.get("command"):
                out.line(f"       $ {t['command']}")
        out.line(f"\nTo execute: doogle teardown {run['run_id']} --apply --approve-high-risk")
        return 0
    ex = E.Executor(store, on_event=_event_printer(out))
    run = ex.teardown(run, _gates(args, "teardown", out))
    if out.as_json:
        out.json(run)
    else:
        out.line(f"Teardown {run['teardown_status'].upper()} for {run['run_id']}")
        for t in run["teardown"]:
            out.line(f"  {t['status']:<16} {t['id']}")
            if t["status"] == "manual_required":
                out.line(f"      -> {t['description']}")
    return 0 if run["teardown_status"] == "succeeded" else 1


def cmd_history(args, out: Out) -> int:
    store = E.StateStore(E.resolve_state_path(args.state_dir))
    if args.run_id:
        run = store.get(args.run_id)
        if out.as_json:
            out.json(run)
        else:
            out.line(f"{run['run_id']}  blueprint={run['blueprint_id']}  status={run['status']}  "
                     f"teardown={run.get('teardown_status')}")
            out.line(f"started {run['started_at']}  finished {run.get('finished_at')}")
            out.line("variables: " + ", ".join(f"{k}={v}" for k, v in run["variables"].items()))
            _print_run(out, run)
        return 0
    runs = store.runs()
    if out.as_json:
        out.json({"state_file": str(store.path), "runs": [
            {k: r.get(k) for k in ("run_id", "blueprint_id", "status", "teardown_status", "started_at", "finished_at")}
            for r in runs]})
        return 0
    if not runs:
        out.line(f"No runs recorded yet ({store.path}).")
        return 0
    out.line(f"Runs in {store.path}:\n")
    for r in runs:
        done = sum(s["status"] == "succeeded" for s in r["steps"])
        out.line(f"  {r['run_id']}  {r['status']:<10} steps {done}/{len(r['steps'])}  "
                 f"teardown={r.get('teardown_status')}  started {r['started_at']}")
    return 0


def cmd_ask(args, out: Out) -> int:
    cat = E.Catalog.load(args.blueprints_dir)
    goal = " ".join(args.goal)
    result = E.route(goal, cat, use_ai=args.ai, top=args.top)
    if out.as_json:
        out.json(result)
        return 0
    if result.get("ai_note"):
        out.err(result["ai_note"])
    if not result["recommendation"]:
        out.line("No blueprint is a clear fit for that goal.")
        if result.get("ai_reason"):
            out.line(f"AI note: {result['ai_reason']}")
        out.line("Try: doogle list, or describe it with product words (API, agent, spreadsheet, webhook).")
        return 0
    rec = cat.get(result["recommendation"])
    out.line(f"Recommended: {rec.id}  ({rec.name})  [{result['mode']}, confidence: {result['confidence']}]")
    out.line(f"  {rec.tagline}")
    if result.get("ai_reason"):
        out.line(f"  Why (AI): {result['ai_reason']}")
        if result.get("ai_alternative"):
            out.line(f"  Consider also: {result['ai_alternative']}")
    for m in result["matches"]:
        if m["blueprint_id"] == rec.id:
            out.line("  Why: " + "; ".join(m["reasons"]))
    out.line(f"  Watch out: {rec.when_not_to_use[0]}")
    others = [m for m in result["matches"] if m["blueprint_id"] != rec.id]
    if others:
        out.line("\nOther candidates:")
        for m in others:
            out.line(f"  {m['blueprint_id']} (score {m['score']}): {m['tagline']}")
    out.line("\nNext:")
    for n in result["next_steps"]:
        out.line(f"  {n}")
    return 0


def cmd_doctor(args, out: Out) -> int:
    report = E.doctor(args.blueprints_dir, args.state_dir)
    if out.as_json:
        out.json(report)
    else:
        icon = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}
        out.line("Doogle doctor (read-only; no cloud calls)\n")
        for c in report["checks"]:
            out.line(f"  [{icon[c['status']]}] {c['name']}: {c['detail']}")
            if c.get("hint"):
                out.line(f"         -> {c['hint']}")
        out.line(f"\nOverall: {report['status'].upper()}")
    return 1 if report["status"] == "fail" else 0


def cmd_docs(args, out: Out) -> int:
    cat = E.Catalog.load(args.blueprints_dir)
    if cat.errors:
        raise E.BlueprintValidationError(", ".join(cat.errors), [e for es in cat.errors.values() for e in es])
    root = Path(args.root).resolve()
    try:
        desired = D.generate(cat, root)
    except ValueError as exc:
        raise E.DoogleError(str(exc)) from exc
    if args.check:
        stale = D.stale_files(desired)
        if out.as_json:
            out.json({"up_to_date": not stale, "out_of_date": [str(p) for p in stale]})
        elif stale:
            out.err("generated docs are out of date: " + ", ".join(str(p.relative_to(root)) for p in stale)
                    + "\nrun: doogle docs   (then commit the result)")
        else:
            out.line("docs are up to date")
        return 1 if stale else 0
    changed = D.write(desired)
    if out.as_json:
        out.json({"written": [str(p) for p in changed], "unchanged": [str(p) for p in desired if p not in changed]})
    else:
        for p in desired:
            out.line(f"  {'updated ' if p in changed else 'unchanged'} {p.relative_to(root)}")
    return 0


def cmd_share(args, out: Out) -> int:
    cat = E.Catalog.load(args.blueprints_dir)
    bp = cat.get(args.blueprint)
    snippet = D.share_snippet(bp, D.repo_url(args.repo_url))
    if out.as_json:
        out.json(snippet)
        return 0
    out.line(snippet["markdown"])
    if snippet["placeholder_repo"]:
        out.err(f"note: links use the placeholder {D.PLACEHOLDER_REPO}; pass --repo-url or set ${D.REPO_URL_ENV}.")
    return 0


def cmd_validate(args, out: Out) -> int:
    root, source = E.resolve_blueprints_dir(args.blueprints_dir)
    cat = E.Catalog(root, source)
    if out.as_json:
        out.json({"blueprints_dir": str(root), "valid": sorted(cat.blueprints), "invalid": cat.errors})
    else:
        for bp_id in sorted(cat.blueprints):
            out.line(f"  ok       {bp_id}")
        for bp_id, errs in cat.errors.items():
            out.line(f"  INVALID  {bp_id}")
            for e in errs:
                out.line(f"           - {e}")
        out.line(f"\n{len(cat.blueprints)} valid, {len(cat.errors)} invalid in {root}")
    if not cat.blueprints and not cat.errors:
        out.err("no blueprints found")
        return 2
    return 0 if not cat.errors else 2


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--blueprints-dir", metavar="PATH",
                        help=f"blueprints folder (default: ${E.BLUEPRINTS_ENV} or the bundled set)")
    common.add_argument("--state-dir", metavar="PATH",
                        help=f"where state.json lives (default: ${E.STATE_DIR_ENV} or ./.doogle)")
    common.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")

    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--yes", "-y", action="store_true",
                      help="non-interactive approval of the reviewed plan (for scripts/agents)")
    gate.add_argument("--approve-high-risk", action="store_true",
                      help="also allow steps marked risk: high (IAM, public endpoints, deletes)")

    p = argparse.ArgumentParser(
        prog="doogle",
        description="Doogle - Do the Google Agent. Route a goal to a verifiable blueprint, "
                    "dry-run it, then apply with explicit approval. Independent community "
                    "project; not affiliated with Google LLC.",
        epilog="Safe loop: doogle ask \"<goal>\" -> doogle explain <id> -> doogle plan <id> "
               "-> doogle plan <id> --apply -> doogle teardown <run-id>",
    )
    p.add_argument("--version", action="version", version=f"doogle {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    s = sub.add_parser("list", parents=[common], help="list available blueprints and their freshness")
    s.add_argument("--category", help="filter by category")
    s.add_argument("--stale", action="store_true", help="only blueprints not verified recently")
    s.add_argument("--stale-days", type=int, default=E.STALE_AFTER_DAYS, metavar="N",
                   help=f"age in days after which a blueprint counts as stale (default {E.STALE_AFTER_DAYS})")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("explain", parents=[common], help="trade-offs, services, cost, and steps of a blueprint")
    s.add_argument("blueprint")
    s.add_argument("--guide", action="store_true", help="print the blueprint's GUIDE.md")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("plan", parents=[common, gate],
                       help="dry-run a blueprint (default) or execute it with --apply")
    s.add_argument("blueprint")
    s.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="set a variable (repeatable)")
    s.add_argument("--vars-file", metavar="FILE", help="YAML or JSON file of variables")
    s.add_argument("--apply", action="store_true", help="execute the plan (asks for confirmation)")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("resume", parents=[common, gate], help="continue a failed or paused run")
    s.add_argument("run_id", help="run id, unique prefix, or 'latest'")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("teardown", parents=[common, gate],
                       help="show (default) or run (--apply) the teardown recorded for a run")
    s.add_argument("run_id", help="run id, unique prefix, or 'latest'")
    s.add_argument("--apply", action="store_true", help="execute the teardown commands")
    s.set_defaults(func=cmd_teardown)

    s = sub.add_parser("history", parents=[common], help="list recorded runs, or show one run")
    s.add_argument("run_id", nargs="?", help="run id, unique prefix, or 'latest'")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("ask", parents=[common], help="match a plain-English goal to a blueprint")
    s.add_argument("goal", nargs="+")
    s.add_argument("--ai", action="store_true",
                   help="also ask Gemini (needs 'doogle[ai]' and GEMINI_API_KEY); falls back to offline")
    s.add_argument("--top", type=int, default=3, help="number of candidates to show")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("doctor", parents=[common], help="read-only environment diagnostics")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("docs", parents=[common],
                       help="regenerate docs/DECISION_MATRIX.md and the README gallery from blueprints")
    s.add_argument("--check", action="store_true", help="exit 1 if generated docs are out of date (CI)")
    s.add_argument("--root", default=".", help="repository root (default: current directory)")
    s.set_defaults(func=cmd_docs)

    s = sub.add_parser("share", parents=[common], help="print a markdown badge + blurb for a blueprint")
    s.add_argument("blueprint")
    s.add_argument("--repo-url", help=f"repository URL for links (default: ${D.REPO_URL_ENV} or a placeholder)")
    s.set_defaults(func=cmd_share)

    s = sub.add_parser("validate", parents=[common], help="strictly validate every blueprint (used by CI)")
    s.set_defaults(func=cmd_validate)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out(getattr(args, "json", False))
    try:
        return args.func(args, out)
    except E.DoogleError as exc:
        if out.as_json:
            out.json({"error": str(exc), "error_type": type(exc).__name__, "exit_code": exc.exit_code})
        else:
            out.err(f"doogle: error: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        out.err("\ninterrupted; state saved up to the last finished step (see: doogle history)")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
