"""v1.2 hardening tests: hostile inputs, tampered state, crash-proof validation,
property-based rendering, concurrency, interruption and redaction."""

from __future__ import annotations

import contextlib
import copy
import json
import multiprocessing as mp
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from doogle import cli
from doogle import engine as E
from doogle import validator as V
from test_engine import ALL_TOOLS, BLUEPRINTS, FakeRunner, make_bp, minimal_data, run_cli, write_bp_dir

GATES = E.Gates(yes=True, approve_high_risk=True)
SHIPPED_DATA = {p.parent.name: yaml.safe_load(p.read_text(encoding="utf-8"))
                for p in sorted(BLUEPRINTS.glob("[!_]*/blueprint.yaml"))}
FUZZ = settings(max_examples=int(os.environ.get("DOOGLE_FUZZ_EXAMPLES", "300")), deadline=None,
                suppress_health_check=[HealthCheck.too_slow])


@pytest.fixture
def store(tmp_path) -> E.StateStore:
    return E.StateStore(tmp_path / ".doogle" / "state.json")


def _plan(data=None, values=None):
    return E.build_plan(make_bp(data), values or {"project_id": "demo-project"}, which=ALL_TOOLS)


# --------------------------------------------------------------------------- rendering properties

ANY_TEXT = st.text(min_size=1, max_size=600)
SAFE_VALUE = st.text(alphabet=st.characters(blacklist_categories=tuple(E.FORBIDDEN_VALUE_CATEGORIES)),
                     min_size=1, max_size=200).filter(
    lambda v: not v.startswith("-") and "{{" not in v and "}}" not in v and v == v.strip())


@FUZZ
@given(value=SAFE_VALUE)
def test_property_value_is_exactly_one_argv_element(value):
    """Whatever characters a value holds (shell metachars, quotes, unicode), it stays one argument."""
    var = E.Variable("label", "p")
    E.check_value(var, value)
    for template in ("gcloud x --label {{label}} end", "gcloud x '--label={{label}}' end",
                     "gcloud x pre-{{label}}-post end"):
        argv = E.render_argv(template, {"label": value})
        assert len(argv) == len(template.split()) and argv[0] == "gcloud" and argv[-1] == "end"
        assert sum(value in a for a in argv[2:-1]) >= 1 and argv[1] == "x"


@FUZZ
@given(value=ANY_TEXT)
def test_property_check_value_accepts_only_safe_values(value):
    var = E.Variable("label", "p")
    try:
        E.check_value(var, value)
    except E.VariableError:
        return
    import unicodedata
    assert not value.startswith("-") and len(value) <= E.MAX_VALUE_LENGTH and value == value.strip()
    assert all(unicodedata.category(c) not in E.FORBIDDEN_VALUE_CATEGORIES for c in value)
    assert "{{" not in value and "}}" not in value


@pytest.mark.parametrize("value", [
    "a\u202eb",          # right-to-left override (Trojan Source style display spoofing)
    "a\u200bb",          # zero-width space
    "a\u2028b",          # line separator
    "a\u0085b",          # next line (NEL)
    "a\ufeffb",          # BOM
    "x" * 513,           # huge
    " leading", "trailing ", "a\tb", "a\x00b", "--flag", "-x", "{{other}}",
])
def test_hostile_values_rejected_with_clean_error(value):
    with pytest.raises(E.VariableError):
        E.check_value(E.Variable("label", "p"), value)


@FUZZ
@given(st.text(max_size=300))
def test_render_never_crashes_unexpectedly(template):
    with contextlib.suppress(E.VariableError, ValueError):
        E.render_argv(template, {"a": "x"})


# --------------------------------------------------------------------------- validator fuzzing

JSONISH = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=30),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=12), children, max_size=5),
    max_leaves=40)


@FUZZ
@given(JSONISH)
def test_validator_never_crashes_on_arbitrary_data(data):
    errors = V.validate_blueprint_data(data, "x")
    assert isinstance(errors, list) and all(isinstance(e, str) for e in errors)


