"""orch.toml: default run options and the provider and model each agent role uses.

The command line overrides the file and the file overrides the built-in defaults below; a missing file or key keeps
the default. SDK-free so `preview` works without claude_agent_sdk installed.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

CONFIG_FILE_NAME = "orch.toml"  # at the project root, next to TASKS.md
PROVIDERS = ("claude", "gemini")
# Every agent role; runner.ROLE_TOOLS has the same keys
ROLES = (
    "doc-worker", "code-worker", "sys-worker", "reviewer", "doc-merger", "code-merger", "planner", "namer", "reporter",
)
SMALL_ROLES = ("namer", "reporter")  # one-shot chores: the provider's small_model is enough
ROLES_SECTION = "roles"  # [roles.<role>] tables override provider and model per role
# Keys each section accepts and the type of their values
SECTION_KEYS: dict[str, dict[str, type]] = {
    "run": {"max_parallel": int, "max_rounds": int, "budget_per_agent": float, "max_turns": int},
    "agent": {"provider": str},
    "claude": {"model": str, "small_model": str, "cli_path": str, "max_retries": int},
    "gemini": {"model": str, "small_model": str, "cli_path": str, "max_retries": int},
}
GEMINI_MAX_RETRIES = 9  # the Gemini CLI allows at most 10 attempts per request
ROLE_KEYS: dict[str, type] = {"provider": str, "model": str}
# orch.toml key -> attribute name
FIELDS = {
    "max_parallel": "maxParallel",
    "max_rounds": "maxRounds",
    "budget_per_agent": "budgetPerAgent",
    "max_turns": "maxTurns",
    "provider": "provider",
    "model": "model",
    "small_model": "smallModel",
    "cli_path": "cliPath",
    "max_retries": "maxRetries",
}
TYPE_NAMES = {int: "an integer", float: "a number", str: "a string"}


@dataclass
class ProviderSettings:
    model: str | None = None  # None = the CLI's default model
    smallModel: str | None = None  # namer and reporter; None = model
    cliPath: str | None = None  # claude: native executable; gemini: executable or gemini.js; None = auto-detect
    maxRetries: int | None = None  # retries of a failed API request (429, network); None = the CLI's default (10)


@dataclass
class RoleSettings:
    provider: str | None = None  # None = [agent] provider
    model: str | None = None  # None = the provider's model (small_model for namer and reporter)


class AgentChoice(NamedTuple):
    provider: str
    model: str | None  # None = the CLI's default model


def DefaultProviders() -> dict[str, ProviderSettings]:
    # gemini fails fast: the CLI's own 10 attempts per rate-limited request can stall an agent for many minutes
    return {
        "claude": ProviderSettings(smallModel="haiku"),
        "gemini": ProviderSettings(smallModel="flash-lite", maxRetries=0),
    }


@dataclass
class OrchConfig:
    source: str = f"built-in defaults (no {CONFIG_FILE_NAME})"  # where the values came from, shown by preview
    maxParallel: int = 0  # concurrent agents across all tasks; 0 = automatic
    maxRounds: int = 1  # review/fix rounds; 0 = no review
    budgetPerAgent: float = 2.0  # USD cap per agent call
    maxTurns: int = 40  # turn cap per agent call
    provider: str = "claude"  # every role without its own [roles.<role>] provider
    providers: dict[str, ProviderSettings] = field(default_factory=DefaultProviders)
    roles: dict[str, RoleSettings] = field(default_factory=dict)

    def AgentFor(self, role: str) -> AgentChoice:
        override = self.roles.get(role, RoleSettings())
        provider = override.provider or self.provider
        settings = self.providers[provider]
        small = settings.smallModel if role in SMALL_ROLES else None
        return AgentChoice(provider, override.model or small or settings.model)


# ---------- loading ----------

def LoadConfig(projectDir: Path) -> tuple[OrchConfig, list[str]]:
    """The settings of <project>/orch.toml over the built-in defaults, and every problem found in the file."""
    config = OrchConfig()
    path = projectDir / CONFIG_FILE_NAME
    if not path.is_file():
        return config, []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))  # -sig: Windows editors may add a BOM
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return config, [f"{CONFIG_FILE_NAME}: {error}"]
    config.source = CONFIG_FILE_NAME
    errors: list[str] = []
    for section, table in data.items():
        if section == ROLES_SECTION:
            ReadRoles(table, config, errors)
        elif section in SECTION_KEYS:
            target = config if section in ("run", "agent") else config.providers[section]
            for key, value in ReadTable(f"[{section}]", table, SECTION_KEYS[section], errors).items():
                setattr(target, FIELDS[key], value)
        else:
            sections = ", ".join([*SECTION_KEYS, ROLES_SECTION])
            errors.append(f"{CONFIG_FILE_NAME}: unknown section [{section}] (use: {sections})")
    return config, errors


def ReadRoles(table: object, config: OrchConfig, errors: list[str]) -> None:
    if not isinstance(table, dict):
        errors.append(f"{CONFIG_FILE_NAME}: [{ROLES_SECTION}] must hold one table per role, e.g. [roles.reviewer]")
        return
    for role, settings in table.items():
        if role not in ROLES:
            errors.append(f"{CONFIG_FILE_NAME}: unknown role [roles.{role}] (use: {', '.join(ROLES)})")
            continue
        values = ReadTable(f"[roles.{role}]", settings, ROLE_KEYS, errors)
        config.roles[role] = RoleSettings(**{FIELDS[key]: value for key, value in values.items()})


def ReadTable(where: str, table: object, keys: dict[str, type], errors: list[str]) -> dict[str, object]:
    """The table's valid values by key; unknown keys and wrong types become errors instead of being ignored."""
    if not isinstance(table, dict):
        errors.append(f"{CONFIG_FILE_NAME}: {where} must be a table")
        return {}
    values = {}
    for key, value in table.items():
        problem = None
        if key not in keys:
            problem = f"is unknown (use: {', '.join(keys)})"
        elif not IsType(value, keys[key]):
            problem = f"must be {TYPE_NAMES[keys[key]]}, not {value!r}"
        elif key == "provider" and value not in PROVIDERS:
            problem = f"must be one of: {', '.join(PROVIDERS)}"
        if problem:
            errors.append(f"{CONFIG_FILE_NAME}: {where} {key} {problem}")
            continue
        values[key] = Normalize(value, keys[key])
    return values


