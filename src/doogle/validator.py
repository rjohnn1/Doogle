"""Strict blueprint schema and safety validation (schema v2).

Kept free of execution code so it can be used by CI, editors, or other tools:

    from doogle.validator import validate_blueprint_data
    errors = validate_blueprint_data(yaml.safe_load(text), folder_name="my-blueprint")

``doogle.engine`` re-exports everything here for backwards compatibility.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Iterator
from datetime import date
from typing import Any

import yaml

SCHEMA_VERSION = 2
STALE_AFTER_DAYS = 180
VERIFICATION_STATUSES = ("simulated", "syntax-checked", "live")
VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}(?:[-+.][0-9A-Za-z.]+)?$")
TOOL_KEY_RE = re.compile(r"^[a-z@][a-z0-9_.@/-]*$")
UNVERIFIED = "unverified"
# Tools that are part of the OS and need no version pin.
UNPINNED_TOOLS = {"test", "curl"}

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
# Allow-list of executables a blueprint may run. Anything else (env, python, node, xargs,
# find -exec, ...) could smuggle in a shell, so it is rejected. Adding a tool is an engine
# change reviewed by maintainers, not a blueprint change.
ALLOWED_BINARIES = {"gcloud", "gsutil", "bq", "firebase", "clasp", "curl", "test"}
# Sub-command words that change state. Read-only checks (verify_command, skip_if) must not use them.
MUTATING_WORDS = {"create", "deploy", "enable", "disable", "add-iam-policy-binding", "set-iam-policy",
                  "update", "set", "push", "apply", "import", "upload", "cp", "mv", "rm", "init",
                  "login", "logout", "revoke", "grant", "submit", "patch", "replace", "write",
                  "addfirebase", "clone", "pull", "rollback", "release", "undelete", "activate",
                  "create-script", "create-deployment", "update-deployment", "redeploy"} | {
                      "delete", "remove-iam-policy-binding", "delete-deployment", "delete-script",
                      "undeploy", "destroy", "purge"}
# curl flags that write files, upload data or change the HTTP method.
CURL_FORBIDDEN_FLAGS = {"-o", "--output", "-O", "--remote-name", "--remote-name-all", "-T", "--upload-file",
                        "-K", "--config", "-d", "--data", "--data-binary", "--data-raw", "--data-urlencode",
                        "--json", "-F", "--form", "-X", "--request", "-c", "--cookie-jar", "-D",
                        "--dump-header", "--output-dir", "--create-dirs", "--trace", "--trace-ascii",
                        "--libcurl", "--stderr", "-u", "--user", "-E", "--cert", "--key"}
MAX_BLUEPRINT_BYTES = 256 * 1024
NEVER_AUTOMATE = [("gcloud", "projects", "delete"), ("gcloud", "organizations"),
                  ("gcloud", "billing", "accounts")]

TOP_LEVEL_REQUIRED = ("schema_version", "id", "name", "category", "tagline", "maintainer",
                      "last_verified", "verification_status", "verified_versions", "when_to_use",
                      "when_not_to_use", "services", "estimated_cost", "variables", "steps",
                      "teardown")
TOP_LEVEL_OPTIONAL = ("keywords", "requires_tools", "docs", "prerequisites", "verification_notes")
STEP_FIELDS = {"id", "description", "command", "risk", "public_exposure", "verify_command",
               "verify_expect", "manual", "timeout_seconds", "skip_if", "skip_if_expect"}
TEARDOWN_FIELDS = {"id", "description", "command", "risk", "manual", "undoes"}
VARIABLE_FIELDS = {"key", "prompt", "default", "pattern", "example"}


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


# --------------------------------------------------------------------------- validation


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(_nonempty_str(v) for v in value)


def _validate_command(where: str, command: Any, declared: set[str], errors: list[str]) -> list[str] | None:
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
    elif binary not in ALLOWED_BINARIES and not PLACEHOLDER_RE.search(binary):
        errors.append(f"{where}: '{binary}' is not on the tool allow-list {sorted(ALLOWED_BINARIES)} "
                      "(adding a tool is an engine change; see CONTRIBUTING.md)")
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


def _words(tokens: Iterable[str]) -> list[str]:
    """Sub-command words, including the parts of firebase-style ``a:b:c`` commands."""
    words: list[str] = []
    for t in tokens:
        if t.startswith("-") or PLACEHOLDER_RE.search(t):
            continue
        low = t.lower()
        words.append(low)
        if ":" in low and "/" not in low:
            words.extend(p for p in low.split(":") if p)
    return words


def _is_destructive(tokens: Iterable[str]) -> bool:
    return any(w in DESTRUCTIVE_TOKENS or w == "disable" for w in _words(tokens))


def _readonly_problems(tokens: list[str]) -> list[str]:
    """Why a verify_command / skip_if is not read-only (empty list means it is)."""
    problems = []
    binary = tokens[0] if tokens else ""
    if binary == "curl":
        bad = sorted({t.split("=", 1)[0] for t in tokens[1:]
                      if t.split("=", 1)[0] in CURL_FORBIDDEN_FLAGS
                      or (t.startswith("-") and not t.startswith("--") and len(t) > 2
                          and any(f"-{c}" in CURL_FORBIDDEN_FLAGS for c in t[1:]))})
        if bad:
            problems.append(f"curl flags {bad} can write files, upload data or change the method")
        return problems
    if binary == "test":
        return problems
    mutating = sorted({w for w in _words(tokens[1:]) if w in MUTATING_WORDS})
    if mutating:
        problems.append(f"uses state-changing word(s) {mutating}")
    return problems


def parse_date(value: Any) -> date | None:
    """Accept a YAML date or an ISO ``YYYY-MM-DD`` string."""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def required_tools_from_data(data: dict[str, Any]) -> set[str]:
    tools = set(t for t in _as_list(data.get("requires_tools")) if isinstance(t, str))
    for group in ("steps", "teardown"):
        for item in _as_list(data.get(group)):
            if not isinstance(item, dict):
                continue
            for key in ("command", "verify_command", "skip_if"):
                cmd = item.get(key)
                if isinstance(cmd, str):
                    try:
                        toks = _tokens(cmd)
                    except ValueError:
                        continue
                    if toks:
                        tools.add(toks[0])
    return tools


def _validate_freshness(data: dict[str, Any], errors: list[str], today: date | None) -> None:
    today = today or date.today()
    if "maintainer" in data and not _nonempty_str(data.get("maintainer")):
        errors.append("'maintainer' must be a non-empty string (a GitHub handle or team)")
    if "last_verified" in data:
        d = parse_date(data["last_verified"])
        if d is None:
            errors.append("last_verified must be an ISO date (YYYY-MM-DD)")
        elif d > today:
            errors.append(f"last_verified {d.isoformat()} is in the future")
    status = data.get("verification_status")
    if "verification_status" in data and status not in VERIFICATION_STATUSES:
        errors.append(f"verification_status must be one of {list(VERIFICATION_STATUSES)}")
    if "verification_notes" in data and not _nonempty_str(data["verification_notes"]):
        errors.append("verification_notes must be a non-empty string")
    if "verified_versions" not in data:
        return
    versions = data["verified_versions"]
    if not isinstance(versions, dict) or not versions:
        errors.append("verified_versions must be a non-empty mapping of tool -> version")
        return
    for tool, ver in versions.items():
        if not isinstance(tool, str) or not TOOL_KEY_RE.match(tool):
            errors.append(f"verified_versions: invalid tool name {tool!r}")
        if not isinstance(ver, str) or not (ver == UNVERIFIED or VERSION_RE.match(ver)):
            errors.append(f"verified_versions[{tool}]: {ver!r} must be a quoted version string "
                          f"like \"3.4.1\" or \"{UNVERIFIED}\"")
    if status in ("syntax-checked", "live"):
        aliases = {"firebase": "firebase-tools", "clasp": "@google/clasp"}
        for tool in sorted(required_tools_from_data(data) - UNPINNED_TOOLS):
            ver = versions.get(tool, versions.get(aliases.get(tool, ""), None))
            if ver is None or ver == UNVERIFIED:
                errors.append(f"verification_status '{status}' requires a real version for '{tool}' "
                              "in verified_versions")


MAX_DEPTH = 32


def _find_todo(value: Any, path: str = "", depth: int = 0) -> Iterator[str]:
    if depth > MAX_DEPTH:
        return
    if isinstance(value, str):
        if re.search(r"\bTODO\b", value):
            yield path or "(root)"
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _find_todo(v, f"{path}.{k}" if path else str(k), depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _find_todo(v, f"{path}[{i}]", depth + 1)


def _depth(value: Any, limit: int = MAX_DEPTH) -> int:
    """Nesting depth, capped at ``limit + 1`` (iterative, so it cannot overflow the stack)."""
    deepest, stack = 0, [(value, 0)]
    while stack:
        item, d = stack.pop()
        deepest = max(deepest, d)
        if deepest > limit:
            return deepest
        if isinstance(item, dict):
            stack.extend((v, d + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, d + 1) for v in item)
    return deepest


def validate_blueprint_data(data: Any, folder_name: str | None = None,
                            today: date | None = None) -> list[str]:
    """Strict schema check (spec section 5). Returns a list of error strings."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["blueprint.yaml must contain a mapping at the top level"]
    if _depth(data) > MAX_DEPTH:
        return [f"blueprint.yaml is nested more than {MAX_DEPTH} levels deep"]
    if not all(isinstance(k, str) for k in data):
        return ["top-level keys must be strings"]

    _validate_header(data, folder_name, today, errors)
    declared = _validate_variables(data, errors)
    step_ids = _validate_steps(data, declared, errors)
    _validate_teardown(data, declared, step_ids, errors)
    return errors


