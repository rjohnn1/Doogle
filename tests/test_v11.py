"""Tests for v1.1: validator module, freshness metadata, skip_if idempotency,
new blueprints, docs generator, share, and community files."""

from __future__ import annotations

import copy
import json
import shutil
from datetime import date

import pytest
import yaml

from doogle import __version__
from doogle import docs as D
from doogle import engine as E
from doogle import validator as V
from test_engine import ALL_TOOLS, BLUEPRINTS, REPO, FakeRunner, make_bp, minimal_data, run_cli, write_bp_dir

TODAY = date(2026, 11, 19)


def test_version_matches_pyproject():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    assert __version__ == "1.2.0rc1"


# --------------------------------------------------------------------------- validator module


def test_validator_is_standalone_and_reexported():
    assert E.validate_blueprint_data is V.validate_blueprint_data
    assert E.PLACEHOLDER_RE is V.PLACEHOLDER_RE and E.SCHEMA_VERSION == V.SCHEMA_VERSION == 2
    assert V.validate_blueprint_data(minimal_data(), "demo-bp", today=TODAY) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(schema_version=1), "v1 -> v2"),
    (lambda d: d.pop("maintainer"), "maintainer"),
    (lambda d: d.update(maintainer=" "), "maintainer"),
    (lambda d: d.pop("last_verified"), "last_verified"),
    (lambda d: d.update(last_verified="19/11/2026"), "ISO date"),
    (lambda d: d.update(last_verified="2026-02-30"), "ISO date"),
    (lambda d: d.update(last_verified="2027-01-01"), "in the future"),
    (lambda d: d.update(verification_status="tested"), "verification_status must be one of"),
    (lambda d: d.pop("verified_versions"), "verified_versions"),
    (lambda d: d.update(verified_versions={}), "non-empty mapping"),
    (lambda d: d.update(verified_versions={"gcloud": 594.0}), "quoted version string"),
    (lambda d: d.update(verified_versions={"gcloud": "latest"}), "quoted version string"),
    (lambda d: d.update(verified_versions={"Tool 1": "1.0"}), "invalid tool name"),
    (lambda d: d.update(verification_status="live"), "requires a real version for 'firebase'"),
    (lambda d: d.update(verification_notes=""), "verification_notes"),
    (lambda d: d.update(tagline="TODO write this"), "TODO placeholders in: tagline"),
    (lambda d: d["steps"][0].update(skip_if="gcloud delete x"), "skip_if must be read-only"),
    (lambda d: d["steps"][0].update(skip_if_expect="x"), "skip_if_expect requires skip_if"),
    (lambda d: d["steps"][0].update(skip_if="gcloud get {{nope}}"), "not a declared variable"),
    (lambda d: d["steps"][0].update(skip_if="gcloud a | firebase"), "shell operators"),
])
def test_v2_validator_rules(mutate, needle):
    data = copy.deepcopy(minimal_data())
    mutate(data)
    errors = V.validate_blueprint_data(data, "demo-bp", today=TODAY)
    assert any(needle in e for e in errors), errors


def test_yaml_date_and_aliases_accepted():
    data = minimal_data(last_verified=date(2026, 1, 15), verification_status="syntax-checked",
                        verified_versions={"gcloud": "1.0.0", "firebase": "2.3"})
    assert V.validate_blueprint_data(data, "demo-bp", today=TODAY) == []
    data = minimal_data(verification_status="syntax-checked",
                        verified_versions={"gcloud": "1.0", "firebase": "unverified"})
    assert any("firebase" in e for e in V.validate_blueprint_data(data, "demo-bp", today=TODAY))


def test_guide_with_todo_is_rejected(tmp_path):
    folder = write_bp_dir(tmp_path, minimal_data())
    (folder / "GUIDE.md").write_text("# Guide\nTODO finish\n")
    assert any("GUIDE.md still contains TODO" in e for e in E.Catalog(tmp_path).errors["demo-bp"])