@FUZZ
@given(bp_id=st.sampled_from(sorted(SHIPPED_DATA)), path_seed=st.integers(0, 10_000), value=JSONISH)
def test_validator_never_crashes_on_mutated_shipped_blueprints(bp_id, path_seed, value):
    data = copy.deepcopy(SHIPPED_DATA[bp_id])
    # walk to a pseudo-random container and replace one child with fuzz
    node, seed = data, path_seed
    for _ in range(4):
        keys = list(node.keys()) if isinstance(node, dict) else list(range(len(node))) if isinstance(node, list) else []
        if not keys:
            break
        key = keys[seed % len(keys)]
        seed //= max(1, len(keys))
        if isinstance(node[key], (dict, list)) and seed % 2:
            node = node[key]
        else:
            node[key] = value
            break
    errors = V.validate_blueprint_data(data, bp_id)
    assert isinstance(errors, list)
    if errors == []:  # anything the validator accepts must also build into a blueprint
        E.blueprint_from_data(data)


@FUZZ
@given(st.text(max_size=400))
def test_blueprint_loader_never_crashes_on_arbitrary_yaml_text(tmp_path_factory, text):
    folder = tmp_path_factory.mktemp("fz") / "fz"
    folder.mkdir()
    (folder / "blueprint.yaml").write_text(text, encoding="utf-8")
    (folder / "GUIDE.md").write_text("g", encoding="utf-8")
    with pytest.raises(E.BlueprintValidationError):
        E.load_blueprint_file(folder)


@pytest.mark.parametrize("payload", [
    b"a: &a [*a]\n",                                       # recursive alias
    b"a: &a [x]\nb: [*a, *a, *a]\n",                        # any alias (billion-laughs building block)
    b"\xff\xfe\x00 not utf-8",                              # binary
    ("x: " + "[" * 5000 + "]" * 5000 + "\n").encode(),      # deep nesting
    b"!!python/object/apply:os.system ['echo pwned']\n",    # unsafe tag
    b"x: " + b"a" * (300 * 1024),                           # oversized
    b"? [a, b]\n: 1\n",                                     # non-string key
])
def test_hostile_yaml_files_give_clean_errors(tmp_path, payload):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "blueprint.yaml").write_bytes(payload)
    (folder / "GUIDE.md").write_text("g", encoding="utf-8")
    with pytest.raises(E.BlueprintValidationError):
        E.load_blueprint_file(folder)


def test_catalog_survives_a_hostile_blueprint(tmp_path):
    write_bp_dir(tmp_path, minimal_data())
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "blueprint.yaml").write_text("a: &a [*a]\n", encoding="utf-8")
    cat = E.Catalog(tmp_path)
    assert "demo-bp" in cat.blueprints and "evil" in cat.errors


@pytest.mark.parametrize("command, needle", [
    ("env bash -c id", "allow-list"),
    ("python3 -c print", "allow-list"),
    ("xargs gcloud", "allow-list"),
    ("node -e x", "allow-list"),
    ("npx something", "allow-list"),
])
def test_tool_allow_list_blocks_shell_smuggling(command, needle):
    data = minimal_data()
    data["steps"][0]["command"] = command
    assert any(needle in e for e in V.validate_blueprint_data(data, "demo-bp"))


@pytest.mark.parametrize("field, command, needle", [
    ("verify_command", "gcloud services enable run.googleapis.com", "read-only"),
    ("verify_command", "gcloud run deploy x --source .", "read-only"),
    ("skip_if", "firebase deploy --only hosting", "read-only"),
    ("skip_if", "gcloud projects add-iam-policy-binding p --member x --role y", "read-only"),
    ("verify_command", "curl -o /tmp/x https://example.com", "curl flags"),
    ("verify_command", "curl -sSo/tmp/x https://example.com", "curl flags"),
    ("verify_command", "curl --data @secret https://example.com", "curl flags"),
    ("verify_command", "curl -X DELETE https://example.com", "curl flags"),
    ("command", "curl -T file https://example.com", "curl may only be used read-only"),
])
def test_read_only_positions_are_really_read_only(field, command, needle):
    data = minimal_data()
    data["steps"][0][field] = command
    assert any(needle in e for e in V.validate_blueprint_data(data, "demo-bp"))


