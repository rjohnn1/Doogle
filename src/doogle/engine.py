"""Doogle engine: blueprint loading, strict validation, safe rendering,
dry-run planning, gated execution, state storage, routing and diagnostics.

Design rules (see README "Safety rules"):

* Commands are never passed to a shell. Each template is tokenised with
  ``shlex`` *before* variables are substituted, so a variable value always
  lands inside exactly one argv element no matter what characters it holds.
  The human-readable form is produced with ``shlex.join`` (shell-quoted).
* Planning is pure: it never writes files, calls the network, or runs tools.
* Mutation only happens through :class:`Executor`, which refuses to start
  unless every safety gate (tools present, high-risk approval, confirmation)
  has passed, and which persists a run record after every step.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess  # nosec B404 - used only with argv lists, never shell=True
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .validator import (
    ALLOWED_BINARIES,
    DESTRUCTIVE_TOKENS,
    FORBIDDEN_BINARIES,
    ID_RE,
    NEVER_AUTOMATE,
    PLACEHOLDER_RE,
    PRICE_RE,
    RISK_LEVELS,
    SCHEMA_VERSION,
    SECRET_KEY_RE,
    SHELL_OPERATORS,
    STALE_AFTER_DAYS,
    STEP_FIELDS,
    TEARDOWN_FIELDS,
    TOP_LEVEL_OPTIONAL,
    TOP_LEVEL_REQUIRED,
    UNVERIFIED,
    VAR_KEY_RE,
    VARIABLE_FIELDS,
    VERIFICATION_STATUSES,
    _tokens,
    parse_date,
    recorded_argv_problems,
    safe_load_blueprint_text,
    validate_blueprint_data,
)

__all__ = [
    "ALLOWED_BINARIES",
    "DESTRUCTIVE_TOKENS",
    "FORBIDDEN_BINARIES",
    "ID_RE",
    "NEVER_AUTOMATE",
    "PLACEHOLDER_RE",
    "PRICE_RE",
    "RISK_LEVELS",
    "SCHEMA_VERSION",
    "SECRET_KEY_RE",
    "SHELL_OPERATORS",
    "STALE_AFTER_DAYS",
    "STEP_FIELDS",
    "TEARDOWN_FIELDS",
    "TOP_LEVEL_OPTIONAL",
    "TOP_LEVEL_REQUIRED",
    "UNVERIFIED",
    "VARIABLE_FIELDS",
    "VAR_KEY_RE",
    "VERIFICATION_STATUSES",
    "parse_date",
    "recorded_argv_problems",
    "safe_load_blueprint_text",
    "validate_blueprint_data",
]

STATE_VERSION = 1
MAX_VALUE_LENGTH = 512
# Unicode categories never allowed in a variable value: controls, format characters (bidi
# overrides, zero-width joiners), line/paragraph separators, surrogates, private use, unassigned.
FORBIDDEN_VALUE_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"}
BLUEPRINTS_ENV = "DOOGLE_BLUEPRINTS_DIR"
STATE_DIR_ENV = "DOOGLE_STATE_DIR"
GEMINI_MODEL_ENV = "DOOGLE_GEMINI_MODEL"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
AI_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

TOOL_HINTS = {
    "gcloud": "Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install",
    "firebase": "Install the Firebase CLI: npm install -g firebase-tools  (then run: firebase login)",
    "clasp": "Install clasp: npm install -g @google/clasp  (then run: clasp login)",
    "adk": "Install Google ADK: pip install google-adk",
    "curl": "Install curl (preinstalled on most Linux/macOS systems)",
    "test": "POSIX 'test' utility (present on Linux/macOS; use WSL or Git Bash on Windows)",
}


# --------------------------------------------------------------------------- errors


class DoogleError(Exception):
    """Base error. ``exit_code`` is used by the CLI."""

    exit_code = 1


class BlueprintNotFound(DoogleError):
    exit_code = 2


class BlueprintValidationError(DoogleError):
    exit_code = 2

    def __init__(self, blueprint: str, errors: Sequence[str]):
        self.blueprint = blueprint
        self.errors = list(errors)
        super().__init__(f"blueprint '{blueprint}' is invalid:\n  - " + "\n  - ".join(self.errors))


class VariableError(DoogleError):
    exit_code = 2


class SafetyGateError(DoogleError):
    """Raised when an execution gate blocks mutation. Nothing has run."""

    exit_code = 3


class StateError(DoogleError):
    exit_code = 2


# --------------------------------------------------------------------------- utils


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- blueprint resolution


def _packaged_blueprints_dir() -> Path | None:
    try:
        candidate = resources.files("doogle") / "blueprints"
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None
    path = Path(str(candidate))
    return path if path.is_dir() else None


def _checkout_blueprints_dir() -> Path | None:
    path = Path(__file__).resolve().parents[2] / "blueprints"
    return path if path.is_dir() else None


def resolve_blueprints_dir(explicit: str | os.PathLike[str] | None = None,
                           env: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """Return ``(path, source)``.

    Order: ``--blueprints-dir`` > ``$DOOGLE_BLUEPRINTS_DIR`` > packaged data
    (wheel installs) > source checkout (editable installs). Explicit choices
    that do not exist are errors; we never silently fall through them.
    """
    env = os.environ if env is None else env
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise BlueprintNotFound(f"--blueprints-dir '{path}' does not exist or is not a directory")
        return path.resolve(), "--blueprints-dir"
    env_value = env.get(BLUEPRINTS_ENV)
    if env_value:
        path = Path(env_value).expanduser()
        if not path.is_dir():
            raise BlueprintNotFound(f"${BLUEPRINTS_ENV}='{path}' does not exist or is not a directory")
        return path.resolve(), f"${BLUEPRINTS_ENV}"
    packaged = _packaged_blueprints_dir()
    if packaged:
        return packaged.resolve(), "package data"
    checkout = _checkout_blueprints_dir()
    if checkout:
        return checkout, "source checkout"
    raise BlueprintNotFound(
        "could not locate blueprints. Reinstall doogle, or pass --blueprints-dir PATH, "
        f"or set ${BLUEPRINTS_ENV}."
    )


# --------------------------------------------------------------------------- model


@dataclass
class Variable:
    key: str
    prompt: str
    default: str | None = None
    pattern: str | None = None
    example: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Step:
    id: str
    description: str
    command: str | None = None
    risk: str = "low"
    public_exposure: bool = False
    verify_command: str | None = None
    verify_expect: str | None = None
    manual: bool = False
    timeout_seconds: int = 1800
    skip_if: str | None = None
    skip_if_expect: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TeardownStep:
    id: str
    description: str
    command: str | None = None
    risk: str = "high"
    manual: bool = False
    undoes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Blueprint:
    id: str
    name: str
    category: str
    tagline: str
    when_to_use: list[str]
    when_not_to_use: list[str]
    services: list[dict[str, Any]]
    estimated_cost: str
    variables: list[Variable]
    steps: list[Step]
    teardown: list[TeardownStep]
    maintainer: str = ""
    last_verified: str = ""
    verification_status: str = "simulated"
    verified_versions: dict[str, Any] = field(default_factory=dict)
    verification_notes: str = ""
    keywords: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def guide_path(self) -> Path | None:
        if self.path is None:
            return None
        guide = self.path / "GUIDE.md"
        return guide if guide.is_file() else None

    def required_tools(self) -> list[str]:
        tools = set(self.requires_tools)
        commands = [s.command for s in self.steps] + [s.verify_command for s in self.steps]
        commands += [s.skip_if for s in self.steps]
        commands += [t.command for t in self.teardown]
        for cmd in commands:
            if cmd:
                toks = _tokens(cmd)
                if toks:
                    tools.add(toks[0])
        return sorted(tools)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "tagline": self.tagline,
            "maintainer": self.maintainer,
            "freshness": freshness(self),
            "keywords": self.keywords,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "services": self.services,
            "estimated_cost": self.estimated_cost,
            "prerequisites": self.prerequisites,
            "docs": self.docs,
            "required_tools": self.required_tools(),
            "variables": [v.to_dict() for v in self.variables],
            "steps": [s.to_dict() for s in self.steps],
            "teardown": [t.to_dict() for t in self.teardown],
            "path": str(self.path) if self.path else None,
            "guide": str(self.guide_path) if self.guide_path else None,
        }


def freshness(bp: Blueprint, today: date | None = None, stale_days: int = STALE_AFTER_DAYS) -> dict[str, Any]:
    """How recently a blueprint's commands were checked, and how."""
    today = today or date.today()
    verified = parse_date(bp.last_verified)
    age = (today - verified).days if verified else None
    return {
        "last_verified": bp.last_verified,
        "age_days": age,
        "stale": age is None or age > stale_days,
        "stale_after_days": stale_days,
        "verification_status": bp.verification_status,
        "verified_versions": dict(bp.verified_versions),
        "notes": bp.verification_notes,
    }


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def blueprint_from_data(data: dict[str, Any], path: Path | None = None) -> Blueprint:
    return Blueprint(
        id=data["id"],
        name=data["name"].strip(),
        category=data["category"].strip(),
        tagline=data["tagline"].strip(),
        when_to_use=list(data["when_to_use"]),
        when_not_to_use=list(data["when_not_to_use"]),
        services=[dict(s) for s in data["services"]],
        estimated_cost=data["estimated_cost"].strip(),
        variables=[Variable(key=v["key"], prompt=v["prompt"],
                            default=None if v.get("default") is None else str(v["default"]),
                            pattern=v.get("pattern"), example=v.get("example"))
                   for v in data["variables"]],
        steps=[Step(id=s["id"], description=s["description"].strip(), command=s.get("command"),
                    risk=s.get("risk", "low"), public_exposure=bool(s.get("public_exposure", False)),
                    verify_command=s.get("verify_command"), verify_expect=s.get("verify_expect"),
                    manual=bool(s.get("manual", False)), timeout_seconds=int(s.get("timeout_seconds", 1800)),
                    skip_if=s.get("skip_if"), skip_if_expect=s.get("skip_if_expect"))
               for s in data["steps"]],
        teardown=[TeardownStep(id=t["id"], description=t["description"].strip(), command=t.get("command"),
                               risk=t.get("risk", "high"), manual=bool(t.get("manual", False)),
                               undoes=list(t.get("undoes") or []))
                  for t in data["teardown"]],
        maintainer=str(data["maintainer"]).strip(),
        last_verified=_iso(parse_date(data["last_verified"])),
        verification_status=data["verification_status"],
        verified_versions={str(k): str(v) for k, v in data["verified_versions"].items()},
        verification_notes=str(data.get("verification_notes") or "").strip(),
        keywords=list(data.get("keywords") or []),
        requires_tools=list(data.get("requires_tools") or []),
        prerequisites=list(data.get("prerequisites") or []),
        docs=list(data.get("docs") or []),
        path=path,
    )