def test_template_needs_filling_then_validates(tmp_path):
    shutil.copytree(BLUEPRINTS / "_template", tmp_path / "my-blueprint")
    errors = E.Catalog(tmp_path).errors["my-blueprint"]
    assert any("TODO" in e for e in errors)
    for name in ("blueprint.yaml", "GUIDE.md"):
        p = tmp_path / "my-blueprint" / name
        p.write_text(p.read_text().replace("TODO", "Filled"))
    cat = E.Catalog(tmp_path)
    assert cat.errors == {} and "my-blueprint" in cat.blueprints


def test_template_is_not_in_catalog():
    assert "_template" not in E.Catalog(BLUEPRINTS).blueprints
    assert not any(k.startswith("_") for k in E.Catalog(BLUEPRINTS).errors)


# --------------------------------------------------------------------------- freshness


def test_freshness_age_and_stale():
    bp = make_bp(minimal_data(last_verified="2026-01-15"))
    f = E.freshness(bp, today=TODAY)
    assert f["age_days"] == (TODAY - date(2026, 1, 15)).days and f["stale"] is True
    f = E.freshness(bp, today=date(2026, 2, 1))
    assert f["stale"] is False and f["verification_status"] == "simulated"
    assert E.freshness(bp, today=TODAY, stale_days=400)["stale"] is False


def test_shipped_blueprints_have_honest_freshness():
    for bp in E.Catalog(BLUEPRINTS):
        assert bp.verification_status in ("simulated", "syntax-checked")  # nobody has run them live yet
        assert "NOT yet been run against a live" in bp.verification_notes
        assert bp.maintainer
        assert all(v != "unverified" for v in bp.verified_versions.values())


def _old_catalog(tmp_path):
    write_bp_dir(tmp_path, minimal_data(id="old-bp", last_verified="2020-01-01"))
    write_bp_dir(tmp_path, minimal_data(id="new-bp", last_verified=date.today().isoformat()))
    return str(tmp_path)


def test_cli_list_stale(capsys, tmp_path):
    root = _old_catalog(tmp_path)
    code, out, _ = run_cli(capsys, "list", "--stale", "--json", "--blueprints-dir", root)
    data = json.loads(out)
    assert code == 0 and [b["id"] for b in data["blueprints"]] == ["old-bp"] and data["stale"] == ["old-bp"]
    code, out, err = run_cli(capsys, "list", "--blueprints-dir", root)
    assert "STALE" in out and "warning: 1 blueprint(s) not verified" in err
    code, out, _ = run_cli(capsys, "list", "--stale", "--stale-days", "100000", "--json", "--blueprints-dir", root)
    assert json.loads(out)["blueprints"] == []


def test_cli_explain_shows_freshness_and_warns(capsys, tmp_path):
    root = _old_catalog(tmp_path)
    code, out, err = run_cli(capsys, "explain", "old-bp", "--blueprints-dir", root)
    assert code == 0 and "Freshness: simulated, 2020-01-01" in out and "last verified" in err
    code, out, _ = run_cli(capsys, "explain", "firebase-auth-mvp", "--json")
    fresh = json.loads(out)["freshness"]
    assert fresh["verification_status"] == "syntax-checked" and fresh["verified_versions"]["firebase-tools"]


# --------------------------------------------------------------------------- skip_if idempotency


def _skip_plan():
    data = minimal_data()
    data["steps"][0].update(skip_if="gcloud get {{project_id}}", skip_if_expect="{{project_id}}")
    return E.build_plan(make_bp(data), {"project_id": "demo-project"}, which=ALL_TOOLS)


