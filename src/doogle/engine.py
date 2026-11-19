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

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import yaml

SCHEMA_VERSION = 1
STATE_VERSION = 1
BLUEPRINTS_ENV = "DOOGLE_BLUEPRINTS_DIR"
STATE_DIR_ENV = "DOOGLE_STATE_DIR"
GEMINI_MODEL_ENV = "DOOGLE_GEMINI_MODEL"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
AI_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

RISK_LEVELS = ("low", "medium", "high")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VAR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
SECRET_KEY_RE = re.compile(r"(secret|password|passwd|token|api_?key|private_?key|credential)", re.I)
PRICE_RE = re.compile(r"[$€£₹¥]\s?\d|\d+(?:\.\d+)?\s?(?:usd|eur|inr|dollars?)\b", re.I)
SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ";;", ">", ">>", "<", "<<", "(", ")"}
FORBIDDEN_BINARIES = {"sudo", "su", "doas", "rm", "dd", "mkfs", "shutdown", "reboot",
                      "eval", "exec", "sh", "bash", "zsh", "fish", "pwsh", "powershell", "cmd"}
DESTRUCTIVE_TOKENS = {"delete", "remove-iam-policy-binding", "delete-deployment", "delete-script",
                      "undeploy", "destroy", "purge"}
NEVER_AUTOMATE = [("gcloud", "projects", "delete"), ("gcloud", "organizations"),
                  ("gcloud", "billing", "accounts")]

TOP_LEVEL_REQUIRED = ("schema_version", "id", "name", "category", "tagline", "when_to_use",
                      "when_not_to_use", "services", "estimated_cost", "variables", "steps",
                      "teardown")
TOP_LEVEL_OPTIONAL = ("keywords", "requires_tools", "docs", "prerequisites")
STEP_FIELDS = {"id", "description", "command", "risk", "public_exposure", "verify_command",
               "verify_expect", "manual", "timeout_seconds"}
TEARDOWN_FIELDS = {"id", "description", "command", "risk", "manual"}
VARIABLE_FIELDS = {"key", "prompt", "default", "pattern", "example"}