def load_blueprint_file(folder: Path) -> Blueprint:
    folder = Path(folder)
    file = folder / "blueprint.yaml"
    if not file.is_file():
        raise BlueprintNotFound(f"no blueprint.yaml in '{folder}'")
    try:
        data = safe_load_blueprint_text(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BlueprintValidationError(folder.name, [f"YAML parse error: {exc}"]) from exc
    except UnicodeDecodeError as exc:
        raise BlueprintValidationError(folder.name, [f"blueprint.yaml is not UTF-8 text ({exc.reason})"]) from exc
    except (ValueError, RecursionError, OSError) as exc:
        raise BlueprintValidationError(folder.name, [f"cannot read blueprint.yaml: {exc}"]) from exc
    errors = validate_blueprint_data(data, folder.name)
    guide = folder / "GUIDE.md"
    if not guide.is_file():
        errors.append("missing GUIDE.md next to blueprint.yaml")
    else:
        try:
            if re.search(r"\bTODO\b", guide.read_text(encoding="utf-8")):
                errors.append("GUIDE.md still contains TODO placeholders")
        except (UnicodeDecodeError, OSError) as exc:
            errors.append(f"GUIDE.md cannot be read as UTF-8 text ({exc})")
    if errors:
        raise BlueprintValidationError(folder.name, errors)
    return blueprint_from_data(data, folder)


class Catalog:
    """All blueprints found in one directory."""

    def __init__(self, root: Path, source: str = "explicit"):
        self.root = Path(root)
        self.source = source
        self.blueprints: dict[str, Blueprint] = {}
        self.errors: dict[str, list[str]] = {}
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            try:
                bp = load_blueprint_file(folder)
            except BlueprintValidationError as exc:  # noqa: PERF203 - one entry per folder
                self.errors[folder.name] = exc.errors
            except BlueprintNotFound as exc:
                self.errors[folder.name] = [str(exc)]
            else:
                self.blueprints[bp.id] = bp

    @classmethod
    def load(cls, explicit: str | None = None) -> Catalog:
        root, source = resolve_blueprints_dir(explicit)
        return cls(root, source)

    def get(self, bp_id: str) -> Blueprint:
        if bp_id in self.blueprints:
            return self.blueprints[bp_id]
        if bp_id in self.errors:
            raise BlueprintValidationError(bp_id, self.errors[bp_id])
        known = ", ".join(sorted(self.blueprints)) or "(none)"
        raise BlueprintNotFound(f"unknown blueprint '{bp_id}'.{_did_you_mean(bp_id, self.blueprints)} "
                                f"Available: {known}")

    def __iter__(self) -> Iterator[Blueprint]:
        return iter(self.blueprints.values())


def _did_you_mean(word: str, choices: Iterable[str]) -> str:
    close = difflib.get_close_matches(word, list(choices), n=1, cutoff=0.6)
    return f" Did you mean '{close[0]}'?" if close else ""


# --------------------------------------------------------------------------- variables & rendering


def check_value(var: Variable, value: str) -> None:
    if not isinstance(value, str):
        value = str(value)
    if value == "":
        raise VariableError(f"variable '{var.key}' cannot be empty")
    if len(value) > MAX_VALUE_LENGTH:
        raise VariableError(f"variable '{var.key}' is {len(value)} characters long (max {MAX_VALUE_LENGTH})")
    bad = sorted({f"U+{ord(c):04X}" for c in value if unicodedata.category(c) in FORBIDDEN_VALUE_CATEGORIES})
    if bad:
        raise VariableError(f"variable '{var.key}' contains control characters, newlines or invisible/bidi "
                            f"characters ({', '.join(bad[:5])}); these could hide what a command really does")
    if value != value.strip():
        raise VariableError(f"variable '{var.key}' has leading or trailing whitespace")
    if value.startswith("-"):
        raise VariableError(f"variable '{var.key}' cannot start with '-' (it could be read as a flag)")
    if "{{" in value or "}}" in value:
        raise VariableError(f"variable '{var.key}' cannot contain '{{{{' or '}}}}'")
    if var.pattern and not re.fullmatch(var.pattern, value):
        hint = f" (example: {var.example})" if var.example else ""
        raise VariableError(f"variable '{var.key}'='{value}' does not match required pattern {var.pattern}{hint}")


def resolve_variables(bp: Blueprint,
                      provided: Mapping[str, Any] | None = None) -> tuple[dict[str, str], list[Variable]]:
    """Merge provided values with defaults. Returns (values, missing)."""
    provided = dict(provided or {})
    unknown = set(provided) - {v.key for v in bp.variables}
    if unknown:
        declared = [v.key for v in bp.variables]
        tips = "".join(_did_you_mean(u, declared) for u in sorted(unknown))
        raise VariableError(f"unknown variable(s) for '{bp.id}': {sorted(unknown)}.{tips} "
                            f"Declared: {declared}")
    values: dict[str, str] = {}
    missing: list[Variable] = []
    for var in bp.variables:
        if var.key in provided and provided[var.key] is not None:
            value = str(provided[var.key])
        elif var.default is not None:
            value = var.default
        else:
            missing.append(var)
            continue
        check_value(var, value)
        values[var.key] = value
    return values, missing


def render_argv(template: str, values: Mapping[str, str]) -> list[str]:
    """Tokenise ``template`` first, then substitute values inside tokens.

    Raises VariableError on any unresolved placeholder.
    """
    argv = []
    for token in shlex.split(template):
        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise VariableError(f"unresolved placeholder '{{{{{key}}}}}' in: {template}")
            return values[key]

        rendered = PLACEHOLDER_RE.sub(sub, token)
        if "{{" in rendered or "}}" in rendered:
            raise VariableError(f"unresolved placeholder in: {template}")
        argv.append(rendered)
    return argv


def render_text(template: str, values: Mapping[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise VariableError(f"unresolved placeholder '{{{{{key}}}}}' in: {template}")
        return values[key]

    out = PLACEHOLDER_RE.sub(sub, template)
    if "{{" in out or "}}" in out:
        raise VariableError(f"unresolved placeholder in: {template}")
    return out


def display(argv: Sequence[str]) -> str:
    return shlex.join(argv)


# --------------------------------------------------------------------------- planning


@dataclass
class PlannedStep:
    index: int
    id: str
    description: str
    risk: str
    public_exposure: bool
    manual: bool
    argv: list[str] | None
    verify_argv: list[str] | None
    verify_expect: str | None
    timeout_seconds: int = 1800
    skip_argv: list[str] | None = None
    skip_if_expect: str | None = None

    @property
    def command(self) -> str | None:
        return display(self.argv) if self.argv else None

    @property
    def verify_command(self) -> str | None:
        return display(self.verify_argv) if self.verify_argv else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "id": self.id, "description": self.description, "risk": self.risk,
            "public_exposure": self.public_exposure, "manual": self.manual, "command": self.command,
            "argv": self.argv, "verify_command": self.verify_command, "verify_argv": self.verify_argv,
            "verify_expect": self.verify_expect,
            "skip_if": display(self.skip_argv) if self.skip_argv else None,
            "skip_argv": self.skip_argv, "skip_if_expect": self.skip_if_expect,
        }


@dataclass
class PlannedTeardown:
    id: str
    description: str
    risk: str
    manual: bool
    argv: list[str] | None
    undoes: list[str] = field(default_factory=list)

    @property
    def command(self) -> str | None:
        return display(self.argv) if self.argv else None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "risk": self.risk, "manual": self.manual,
                "command": self.command, "argv": self.argv, "undoes": list(self.undoes)}


@dataclass
class Plan:
    blueprint: Blueprint
    variables: dict[str, str]
    steps: list[PlannedStep]
    teardown: list[PlannedTeardown]
    tools: dict[str, str | None]

    @property
    def high_risk_steps(self) -> list[str]:
        return [s.id for s in self.steps if s.risk == "high"]

    @property
    def public_steps(self) -> list[str]:
        return [s.id for s in self.steps if s.public_exposure]

    @property
    def missing_tools(self) -> list[str]:
        return [t for t, p in self.tools.items() if p is None]

    def warnings(self) -> list[str]:
        out = []
        if self.missing_tools:
            out.append("missing tools (apply will refuse to start): " + ", ".join(
                f"{t} -> {TOOL_HINTS.get(t, 'install it and make sure it is on PATH')}" for t in self.missing_tools))
        if self.high_risk_steps:
            out.append("high-risk steps need --approve-high-risk: " + ", ".join(self.high_risk_steps))
        if self.public_steps:
            out.append("these steps create publicly reachable endpoints: " + ", ".join(self.public_steps))
        manual = [s.id for s in self.steps if s.manual]
        if manual:
            out.append("manual steps pause the run until you confirm them: " + ", ".join(manual))
        out.append("cost: " + self.blueprint.estimated_cost)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "blueprint_id": self.blueprint.id,
            "name": self.blueprint.name,
            "mode": "dry-run",
            "variables": self.variables,
            "tools": self.tools,
            "prerequisites": self.blueprint.prerequisites,
            "steps": [s.to_dict() for s in self.steps],
            "teardown": [t.to_dict() for t in self.teardown],
            "summary": {
                "step_count": len(self.steps),
                "high_risk_steps": self.high_risk_steps,
                "public_exposure_steps": self.public_steps,
                "missing_tools": self.missing_tools,
                "estimated_cost": self.blueprint.estimated_cost,
                "warnings": self.warnings(),
            },
        }