def _validate_header(data: dict[str, Any], folder_name: str | None, today: date | None, errors: list[str]) -> None:
    for key in TOP_LEVEL_REQUIRED:
        if key not in data:
            errors.append(f"missing required field '{key}'")
    unknown = set(data) - set(TOP_LEVEL_REQUIRED) - set(TOP_LEVEL_OPTIONAL)
    if unknown:
        errors.append(f"unknown top-level field(s): {sorted(unknown)}")

    if "schema_version" in data and data.get("schema_version") != SCHEMA_VERSION:
        hint = (" (v1 -> v2: add maintainer, last_verified, verification_status and verified_versions;"
                " see CONTRIBUTING.md)") if data.get("schema_version") == 1 else ""
        errors.append(f"schema_version must be {SCHEMA_VERSION}{hint}")

    _validate_freshness(data, errors, today)

    todo_paths = list(_find_todo(data))
    if todo_paths:
        shown = ", ".join(todo_paths[:8]) + (" ..." if len(todo_paths) > 8 else "")
        errors.append(f"replace the TODO placeholders in: {shown}")

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
    for tool in _as_list(data.get("requires_tools")):
        if isinstance(tool, str) and (tool in FORBIDDEN_BINARIES or tool not in ALLOWED_BINARIES):
            errors.append(f"requires_tools: '{tool}' is not an allowed tool")

    services = data.get("services")
    if "services" in data:
        if not isinstance(services, list) or not services:
            errors.append("'services' must be a non-empty list")
        else:
            for i, svc in enumerate(services):
                if not (isinstance(svc, dict) and _nonempty_str(svc.get("name")) and _nonempty_str(svc.get("role"))):
                    errors.append(f"services[{i}] must have non-empty 'name' and 'role'")
                elif set(svc) - {"name", "role", "docs"}:
                    errors.append(f"services[{i}] has unknown field(s): {sorted(set(svc) - {'name', 'role', 'docs'})}")