def test_read_only_allows_real_describe_and_list_commands():
    data = minimal_data()
    data["steps"][0]["verify_command"] = ("gcloud run services describe svc --region r '--format=value(status.url)'")
    data["steps"][0]["skip_if"] = "firebase hosting:sites:get site --project p"
    assert V.validate_blueprint_data(data, "demo-bp") == []


@pytest.mark.parametrize("command", ["firebase firestore:delete --all-collections --force",
                                     "firebase hosting:disable --force", "gcloud services disable run.googleapis.com",
                                     "firebase functions:delete fn --force"])
def test_colon_style_and_disable_commands_are_destructive(command):
    data = minimal_data()
    data["steps"][0]["command"] = command
    assert any("must be marked risk: high" in e for e in V.validate_blueprint_data(data, "demo-bp"))


def test_teardown_requires_valid_undoes():
    data = minimal_data()
    del data["teardown"][0]["undoes"]
    assert any("must declare 'undoes" in e for e in V.validate_blueprint_data(data, "demo-bp"))
    data["teardown"][0]["undoes"] = ["nope"]
    assert any("unknown step 'nope'" in e for e in V.validate_blueprint_data(data, "demo-bp"))
    data["teardown"][0]["undoes"] = "one"
    assert any("non-empty list" in e for e in V.validate_blueprint_data(data, "demo-bp"))


def test_every_shipped_teardown_command_undoes_a_step():
    for bp_id, data in SHIPPED_DATA.items():
        step_ids = {s["id"] for s in data["steps"]}
        for t in data["teardown"]:
            if not t.get("manual"):
                assert t.get("undoes") and set(t["undoes"]) <= step_ids, (bp_id, t["id"])


def test_shipped_iam_grants_are_idempotent():
    """A binding that already existed must not be removed by teardown, so grants need skip_if."""
    for bp_id, data in SHIPPED_DATA.items():
        for s in data["steps"]:
            if "add-iam-policy-binding" in (s.get("command") or ""):
                assert s.get("skip_if") and s.get("skip_if_expect"), (bp_id, s["id"])


# --------------------------------------------------------------------------- tampered state

def _applied(store, data=None, runner=None):
    plan = _plan(data)
    return plan, E.Executor(store, runner or FakeRunner()).apply(plan, GATES)


@pytest.mark.parametrize("argv", [["bash", "-c", "echo pwned"], ["rm", "-rf", "/"], ["/usr/bin/gcloud", "x"],
                                  ["gcloud", "projects", "delete", "p"], "gcloud delete", [1, 2], ["gcloud", "a\nb"]])
def test_teardown_refuses_tampered_state(store, argv):
    _, run = _applied(store)
    run["teardown"][0]["argv"] = argv
    store.save_run(run)
    runner = FakeRunner()
    with pytest.raises(E.StateError, match="Nothing was run"):
        E.Executor(store, runner).teardown(store.get("latest"), GATES, which=ALL_TOOLS)
    assert runner.calls == []


def test_teardown_with_emptied_argv_runs_nothing(store):
    _, run = _applied(store)
    run["teardown"][0]["argv"] = []
    runner = FakeRunner()
    run = E.Executor(store, runner).teardown(run, GATES, which=ALL_TOOLS)
    assert runner.calls == [] and run["teardown"][0]["status"] == "manual_required"


def test_resume_refuses_tampered_argv_and_ignores_tampered_risk(store):
    plan, run = _applied(store, runner=FakeRunner({("gcloud", "create"): E.CommandResult(1, "", "boom")}))
    assert run["status"] == "failed"
    run["steps"][0]["argv"] = ["gcloud", "projects", "delete", "x"]
    store.save_run(run)
    runner = FakeRunner()
    with pytest.raises(E.StateError, match="no longer matches"):
        E.Executor(store, runner).resume(store.get("latest"), plan, GATES)
    assert runner.calls == []
    # downgrading a high-risk step to low in the state file must not bypass --approve-high-risk
    run = store.get("latest")
    run["steps"][0]["argv"] = plan.steps[0].argv
    for s in run["steps"]:
        s["risk"] = "low"
    store.save_run(run)
    with pytest.raises(E.SafetyGateError, match="approve-high-risk"):
        E.Executor(store, runner).resume(store.get("latest"), plan, E.Gates(yes=True))
    assert runner.calls == []