def check_tools(tools: Iterable[str], which: Callable[[str], str | None] | None = None) -> dict[str, str | None]:
    which = which or shutil.which
    return {t: which(t) for t in sorted(set(tools))}


def build_plan(bp: Blueprint, provided: Mapping[str, Any] | None = None,
               which: Callable[[str], str | None] | None = None) -> Plan:
    """Pure dry-run plan. Never mutates anything."""
    values, missing = resolve_variables(bp, provided)
    if missing:
        lines = [f"  --var {v.key}=...   # {v.prompt}" + (f" (e.g. {v.example})" if v.example else "")
                 for v in missing]
        raise VariableError(f"missing required variable(s) for '{bp.id}':\n" + "\n".join(lines))
    steps = []
    for i, s in enumerate(bp.steps, start=1):
        steps.append(PlannedStep(
            index=i, id=s.id, description=render_text(s.description, values), risk=s.risk,
            public_exposure=s.public_exposure, manual=s.manual,
            argv=render_argv(s.command, values) if s.command else None,
            verify_argv=render_argv(s.verify_command, values) if s.verify_command else None,
            verify_expect=render_text(s.verify_expect, values) if s.verify_expect else None,
            timeout_seconds=s.timeout_seconds,
            skip_argv=render_argv(s.skip_if, values) if s.skip_if else None,
            skip_if_expect=render_text(s.skip_if_expect, values) if s.skip_if_expect else None,
        ))
    teardown = [PlannedTeardown(id=t.id, description=render_text(t.description, values), risk=t.risk,
                                manual=t.manual, argv=render_argv(t.command, values) if t.command else None,
                                undoes=list(t.undoes))
                for t in bp.teardown]
    return Plan(blueprint=bp, variables=values, steps=steps, teardown=teardown,
                tools=check_tools(bp.required_tools(), which))


# --------------------------------------------------------------------------- state


