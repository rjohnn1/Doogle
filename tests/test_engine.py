"""Unit tests for Doogle: schema validation, safe rendering, planning,
safety gates, execution/state, teardown, routing, doctor and CLI."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from doogle import __version__
from doogle import cli
from doogle import engine as E

REPO = Path(__file__).resolve().parents[1]
BLUEPRINTS = REPO / "blueprints"
SHIPPED = ["gemini-adk-service", "solo-serverless-api", "workspace-sheet-webhook"]


# --------------------------------------------------------------------------- fixtures


def minimal_data(**overrides) -> dict:
    data = {
        "schema_version": 1,
        "id": "demo-bp",
        "name": "Demo",
        "category": "test",
        "tagline": "A demo blueprint.",
        "keywords": ["demo"],
        "when_to_use": ["testing"],
        "when_not_to_use": ["production: use something simpler"],
        "services": [{"name": "Thing", "role": "does stuff"}],
        "estimated_cost": "Free tier, then usage-based.",
        "variables": [{"key": "project_id", "prompt": "Project", "pattern": "^[a-z][a-z0-9-]{4,28}[a-z0-9]$"},
                      {"key": "label", "prompt": "Label", "default": "hello world"}],
        "steps": [
            {"id": "one", "description": "first", "command": "tool1 create {{project_id}} --label {{label}}",
             "risk": "low", "verify_command": "tool1 describe {{project_id}}", "verify_expect": "{{project_id}}"},
            {"id": "two", "description": "second", "command": "tool2 deploy {{project_id}}", "risk": "high",
             "public_exposure": True},
        ],
        "teardown": [{"id": "undo", "description": "undo", "command": "tool1 delete {{project_id}}", "risk": "high"}],
    }
    data.update(overrides)
    return data


def make_bp(data=None) -> E.Blueprint:
    data = data or minimal_data()
    assert E.validate_blueprint_data(data, data["id"]) == []
    return E.blueprint_from_data(data)


def write_bp_dir(root: Path, data: dict, guide: bool = True, folder: str | None = None) -> Path:
    folder_path = root / (folder or data["id"])
    folder_path.mkdir(parents=True)
    (folder_path / "blueprint.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    if guide:
        (folder_path / "GUIDE.md").write_text("# guide\n", encoding="utf-8")
    return folder_path


class FakeRunner:
    """Records argv; returns scripted results keyed by the first two argv tokens."""

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        for key, res in self.results.items():
            if tuple(argv[: len(key)]) == key:
                return res(argv) if callable(res) else res
        return E.CommandResult(0, " ".join(argv), "")


ALL_TOOLS = lambda name: f"/usr/bin/{name}"  # noqa: E731
NO_TOOLS = lambda name: None  # noqa: E731


@pytest.fixture
def store(tmp_path) -> E.StateStore:
    return E.StateStore(tmp_path / ".doogle" / "state.json")


# --------------------------------------------------------------------------- shipped blueprints


def test_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__) and __version__.startswith("1.")


def test_shipped_blueprints_all_valid():
    cat = E.Catalog(BLUEPRINTS)
    assert cat.errors == {}
    assert sorted(cat.blueprints) == SHIPPED


@pytest.mark.parametrize("bp_id", SHIPPED)
def test_shipped_blueprint_contract(bp_id):
    bp = E.Catalog(BLUEPRINTS).get(bp_id)
    assert bp.path.name == bp_id
    assert bp.guide_path and bp.guide_path.read_text().strip()
    assert bp.when_not_to_use and bp.teardown
    assert any(t.command for t in bp.teardown)
    assert not E.PRICE_RE.search(bp.estimated_cost)
    # Public steps must be at least medium risk; every mutating cloud step has a verify or is read-only.
    for step in bp.steps:
        if step.public_exposure:
            assert step.risk in ("medium", "high")
    # A plan can be built with only required vars supplied.
    required = {v.key: (v.example or "demo-project-123") for v in bp.variables if v.default is None}
    plan = E.build_plan(bp, required, which=ALL_TOOLS)
    assert plan.steps and plan.teardown


def test_shipped_blueprints_use_no_shell_and_known_tools():
    for bp in E.Catalog(BLUEPRINTS):
        assert set(bp.required_tools()) <= {"gcloud", "clasp", "test", "firebase", "adk"}


# --------------------------------------------------------------------------- validator


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(id="Demo_BP"), "kebab-case"),
    (lambda d: d.update(when_not_to_use=[]), "when_not_to_use"),
    (lambda d: d.update(when_to_use=[""]), "when_to_use"),
    (lambda d: d.pop("teardown"), "teardown"),
    (lambda d: d.update(teardown=[]), "teardown"),
    (lambda d: d.update(teardown=[{"id": "x", "description": "manual", "manual": True}]), "at least one command"),
    (lambda d: d.update(steps=[]), "steps"),
    (lambda d: d["steps"][0].update(risk="extreme"), "risk must be one of"),
    (lambda d: d["steps"][0].update(command="tool1 {{nope}}"), "not a declared variable"),
    (lambda d: d["steps"][0].update(command="tool1 a | tool2 b"), "shell operators"),
    (lambda d: d["steps"][0].update(command="tool1 a && tool2 b"), "shell operators"),
    (lambda d: d["steps"][0].update(command="tool1 $(whoami)"), "substitution"),
    (lambda d: d["steps"][0].update(command="bash -c 'echo hi'"), "not an allowed tool"),
    (lambda d: d["steps"][0].update(command="sudo tool1"), "not an allowed tool"),
    (lambda d: d["steps"][0].update(command="/usr/bin/tool1 x"), "bare tool name"),
    (lambda d: d["steps"][0].update(command="gcloud projects delete {{project_id}}", risk="high"), "never be automated"),
    (lambda d: d["steps"][0].update(command="gcloud run services delete x", risk="low"), "risk: high"),
    (lambda d: d["teardown"][0].update(risk="low"), "risk: high"),
    (lambda d: d["steps"][1].pop("command"), "command must be a non-empty string"),
    (lambda d: d["steps"][1].update(id="one"), "duplicate step id"),
    (lambda d: d["steps"][0].update(surprise=True), "unknown field"),
    (lambda d: d.update(extra_field=1), "unknown top-level"),
    (lambda d: d["services"][0].pop("role"), "services[0]"),
    (lambda d: d.update(estimated_cost="About $5 per month"), "exact prices"),
    (lambda d: d["variables"].append({"key": "api_key", "prompt": "key"}), "looks like a secret"),
    (lambda d: d["variables"].append({"key": "Bad-Key", "prompt": "x"}), "snake_case"),
    (lambda d: d["variables"][1].update(pattern="^[0-9]+$"), "does not match its pattern"),
    (lambda d: d["steps"][0].pop("verify_command"), "verify_expect requires verify_command"),
    (lambda d: d["steps"][0].update(manual=True), "manual steps must not define"),
    (lambda d: d["steps"][0].update(verify_command="tool1 delete x"), "read-only"),
])
def test_validator_rejects(mutate, needle):
    data = copy.deepcopy(minimal_data())
    mutate(data)
    errors = E.validate_blueprint_data(data, "demo-bp")
    assert errors, f"expected an error containing {needle!r}"
    assert any(needle in e for e in errors), errors


def test_validator_id_must_match_folder(tmp_path):
    write_bp_dir(tmp_path, minimal_data(), folder="other-name")
    cat = E.Catalog(tmp_path)
    assert "other-name" in cat.errors
    assert any("must match its folder name" in e for e in cat.errors["other-name"])


def test_validator_requires_guide(tmp_path):
    write_bp_dir(tmp_path, minimal_data(), guide=False)
    cat = E.Catalog(tmp_path)
    assert any("GUIDE.md" in e for e in cat.errors["demo-bp"])
    with pytest.raises(E.BlueprintValidationError):
        cat.get("demo-bp")


def test_validator_accepts_minimal():
    assert E.validate_blueprint_data(minimal_data(), "demo-bp") == []


# --------------------------------------------------------------------------- path resolution


def test_resolve_explicit_dir(tmp_path):
    path, source = E.resolve_blueprints_dir(tmp_path, env={})
    assert path == tmp_path.resolve() and source == "--blueprints-dir"


def test_resolve_env_dir(tmp_path):
    path, source = E.resolve_blueprints_dir(None, env={E.BLUEPRINTS_ENV: str(tmp_path)})
    assert path == tmp_path.resolve() and "DOOGLE_BLUEPRINTS_DIR" in source


def test_resolve_explicit_missing_is_an_error(tmp_path):
    with pytest.raises(E.BlueprintNotFound, match="does not exist"):
        E.resolve_blueprints_dir(tmp_path / "nope", env={})
    with pytest.raises(E.BlueprintNotFound, match="DOOGLE_BLUEPRINTS_DIR"):
        E.resolve_blueprints_dir(None, env={E.BLUEPRINTS_ENV: str(tmp_path / "nope")})


def test_resolve_default_works_from_any_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(E.BLUEPRINTS_ENV, raising=False)
    path, _ = E.resolve_blueprints_dir()
    assert (path / "solo-serverless-api" / "blueprint.yaml").is_file()


# --------------------------------------------------------------------------- rendering


def test_render_keeps_hostile_value_in_one_argv_token():
    argv = E.render_argv("tool1 --label {{label}} end", {"label": "a b; rm -rf / && $(x) `y`"})
    assert argv == ["tool1", "--label", "a b; rm -rf / && $(x) `y`", "end"]
    shown = E.display(argv)
    assert E.shlex.split(shown) == argv  # display form is safely shell-quoted
    assert "'a b; rm -rf / && $(x) `y`'" in shown


def test_render_inside_quoted_template_token():
    argv = E.render_argv("tool --filter 'role:{{r}} AND x'", {"r": "a b"})
    assert argv == ["tool", "--filter", "role:a b AND x"]


def test_render_rejects_unresolved_placeholder():
    with pytest.raises(E.VariableError, match="unresolved placeholder"):
        E.render_argv("tool1 {{missing}}", {})
    with pytest.raises(E.VariableError, match="unresolved"):
        E.render_text("hello {{who}}", {})


@pytest.mark.parametrize("value,needle", [
    ("--impersonate-service-account=x", "cannot start with '-'"),
    ("line1\nline2", "control characters"),
    ("", "cannot be empty"),
    ("{{label}}", "cannot contain"),
    ("UPPER_case", "does not match required pattern"),
])
def test_variable_values_are_checked(value, needle):
    bp = make_bp()
    with pytest.raises(E.VariableError, match=re.escape(needle)):
        E.resolve_variables(bp, {"project_id": value})


def test_unknown_variable_rejected():
    with pytest.raises(E.VariableError, match="unknown variable"):
        E.resolve_variables(make_bp(), {"project_id": "demo-project", "typo": "x"})


def test_missing_variable_reported_with_prompt():
    with pytest.raises(E.VariableError, match="project_id"):
        E.build_plan(make_bp(), {}, which=ALL_TOOLS)


# --------------------------------------------------------------------------- planning


def test_plan_is_pure_and_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = E.build_plan(make_bp(), {"project_id": "demo-project"}, which=NO_TOOLS)
    d = plan.to_dict()
    assert d["mode"] == "dry-run"
    assert d["variables"] == {"project_id": "demo-project", "label": "hello world"}
    assert d["steps"][0]["argv"] == ["tool1", "create", "demo-project", "--label", "hello world"]
    assert d["steps"][0]["command"] == "tool1 create demo-project --label 'hello world'"
    assert d["steps"][0]["verify_expect"] == "demo-project"
    assert d["summary"]["high_risk_steps"] == ["two"]
    assert d["summary"]["public_exposure_steps"] == ["two"]
    assert d["summary"]["missing_tools"] == ["tool1", "tool2"]
    assert d["teardown"][0]["command"] == "tool1 delete demo-project"
    json.dumps(d)  # serialisable
    assert not (tmp_path / ".doogle").exists()  # planning never writes state


# --------------------------------------------------------------------------- gates


def _plan(which=ALL_TOOLS):
    return E.build_plan(make_bp(), {"project_id": "demo-project"}, which=which)


def test_apply_blocks_on_missing_tools_before_running_anything(store):
    runner = FakeRunner()
    with pytest.raises(E.SafetyGateError, match="not found on PATH"):
        E.Executor(store, runner).apply(_plan(NO_TOOLS), E.Gates(yes=True, approve_high_risk=True))
    assert runner.calls == [] and not store.path.exists()


def test_apply_blocks_high_risk_without_approval(store):
    runner = FakeRunner()
    with pytest.raises(E.SafetyGateError, match="--approve-high-risk"):
        E.Executor(store, runner).apply(_plan(), E.Gates(yes=True))
    assert runner.calls == []


def test_apply_blocks_noninteractive_without_yes(store):
    runner = FakeRunner()
    with pytest.raises(E.SafetyGateError, match="--yes"):
        E.Executor(store, runner).apply(_plan(), E.Gates(approve_high_risk=True, confirm=None))
    assert runner.calls == []


def test_apply_respects_declined_confirmation(store):
    runner = FakeRunner()
    with pytest.raises(E.SafetyGateError, match="cancelled"):
        E.Executor(store, runner).apply(_plan(), E.Gates(approve_high_risk=True, confirm=lambda s: False))
    assert runner.calls == []


def test_apply_with_interactive_confirmation(store):
    runner = FakeRunner()
    seen = []
    run = E.Executor(store, runner).apply(
        _plan(), E.Gates(approve_high_risk=True, confirm=lambda s: seen.append(s) or True))
    assert run["status"] == "succeeded" and seen


def test_exit_codes():
    assert E.SafetyGateError.exit_code == 3
    assert E.BlueprintValidationError.exit_code == 2
    assert E.VariableError.exit_code == 2


# --------------------------------------------------------------------------- execution & state


def test_successful_run_records_everything(store):
    runner = FakeRunner()
    run = E.Executor(store, runner).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "succeeded"
    assert [s["status"] for s in run["steps"]] == ["succeeded", "succeeded"]
    assert run["steps"][0]["verify_status"] == "passed"
    assert run["steps"][1]["verify_status"] == "not_defined"
    assert all(s["started_at"] and s["finished_at"] for s in run["steps"])
    assert run["variables"]["project_id"] == "demo-project"
    assert run["teardown"][0]["argv"] == ["tool1", "delete", "demo-project"]
    assert runner.calls == [["tool1", "create", "demo-project", "--label", "hello world"],
                            ["tool1", "describe", "demo-project"],
                            ["tool2", "deploy", "demo-project"]]
    on_disk = json.loads(store.path.read_text())
    assert on_disk["state_version"] == E.STATE_VERSION
    assert on_disk["runs"][0]["run_id"] == run["run_id"]
    assert (store.path.parent / ".gitignore").read_text().strip().endswith("*")


def test_failure_stops_and_skips_remaining(store):
    runner = FakeRunner({("tool1", "create"): E.CommandResult(1, "", "boom: bad project")})
    run = E.Executor(store, runner).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "failed"
    assert [s["status"] for s in run["steps"]] == ["failed", "skipped"]
    assert run["steps"][0]["exit_code"] == 1 and "boom" in run["steps"][0]["output_tail"]
    assert len(runner.calls) == 1


def test_verify_expectation_failure_marks_step(store):
    runner = FakeRunner({("tool1", "describe"): E.CommandResult(0, "something else", "")})
    run = E.Executor(store, runner).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "failed"
    assert run["steps"][0]["status"] == "verify_failed"
    assert run["steps"][0]["verify_status"] == "failed"
    assert run["steps"][1]["status"] == "skipped"


def test_resume_skips_succeeded_steps(store):
    runner = FakeRunner({("tool2", "deploy"): E.CommandResult(2, "", "quota")})
    ex = E.Executor(store, runner)
    run = ex.apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "failed"
    runner2 = FakeRunner()
    run2 = E.Executor(store, runner2).resume(store.get(run["run_id"]), _plan(),
                                             E.Gates(yes=True, approve_high_risk=True))
    assert run2["status"] == "succeeded" and run2["run_id"] == run["run_id"]
    assert runner2.calls == [["tool2", "deploy", "demo-project"]]
    assert len(store.runs()) == 1


def test_resume_still_requires_high_risk_approval(store):
    runner = FakeRunner({("tool2", "deploy"): E.CommandResult(2, "", "x")})
    run = E.Executor(store, runner).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    with pytest.raises(E.SafetyGateError):
        E.Executor(store, FakeRunner()).resume(run, _plan(), E.Gates(yes=True))


def test_manual_step_pauses_then_resumes(store):
    data = minimal_data()
    data["steps"].insert(1, {"id": "do-by-hand", "description": "edit files", "manual": True})
    bp = make_bp(data)
    plan = E.build_plan(bp, {"project_id": "demo-project"}, which=ALL_TOOLS)
    run = E.Executor(store, FakeRunner()).apply(plan, E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "paused"
    assert [s["status"] for s in run["steps"]] == ["succeeded", "awaiting_manual", "skipped"]
    runner = FakeRunner()
    run = E.Executor(store, runner).resume(store.get("latest"), plan, E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "succeeded"
    assert runner.calls == [["tool2", "deploy", "demo-project"]]


def test_manual_step_interactive_confirmation(store):
    data = minimal_data()
    data["steps"].insert(0, {"id": "by-hand", "description": "do it", "manual": True})
    plan = E.build_plan(make_bp(data), {"project_id": "demo-project"}, which=ALL_TOOLS)
    run = E.Executor(store, FakeRunner(), confirm_manual=lambda s: True).apply(
        plan, E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "succeeded"


def test_state_lookup_by_prefix_latest_and_errors(store):
    with pytest.raises(E.StateError, match="no runs"):
        store.get("latest")
    a = E.Executor(store, FakeRunner()).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    b = E.Executor(store, FakeRunner()).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert store.get("latest")["run_id"] == b["run_id"]
    assert store.get(a["run_id"])["run_id"] == a["run_id"]
    with pytest.raises(E.StateError):
        store.get("does-not-exist")


def test_corrupt_state_is_reported(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    with pytest.raises(E.StateError, match="not valid JSON"):
        store.runs()


def test_teardown_from_state(store):
    run = E.Executor(store, FakeRunner()).apply(_plan(), E.Gates(yes=True, approve_high_risk=True))
    with pytest.raises(E.SafetyGateError):
        E.Executor(store, FakeRunner()).teardown(run, E.Gates(yes=True), which=ALL_TOOLS)
    runner = FakeRunner()
    run = E.Executor(store, runner).teardown(store.get(run["run_id"]),
                                             E.Gates(yes=True, approve_high_risk=True), which=ALL_TOOLS)
    assert runner.calls == [["tool1", "delete", "demo-project"]]
    assert run["teardown_status"] == "succeeded"
    assert store.get(run["run_id"])["teardown"][0]["status"] == "succeeded"


def test_teardown_partial_failure_continues(store):
    data = minimal_data()
    data["teardown"].append({"id": "undo-two", "description": "x", "command": "tool2 delete {{project_id}}",
                             "risk": "high"})
    plan = E.build_plan(make_bp(data), {"project_id": "demo-project"}, which=ALL_TOOLS)
    run = E.Executor(store, FakeRunner()).apply(plan, E.Gates(yes=True, approve_high_risk=True))
    runner = FakeRunner({("tool1", "delete"): E.CommandResult(1, "", "gone")})
    run = E.Executor(store, runner).teardown(run, E.Gates(yes=True, approve_high_risk=True), which=ALL_TOOLS)
    assert len(runner.calls) == 2 and run["teardown_status"] == "partial"


def test_subprocess_runner_never_uses_shell(tmp_path):
    marker = tmp_path / "pwned"
    res = E.subprocess_runner([sys.executable, "-c", "import sys; print(sys.argv[1])", f"x; touch {marker}"], 30)
    assert res.returncode == 0 and "touch" in res.stdout and not marker.exists()
    assert E.subprocess_runner(["definitely-not-a-real-binary-xyz"], 5).returncode == 127


# --------------------------------------------------------------------------- routing


@pytest.mark.parametrize("goal,expected", [
    ("I want a cheap API for my side project", "solo-serverless-api"),
    ("deploy my flask backend with scale to zero", "solo-serverless-api"),
    ("build an AI agent with Gemini that calls tools", "gemini-adk-service"),
    ("a chatbot assistant using vertex ai", "gemini-adk-service"),
    ("log stripe webhooks into a google sheet", "workspace-sheet-webhook"),
    ("automate my spreadsheet with apps script", "workspace-sheet-webhook"),
])
def test_offline_routing(goal, expected):
    result = E.route(goal, E.Catalog(BLUEPRINTS))
    assert result["mode"] == "offline"
    assert result["recommendation"] == expected
    assert result["matches"][0]["reasons"]


def test_offline_routing_no_match():
    result = E.route("bake a chocolate cake", E.Catalog(BLUEPRINTS))
    assert result["recommendation"] is None and result["confidence"] == "none"


def test_ai_routing_falls_back_without_key():
    result = E.route("cheap api", E.Catalog(BLUEPRINTS), use_ai=True, env={})
    assert result["mode"] == "offline" and "ai_note" in result


def test_ai_routing_uses_model_and_validates_answer(monkeypatch):
    import types

    class FakeModels:
        def __init__(self, answer):
            self.answer = answer

        def generate_content(self, **kw):
            return types.SimpleNamespace(text=json.dumps(self.answer))

    def install(answer):
        genai = types.SimpleNamespace(Client=lambda api_key: types.SimpleNamespace(models=FakeModels(answer)))
        google = types.ModuleType("google")
        google.genai = genai
        monkeypatch.setitem(sys.modules, "google", google)
        monkeypatch.setitem(sys.modules, "google.genai", genai)

    cat = E.Catalog(BLUEPRINTS)
    install({"blueprint_id": "gemini-adk-service", "reason": "agentic", "alternative": "AI Studio"})
    res = E.route("cheap api", cat, use_ai=True, env={"GEMINI_API_KEY": "test-not-a-key"})
    assert res["mode"] == "ai" and res["recommendation"] == "gemini-adk-service"
    assert res["offline_recommendation"] == "solo-serverless-api"

    install({"blueprint_id": "made-up-blueprint", "reason": "?"})
    res = E.route("cheap api", cat, use_ai=True, env={"GEMINI_API_KEY": "test-not-a-key"})
    assert res["mode"] == "offline" and "unknown blueprint" in res["ai_note"]


def test_empty_goal_rejected():
    with pytest.raises(E.DoogleError):
        E.route("   ", E.Catalog(BLUEPRINTS))


# --------------------------------------------------------------------------- doctor


def test_doctor_without_any_tools(tmp_path):
    report = E.doctor(str(BLUEPRINTS), str(tmp_path), which=NO_TOOLS, run_local=lambda a: None, env={})
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["python"]["status"] == "pass"
    assert by_name["blueprints-valid"]["status"] == "pass"
    assert by_name["tool:gcloud"]["status"] == "warn" and "hint" in by_name["tool:gcloud"]
    assert by_name["ai-routing"]["status"] in ("info", "pass")
    assert report["status"] == "warn"


def test_doctor_with_gcloud_context(tmp_path):
    report = E.doctor(str(BLUEPRINTS), str(tmp_path), which=ALL_TOOLS,
                      run_local=lambda a: "someone" if "auth" in a else "", env={})
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["gcloud-auth"]["status"] == "pass"
    assert by_name["gcloud-project"]["status"] == "warn"


def test_doctor_fails_on_invalid_blueprint(tmp_path):
    write_bp_dir(tmp_path, minimal_data(when_not_to_use=[]))
    report = E.doctor(str(tmp_path), str(tmp_path), which=NO_TOOLS, run_local=lambda a: None, env={})
    assert report["status"] == "fail"


# --------------------------------------------------------------------------- CLI


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("list", "explain", "plan", "ask", "doctor", "history", "teardown", "resume", "validate"):
        assert cmd in out


def test_cli_list_json(capsys):
    code, out, _ = run_cli(capsys, "list", "--json")
    data = json.loads(out)
    assert code == 0 and [b["id"] for b in data["blueprints"]] == SHIPPED


def test_cli_explain_json_and_text(capsys):
    code, out, _ = run_cli(capsys, "explain", "solo-serverless-api", "--json")
    assert code == 0 and json.loads(out)["id"] == "solo-serverless-api"
    code, out, _ = run_cli(capsys, "explain", "solo-serverless-api")
    assert code == 0 and "Do NOT use it when" in out


def test_cli_unknown_blueprint(capsys):
    code, out, _ = run_cli(capsys, "explain", "nope", "--json")
    assert code == 2 and "unknown blueprint" in json.loads(out)["error"]


def test_cli_plan_json(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(capsys, "plan", "solo-serverless-api", "--var", "project_id=demo-project-123", "--json")
    data = json.loads(out)
    assert code == 0 and data["mode"] == "dry-run"
    assert data["summary"]["high_risk_steps"] == ["deploy-service"]
    assert not (tmp_path / ".doogle").exists()


def test_cli_plan_missing_var(capsys):
    code, _, err = run_cli(capsys, "plan", "solo-serverless-api")
    assert code == 2 and "project_id" in err


def test_cli_vars_file(capsys, tmp_path):
    f = tmp_path / "vars.yaml"
    f.write_text("project_id: demo-project-123\nregion: europe-west1\n")
    code, out, _ = run_cli(capsys, "plan", "solo-serverless-api", "--vars-file", str(f), "--json")
    assert code == 0 and json.loads(out)["variables"]["region"] == "europe-west1"


def test_cli_apply_noninteractive_is_blocked(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(E.shutil, "which", ALL_TOOLS)
    code, _, err = run_cli(capsys, "plan", "solo-serverless-api", "--var", "project_id=demo-project-123",
                           "--apply", "--approve-high-risk", "--state-dir", str(tmp_path))
    assert code == 3 and "--yes" in err and not (tmp_path / "state.json").exists()


def test_cli_apply_end_to_end_with_fake_runner(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(E.shutil, "which", ALL_TOOLS)
    fake = FakeRunner({("gcloud", "run", "services", "describe"): E.CommandResult(0, "https://x.run.app\n", ""),
                       ("gcloud", "services", "list"): E.CommandResult(0, "run.googleapis.com\n", "")})
    monkeypatch.setattr(E, "subprocess_runner", fake)
    code, out, _ = run_cli(capsys, "plan", "solo-serverless-api", "--var", "project_id=demo-project-123",
                           "--apply", "--yes", "--approve-high-risk", "--state-dir", str(tmp_path), "--json")
    run = json.loads(out)
    assert code == 0 and run["status"] == "succeeded"
    code, out, _ = run_cli(capsys, "history", "--state-dir", str(tmp_path), "--json")
    assert json.loads(out)["runs"][0]["run_id"] == run["run_id"]
    code, out, _ = run_cli(capsys, "teardown", "latest", "--state-dir", str(tmp_path), "--json")
    assert json.loads(out)["mode"] == "dry-run"
    code, out, _ = run_cli(capsys, "teardown", "latest", "--state-dir", str(tmp_path), "--apply", "--yes",
                           "--approve-high-risk", "--json")
    assert code == 0 and json.loads(out)["teardown_status"] == "succeeded"
    assert ["gcloud", "run", "services", "delete", "doogle-api", "--region", "us-central1",
            "--project", "demo-project-123", "--quiet"] in fake.calls


def test_cli_history_empty(capsys, tmp_path):
    code, out, _ = run_cli(capsys, "history", "--state-dir", str(tmp_path))
    assert code == 0 and "No runs" in out


def test_cli_ask_json(capsys):
    code, out, _ = run_cli(capsys, "ask", "I want a cheap API for my side project", "--json")
    assert code == 0 and json.loads(out)["recommendation"] == "solo-serverless-api"


def test_cli_doctor_json(capsys, tmp_path):
    code, out, _ = run_cli(capsys, "doctor", "--json", "--state-dir", str(tmp_path))
    data = json.loads(out)
    assert code == 0 and data["status"] in ("pass", "warn")


def test_cli_validate(capsys, tmp_path):
    code, _, _ = run_cli(capsys, "validate")
    assert code == 0
    write_bp_dir(tmp_path, minimal_data(teardown=[]))
    code, out, _ = run_cli(capsys, "validate", "--blueprints-dir", str(tmp_path), "--json")
    assert code == 2 and "demo-bp" in json.loads(out)["invalid"]


def test_cli_module_entrypoint():
    proc = subprocess.run([sys.executable, "-m", "doogle", "list", "--json"], capture_output=True, text=True)
    assert proc.returncode == 0 and json.loads(proc.stdout)["blueprints"]


# --------------------------------------------------------------------------- hygiene


SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                      # Google API key
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    re.compile(r'"type"\s*:\s*"service_account"'),
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),                    # OAuth access token
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]{20,}"),                   # OAuth client secret
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
]


def test_no_secrets_in_repository():
    skip = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", ".doogle"}
    for path in REPO.rglob("*"):
        if any(part in skip or part.endswith(".egg-info") for part in path.parts) or not path.is_file():
            continue
        if path.suffix in {".png", ".jpg", ".gif", ".zip", ".whl", ".gz"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            assert not pat.search(text), f"possible secret in {path}"


def test_gitignore_protects_state_and_secrets():
    ignored = (REPO / ".gitignore").read_text()
    for entry in (".doogle/", ".env", ".venv/", "__pycache__/"):
        assert entry in ignored


def test_readme_has_non_affiliation_note():
    readme = (REPO / "README.md").read_text()
    assert "not affiliated with or endorsed by Google LLC" in readme