def IsType(value: object, kind: type) -> bool:
    if isinstance(value, bool):  # TOML true/false is not a number
        return False
    return isinstance(value, (int, float)) if kind is float else isinstance(value, kind)


def Normalize(value, kind: type):
    if kind is float:
        return float(value)
    if kind is str:
        return value.strip() or None  # "" = not set: the CLI's default model, an auto-detected path
    return value


# ---------- checks and display ----------

def Problems(config: OrchConfig) -> list[str]:
    """Values out of range in the final settings (orch.toml + command line); they stop a run."""
    limits = [
        (config.maxParallel >= 0, "max_parallel (--max-parallel) must be >= 0; 0 = automatic"),
        (config.maxRounds >= 0, "max_rounds (--max-rounds) must be >= 0"),
        (config.budgetPerAgent > 0, "budget_per_agent (--budget-per-agent) must be > 0"),
        (config.maxTurns >= 1, "max_turns (--max-turns) must be >= 1"),
    ]
    claudeRetries, geminiRetries = (config.providers[name].maxRetries for name in ("claude", "gemini"))
    limits += [
        (claudeRetries is None or claudeRetries >= 0, "[claude] max_retries must be >= 0"),
        (geminiRetries is None or 0 <= geminiRetries <= GEMINI_MAX_RETRIES, f"[gemini] max_retries must be 0-{GEMINI_MAX_RETRIES}"),
    ]
    return [message for ok, message in limits if not ok]


def UsesProvider(config: OrchConfig, provider: str) -> bool:
    return any(config.AgentFor(role).provider == provider for role in ROLES)


def AgentSummary(config: OrchConfig) -> list[str]:
    """One line per provider and model in use, with the roles that run on it."""
    groups: dict[AgentChoice, list[str]] = {}
    for role in ROLES:
        groups.setdefault(config.AgentFor(role), []).append(role)
    return [
        f"{choice.provider} {choice.model or '(CLI default model)'} - {', '.join(roles)}"
        for choice, roles in groups.items()
    ]