def test_skip_if_matched_skips_command(tmp_path):
    store = E.StateStore(tmp_path / "state.json")
    runner = FakeRunner({("gcloud", "get"): E.CommandResult(0, "exists: demo-project", "")})
    run = E.Executor(store, runner).apply(_skip_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert run["status"] == "succeeded"
    assert ["gcloud", "create", "demo-project", "--label", "hello world"] not in runner.calls
    assert run["steps"][0]["skip_reason"] and run["steps"][0]["verify_status"] == "passed"


def test_skip_if_not_matched_runs_command(tmp_path):
    store = E.StateStore(tmp_path / "state.json")
    runner = FakeRunner({("gcloud", "get"): E.CommandResult(1, "", "not found")})
    run = E.Executor(store, runner).apply(_skip_plan(), E.Gates(yes=True, approve_high_risk=True))
    assert ["gcloud", "create", "demo-project", "--label", "hello world"] in runner.calls
    assert run["steps"][0]["skip_reason"] is None


def test_plan_shows_skip_if(capsys):
    code, out, _ = run_cli(capsys, "plan", "firebase-auth-mvp", "--var", "project_id=demo-mvp-123")
    assert code == 0 and "skip if already done: firebase projects:list --json" in out
    code, out, _ = run_cli(capsys, "plan", "firebase-auth-mvp", "--var", "project_id=demo-mvp-123", "--json")
    step = next(s for s in json.loads(out)["steps"] if s["id"] == "create-firestore")
    assert step["skip_argv"][:4] == ["gcloud", "firestore", "databases", "describe"]
    assert step["skip_if_expect"] == "databases/(default)"


# --------------------------------------------------------------------------- new blueprints


@pytest.mark.parametrize("bp_id", ["firebase-auth-mvp", "ai-studio-to-vertex"])
def test_new_blueprints_contract(bp_id):
    bp = E.Catalog(BLUEPRINTS).get(bp_id)
    assert len(bp.when_not_to_use) >= 3
    for s in bp.steps:
        if not s.manual and s.risk in ("medium", "high"):
            assert s.verify_command, f"{s.id} mutates but has no verify_command"
    assert any(t.command for t in bp.teardown) and any(t.manual for t in bp.teardown)
    assert any(s.manual for s in bp.steps)  # honest about console/browser-only steps
    plan = E.build_plan(bp, {"project_id": "demo-project-123"}, which=ALL_TOOLS)
    for s in plan.steps:
        if s.argv:
            assert "{{" not in " ".join(s.argv)


def test_firebase_mvp_specifics():
    bp = E.Catalog(BLUEPRINTS).get("firebase-auth-mvp")
    deploy = next(s for s in bp.steps if s.id == "deploy")
    assert deploy.risk == "high" and deploy.public_exposure
    assert "--only hosting,firestore:rules" in deploy.command and "--non-interactive" in deploy.command
    assert set(bp.required_tools()) == {"curl", "firebase", "gcloud"}
    assert next(t for t in bp.teardown if t.id == "delete-firestore").risk == "high"


def test_ai_studio_specifics():
    bp = E.Catalog(BLUEPRINTS).get("ai-studio-to-vertex")
    grant = next(s for s in bp.steps if s.id == "grant-vertex-user")
    assert grant.risk == "high" and "roles/aiplatform.user" in grant.command
    text = " ".join(s.description for s in bp.steps)
    assert "GOOGLE_GENAI_USE_VERTEXAI" in text and "application-default login" in text
    assert "key file" in text.lower()  # warns against downloading service-account keys
    assert not any(s.public_exposure for s in bp.steps)


@pytest.mark.parametrize("goal,expected", [
    ("add login and a database to my react web app", "firebase-auth-mvp"),
    ("MVP website with user sign up and firestore", "firebase-auth-mvp"),
    ("move my gemini api key prototype from ai studio to production", "ai-studio-to-vertex"),
    ("migrate to vertex ai with application default credentials", "ai-studio-to-vertex"),
    ("I want a cheap API for my side project", "solo-serverless-api"),
    ("build an AI agent with Gemini that calls tools", "gemini-adk-service"),
])
def test_routing_with_five_blueprints(goal, expected):
    assert E.route(goal, E.Catalog(BLUEPRINTS))["recommendation"] == expected


# --------------------------------------------------------------------------- docs generator


def test_decision_matrix_is_deterministic_and_complete():
    cat = E.Catalog(BLUEPRINTS)
    a, b = D.render_decision_matrix(cat), D.render_decision_matrix(E.Catalog(BLUEPRINTS))
    assert a == b
    for bp in cat:
        assert f"[`{bp.id}`](../blueprints/{bp.id}/GUIDE.md)" in a
        assert bp.when_not_to_use[0].split(":")[0] in a
    assert "age" not in a.lower().split("verification levels")[0].split("|")[-1]  # no time-dependent cells


def test_repository_docs_are_current():
    desired = D.generate(E.Catalog(BLUEPRINTS), REPO)
    assert D.stale_files(desired) == [], "run `doogle docs` and commit the result"


def test_docs_write_and_check(tmp_path, capsys):
    (tmp_path / "README.md").write_text(f"# x\n{D.GALLERY_START}\nold\n{D.GALLERY_END}\ntail\n")
    code, _, err = run_cli(capsys, "docs", "--check", "--root", str(tmp_path))
    assert code == 1 and "out of date" in err
    code, out, _ = run_cli(capsys, "docs", "--root", str(tmp_path), "--json")
    assert code == 0 and len(json.loads(out)["written"]) == 2
    readme = (tmp_path / "README.md").read_text()
    assert "old" not in readme and "firebase-auth-mvp" in readme and readme.endswith("tail\n")
    code, out, _ = run_cli(capsys, "docs", "--check", "--root", str(tmp_path), "--json")
    assert code == 0 and json.loads(out)["up_to_date"] is True


def test_docs_requires_markers(tmp_path, capsys):
    (tmp_path / "README.md").write_text("# no markers\n")
    code, _, err = run_cli(capsys, "docs", "--root", str(tmp_path))
    assert code == 1 and "markers" in err


def test_docs_refuses_invalid_blueprints(tmp_path, capsys):
    write_bp_dir(tmp_path, minimal_data(when_not_to_use=[]))
    code, _, _ = run_cli(capsys, "docs", "--blueprints-dir", str(tmp_path), "--root", str(tmp_path))
    assert code == 2


def test_cell_escaping():
    assert D._cell("a | b\n c") == "a \\| b c"


# --------------------------------------------------------------------------- share


def test_share_snippet(capsys, monkeypatch):
    monkeypatch.delenv(D.REPO_URL_ENV, raising=False)
    code, out, _ = run_cli(capsys, "share", "solo-serverless-api", "--json")
    s = json.loads(out)
    assert code == 0 and s["placeholder_repo"] is True
    assert s["badge_url"] == ("https://img.shields.io/badge/built%20with-doogle%3A%20solo--serverless--api-"
                              + D.BADGE_COLOR)
    assert s["link"] == "https://github.com/OWNER/REPO/tree/main/blueprints/solo-serverless-api"
    assert "not affiliated with or endorsed by Google LLC" in s["markdown"]
    monkeypatch.setenv(D.REPO_URL_ENV, "https://github.com/acme/doogle/")
    code, out, err = run_cli(capsys, "share", "firebase-auth-mvp")
    assert "https://github.com/acme/doogle/tree/main/blueprints/firebase-auth-mvp" in out and "placeholder" not in err
    code, out, _ = run_cli(capsys, "share", "nope")
    assert code == 2


# --------------------------------------------------------------------------- community & docs files


REQUIRED_FILES = [
    "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md", "GOVERNANCE.md", "CHANGELOG.md",
    ".github/ISSUE_TEMPLATE/blueprint_drift_or_bug.yml", ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml", ".github/ISSUE_TEMPLATE/new_blueprint.yml",
    ".github/ISSUE_TEMPLATE/config.yml", ".github/PULL_REQUEST_TEMPLATE.md", ".github/CODEOWNERS",
    ".github/dependabot.yml", ".github/workflows/ci.yml", ".github/workflows/codeql.yml",
    ".github/workflows/freshness.yml", ".github/labels.yml", "docs/LABELS.md", "docs/DECISION_MATRIX.md",
    "docs/LAUNCH_PLAYBOOK.md", "docs/ROADMAP.md", "demo/demo.tape", "demo/README.md",
    "scripts/check_cli_syntax.py", "blueprints/_template/blueprint.yaml",
]


@pytest.mark.parametrize("rel", REQUIRED_FILES)
def test_required_project_files_exist(rel):
    assert (REPO / rel).is_file() and (REPO / rel).read_text().strip()


def test_github_yaml_files_parse():
    for path in (REPO / ".github").rglob("*.yml"):
        assert yaml.safe_load(path.read_text()) is not None, path


def test_ci_checks_docs_and_freshness_workflow_is_scheduled():
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    assert "doogle docs --check" in ci and "doogle validate" in ci
    fresh = yaml.safe_load((REPO / ".github/workflows/freshness.yml").read_text())
    triggers = fresh[True] if True in fresh else fresh["on"]  # YAML 1.1 parses `on` as True
    assert "schedule" in triggers and "secrets." not in (REPO / ".github/workflows/freshness.yml").read_text()


def test_code_of_conduct_and_placeholders():
    coc = (REPO / "CODE_OF_CONDUCT.md").read_text()
    assert "Contributor Covenant" in coc and "version 2.1" in coc and "MAINTAINER: INSERT CONTACT METHOD" in coc


def test_readme_privacy_badges_and_gallery():
    readme = (REPO / "README.md").read_text()
    assert "no telemetry" in readme.lower()
    assert "Nothing leaves your machine except the commands you approve" in readme
    assert "actions/workflows/ci.yml/badge.svg" in readme and "OWNER/REPO" in readme
    assert D.GALLERY_START in readme and "firebase-auth-mvp" in readme


def test_demo_tape_is_offline_and_read_only():
    tape = "\n".join(ln for ln in (REPO / "demo/demo.tape").read_text().splitlines() if not ln.startswith("#"))
    for cmd in ("doogle ask", "doogle explain", "doogle plan"):
        assert cmd in tape
    assert "--apply" not in tape and "teardown" not in tape and "gcloud" not in tape


def test_launch_playbook_covers_wholesome_rules():
    text = (REPO / "docs/LAUNCH_PLAYBOOK.md").read_text()
    for needle in ("Show HN", "r/googlecloud", "r/Firebase", "dev.to", "Google Developer Groups",
                   "awesome", "Blueprint of the week", "all-contributors", "GitHub Projects",
                   "Semantic versioning", "No fake stars", "Disclose AI-assisted", "not affiliated",
                   "1280 x 640"):
        assert needle.lower() in text.lower(), needle


def test_drift_template_lists_every_blueprint():
    tpl = yaml.safe_load((REPO / ".github/ISSUE_TEMPLATE/blueprint_drift_or_bug.yml").read_text())
    options = next(b for b in tpl["body"] if b.get("id") == "blueprint")["attributes"]["options"]
    assert set(E.Catalog(BLUEPRINTS).blueprints) <= set(options)


# --------------------------------------------------------------------------- syntax checker helpers


def _load_checker():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_cli_syntax", REPO / "scripts/check_cli_syntax.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_syntax_checker_helpers():
    m = _load_checker()
    assert m.command_path(["gcloud", "run", "deploy", "{{service_name}}", "--region", "x"]) == ["run", "deploy"]
    assert m.command_path(["firebase", "hosting:disable", "--force"]) == ["hosting:disable"]
    assert m.flag_ok("--region=us-central1", "  --region=REGION", "")
    assert m.flag_ok("--no-allow-unauthenticated", "--[no-]allow-unauthenticated", "")
    assert m.flag_ok("--project", "", "--project <id>")
    assert not m.flag_ok("--max-instanzes", "--max-instances=MAX", "")