def resolve_state_path(state_dir: str | os.PathLike[str] | None = None) -> Path:
    base = state_dir or os.environ.get(STATE_DIR_ENV) or (Path.cwd() / ".doogle")
    return Path(base).expanduser() / "state.json"


def _lock_fd(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        try:
            import msvcrt
        except ImportError:
            return
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        try:
            import msvcrt
        except ImportError:
            return
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _check_run_shape(run: Any, where: str) -> None:
    if not isinstance(run, dict):
        raise StateError(f"{where} is not an object")
    if not isinstance(run.get("run_id"), str) or not run["run_id"]:
        raise StateError(f"{where} has no run_id")
    for key in ("steps", "teardown"):
        items = run.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise StateError(f"{where} ('{run['run_id']}') has a malformed '{key}' list")
    if not isinstance(run.get("variables", {}), dict):
        raise StateError(f"{where} ('{run['run_id']}') has malformed variables")


class StateStore:
    """``.doogle/state.json``: a list of run records.

    Writes are atomic (temp file + ``os.replace``), the file is created ``0600`` inside a
    ``0700`` directory, the previous version is kept as ``state.json.bak``, and every
    read-modify-write holds an exclusive lock so concurrent ``doogle`` processes cannot
    lose each other's runs.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _ensure_dir(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(parent, 0o700)
        ignore = parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# Created by doogle: never commit run state.\n*\n", encoding="utf-8")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_dir()
        fd = os.open(self.path.parent / ".state.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_fd(fd)
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock_fd(fd)
            os.close(fd)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"state_version": STATE_VERSION, "runs": []}
        hint = (f"; a previous copy is at '{self.backup_path}'" if self.backup_path.exists() else "")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateError(f"state file '{self.path}' is not valid JSON ({exc}); "
                             f"move it aside to start fresh{hint}") from exc
        except OSError as exc:
            raise StateError(f"cannot read state file '{self.path}': {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            raise StateError(f"state file '{self.path}' has an unexpected structure{hint}")
        version = data.get("state_version", STATE_VERSION)
        if not isinstance(version, int) or version > STATE_VERSION:
            raise StateError(f"state file '{self.path}' was written by a newer doogle "
                             f"(state_version {version!r}); upgrade doogle to read it")
        for i, run in enumerate(data["runs"]):
            _check_run_shape(run, f"state file '{self.path}': runs[{i}]")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            if self.path.exists():
                with contextlib.suppress(OSError):
                    shutil.copy2(self.path, self.backup_path)
                    os.chmod(self.backup_path, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = self.load()["runs"]
        return runs

    def get(self, run_id: str) -> dict[str, Any]:
        runs = self.runs()
        if not runs:
            raise StateError(f"no runs recorded in '{self.path}' (runs are recorded by 'doogle plan <id> --apply')")
        if run_id == "latest":
            return runs[-1]
        exact = [r for r in runs if r["run_id"] == run_id]
        if exact:
            return exact[-1]
        matches = [r for r in runs if r["run_id"].startswith(run_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise StateError(f"no run '{run_id}' in '{self.path}' (see: doogle history)")
        raise StateError(f"run id prefix '{run_id}' is ambiguous ({len(matches)} runs match; use more characters)")

    def save_run(self, run: dict[str, Any]) -> None:
        _check_run_shape(run, "run record")
        with self._locked():
            data = self.load()
            for i, existing in enumerate(data["runs"]):
                if existing["run_id"] == run["run_id"]:
                    data["runs"][i] = run
                    break
            else:
                data["runs"].append(run)
            self._write(data)


# --------------------------------------------------------------------------- execution


Runner = Callable[[Sequence[str], int], "CommandResult"]


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def subprocess_runner(argv: Sequence[str], timeout: int) -> CommandResult:
    """Run argv without a shell (never ``shell=True``); capture output as UTF-8 with replacement."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",  # nosec B603 - argv, no shell
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout}s")
    except OSError as exc:
        return CommandResult(126, "", f"cannot run {argv[0]}: {exc}")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# Secrets that CLIs sometimes echo. Output tails are redacted before they are printed or stored.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?![A-Za-z0-9.-]*gserviceaccount\.com\b)"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "<redacted:private-key>"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "<redacted:google-api-key>"),
    (re.compile(r"ya29\.[0-9A-Za-z_\-.]+"), "<redacted:oauth-token>"),
    (re.compile(r"1//[0-9A-Za-z_\-]{20,}"), "<redacted:refresh-token>"),
    (re.compile(r"GOCSPX-[0-9A-Za-z_\-]{10,}"), "<redacted:client-secret>"),
    (re.compile(r"eyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}"), "<redacted:jwt>"),
    (re.compile(r"(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{40,})"), "<redacted:github-token>"),
    (re.compile(r"(?:sk-[0-9A-Za-z_\-]{20,}|xox[abposr]-[0-9A-Za-z\-]{10,})"), "<redacted:token>"),
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+"), r"\1<redacted>"),
    (re.compile(r"""(?i)(["']?(?:access_token|refresh_token|id_token|client_secret|private_key(?:_id)?|"""
                r"""api[_-]?key|password|passwd|secret|token)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;}&]+)"""),
     r"\1<redacted>"),
    (re.compile(r"(?i)([?&](?:token|key|access_token|sig|signature|code)=)[^&\s\"']+"), r"\1<redacted>"),
    (_EMAIL_RE, "<redacted:email>"),
]


def redact(text: str) -> str:
    """Mask tokens, keys, private keys and personal email addresses (service-account emails are kept)."""
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# Known failure signatures -> one actionable next step. Matched against redacted output.
FAILURE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"do not currently have an active account|gcloud auth login|Reauthentication (is )?required"
                r"|invalid_grant", re.I),
     "gcloud is not logged in (or the login expired): run 'gcloud auth login', then resume."),
    (re.compile(r"Failed to authenticate, have you run firebase login|firebase login", re.I),
     "firebase is not logged in: run 'firebase login', then resume."),
    (re.compile(r"clasp login|Could not read API credentials|not logged in", re.I),
     "clasp is not logged in: run 'clasp login', then resume."),
    (re.compile(r"BILLING_DISABLED|billing (account|is) (not|disabled)|Billing account .{0,80}not found"
                r"|Billing must be enabled|requires billing|billing to be enabled", re.I),
     "billing is not enabled on this project: link a billing account in the Cloud console, then resume."),
    (re.compile(r"SERVICE_DISABLED|has not been used in project|API .* is disabled|it is disabled", re.I),
     "a required API is not enabled yet (enabling can take a few minutes to propagate): wait, then resume."),
    (re.compile(r"project .{0,80}(not found|does not exist)|NOT_FOUND: .*project|Unknown project", re.I),
     "the project was not found: check project_id (the ID, not the display name) with 'gcloud projects list'."),
    (re.compile(r"PERMISSION_DENIED|does not have (the )?permission|permission denied|"
                r"(HTTP|Error|status|code)[ =:]*403\b|\(403\)|"
                r"caller does not have permission", re.I),
     "permission denied: the active account lacks a role on this project, or project_id is wrong. "
     "Check 'gcloud auth list' and 'gcloud projects get-iam-policy <project_id>'."),
    (re.compile(r"Quota exceeded|RESOURCE_EXHAUSTED", re.I),
     "a quota was hit: check the quotas page for this project, or retry later."),
]


