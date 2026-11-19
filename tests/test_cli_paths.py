"""Human-readable CLI paths (the JSON paths are covered elsewhere).

Every test drives ``cli.main`` the way a user would and asserts on what they would read.
"""
from __future__ import annotations

import io
import json

import pytest

from doogle import cli
from doogle import engine as E
from test_engine import BLUEPRINTS, minimal_data, run_cli, write_bp_dir


@pytest.fixture
def env(tmp_path, monkeypatch):
    bps = tmp_path / "bps"
    data = minimal_data()
    data["steps"].insert(1, {"id": "click", "description": "Click the button in the console", "manual": True})
    write_bp_dir(bps, data)
    calls: list[list[str]] = []
    results: dict[tuple[str, ...], E.CommandResult] = {}

    def runner(argv, timeout):
        calls.append(list(argv))
        for key, res in results.items():
            if tuple(argv[: len(key)]) == key:
                return res
        return E.CommandResult(0, " ".join(argv), "")

    monkeypatch.setattr(E, "subprocess_runner", runner)
    monkeypatch.setattr(E, "check_tools", lambda tools, which=None: {t: f"/usr/bin/{t}" for t in tools})
    st = tmp_path / "st"
    common = ["--blueprints-dir", str(bps), "--state-dir", str(st)]
    return {"bps": bps, "st": st, "calls": calls, "results": results, "common": common}


def _interactive(monkeypatch, answers):
    feed = iter(answers)

    def fake_input(*_a):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)


def test_full_human_journey(capsys, env, monkeypatch):
    c = env["common"]
    code, out, _ = run_cli(capsys, "plan", "demo-bp", "--var", "project_id=demo-project", *c)
    assert code == 0 and "DRY-RUN PLAN" in out and "To execute: doogle plan demo-bp" in out
    assert "--approve-high-risk" in out.splitlines()[-1]

    # interactive: prompted for the missing variable, confirms by typing the id, pauses at the manual step
    _interactive(monkeypatch, ["demo-project", "demo-bp", ""])
    code, out, err = run_cli(capsys, "plan", "demo-bp", "--apply", "--approve-high-risk", *c)
    assert code == 0, err
    assert "Project" in err and "Type 'demo-bp' to continue" in err and "MANUAL STEP 'click'" in err
    assert "PAUSED" in out and "Paused at a manual step. When it is done: doogle resume" in out
    assert "--approve-high-risk" in out.splitlines()[-1]

    # resume, typing 'done' at the manual step
    _interactive(monkeypatch, ["resume", "done"])
    code, out, err = run_cli(capsys, "resume", "latest", "--approve-high-risk", *c)
    assert code == 0, err
    assert "SUCCEEDED" in out and "Clean up later with: doogle teardown" in out
    assert "-> two: firebase deploy demo-project" in err and "-> click (manual)" in err

    code, out, _ = run_cli(capsys, "history", *c)
    assert code == 0 and "succeeded" in out and "steps 3/3" in out
    code, out, _ = run_cli(capsys, "history", "latest", *c)
    assert code == 0 and "blueprint=demo-bp" in out and "variables: project_id=demo-project" in out

    code, out, _ = run_cli(capsys, "teardown", "latest", *c)
    assert code == 0 and "TEARDOWN DRY-RUN" in out and "$ gcloud delete demo-project" in out
    assert "[risk=" in out and "Nothing has been executed." in out

    env["results"][("gcloud", "delete")] = E.CommandResult(1, "", "ERROR: PERMISSION_DENIED: denied")
    code, out, err = run_cli(capsys, "teardown", "latest", "--apply", "--yes", "--approve-high-risk", *c)
    assert code == 1
    assert "Teardown FAILED" in out and "| ERROR: PERMISSION_DENIED: denied" in out
    assert "hint: permission denied" in out and "-> teardown undo: gcloud delete demo-project" in err


def test_interactive_wrong_confirmation_aborts_and_runs_nothing(capsys, env, monkeypatch):
    _interactive(monkeypatch, ["nope"])
    code, _, err = run_cli(capsys, "plan", "demo-bp", "--var", "project_id=demo-project", "--apply",
                           "--approve-high-risk", *env["common"])
    assert code == 3 and "cancelled at the confirmation prompt. Nothing was run." in err
    assert env["calls"] == []
    _interactive(monkeypatch, [])   # EOF at the prompt is a "no"
    code, _, err = run_cli(capsys, "plan", "demo-bp", "--var", "project_id=demo-project", "--apply",
                           "--approve-high-risk", *env["common"])
    assert code == 3 and env["calls"] == []


def test_manual_step_eof_pauses(capsys, env, monkeypatch):
    _interactive(monkeypatch, ["demo-bp"])     # confirm, then EOF at the manual step
    code, out, _ = run_cli(capsys, "plan", "demo-bp", "--var", "project_id=demo-project", "--apply",
                           "--approve-high-risk", *env["common"])
    assert code == 0 and "PAUSED" in out