TOOL_HINTS = {
    "gcloud": "Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install",
    "firebase": "Install the Firebase CLI: npm install -g firebase-tools",
    "clasp": "Install clasp: npm install -g @google/clasp  (then run: clasp login)",
    "adk": "Install Google ADK: pip install google-adk",
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


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


# --------------------------------------------------------------------------- blueprint resolution


def _packaged_blueprints_dir() -> Optional[Path]:
    try:
        candidate = resources.files("doogle") / "blueprints"
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None
    path = Path(str(candidate))
    return path if path.is_dir() else None


def _checkout_blueprints_dir() -> Optional[Path]:
    path = Path(__file__).resolve().parents[2] / "blueprints"
    return path if path.is_dir() else None


def resolve_blueprints_dir(explicit: Optional[str | os.PathLike] = None,
                           env: Optional[Mapping[str, str]] = None) -> tuple[Path, str]:
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
    default: Optional[str] = None
    pattern: Optional[str] = None
    example: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Step:
    id: str
    description: str
    command: Optional[str] = None
    risk: str = "low"
    public_exposure: bool = False
    verify_command: Optional[str] = None
    verify_expect: Optional[str] = None
    manual: bool = False
    timeout_seconds: int = 1800

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TeardownStep:
    id: str
    description: str
    command: Optional[str] = None
    risk: str = "high"
    manual: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Blueprint:
    id: str
    name: str
    category: str
    tagline: str
    when_to_use: list[str]
    when_not_to_use: list[str]
    services: list[dict]
    estimated_cost: str
    variables: list[Variable]
    steps: list[Step]
    teardown: list[TeardownStep]
    keywords: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    path: Optional[Path] = None

    @property
    def guide_path(self) -> Optional[Path]:
        if self.path is None:
            return None
        guide = self.path / "GUIDE.md"
        return guide if guide.is_file() else None

    def required_tools(self) -> list[str]:
        tools = set(self.requires_tools)
        commands = [s.command for s in self.steps] + [s.verify_command for s in self.steps]
        commands += [t.command for t in self.teardown]
        for cmd in commands:
            if cmd:
                toks = _tokens(cmd)
                if toks:
                    tools.add(toks[0])
        return sorted(tools)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "tagline": self.tagline,
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


# --------------------------------------------------------------------------- validation


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(_nonempty_str(v) for v in value)


def _validate_command(where: str, command: Any, declared: set[str], errors: list[str]) -> Optional[list[str]]:
    if not _nonempty_str(command):
        errors.append(f"{where}: command must be a non-empty string")
        return None
    if "\n" in command:
        errors.append(f"{where}: command must be a single line")
    if "$(" in command or "`" in command or "${" in command:
        errors.append(f"{where}: command substitution / shell expansion is not allowed")
    try:
        toks = _tokens(command)
    except ValueError as exc:
        errors.append(f"{where}: command cannot be tokenised ({exc})")
        return None
    if not toks:
        errors.append(f"{where}: command is empty")
        return None
    ops = [t for t in toks if t in SHELL_OPERATORS or set(t) <= set("|&;<>()")]
    if ops:
        errors.append(f"{where}: shell operators {sorted(set(ops))} are not allowed; split into separate steps")
    binary = toks[0]
    if "/" in binary or "\\" in binary:
        errors.append(f"{where}: first token must be a bare tool name, not a path ('{binary}')")
    if binary in FORBIDDEN_BINARIES:
        errors.append(f"{where}: '{binary}' is not an allowed tool in blueprints")
    if PLACEHOLDER_RE.search(binary):
        errors.append(f"{where}: the tool name cannot be a variable")
    for match in PLACEHOLDER_RE.finditer(command):
        name = match.group(1)
        if not VAR_KEY_RE.match(name):
            errors.append(f"{where}: malformed placeholder '{{{{{name}}}}}'")
        elif name not in declared:
            errors.append(f"{where}: placeholder '{{{{{name}}}}}' is not a declared variable")
    stripped = PLACEHOLDER_RE.sub("", command)
    if "{{" in stripped or "}}" in stripped:
        errors.append(f"{where}: unbalanced '{{{{' / '}}}}' in command")
    lowered = [t.lower() for t in toks]
    for pattern in NEVER_AUTOMATE:
        n = len(pattern)
        if any(tuple(lowered[i:i + n]) == pattern for i in range(len(lowered) - n + 1)):
            errors.append(f"{where}: '{' '.join(pattern)}' must never be automated by a blueprint")
    return toks


def _is_destructive(tokens: Iterable[str]) -> bool:
    return any(t.lower() in DESTRUCTIVE_TOKENS for t in tokens)


def validate_blueprint_data(data: Any, folder_name: Optional[str] = None) -> list[str]:
    """Strict schema check (spec section 5). Returns a list of error strings."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["blueprint.yaml must contain a mapping at the top level"]

    for key in TOP_LEVEL_REQUIRED:
        if key not in data:
            errors.append(f"missing required field '{key}'")
    unknown = set(data) - set(TOP_LEVEL_REQUIRED) - set(TOP_LEVEL_OPTIONAL)
    if unknown:
        errors.append(f"unknown top-level field(s): {sorted(unknown)}")

    if data.get("schema_version") not in (None, SCHEMA_VERSION) and "schema_version" in data:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    bp_id = data.get("id")
    if "id" in data:
        if not isinstance(bp_id, str) or not ID_RE.match(bp_id):
            errors.append("id must be lowercase kebab-case (e.g. 'solo-serverless-api')")
        elif folder_name is not None and bp_id != folder_name:
            errors.append(f"id '{bp_id}' must match its folder name '{folder_name}'")

    for key in ("name", "category", "tagline", "estimated_cost"):
        if key in data and not _nonempty_str(data[key]):
            errors.append(f"'{key}' must be a non-empty string")
    if _nonempty_str(data.get("estimated_cost")) and PRICE_RE.search(data["estimated_cost"]):
        errors.append("estimated_cost must be a plain-language note; do not state exact prices "
                      "(link to the official pricing page instead)")

    for key in ("when_to_use", "when_not_to_use"):
        if key in data and not _str_list(data[key]):
            errors.append(f"'{key}' must be a non-empty list of non-empty strings")
    for key in ("keywords", "requires_tools", "prerequisites", "docs"):
        if key in data and not (isinstance(data[key], list) and all(_nonempty_str(v) for v in data[key])):
            errors.append(f"'{key}' must be a list of non-empty strings")
    for tool in data.get("requires_tools") or []:
        if isinstance(tool, str) and tool in FORBIDDEN_BINARIES:
            errors.append(f"requires_tools: '{tool}' is not an allowed tool")

    services = data.get("services")
    if "services" in data:
        if not isinstance(services, list) or not services:
            errors.append("'services' must be a non-empty list")
        else:
            for i, svc in enumerate(services):
                if not isinstance(svc, dict) or not _nonempty_str(svc.get("name")) or not _nonempty_str(svc.get("role")):
                    errors.append(f"services[{i}] must have non-empty 'name' and 'role'")
                elif set(svc) - {"name", "role", "docs"}:
                    errors.append(f"services[{i}] has unknown field(s): {sorted(set(svc) - {'name', 'role', 'docs'})}")

    declared: set[str] = set()
    variables = data.get("variables")
    if "variables" in data:
        if not isinstance(variables, list):
            errors.append("'variables' must be a list (may be empty)")
            variables = []
        for i, var in enumerate(variables):
            where = f"variables[{i}]"
            if not isinstance(var, dict):
                errors.append(f"{where} must be a mapping")
                continue
            extra = set(var) - VARIABLE_FIELDS
            if extra:
                errors.append(f"{where} has unknown field(s): {sorted(extra)}")
            key = var.get("key")
            if not isinstance(key, str) or not VAR_KEY_RE.match(key):
                errors.append(f"{where}: key must be snake_case (e.g. 'project_id')")
                continue
            if key in declared:
                errors.append(f"{where}: duplicate variable key '{key}'")
            declared.add(key)
            if SECRET_KEY_RE.search(key):
                errors.append(f"{where}: '{key}' looks like a secret; blueprints must not take secrets "
                              "as variables (use Secret Manager or the tool's own login flow)")
            if not _nonempty_str(var.get("prompt")):
                errors.append(f"{where}: prompt must be a non-empty string")
            if "default" in var and var["default"] is not None and not isinstance(var["default"], (str, int)):
                errors.append(f"{where}: default must be a string")
            pattern = var.get("pattern")
            if pattern is not None:
                try:
                    compiled = re.compile(pattern)
                except (re.error, TypeError) as exc:
                    errors.append(f"{where}: invalid pattern ({exc})")
                else:
                    default = var.get("default")
                    if default is not None and not compiled.fullmatch(str(default)):
                        errors.append(f"{where}: default '{default}' does not match its pattern")

    steps = data.get("steps")
    step_ids: set[str] = set()
    if "steps" in data:
        if not isinstance(steps, list) or not steps:
            errors.append("'steps' must be a non-empty list")
            steps = []
        if steps and all(isinstance(s, dict) and s.get("manual") for s in steps):
            errors.append("'steps' must contain at least one automated (non-manual) step")
        for i, step in enumerate(steps):
            where = f"steps[{i}]"
            if not isinstance(step, dict):
                errors.append(f"{where} must be a mapping")
                continue
            sid = step.get("id")
            if isinstance(sid, str):
                where = f"steps[{i}] '{sid}'"
            extra = set(step) - STEP_FIELDS
            if extra:
                errors.append(f"{where} has unknown field(s): {sorted(extra)}")
            if not isinstance(sid, str) or not ID_RE.match(sid):
                errors.append(f"{where}: id must be lowercase kebab-case")
            elif sid in step_ids:
                errors.append(f"{where}: duplicate step id")
            else:
                step_ids.add(sid)
            if not _nonempty_str(step.get("description")):
                errors.append(f"{where}: description must be a non-empty string")
            risk = step.get("risk", "low")
            if risk not in RISK_LEVELS:
                errors.append(f"{where}: risk must be one of {list(RISK_LEVELS)}")
            if "public_exposure" in step and not isinstance(step["public_exposure"], bool):
                errors.append(f"{where}: public_exposure must be true/false")
            if "manual" in step and not isinstance(step["manual"], bool):
                errors.append(f"{where}: manual must be true/false")
            if "timeout_seconds" in step and (not isinstance(step["timeout_seconds"], int)
                                              or step["timeout_seconds"] <= 0):
                errors.append(f"{where}: timeout_seconds must be a positive integer")
            if step.get("manual"):
                for f_ in ("command", "verify_command", "verify_expect"):
                    if f_ in step:
                        errors.append(f"{where}: manual steps must not define '{f_}'")
                continue
            toks = _validate_command(where, step.get("command"), declared, errors)
            if toks and _is_destructive(toks) and risk != "high":
                errors.append(f"{where}: destructive command must be marked risk: high")
            if "verify_command" in step:
                vtoks = _validate_command(f"{where} verify_command", step["verify_command"], declared, errors)
                if vtoks and _is_destructive(vtoks):
                    errors.append(f"{where}: verify_command must be read-only")
            if "verify_expect" in step:
                if not _nonempty_str(step["verify_expect"]):
                    errors.append(f"{where}: verify_expect must be a non-empty string")
                elif "verify_command" not in step:
                    errors.append(f"{where}: verify_expect requires verify_command")
                else:
                    for m in PLACEHOLDER_RE.finditer(step["verify_expect"]):
                        if m.group(1) not in declared:
                            errors.append(f"{where}: verify_expect placeholder '{m.group(1)}' is not declared")

    teardown = data.get("teardown")
    if "teardown" in data:
        if not isinstance(teardown, list) or not teardown:
            errors.append("'teardown' must be a non-empty list")
            teardown = []
        td_ids: set[str] = set()
        has_command = False
        for i, td in enumerate(teardown):
            where = f"teardown[{i}]"
            if not isinstance(td, dict):
                errors.append(f"{where} must be a mapping")
                continue
            tid = td.get("id")
            if isinstance(tid, str):
                where = f"teardown[{i}] '{tid}'"
            extra = set(td) - TEARDOWN_FIELDS
            if extra:
                errors.append(f"{where} has unknown field(s): {sorted(extra)}")
            if not isinstance(tid, str) or not ID_RE.match(tid):
                errors.append(f"{where}: id must be lowercase kebab-case")
            elif tid in td_ids:
                errors.append(f"{where}: duplicate teardown id")
            else:
                td_ids.add(tid)
            if not _nonempty_str(td.get("description")):
                errors.append(f"{where}: description must be a non-empty string")
            risk = td.get("risk", "high")
            if risk not in RISK_LEVELS:
                errors.append(f"{where}: risk must be one of {list(RISK_LEVELS)}")
            if td.get("manual"):
                if "command" in td:
                    errors.append(f"{where}: manual teardown entries must not define 'command'")
                continue
            has_command = True
            toks = _validate_command(where, td.get("command"), declared, errors)
            if toks and _is_destructive(toks) and risk != "high":
                errors.append(f"{where}: destructive command must be marked risk: high")
        if teardown and not has_command:
            errors.append("'teardown' must include at least one command")
    return errors


def blueprint_from_data(data: dict, path: Optional[Path] = None) -> Blueprint:
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
                    manual=bool(s.get("manual", False)), timeout_seconds=int(s.get("timeout_seconds", 1800)))
               for s in data["steps"]],
        teardown=[TeardownStep(id=t["id"], description=t["description"].strip(), command=t.get("command"),
                               risk=t.get("risk", "high"), manual=bool(t.get("manual", False)))
                  for t in data["teardown"]],
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
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BlueprintValidationError(folder.name, [f"YAML parse error: {exc}"]) from exc
    errors = validate_blueprint_data(data, folder.name)
    if not (folder / "GUIDE.md").is_file():
        errors.append("missing GUIDE.md next to blueprint.yaml")
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
            except BlueprintValidationError as exc:
                self.errors[folder.name] = exc.errors
            except BlueprintNotFound as exc:
                self.errors[folder.name] = [str(exc)]
            else:
                self.blueprints[bp.id] = bp

    @classmethod
    def load(cls, explicit: Optional[str] = None) -> "Catalog":
        root, source = resolve_blueprints_dir(explicit)
        return cls(root, source)

    def get(self, bp_id: str) -> Blueprint:
        if bp_id in self.blueprints:
            return self.blueprints[bp_id]
        if bp_id in self.errors:
            raise BlueprintValidationError(bp_id, self.errors[bp_id])
        known = ", ".join(sorted(self.blueprints)) or "(none)"
        raise BlueprintNotFound(f"unknown blueprint '{bp_id}'. Available: {known}")

    def __iter__(self):
        return iter(self.blueprints.values())


# --------------------------------------------------------------------------- variables & rendering


def check_value(var: Variable, value: str) -> None:
    if not isinstance(value, str):
        value = str(value)
    if value == "":
        raise VariableError(f"variable '{var.key}' cannot be empty")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise VariableError(f"variable '{var.key}' contains control characters or newlines")
    if value.startswith("-"):
        raise VariableError(f"variable '{var.key}' cannot start with '-' (it could be read as a flag)")
    if "{{" in value or "}}" in value:
        raise VariableError(f"variable '{var.key}' cannot contain '{{{{' or '}}}}'")
    if var.pattern and not re.fullmatch(var.pattern, value):
        hint = f" (example: {var.example})" if var.example else ""
        raise VariableError(f"variable '{var.key}'='{value}' does not match required pattern {var.pattern}{hint}")


def resolve_variables(bp: Blueprint, provided: Optional[Mapping[str, Any]] = None) -> tuple[dict[str, str], list[Variable]]:
    """Merge provided values with defaults. Returns (values, missing)."""
    provided = dict(provided or {})
    unknown = set(provided) - {v.key for v in bp.variables}
    if unknown:
        raise VariableError(f"unknown variable(s) for '{bp.id}': {sorted(unknown)}. "
                            f"Declared: {[v.key for v in bp.variables]}")
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
        def sub(match: re.Match) -> str:
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
    def sub(match: re.Match) -> str:
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
    argv: Optional[list[str]]
    verify_argv: Optional[list[str]]
    verify_expect: Optional[str]
    timeout_seconds: int = 1800

    @property
    def command(self) -> Optional[str]:
        return display(self.argv) if self.argv else None

    @property
    def verify_command(self) -> Optional[str]:
        return display(self.verify_argv) if self.verify_argv else None

    def to_dict(self) -> dict:
        return {
            "index": self.index, "id": self.id, "description": self.description, "risk": self.risk,
            "public_exposure": self.public_exposure, "manual": self.manual, "command": self.command,
            "argv": self.argv, "verify_command": self.verify_command, "verify_argv": self.verify_argv,
            "verify_expect": self.verify_expect,
        }


@dataclass
class PlannedTeardown:
    id: str
    description: str
    risk: str
    manual: bool
    argv: Optional[list[str]]

    @property
    def command(self) -> Optional[str]:
        return display(self.argv) if self.argv else None

    def to_dict(self) -> dict:
        return {"id": self.id, "description": self.description, "risk": self.risk, "manual": self.manual,
                "command": self.command, "argv": self.argv}


@dataclass
class Plan:
    blueprint: Blueprint
    variables: dict[str, str]
    steps: list[PlannedStep]
    teardown: list[PlannedTeardown]
    tools: dict[str, Optional[str]]

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

    def to_dict(self) -> dict:
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


def check_tools(tools: Iterable[str], which: Optional[Callable[[str], Optional[str]]] = None) -> dict[str, Optional[str]]:
    which = which or shutil.which
    return {t: which(t) for t in sorted(set(tools))}


def build_plan(bp: Blueprint, provided: Optional[Mapping[str, Any]] = None,
               which: Optional[Callable[[str], Optional[str]]] = None) -> Plan:
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
        ))
    teardown = [PlannedTeardown(id=t.id, description=render_text(t.description, values), risk=t.risk,
                                manual=t.manual, argv=render_argv(t.command, values) if t.command else None)
                for t in bp.teardown]
    return Plan(blueprint=bp, variables=values, steps=steps, teardown=teardown,
                tools=check_tools(bp.required_tools(), which))


# --------------------------------------------------------------------------- state


def resolve_state_path(state_dir: Optional[str | os.PathLike] = None) -> Path:
    base = state_dir or os.environ.get(STATE_DIR_ENV) or (Path.cwd() / ".doogle")
    return Path(base).expanduser() / "state.json"


class StateStore:
    """``.doogle/state.json``: an append-only list of run records."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {"state_version": STATE_VERSION, "runs": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"state file '{self.path}' is not valid JSON ({exc}); "
                             "move it aside to start fresh") from exc
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            raise StateError(f"state file '{self.path}' has an unexpected structure")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ignore = self.path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# Created by doogle: never commit run state.\n*\n", encoding="utf-8")
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def runs(self) -> list[dict]:
        return self.load()["runs"]

    def get(self, run_id: str) -> dict:
        runs = self.runs()
        if not runs:
            raise StateError(f"no runs recorded in '{self.path}'")
        if run_id == "latest":
            return runs[-1]
        matches = [r for r in runs if r["run_id"] == run_id or r["run_id"].startswith(run_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise StateError(f"no run '{run_id}' in '{self.path}' (see: doogle history)")
        raise StateError(f"run id prefix '{run_id}' is ambiguous")

    def save_run(self, run: dict) -> None:
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
    """Run argv without a shell, streaming nothing; capture output."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def new_run_record(plan: Plan, mode: str = "apply") -> dict:
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
    confirm: Optional[Callable[[str], bool]] = None


def enforce_gates(*, tools: Mapping[str, Optional[str]], high_risk: Sequence[str], gates: Gates,
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


class Executor:
    """Runs a plan step by step, persisting state after every transition."""

    def __init__(self, store: StateStore, runner: Optional[Runner] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 confirm_manual: Optional[Callable[[dict], bool]] = None):
        self.store = store
        self.runner = runner or (lambda argv, timeout: subprocess_runner(argv, timeout))
        self.on_event = on_event or (lambda kind, payload: None)
        self.confirm_manual = confirm_manual

    def _emit(self, kind: str, payload: dict) -> None:
        self.on_event(kind, payload)

    def apply(self, plan: Plan, gates: Gates) -> dict:
        enforce_gates(tools=plan.tools, high_risk=plan.high_risk_steps, gates=gates,
                      summary=f"Apply blueprint '{plan.blueprint.id}' ({len(plan.steps)} steps)?")
        run = new_run_record(plan)
        self.store.save_run(run)
        return self._execute(run, assume_manual_done=set())

    def resume(self, run: dict, plan: Plan, gates: Gates) -> dict:
        if run["status"] == "succeeded":
            raise StateError(f"run {run['run_id']} already succeeded; nothing to resume")
        if run.get("teardown_status") not in (None, "not_run"):
            raise StateError(f"run {run['run_id']} has been torn down; start a new run instead")
        recorded = [s["id"] for s in run["steps"]]
        if recorded != [s.id for s in plan.steps]:
            raise StateError("the blueprint's steps changed since this run was recorded; start a new run")
        pending_high = [s["id"] for s in run["steps"] if s["risk"] == "high" and s["status"] != "succeeded"]
        enforce_gates(tools=plan.tools, high_risk=pending_high, gates=gates,
                      summary=f"Resume run {run['run_id']}?", what="resume")
        paused_manual = {s["id"] for s in run["steps"] if s["status"] == "awaiting_manual"}
        for s in run["steps"]:
            if s["status"] != "succeeded":
                s.update(status="pending", exit_code=None, verify_status=None, output_tail=None)
        run.update(status="running", finished_at=None, resumed_at=utc_now())
        self.store.save_run(run)
        return self._execute(run, assume_manual_done=paused_manual)

    def _finish(self, run: dict, status: str) -> dict:
        run["status"] = status
        run["finished_at"] = utc_now()
        for s in run["steps"]:
            if s["status"] == "pending":
                s["status"] = "skipped"
        self.store.save_run(run)
        self._emit("run_finished", run)
        return run

    def _execute(self, run: dict, assume_manual_done: set[str]) -> dict:
        for step in run["steps"]:
            if step["status"] == "succeeded":
                continue
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
                continue

            result = self.runner(step["argv"], step.get("timeout_seconds") or 1800)
            step["exit_code"] = result.returncode
            step["output_tail"] = _tail(result.stdout + ("\n" + result.stderr if result.stderr else ""))
            if result.returncode != 0:
                step["status"] = "failed"
                step["finished_at"] = utc_now()
                self._emit("step_finished", step)
                return self._finish(run, "failed")

            if step["verify_argv"]:
                vres = self.runner(step["verify_argv"], 120)
                ok = vres.returncode == 0 and (not step["verify_expect"] or step["verify_expect"] in vres.stdout)
                step["verify_status"] = "passed" if ok else "failed"
                step["verify_output_tail"] = _tail(vres.stdout + ("\n" + vres.stderr if vres.stderr else ""), 5)
                if not ok:
                    step["status"] = "verify_failed"
                    step["finished_at"] = utc_now()
                    self._emit("step_finished", step)
                    return self._finish(run, "failed")
            else:
                step["verify_status"] = "not_defined"
            step["status"] = "succeeded"
            step["finished_at"] = utc_now()
            self.store.save_run(run)
            self._emit("step_finished", step)
        return self._finish(run, "succeeded")

    def teardown(self, run: dict, gates: Gates, tools: Optional[Mapping[str, Optional[str]]] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None) -> dict:
        entries = run.get("teardown") or []
        automated = [t for t in entries if t.get("argv")]
        if tools is None:
            tools = check_tools([t["argv"][0] for t in automated], which)
        high = [t["id"] for t in automated if t["risk"] == "high"]
        enforce_gates(tools=tools, high_risk=high, gates=gates,
                      summary=f"Tear down run {run['run_id']} ({len(automated)} commands)?", what="teardown")
        failures = 0
        for t in entries:
            if not t.get("argv"):
                t["status"] = "manual_required"
                continue
            self._emit("teardown_started", t)
            res = self.runner(t["argv"], 1800)
            t["exit_code"] = res.returncode
            t["finished_at"] = utc_now()
            t["status"] = "succeeded" if res.returncode == 0 else "failed"
            t["output_tail"] = _tail(res.stdout + ("\n" + res.stderr if res.stderr else ""), 5)
            failures += res.returncode != 0
            self.store.save_run(run)
            self._emit("teardown_finished", t)
        run["teardown_status"] = "succeeded" if failures == 0 else ("failed" if failures == len(automated) else "partial")
        run["teardown_finished_at"] = utc_now()
        self.store.save_run(run)
        return run


# --------------------------------------------------------------------------- routing ("ask")


STOPWORDS = set("""a an and are as at be but by can do for from get have i if in into is it its just me my
need of on or our so some that the their them then there this to too up use using want we what when which
who will with without would you your like make build set setup something thing things very really""".split())

SYNONYMS = {
    "cheap": ["cost", "free", "budget", "scale-to-zero"],
    "cheapest": ["cheap"], "inexpensive": ["cheap"], "affordable": ["cheap"], "low-cost": ["cheap"],
    "free": ["cheap"], "budget": ["cheap"],
    "api": ["backend", "endpoint", "rest"], "apis": ["api"], "endpoint": ["api"], "backend": ["api"],
    "microservice": ["api", "container"], "server": ["backend"], "service": ["backend"],
    "hobby": ["side-project"], "side": ["side-project"], "prototype": ["side-project"], "mvp": ["side-project"],
    "agent": ["ai", "llm"], "agents": ["agent"], "chatbot": ["agent", "ai"], "bot": ["chatbot"],
    "assistant": ["agent"], "llm": ["ai", "gemini"], "genai": ["ai", "gemini"], "gpt": ["llm"],
    "spreadsheet": ["sheet"], "sheets": ["sheet"], "excel": ["spreadsheet"], "form": ["sheet", "form"],
    "forms": ["form"], "zap": ["automation"], "zapier": ["automation", "webhook"], "automate": ["automation"],
    "workflow": ["automation"], "nocode": ["no-code"], "lowcode": ["no-code"],
    "docker": ["container"], "containers": ["container"], "serverless": ["scale-to-zero"],
}


def _normalise(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())
    out = []
    for w in words:
        if w in STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


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
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text) is not None


def route_offline(goal: str, blueprints: Iterable[Blueprint], top: int = 3) -> dict:
    """Deterministic keyword/heuristic routing. No network."""
    goal_l = " ".join(goal.lower().split())
    raw_tokens = set(_normalise(goal_l))
    goal_tokens = _expand(raw_tokens)
    results = []
    for bp in blueprints:
        score = 0.0
        reasons = []
        for kw in bp.keywords:
            kw_l = kw.lower()
            if _phrase_in(kw_l, goal_l) or (" " not in kw_l and kw_l in raw_tokens):
                score += 3
                reasons.append(f"keyword '{kw}'")
            elif " " not in kw_l and kw_l in goal_tokens:
                score += 1.5
                reasons.append(f"related to '{kw}'")
        text_tokens = set(_normalise(" ".join([bp.name, bp.tagline, bp.category, *bp.when_to_use,
                                               *(s["name"] for s in bp.services)])))
        overlap = sorted((goal_tokens & text_tokens) - {r.split("'")[1] for r in reasons})
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
    return {
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


class AIUnavailable(DoogleError):
    pass


def ai_key_present(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    env = os.environ if env is None else env
    for name in AI_KEY_ENVS:
        if env.get(name):
            return name
    return None


def route_ai(goal: str, blueprints: Sequence[Blueprint], offline: dict,
             env: Optional[Mapping[str, str]] = None) -> dict:
    """Ask Gemini (via google-genai) to pick a blueprint. Advisory only: the
    answer must name a catalogued blueprint or it is discarded."""
    env = os.environ if env is None else env
    key_name = ai_key_present(env)
    if not key_name:
        raise AIUnavailable(f"no API key set ({' or '.join(AI_KEY_ENVS)})")
    try:
        from google import genai  # type: ignore
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
          env: Optional[Mapping[str, str]] = None) -> dict:
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


def _local_cmd(argv: Sequence[str], timeout: int = 15) -> Optional[str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def doctor(blueprints_dir: Optional[str] = None, state_dir: Optional[str] = None,
           which: Optional[Callable[[str], Optional[str]]] = None,
           run_local: Optional[Callable[[Sequence[str]], Optional[str]]] = None,
           env: Optional[Mapping[str, str]] = None) -> dict:
    """Read-only environment diagnostics. Never touches cloud state or the network."""
    env = os.environ if env is None else env
    which = which or shutil.which
    run_local = run_local or _local_cmd
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, hint: Optional[str] = None) -> None:
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

    key_name = ai_key_present(env)
    try:
        import google.genai  # type: ignore  # noqa: F401
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