def diagnose(text: str | None) -> str | None:
    """Return a one-line next step for a recognised failure, else None."""
    if not text:
        return None
    for pattern, hint in FAILURE_HINTS:
        if pattern.search(text):
            return hint
    return None


def _tail(text: str, lines: int = 15) -> str:
    return redact("\n".join((text or "").strip().splitlines()[-lines:]))[-4000:]


def _output(result: CommandResult, lines: int = 15) -> str:
    return _tail(result.stdout + ("\n" + result.stderr if result.stderr else ""), lines)


def new_run_record(plan: Plan, mode: str = "apply") -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "run_id": f"{stamp}-{plan.blueprint.id}-{secrets.token_hex(3)}",
        "blueprint_id": plan.blueprint.id,
        "doogle_version": _version(),
        "mode": mode,
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "cwd": str(Path.cwd()),
        "variables": dict(plan.variables),
        "steps": [{
            "id": s.id, "description": s.description, "risk": s.risk, "public_exposure": s.public_exposure,
            "manual": s.manual, "command": s.command, "argv": s.argv, "verify_command": s.verify_command,
            "verify_argv": s.verify_argv, "verify_expect": s.verify_expect, "status": "pending",
            "started_at": None, "finished_at": None, "exit_code": None, "verify_status": None,
            "output_tail": None, "timeout_seconds": s.timeout_seconds,
            "skip_argv": s.skip_argv, "skip_if_expect": s.skip_if_expect, "skip_reason": None,
        } for s in plan.steps],
        "teardown": [dict(t.to_dict(), status="not_run", exit_code=None, finished_at=None) for t in plan.teardown],
        "teardown_status": "not_run",
    }


def _version() -> str:
    from . import __version__
    return __version__


@dataclass
class Gates:
    """Inputs to the safety gates. ``confirm`` is None when non-interactive."""

    yes: bool = False
    approve_high_risk: bool = False
    confirm: Callable[[str], bool] | None = None


def enforce_gates(*, tools: Mapping[str, str | None], high_risk: Sequence[str], gates: Gates,
                  summary: str, what: str = "apply") -> None:
    missing = [t for t, p in tools.items() if p is None]
    if missing:
        raise SafetyGateError("refusing to start: required tool(s) not found on PATH: " + ", ".join(
            f"{t} ({TOOL_HINTS.get(t, 'install it')})" for t in missing) + ". Nothing was run.")
    if high_risk and not gates.approve_high_risk:
        raise SafetyGateError("refusing to start: high-risk step(s) " + ", ".join(high_risk)
                              + " require --approve-high-risk. Nothing was run.")
    if gates.yes:
        return
    if gates.confirm is None:
        raise SafetyGateError(f"refusing to {what} without confirmation in a non-interactive session; "
                              "review the plan and pass --yes. Nothing was run.")
    if not gates.confirm(summary):
        raise SafetyGateError(f"{what} cancelled at the confirmation prompt. Nothing was run.")


# Step statuses meaning Doogle may have created (part of) a resource in this run.
TOUCHED_STATUSES = {"succeeded", "failed", "verify_failed", "running", "interrupted"}