def _validate_variables(data: dict[str, Any], errors: list[str]) -> set[str]:
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

    return declared


def _validate_steps(data: dict[str, Any], declared: set[str], errors: list[str]) -> set[str]:
    steps = data.get("steps")
    step_ids: set[str] = set()
    if "steps" in data:
        if not isinstance(steps, list) or not steps:
            errors.append("'steps' must be a non-empty list")
            steps = []
        if steps and all(isinstance(s, dict) and s.get("manual") for s in steps):
            errors.append("'steps' must contain at least one automated (non-manual) step")
        for i, step in enumerate(steps):
            _validate_step(i, step, declared, step_ids, errors)
    return step_ids


def _validate_step(i: int, step: Any, declared: set[str], step_ids: set[str], errors: list[str]) -> None:
    where = f"steps[{i}]"
    if not isinstance(step, dict):
        errors.append(f"{where} must be a mapping")
        return
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
        for f_ in ("command", "verify_command", "verify_expect", "skip_if", "skip_if_expect"):
            if f_ in step:
                errors.append(f"{where}: manual steps must not define '{f_}'")
        return
    toks = _validate_command(where, step.get("command"), declared, errors)
    if toks and _is_destructive(toks) and risk != "high":
        errors.append(f"{where}: destructive command must be marked risk: high")
    if toks and toks[0] == "curl":
        for problem in _readonly_problems(toks):
            errors.append(f"{where}: curl may only be used read-only ({problem})")
    if "verify_command" in step:
        vtoks = _validate_command(f"{where} verify_command", step["verify_command"], declared, errors)
        if vtoks:
            for problem in _readonly_problems(vtoks):
                errors.append(f"{where}: verify_command must be read-only ({problem})")
    if "verify_expect" in step:
        if not _nonempty_str(step["verify_expect"]):
            errors.append(f"{where}: verify_expect must be a non-empty string")
        elif "verify_command" not in step:
            errors.append(f"{where}: verify_expect requires verify_command")
        else:
            for m in PLACEHOLDER_RE.finditer(step["verify_expect"]):
                if m.group(1) not in declared:
                    errors.append(f"{where}: verify_expect placeholder '{m.group(1)}' is not declared")
    if "skip_if" in step:
        stoks = _validate_command(f"{where} skip_if", step["skip_if"], declared, errors)
        if stoks:
            for problem in _readonly_problems(stoks):
                errors.append(f"{where}: skip_if must be read-only ({problem})")
    if "skip_if_expect" in step:
        if not _nonempty_str(step["skip_if_expect"]):
            errors.append(f"{where}: skip_if_expect must be a non-empty string")
        elif "skip_if" not in step:
            errors.append(f"{where}: skip_if_expect requires skip_if")
        else:
            for m in PLACEHOLDER_RE.finditer(step["skip_if_expect"]):
                if m.group(1) not in declared:
                    errors.append(f"{where}: skip_if_expect placeholder '{m.group(1)}' is not declared")