def test_teardown_skips_resources_the_run_did_not_create(store):
    data = minimal_data()
    data["steps"][0]["skip_if"] = "gcloud describe {{project_id}}"
    data["steps"][0]["skip_if_expect"] = "{{project_id}}"
    plan = _plan(data)
    run = E.Executor(store, FakeRunner()).apply(plan, GATES)
    assert run["steps"][0]["skip_reason"]  # the resource already existed
    runner = FakeRunner()
    run = E.Executor(store, runner).teardown(run, GATES, which=ALL_TOOLS)
    assert runner.calls == []
    assert run["teardown"][0]["status"] == "skipped_not_created"
    assert "already existed" in run["teardown"][0]["skip_reason"]
    assert run["teardown_status"] == "nothing_to_run"


def test_teardown_skips_steps_never_reached_but_undoes_failed_ones(store):
    data = minimal_data()
    data["teardown"].append({"id": "undo-two", "description": "x", "command": "firebase delete {{project_id}}",
                             "risk": "high", "undoes": ["two"]})
    plan = _plan(data)
    run = E.Executor(store, FakeRunner({("gcloud", "create"): E.CommandResult(1, "", "boom")})).apply(plan, GATES)
    preview = {p["id"]: p for p in E.teardown_preview(run)}
    # step one failed part-way: it may have created something, so its teardown runs
    assert preview["undo"]["run"] is True
    # step two never ran: nothing to delete
    assert preview["undo-two"]["run"] is False


def test_legacy_runs_without_undoes_still_tear_down(store):
    _, run = _applied(store)
    for t in run["teardown"]:
        t.pop("undoes", None)
    runner = FakeRunner()
    E.Executor(store, runner).teardown(run, GATES, which=ALL_TOOLS)
    assert runner.calls == [["gcloud", "delete", "demo-project"]]


def test_teardown_warns_when_blueprint_changed(store):
    plan, run = _applied(store)
    expected = copy.deepcopy(plan.teardown)
    expected[0].argv = ["gcloud", "delete", "something-else"]
    run = E.Executor(store, FakeRunner()).teardown(run, GATES, which=ALL_TOOLS, expected=expected)
    assert run["teardown_warnings"] and "differs" in run["teardown_warnings"][0]


# --------------------------------------------------------------------------- state file robustness