def teardown_preview(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Decide, per recorded teardown entry, whether it would run and why.

    An entry that declares ``undoes: [step ids]`` only runs if at least one of those steps was
    actually executed by this run. Steps skipped by ``skip_if`` (the resource already existed)
    or never reached do not count, so teardown never deletes something Doogle did not create.
    """
    steps = {s.get("id"): s for s in run.get("steps") or [] if isinstance(s, dict)}
    out = []
    for t in run.get("teardown") or []:
        entry = {"id": t.get("id"), "run": bool(t.get("argv")), "reason": None}
        if not t.get("argv"):
            entry["reason"] = "manual"
        else:
            undoes = t.get("undoes") or []
            if undoes:
                touched = [u for u in undoes if u in steps and steps[u].get("status") in TOUCHED_STATUSES
                           and not steps[u].get("skip_reason")]
                if not touched:
                    entry["run"] = False
                    states = ", ".join(f"{u}={(steps.get(u) or {}).get('status', 'missing')}"
                                       + (" (already existed)" if (steps.get(u) or {}).get("skip_reason") else "")
                                       for u in undoes)
                    entry["reason"] = (f"not created by this run ({states}); skipped so Doogle never deletes "
                                       "resources it did not create")
        out.append(entry)
    return out


class Executor:
    """Runs a plan step by step, persisting state after every transition."""

    def __init__(self, store: StateStore, runner: Runner | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 confirm_manual: Callable[[dict[str, Any]], bool] | None = None):
        self.store = store
        self.runner: Runner = runner or subprocess_runner
        self.on_event = on_event or (lambda kind, payload: None)
        self.confirm_manual = confirm_manual

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.on_event(kind, payload)

    def apply(self, plan: Plan, gates: Gates) -> dict[str, Any]:
        enforce_gates(tools=plan.tools, high_risk=plan.high_risk_steps, gates=gates,
                      summary=f"Apply blueprint '{plan.blueprint.id}' ({len(plan.steps)} steps)?")
        run = new_run_record(plan)
        self.store.save_run(run)
        return self._execute(run, assume_manual_done=set())

    def resume(self, run: dict[str, Any], plan: Plan, gates: Gates) -> dict[str, Any]:
        _check_run_shape(run, "run")
        if run.get("status") == "succeeded":
            raise StateError(f"run {run['run_id']} already succeeded; nothing to resume")
        if run.get("teardown_status") not in (None, "not_run"):
            raise StateError(f"run {run['run_id']} has been torn down; start a new run instead")
        recorded = [s.get("id") for s in run["steps"]]
        if recorded != [s.id for s in plan.steps]:
            raise StateError("the blueprint's steps changed since this run was recorded; start a new run")
        # Never execute commands read back from disk: re-render them from the blueprint and the
        # recorded variables, and refuse if what is on disk differs (edited state or changed blueprint).
        for rec, planned in zip(run["steps"], plan.steps, strict=True):
            fresh = {"argv": planned.argv, "verify_argv": planned.verify_argv, "skip_argv": planned.skip_argv}
            if rec.get("status") != "succeeded" and any(rec.get(k) != v for k, v in fresh.items()):
                raise StateError(f"step '{rec['id']}' recorded in run {run['run_id']} no longer matches the "
                                 f"blueprint '{plan.blueprint.id}' (the state file was edited or the blueprint "
                                 "changed). Nothing was run; start a new run with 'doogle plan ... --apply'.")
            rec.update(fresh, risk=planned.risk, manual=planned.manual, verify_expect=planned.verify_expect,
                       skip_if_expect=planned.skip_if_expect, public_exposure=planned.public_exposure,
                       timeout_seconds=planned.timeout_seconds)
        pending_high = [s["id"] for s in run["steps"] if s["risk"] == "high" and s.get("status") != "succeeded"]
        enforce_gates(tools=plan.tools, high_risk=pending_high, gates=gates,
                      summary=f"Resume run {run['run_id']}?", what="resume")
        paused_manual = {s["id"] for s in run["steps"] if s.get("status") == "awaiting_manual"}
        for s in run["steps"]:
            if s.get("status") != "succeeded":
                s.update(status="pending", exit_code=None, verify_status=None, output_tail=None, skip_reason=None,
                         hint=None)
        run.update(status="running", finished_at=None, resumed_at=utc_now())
        self.store.save_run(run)
        return self._execute(run, assume_manual_done=paused_manual)

    def _finish(self, run: dict[str, Any], status: str) -> dict[str, Any]:
        run["status"] = status
        run["finished_at"] = utc_now()
        for s in run["steps"]:
            if s["status"] == "pending":
                s["status"] = "skipped"
        self.store.save_run(run)
        self._emit("run_finished", run)
        return run

    def _execute(self, run: dict[str, Any], assume_manual_done: set[str]) -> dict[str, Any]:
        current: dict[str, Any] | None = None
        try:
            for step in run["steps"]:
                if step["status"] == "succeeded":
                    continue
                current = step
                result = self._run_step(run, step, assume_manual_done)
                if result is not None:
                    return result
            current = None
            return self._finish(run, "succeeded")
        except KeyboardInterrupt:
            if current is not None:
                current["status"] = "interrupted"
                current["finished_at"] = utc_now()
            run["status"] = "interrupted"
            run["finished_at"] = utc_now()
            self.store.save_run(run)
            self._emit("run_finished", run)
            raise

    def _run_step(self, run: dict[str, Any], step: dict[str, Any],
                  assume_manual_done: set[str]) -> dict[str, Any] | None:
        """Execute one step. Returns the finished run if the run stops here, else None."""
        step["status"] = "running"
        step["started_at"] = utc_now()
        self.store.save_run(run)
        self._emit("step_started", step)

        if step["manual"]:
            done = step["id"] in assume_manual_done or (
                self.confirm_manual is not None and self.confirm_manual(step))
            step["finished_at"] = utc_now()
            if not done:
                step["status"] = "awaiting_manual"
                self._emit("step_finished", step)
                return self._finish(run, "paused")
            step["status"] = "succeeded"
            self.store.save_run(run)
            self._emit("step_finished", step)
            return None

        already = False
        if step.get("skip_argv"):
            sres = self.runner(step["skip_argv"], 120)
            expect = step.get("skip_if_expect")
            already = sres.returncode == 0 and (not expect or expect in sres.stdout)
        if already:
            step["skip_reason"] = "already done: skip_if check matched, command not run"
            result = CommandResult(0, "", "")
        else:
            result = self.runner(step["argv"], step.get("timeout_seconds") or 1800)
        step["exit_code"] = result.returncode
        step["output_tail"] = _output(result)
        if result.returncode != 0:
            step["status"] = "failed"
            step["hint"] = diagnose(step["output_tail"])
            step["finished_at"] = utc_now()
            self._emit("step_finished", step)
            return self._finish(run, "failed")

        if step["verify_argv"]:
            vres = self.runner(step["verify_argv"], 120)
            ok = vres.returncode == 0 and (not step["verify_expect"] or step["verify_expect"] in vres.stdout)
            step["verify_status"] = "passed" if ok else "failed"
            step["verify_output_tail"] = _output(vres, 5)
            if not ok:
                step["status"] = "verify_failed"
                step["hint"] = diagnose(step["verify_output_tail"])
                step["finished_at"] = utc_now()
                self._emit("step_finished", step)
                return self._finish(run, "failed")
        else:
            step["verify_status"] = "not_defined"
        step["status"] = "succeeded"
        step["finished_at"] = utc_now()
        self.store.save_run(run)
        self._emit("step_finished", step)
        return None

    def teardown(self, run: dict[str, Any], gates: Gates, tools: Mapping[str, str | None] | None = None,
                 which: Callable[[str], str | None] | None = None,
                 expected: Sequence[PlannedTeardown] | None = None) -> dict[str, Any]:
        """Run the recorded teardown. ``expected`` (re-rendered from the current blueprint) is
        compared with what is on disk; differences are reported as warnings."""
        _check_run_shape(run, "run")
        entries = run.get("teardown") or []
        preview = {p["id"]: p for p in teardown_preview(run)}
        automated = [t for t in entries if t.get("argv") and preview.get(t.get("id"), {}).get("run")]
        problems = [p for t in automated for p in recorded_argv_problems(t["argv"], f"teardown '{t.get('id')}'")]
        if problems:
            raise StateError("refusing to tear down: the state file contains commands Doogle would never "
                             "generate (was it edited?): " + "; ".join(problems) + ". Nothing was run.")
        warnings = []
        if expected is not None:
            want = {t.id: t.argv for t in expected}
            for t in automated:
                if t.get("id") in want and want[t["id"]] != t["argv"]:
                    warnings.append(f"teardown '{t['id']}' differs from the current blueprint; running the "
                                    "recorded command")
        if tools is None:
            tools = check_tools([t["argv"][0] for t in automated], which)
        high = [t["id"] for t in automated if t.get("risk", "high") == "high"]
        enforce_gates(tools=tools, high_risk=high, gates=gates,
                      summary=f"Tear down run {run['run_id']} ({len(automated)} commands)?", what="teardown")
        run["teardown_warnings"] = warnings
        failures = 0
        current: dict[str, Any] | None = None
        try:
            for t in entries:
                if not t.get("argv"):
                    t["status"] = "manual_required"
                    continue
                if not preview.get(t.get("id"), {}).get("run"):
                    t["status"] = "skipped_not_created"
                    t["skip_reason"] = preview.get(t.get("id"), {}).get("reason")
                    self._emit("teardown_skipped", t)
                    continue
                current = t
                self._emit("teardown_started", t)
                res = self.runner(t["argv"], 1800)
                t["exit_code"] = res.returncode
                t["finished_at"] = utc_now()
                t["status"] = "succeeded" if res.returncode == 0 else "failed"
                t["output_tail"] = _output(res, 5)
                t["hint"] = diagnose(t["output_tail"]) if res.returncode else None
                failures += res.returncode != 0
                self.store.save_run(run)
                self._emit("teardown_finished", t)
            current = None
        except KeyboardInterrupt:
            if current is not None:
                current["status"] = "interrupted"
            run["teardown_status"] = "interrupted"
            run["teardown_finished_at"] = utc_now()
            self.store.save_run(run)
            raise
        if not automated:
            run["teardown_status"] = "nothing_to_run"
        else:
            run["teardown_status"] = ("succeeded" if failures == 0
                                      else ("failed" if failures == len(automated) else "partial"))
        run["teardown_finished_at"] = utc_now()
        self.store.save_run(run)
        return run


# --------------------------------------------------------------------------- routing ("ask")


STOPWORDS = set("""a an and are as at be but by can do for from get have i if in into is it its just me my
need of on or our so some that the their them then there this to too up use using want we what when which
who will with without would you your like make build set setup something thing things very really how now
please help any all should could""".split())  # noqa: SIM905

SYNONYMS = {
    "cheap": ["cost", "free", "budget", "scale-to-zero"],
    "cheapest": ["cheap"], "inexpensive": ["cheap"], "affordable": ["cheap"], "low-cost": ["cheap"],
    "free": ["cheap"], "budget": ["cheap"], "tiny": ["cheap"],
    "api": ["backend", "endpoint", "rest"], "apis": ["api"], "endpoint": ["api"], "backend": ["api"],
    "microservice": ["api", "container"], "server": ["backend"], "service": ["backend"],
    "hobby": ["side-project"], "side": ["side-project"], "prototype": ["side-project"], "mvp": ["side-project"],
    "hackathon": ["mvp"],
    "agent": ["ai", "llm"], "agents": ["agent"], "chatbot": ["agent", "ai"],
    "assistant": ["agent"], "llm": ["ai", "gemini"], "genai": ["ai", "gemini"], "gpt": ["llm"],
    "langchain": ["agent"],
    "spreadsheet": ["sheet"], "sheets": ["sheet"], "excel": ["spreadsheet"], "form": ["sheet", "form"],
    "forms": ["form"], "zap": ["automation"], "zapier": ["automation", "webhook"], "automate": ["automation"],
    "workflow": ["automation"], "nocode": ["no-code"], "lowcode": ["no-code"], "glue": ["automation"],
    "docker": ["container"], "containers": ["container"], "containerize": ["container"],
    "serverless": ["scale-to-zero"],
    "registration": ["signup"], "register": ["signup"], "login": ["auth"], "signup": ["auth"],
    "nosql": ["firestore", "database"], "db": ["database"],
    "keyless": ["adc", "iam"], "workload-identity": ["adc", "iam"],
}

# Two-word phrases folded into one token before matching, so "log in" is not "log".
PHRASES = {"log in": "login", "sign in": "sign-in", "sign up": "signup", "side project": "side-project",
           "scale to zero": "scale-to-zero", "no code": "no-code", "low code": "low-code",
           "workload identity": "workload-identity", "web app": "web-app"}

# Products and goals no blueprint covers. Mentioning one caps confidence at "low" unless a
# blueprint also matches strongly, so Doogle says "no confident match" instead of guessing.
OUT_OF_SCOPE = {
    "bigquery", "warehouse", "gke", "kubernetes", "k8s", "compute-engine", "vm", "vms", "cloud-sql", "sql",
    "postgres", "mysql", "s3", "aws", "azure", "terraform", "vpn", "interconnect", "analytics", "monitoring",
    "dashboards", "alert", "alerts", "pubsub", "pub-sub", "dataflow", "dataproc", "tpu", "tpus", "gpu",
    "ios", "android", "push", "notifications", "wordpress", "video", "stream", "streaming", "cdn", "domain",
    "email", "emails", "marketing", "backup", "backups", "cron", "learn", "weather", "train", "training",
    "classification", "fine-tune", "fine-tuning",
}
# Keywords too generic to be trusted on their own (worth less than a specific keyword).
GENERIC_KEYWORDS = {"api", "users", "database", "migrate", "migration", "production", "service account",
                    "iam", "website", "frontend", "backend", "http", "container", "ai", "form", "automation",
                    "workspace", "enterprise", "startup", "gmail"}


def _singular(w: str) -> str:
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _canonical(text: str) -> str:
    """Lower-case, underscores and dots to spaces, singular words, common phrases folded."""
    words = [_singular(w) for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower().replace("_", " "))]
    out = " ".join(words)
    for phrase, token in PHRASES.items():
        out = re.sub(r"(?<![a-z0-9-])" + re.escape(phrase) + r"(?![a-z0-9-])", token, out)
    return out


def _normalise(text: str) -> list[str]:
    return [w for w in _canonical(text).split() if w not in STOPWORDS]


def _expand(tokens: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    frontier = list(tokens)
    while frontier:
        t = frontier.pop()
        if t in seen:
            continue
        seen.add(t)
        frontier.extend(SYNONYMS.get(t, []))
    return seen


def _phrase_in(phrase: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9-])" + re.escape(phrase) + r"(?![a-z0-9-])", text) is not None


def route_offline(goal: str, blueprints: Iterable[Blueprint], top: int = 3) -> dict[str, Any]:
    """Deterministic keyword/heuristic routing. No network.

    Scoring per blueprint: a specific keyword found in the goal = 3, a generic one = 1.5, a keyword
    reached only through a synonym = 1 (at most 2 in total), and 0.5 per other shared word in its
    description. Words that are part of a matched multi-word keyword (e.g. "api key") do not also
    count as single keywords elsewhere ("api"). Goals that mention an uncovered product get at most
    "low" confidence unless a blueprint matches strongly.
    """
    blueprints = list(blueprints)
    goal_c = _canonical(goal)
    raw_tokens = set(_normalise(goal))
    # multi-word keywords present in the goal consume their words
    consumed: set[str] = set()
    for bp in blueprints:
        for kw in bp.keywords:
            kw_c = _canonical(kw)
            if " " in kw_c and _phrase_in(kw_c, goal_c):
                consumed.update(kw_c.split())
    single_tokens = raw_tokens - consumed
    goal_tokens = _expand(raw_tokens)
    out_of_scope = sorted(raw_tokens & OUT_OF_SCOPE)
    results: list[dict[str, Any]] = []
    for bp in blueprints:
        score = 0.0
        synonym_score = 0.0
        reasons = []
        matched_words: set[str] = set()
        for kw in bp.keywords:
            kw_c = _canonical(kw)
            weight = 1.5 if kw.lower() in GENERIC_KEYWORDS else 3.0
            if (" " in kw_c and _phrase_in(kw_c, goal_c)) or (" " not in kw_c and kw_c in single_tokens):
                score += weight
                reasons.append(f"keyword '{kw}'")
                matched_words.update(kw_c.split())
            elif " " not in kw_c and kw_c in goal_tokens and kw_c not in consumed and synonym_score < 2:
                synonym_score += 1
                reasons.append(f"related to '{kw}'")
                matched_words.add(kw_c)
        score += synonym_score
        text_tokens = set(_normalise(" ".join([bp.name, bp.tagline, bp.category, *bp.when_to_use,
                                               *(s["name"] for s in bp.services)])))
        overlap = sorted((goal_tokens & text_tokens) - matched_words - consumed)
        if overlap:
            score += 0.5 * len(overlap)
            reasons.append("mentions " + ", ".join(overlap[:6]))
        anti_tokens = set(_normalise(" ".join(bp.when_not_to_use)))
        if score > 0:
            results.append({"blueprint_id": bp.id, "name": bp.name, "tagline": bp.tagline,
                            "score": round(score, 2), "reasons": reasons,
                            "watch_out": bp.when_not_to_use[0],
                            "_anti": len(goal_tokens & anti_tokens)})
    results.sort(key=lambda r: (-r["score"], r["_anti"], r["blueprint_id"]))
    for r in results:
        r.pop("_anti", None)
    matches = results[:top]
    if not matches:
        confidence = "none"
    elif matches[0]["score"] >= 6 and (len(matches) == 1 or matches[0]["score"] - matches[1]["score"] >= 3):
        confidence = "high"
    elif matches[0]["score"] >= 3:
        confidence = "medium"
    else:
        confidence = "low"
    if out_of_scope and matches and matches[0]["score"] < 6:
        confidence = "low"
    result = {
        "goal": goal,
        "mode": "offline",
        "confidence": confidence,
        "recommendation": matches[0]["blueprint_id"] if matches else None,
        "matches": matches,
        "next_steps": ([f"doogle explain {matches[0]['blueprint_id']}",
                        f"doogle plan {matches[0]['blueprint_id']} --var key=value ..."]
                       if matches else ["doogle list", "Describe the goal with product words, e.g. "
                                        "'API', 'agent', 'spreadsheet', 'webhook'"]),
    }
    result["confident"] = confidence in ("medium", "high")
    if out_of_scope:
        result["not_covered"] = out_of_scope
    return result


class AIUnavailable(DoogleError):
    pass


def ai_key_present(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    for name in AI_KEY_ENVS:
        if env.get(name):
            return name
    return None


def route_ai(goal: str, blueprints: Sequence[Blueprint], offline: dict[str, Any],
             env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Ask Gemini (via google-genai) to pick a blueprint. Advisory only: the
    answer must name a catalogued blueprint or it is discarded."""
    env = os.environ if env is None else env
    key_name = ai_key_present(env)
    if not key_name:
        raise AIUnavailable(f"no API key set ({' or '.join(AI_KEY_ENVS)})")
    try:
        from google import genai
    except ImportError as exc:
        raise AIUnavailable("google-genai is not installed; run: pip install 'doogle[ai]'") from exc
    catalog = [{"id": b.id, "tagline": b.tagline, "when_to_use": b.when_to_use,
                "when_not_to_use": b.when_not_to_use} for b in blueprints]
    prompt = (
        "You route a developer's goal to ONE blueprint from a fixed catalog, or to none.\n"
        "Reply with JSON only: {\"blueprint_id\": <id or null>, \"reason\": <one sentence>, "
        "\"alternative\": <a simpler or cheaper option to consider, one sentence>}.\n"
        f"Catalog: {json.dumps(catalog)}\n"
        f"Offline keyword router suggested: {offline.get('recommendation')}\n"
        f"Goal: {goal}"
    )
    model = env.get(GEMINI_MODEL_ENV) or DEFAULT_GEMINI_MODEL
    try:
        client = genai.Client(api_key=env[key_name])
        resp = client.models.generate_content(model=model, contents=prompt,
                                              config={"response_mime_type": "application/json"})
        data = json.loads(resp.text or "{}")
    except Exception as exc:  # network, quota, bad JSON: never fatal
        raise AIUnavailable(f"Gemini call failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
    ids = {b.id for b in blueprints}
    choice = data.get("blueprint_id")
    if choice is not None and choice not in ids:
        raise AIUnavailable(f"model suggested unknown blueprint '{choice}'; ignored")
    result = dict(offline)
    result.update(mode="ai", model=model, recommendation=choice,
                  ai_reason=str(data.get("reason", ""))[:500],
                  ai_alternative=str(data.get("alternative", ""))[:500],
                  offline_recommendation=offline.get("recommendation"))
    if choice:
        result["next_steps"] = [f"doogle explain {choice}", f"doogle plan {choice} --var key=value ..."]
    return result


def route(goal: str, catalog: Catalog, use_ai: bool = False, top: int = 3,
          env: Mapping[str, str] | None = None) -> dict[str, Any]:
    if not goal or not goal.strip():
        raise DoogleError("describe your goal, e.g. doogle ask \"a cheap API for my side project\"")
    bps = list(catalog)
    offline = route_offline(goal, bps, top=top)
    if not use_ai:
        return offline
    try:
        return route_ai(goal, bps, offline, env=env)
    except AIUnavailable as exc:
        offline["ai_note"] = f"AI routing unavailable, used offline routing: {exc}"
        return offline


# --------------------------------------------------------------------------- doctor


def _local_cmd(argv: Sequence[str], timeout: int = 15) -> str | None:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",  # nosec B603
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def doctor(blueprints_dir: str | None = None, state_dir: str | None = None,
           which: Callable[[str], str | None] | None = None,
           run_local: Callable[[Sequence[str]], str | None] | None = None,
           env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read-only environment diagnostics. Never touches cloud state or the network."""
    env = os.environ if env is None else env
    which = which or shutil.which
    run_local = run_local or _local_cmd
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, hint: str | None = None) -> None:
        item = {"name": name, "status": status, "detail": detail}
        if hint:
            item["hint"] = hint
        checks.append(item)

    v = sys.version_info
    add("python", "pass" if v >= (3, 10) else "fail", f"Python {v.major}.{v.minor}.{v.micro}",
        None if v >= (3, 10) else "Doogle needs Python 3.10+")
    add("pyyaml", "pass", f"PyYAML {getattr(yaml, '__version__', '?')}")

    catalog = None
    try:
        root, source = resolve_blueprints_dir(blueprints_dir, env)
        add("blueprints-dir", "pass", f"{root} (from {source})")
        catalog = Catalog(root, source)
        if catalog.errors:
            for bp_id, errs in catalog.errors.items():
                add(f"blueprint:{bp_id}", "fail", "; ".join(errs), "fix the blueprint (see CONTRIBUTING.md)")
        add("blueprints-valid", "pass" if not catalog.errors else "fail",
            f"{len(catalog.blueprints)} valid, {len(catalog.errors)} invalid")
    except DoogleError as exc:
        add("blueprints-dir", "fail", str(exc), f"pass --blueprints-dir or set ${BLUEPRINTS_ENV}")

    needed: dict[str, list[str]] = {}
    if catalog:
        for bp in catalog:
            for tool in bp.required_tools():
                needed.setdefault(tool, []).append(bp.id)
    for tool, users in sorted(needed.items()):
        path = which(tool)
        add(f"tool:{tool}", "pass" if path else "warn",
            f"{path} (used by {', '.join(users)})" if path else f"not found (needed by {', '.join(users)})",
            None if path else TOOL_HINTS.get(tool, "install it and put it on PATH"))

    if "gcloud" in needed and which("gcloud"):
        account = run_local(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])
        add("gcloud-auth", "pass" if account else "warn",
            "active account configured" if account else "no active gcloud account",
            None if account else "run: gcloud auth login")
        project = run_local(["gcloud", "config", "get-value", "project"])
        add("gcloud-project", "pass" if project else "warn",
            f"default project: {project}" if project else "no default project (blueprints pass --project explicitly)",
            None if project else "optional: gcloud config set project YOUR_PROJECT_ID")
        adc = env.get("GOOGLE_APPLICATION_CREDENTIALS") or str(
            Path.home() / ".config" / "gcloud" / "application_default_credentials.json")
        adc_ok = Path(adc).expanduser().is_file()
        add("application-default-credentials", "pass" if adc_ok else "info",
            "ADC file present" if adc_ok else "no ADC file (only needed for local SDK/ADK runs)",
            None if adc_ok else "run: gcloud auth application-default login")
    if "clasp" in needed and which("clasp"):
        rc = Path.home() / ".clasprc.json"
        add("clasp-auth", "pass" if rc.is_file() else "warn",
            "clasp credentials present" if rc.is_file() else "clasp is not logged in",
            None if rc.is_file() else "run: clasp login (and enable the Apps Script API at "
                                      "https://script.google.com/home/usersettings)")

    if "firebase" in needed and which("firebase"):
        fb = Path.home() / ".config" / "configstore" / "firebase-tools.json"
        add("firebase-auth", "pass" if fb.is_file() else "warn",
            "firebase-tools credentials present" if fb.is_file() else "firebase-tools is not logged in",
            None if fb.is_file() else "run: firebase login")

    key_name = ai_key_present(env)
    try:
        import google.genai  # noqa: F401
        genai_ok = True
    except ImportError:
        genai_ok = False
    if key_name and genai_ok:
        add("ai-routing", "pass", f"enabled for 'doogle ask --ai' (key from ${key_name}; value not shown)")
    else:
        missing = []
        if not genai_ok:
            missing.append("pip install 'doogle[ai]'")
        if not key_name:
            missing.append("export GEMINI_API_KEY=...")
        add("ai-routing", "info", "optional; offline routing is used", " and ".join(missing))

    state_path = resolve_state_path(state_dir)
    add("state-file", "info", f"{state_path} ({'exists' if state_path.exists() else 'not created yet'})")

    status = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"status": status, "checks": checks}