def test_failed_run_prints_output_and_next_steps(capsys, env):
    env["results"][("firebase", "deploy")] = E.CommandResult(1, "", "Error: Failed to authenticate, have you "
                                                                  "run firebase login?")
    code, out, _ = run_cli(capsys, "plan", "demo-bp", "--var", "project_id=demo-project", "--apply", "--yes",
                           "--approve-high-risk", *env["common"])
    # the manual step comes first non-interactively: it pauses
    assert code == 0 and "PAUSED" in out
    code, out, _ = run_cli(capsys, "resume", "latest", "--yes", "--approve-high-risk", *env["common"])
    assert code == 1 and "FAILED" in out and "hint: firebase is not logged in" in out
    assert "Fix the cause, then: doogle resume" in out and "(or clean up with: doogle teardown" in out


def test_vars_file_yaml_and_json(capsys, env, tmp_path):
    y = tmp_path / "v.yaml"
    y.write_text("project_id: demo-project\nlabel: x\n", encoding="utf-8")
    j = tmp_path / "v.json"
    j.write_text(json.dumps({"project_id": "demo-project"}), encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    for f in (y, j):
        code, out, _ = run_cli(capsys, "plan", "demo-bp", "--vars-file", str(f), "--json", *env["common"])
        assert code == 0 and json.loads(out)["variables"]["project_id"] == "demo-project"
    code, _, err = run_cli(capsys, "plan", "demo-bp", "--vars-file", str(bad), *env["common"])
    assert code == 2 and "must contain a mapping" in err
    code, _, err = run_cli(capsys, "plan", "demo-bp", "--vars-file", str(tmp_path / "nope.yaml"), *env["common"])
    assert code == 2 and "not found" in err


def test_history_empty_and_list_human(capsys, env):
    code, out, _ = run_cli(capsys, "history", *env["common"])
    assert code == 0 and "No runs recorded yet" in out
    code, out, _ = run_cli(capsys, "list", "--blueprints-dir", str(BLUEPRINTS))
    assert code == 0 and "solo-serverless-api" in out
    code, out, _ = run_cli(capsys, "list", "--stale", "--blueprints-dir", str(BLUEPRINTS))
    assert code == 0
    code, out, _ = run_cli(capsys, "explain", "solo-serverless-api", "--blueprints-dir", str(BLUEPRINTS))
    assert code == 0 and "teardown" in out.lower()


@pytest.mark.parametrize("goal,expect", [
    ("I want a cheap API for my side project", "Recommended: solo-serverless-api"),
    ("deploy a kubernetes cluster with terraform", "No shipped blueprint covers"),
    ("zebra quantum lasagna", "No blueprint is a clear fit"),
])
def test_ask_human(capsys, goal, expect):
    code, out, _ = run_cli(capsys, "ask", goal, "--blueprints-dir", str(BLUEPRINTS))
    assert code == 0 and expect in out, out
    if expect.startswith("Recommended"):
        assert "Why:" in out and "Watch out:" in out and "Next:" in out


def test_ask_low_confidence_wording(capsys, monkeypatch):
    real = E.route

    def low(goal, cat, **kw):
        r = real("I want a cheap API for my side project", cat, **kw)
        return dict(r, confidence="low")
    monkeypatch.setattr(E, "route", low)
    code, out, _ = run_cli(capsys, "ask", "x", "--blueprints-dir", str(BLUEPRINTS))
    assert code == 0 and out.startswith("No confident match. Weak candidate: solo-serverless-api")


def test_doctor_validate_docs_share_human(capsys, tmp_path, env):
    code, out, _ = run_cli(capsys, "doctor", "--blueprints-dir", str(BLUEPRINTS), "--state-dir", str(tmp_path))
    assert code in (0, 1) and "Doogle doctor (read-only; no cloud calls)" in out and "Overall:" in out
    code, out, _ = run_cli(capsys, "validate", "--blueprints-dir", str(env["bps"]))
    assert code == 0 and "ok       demo-bp" in out and "1 valid, 0 invalid" in out
    broken = tmp_path / "broken"
    write_bp_dir(broken, minimal_data(risk_note=1, steps=[]))
    code, out, _ = run_cli(capsys, "validate", "--blueprints-dir", str(broken))
    assert code == 2 and "INVALID  demo-bp" in out
    empty = tmp_path / "empty"
    empty.mkdir()
    code, _, err = run_cli(capsys, "validate", "--blueprints-dir", str(empty))
    assert code == 2 and "no blueprints found" in err
    code, out, err = run_cli(capsys, "share", "solo-serverless-api", "--blueprints-dir", str(BLUEPRINTS))
    assert code == 0 and "solo-serverless-api" in out


def test_main_maps_oserror_and_keyboardinterrupt(capsys, monkeypatch):
    def boom(_args, _out):
        raise PermissionError(13, "Permission denied", "/x")
    def ctrl_c(_args, _out):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "cmd_list", boom)
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "doogle: error:" in err and "Permission denied" in err and "Traceback" not in err
    assert cli.main(["list", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "PermissionError"
    monkeypatch.setattr(cli, "cmd_list", ctrl_c)
    assert cli.main(["list"]) == 130
    assert "doogle resume latest" in capsys.readouterr().err


def test_out_err_flushes_stdout_first(monkeypatch):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", buf_out)
    monkeypatch.setattr("sys.stderr", buf_err)
    o = cli.Out(as_json=True)
    o.line("hidden in json mode")
    o.err("e")
    assert buf_out.getvalue() == "" and buf_err.getvalue() == "e\n"