def _validate_teardown(data: dict[str, Any], declared: set[str], step_ids: set[str], errors: list[str]) -> None:
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
            if "manual" in td and not isinstance(td["manual"], bool):
                errors.append(f"{where}: manual must be true/false")
            if td.get("manual"):
                for f_ in ("command", "undoes"):
                    if f_ in td:
                        errors.append(f"{where}: manual teardown entries must not define '{f_}'")
                continue
            has_command = True
            undoes = td.get("undoes")
            if undoes is None:
                errors.append(f"{where}: automated teardown entries must declare 'undoes: [step-id, ...]' "
                              "(the steps whose resources it removes) so teardown never deletes "
                              "something the run did not create")
            else:
                if not (isinstance(undoes, list) and undoes and all(isinstance(u, str) for u in undoes)):
                    errors.append(f"{where}: undoes must be a non-empty list of step ids")
                else:
                    for u in undoes:
                        if u not in step_ids:
                            errors.append(f"{where}: undoes references unknown step '{u}'")
            toks = _validate_command(where, td.get("command"), declared, errors)
            if toks and toks[0] == "curl":
                errors.append(f"{where}: curl cannot be used in teardown")
            if toks and _is_destructive(toks) and risk != "high":
                errors.append(f"{where}: destructive command must be marked risk: high")
        if teardown and not has_command:
            errors.append("'teardown' must include at least one command")


# --------------------------------------------------------------------------- safe loading


class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects anchors/aliases (no billion-laughs, no recursive structures)."""

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                None, None, "YAML aliases (*name) are not allowed in blueprints", event.start_mark)
        return super().compose_node(parent, index)


def safe_load_blueprint_text(text: str) -> Any:
    """``yaml.safe_load`` without aliases and with a size cap. Raises ``yaml.YAMLError`` or ``ValueError``."""
    if len(text.encode("utf-8", errors="replace")) > MAX_BLUEPRINT_BYTES:
        raise ValueError(f"blueprint.yaml is larger than {MAX_BLUEPRINT_BYTES // 1024} KiB")
    return yaml.load(text, Loader=_NoAliasSafeLoader)  # noqa: S506  # nosec B506 - SafeLoader subclass


# --------------------------------------------------------------------------- recorded commands


def recorded_argv_problems(argv: Any, where: str = "command") -> list[str]:
    """Re-check an argv read back from ``.doogle/state.json`` before executing it.

    State files live on disk and can be edited, corrupted or copied from someone else, so the
    executor never trusts them blindly: the same allow-list and never-automate rules apply.
    """
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return [f"{where}: recorded argv must be a non-empty list of strings"]
    problems = []
    binary = argv[0]
    if binary not in ALLOWED_BINARIES or binary in FORBIDDEN_BINARIES:
        problems.append(f"{where}: recorded tool '{binary}' is not on the allow-list {sorted(ALLOWED_BINARIES)}")
    if any(any(ord(c) < 32 or ord(c) == 127 for c in a) for a in argv):
        problems.append(f"{where}: recorded argv contains control characters")
    lowered = [a.lower() for a in argv]
    for pattern in NEVER_AUTOMATE:
        n = len(pattern)
        if any(tuple(lowered[i:i + n]) == pattern for i in range(len(lowered) - n + 1)):
            problems.append(f"{where}: '{' '.join(pattern)}' must never be automated")
    return problems