def test_state_file_and_dir_permissions(store):
    _applied(store)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert (store.path.parent / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")


@pytest.mark.parametrize("content", ['{"runs": [', "", "\x00\x01", '{"runs": [{"foo": 1}]}', '{"runs": "x"}',
                                     '[1, 2]', '{"runs": [{"run_id": "r", "steps": "x"}]}',
                                     '{"state_version": 99, "runs": []}', '{"runs": [{"run_id": 5}]}'])
def test_corrupt_or_truncated_state_gives_clean_error_and_is_not_overwritten(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises(E.StateError):
        store.get("latest")
    with pytest.raises(E.StateError):
        _applied(store)
    assert store.path.read_text(encoding="utf-8") == content  # never clobbered


def test_truncated_state_points_to_backup(store):
    _applied(store)
    _applied(store)
    assert store.backup_path.exists()
    text = store.path.read_text(encoding="utf-8")
    store.path.write_text(text[: len(text) // 2], encoding="utf-8")
    with pytest.raises(E.StateError, match="previous copy"):
        store.runs()
    assert json.loads(store.backup_path.read_text(encoding="utf-8"))["runs"]


@FUZZ
@given(cut=st.integers(min_value=0, max_value=4000))
def test_any_truncation_is_a_clean_error(tmp_path_factory, cut):
    path = tmp_path_factory.mktemp("st") / "state.json"
    s = E.StateStore(path)
    s.save_run({"run_id": "r1", "steps": [{"id": "a", "status": "succeeded"}], "teardown": [], "variables": {}})
    full = path.read_text(encoding="utf-8")
    path.write_text(full[: min(cut, len(full) - 1)], encoding="utf-8")
    with contextlib.suppress(E.StateError):
        s.runs()


def _writer(args):
    path, i = args
    s = E.StateStore(Path(path))
    for j in range(15):
        s.save_run({"run_id": f"r{i}-{j}", "steps": [], "teardown": [], "variables": {}})


@pytest.mark.skipif(sys.platform == "win32", reason="uses fork-safe multiprocessing on POSIX")
def test_concurrent_writers_never_lose_runs(tmp_path):
    path = tmp_path / "state.json"
    with mp.get_context("spawn").Pool(6) as pool:
        pool.map(_writer, [(str(path), i) for i in range(6)])
    runs = E.StateStore(path).runs()
    assert len(runs) == 90 and len({r["run_id"] for r in runs}) == 90


# --------------------------------------------------------------------------- interruption

def test_keyboard_interrupt_mid_step_is_recorded_then_resumable(store):
    plan = _plan()

    def interrupting(argv, timeout):
        if argv[:2] == ["firebase", "deploy"]:
            raise KeyboardInterrupt
        return E.CommandResult(0, " ".join(argv), "")

    with pytest.raises(KeyboardInterrupt):
        E.Executor(store, interrupting).apply(plan, GATES)
    run = store.get("latest")
    assert run["status"] == "interrupted"
    assert [s["status"] for s in run["steps"]] == ["succeeded", "interrupted"]
    runner = FakeRunner()
    run = E.Executor(store, runner).resume(run, plan, GATES)
    assert run["status"] == "succeeded" and runner.calls == [["firebase", "deploy", "demo-project"]]
    run = E.Executor(store, FakeRunner()).teardown(run, GATES, which=ALL_TOOLS)
    assert run["teardown_status"] == "succeeded"


def test_keyboard_interrupt_during_teardown_is_recorded(store):
    _, run = _applied(store)

    def boom(argv, timeout):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        E.Executor(store, boom).teardown(run, GATES, which=ALL_TOOLS)
    assert store.get("latest")["teardown_status"] == "interrupted"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_real_sigint_mid_step_via_cli(tmp_path):
    """Spawn the real CLI with a fake slow gcloud, SIGINT it mid-step, then resume and tear down."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "started"
    fake = bindir / "gcloud"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, time, pathlib
        args = sys.argv[1:]
        if args[:1] == ["deploy-slow"] and not pathlib.Path({str(tmp_path / 'fast')!r}).exists():
            pathlib.Path({str(marker)!r}).write_text("x")
            time.sleep(60)
        print(" ".join(args))
        """), encoding="utf-8")
    fake.chmod(0o755)
    data = minimal_data()
    data["steps"][1]["command"] = "gcloud deploy-slow {{project_id}}"
    data["teardown"][0]["undoes"] = ["one", "two"]
    data["verified_versions"] = {"gcloud": "1.0.0"}
    bps = tmp_path / "bps"
    write_bp_dir(bps, data)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", DOOGLE_STATE_DIR=str(tmp_path / "st"))
    base = [sys.executable, "-m", "doogle"]
    apply = ["plan", "demo-bp", "--blueprints-dir", str(bps), "--var", "project_id=demo-project",
             "--apply", "--yes", "--approve-high-risk"]
    proc = subprocess.Popen([*base, *apply], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(200):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), proc.communicate(timeout=5)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130, (out, err)
    assert "Traceback" not in err and "resume latest" in err
    state = json.loads((tmp_path / "st" / "state.json").read_text(encoding="utf-8"))
    run = state["runs"][-1]
    assert run["status"] == "interrupted" and run["steps"][1]["status"] == "interrupted"
    (tmp_path / "fast").write_text("x")
    r = subprocess.run([*base, "resume", "latest", "--blueprints-dir", str(bps), "--yes", "--approve-high-risk"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    teardown = ["teardown", "latest", "--blueprints-dir", str(bps), "--apply", "--yes", "--approve-high-risk", "--json"]
    r = subprocess.run([*base, *teardown], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["teardown_status"] == "succeeded"


# --------------------------------------------------------------------------- redaction

@pytest.mark.parametrize("secret", [
    # Assembled at runtime so no secret-shaped literal sits in the repository (secret scanners stay quiet).
    "AI" + "zaSyA1234567890abcdefghijklmnopqrstuv",
    "ya" + "29.a0AfH6SMBx-very-secret-token_value",
    "1/" + "/0gABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
    "ey" + "JhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl",
    "gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789",
    "GOC" + "SPX-abcdefghijklmnopqrstuv",
    "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END " + "PRIVATE KEY-----",
    "someone.private@gmail.com",
])
def test_redact_masks_secrets(secret):
    text = f"prefix {secret} suffix"
    out = E.redact(text)
    assert secret not in out and "<redacted" in out


@pytest.mark.parametrize("text", ['"access_token": "abc123def"', "password=hunter2", "Authorization: Bearer abc.def",
                                  "https://x/exec?token=s3cr3t&source=curl", "api_key: abcdefgh"])
def test_redact_masks_key_value_secrets(text):
    out = E.redact(text)
    assert "<redacted" in out
    for leaked in ("abc123def", "hunter2", "abc.def", "s3cr3t", "abcdefgh"):
        assert leaked not in out


def test_redact_keeps_useful_output():
    text = ("Service URL: https://svc-abc-uc.a.run.app\n"
            "serviceAccount:runtime@my-proj.iam.gserviceaccount.com\n"
            "123-compute@developer.gserviceaccount.com\nprojects/my-proj/databases/(default)")
    assert E.redact(text) == text


def test_stored_and_printed_output_is_redacted(store):
    leak = "user@example.com " + "AI" + "zaSyA1234567890abcdefghijklmnopqrstuv"
    runner = FakeRunner({("gcloud", "create"): E.CommandResult(1, leak, "token=abcdef123")})
    _, run = _applied(store, runner=runner)
    raw = store.path.read_text(encoding="utf-8")
    assert "user@example.com" not in raw and "AIza" not in raw and "abcdef123" not in raw
    assert "<redacted:email>" in run["steps"][0]["output_tail"]


# --------------------------------------------------------------------------- subprocess runner

def test_runner_survives_binary_output_and_non_executables(tmp_path):
    res = E.subprocess_runner([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe ok')"], 30)
    assert res.returncode == 0 and "ok" in res.stdout
    noexec = tmp_path / "tool"
    noexec.write_text("#!/bin/sh\n", encoding="utf-8")
    noexec.chmod(0o644)
    assert E.subprocess_runner([str(noexec)], 5).returncode in (126, 127)
    assert E.subprocess_runner([sys.executable, "-c", "import time; time.sleep(5)"], 1).returncode == 124


# --------------------------------------------------------------------------- CLI edge cases

def test_cli_teardown_dry_run_shows_will_skip(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("DOOGLE_STATE_DIR", str(tmp_path / "st"))
    store = E.StateStore(tmp_path / "st" / "state.json")
    data = minimal_data()
    data["steps"][0]["skip_if"] = "gcloud describe {{project_id}}"
    E.Executor(store, FakeRunner()).apply(_plan(data), GATES)
    code, out, _ = run_cli(capsys, "teardown", "latest")
    assert code == 0 and "WILL SKIP" in out and "already existed" in out
    code, out, _ = run_cli(capsys, "teardown", "latest", "--json")
    assert json.loads(out)["teardown"][0]["would_run"] is False


def test_cli_prompt_eof_is_clean(capsys, monkeypatch, tmp_path):
    bps = tmp_path / "bps"
    write_bp_dir(bps, minimal_data())
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    code, _, err = run_cli(capsys, "plan", "demo-bp", "--blueprints-dir", str(bps), "--apply")
    assert code == 2 and "pass --var project_id=" in err


def test_cli_unwritable_state_dir_is_clean(capsys, tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root ignores permissions")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    bps = tmp_path / "bps"
    write_bp_dir(bps, minimal_data())
    monkeypatch.setattr(E, "check_tools", lambda tools, which=None: {t: "/x" for t in tools})
    code, _, err = run_cli(capsys, "plan", "demo-bp", "--blueprints-dir", str(bps), "--var", "project_id=demo-project",
                           "--apply", "--yes", "--approve-high-risk", "--state-dir", str(ro / "sub"))
    ro.chmod(0o700)
    assert code == 1 and "Permission denied" in err and "Traceback" not in err
