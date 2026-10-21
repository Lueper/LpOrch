"""orch.toml: default run options, where the task list lives, and the provider and model each agent role uses.

The command line overrides the file and the file overrides the built-in defaults below; a missing file or key keeps
the default. Every provider of core/providers.py has its own [<provider>] section with the same keys, and
[tasks]/[notion] say whether the tasks come from TASKS.md or a Notion database (core/tasksource.py) and whether
finished tasks may add follow-up tasks to that list.
SDK-free so `preview` works without any provider's SDK installed.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from core.providers import PROVIDER_SPECS, PROVIDERS

CONFIG_FILE_NAME = "orch.toml"  # at the project root, next to TASKS.md
# Every agent role; agents/runner.ROLE_TOOLS has the same keys
ROLES = (
    "doc-worker", "code-worker", "sys-worker", "test-worker", "task-worker",
    "reviewer", "doc-merger", "code-merger", "planner", "namer", "reporter",
)
SMALL_ROLES = ("namer", "reporter")  # one-shot chores: the provider's small_model is enough
ROLES_SECTION = "roles"  # [roles.<role>] tables override provider and model per role
NOTION_SECTION = "notion"  # the Notion database the tasks come from when source = "notion"
SOURCE_FILE = "file"  # TASKS.md at the project root
SOURCE_NOTION = "notion"  # a Notion database (core/notion.py)
TASK_SOURCES = (SOURCE_FILE, SOURCE_NOTION)
DEFAULT_TOKEN_ENV = "NOTION_TOKEN"  # the environment variable the integration token is read from
PROVIDER_KEYS: dict[str, type] = {"model": str, "small_model": str, "cli_path": str, "max_retries": int}
# Only which database and where its token is: property names and status options are fixed in core/notion.py
NOTION_KEYS: dict[str, type] = {"database": str, "token_env": str}
# Keys each section accepts and the type of their values
SECTION_KEYS: dict[str, dict[str, type]] = {
    "run": {"max_parallel": int, "max_rounds": int, "budget_per_agent": float, "max_turns": int},
    "agent": {"provider": str},
    "tasks": {"source": str, "follow_ups": bool},
    NOTION_SECTION: NOTION_KEYS,
    **{provider: PROVIDER_KEYS for provider in PROVIDERS},
}
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
    "source": "taskSource",
    "follow_ups": "followUps",
    "database": "database",
    "token_env": "tokenEnv",
}
TYPE_NAMES = {int: "an integer", float: "a number", str: "a string", bool: "true or false"}


@dataclass
class ProviderSettings:
    model: str | None = None  # None = the CLI's default model
    smallModel: str | None = None  # namer and reporter; None = model
    cliPath: str | None = None  # the provider's CLI, as its backend in agents/ accepts it; None = auto-detect
    maxRetries: int | None = None  # retries of a failed API request (429, network); None = the CLI's default


@dataclass
class NotionSettings:
    """[notion]: which database holds the tasks. Its property names and status options are fixed in core/notion.py."""

    database: str | None = None  # the database link ("copy link") or its id
    tokenEnv: str | None = DEFAULT_TOKEN_ENV  # environment variable with the integration token, never the token itself


@dataclass
class RoleSettings:
    provider: str | None = None  # None = [agent] provider
    model: str | None = None  # None = the provider's model (small_model for namer and reporter)


class AgentChoice(NamedTuple):
    provider: str
    model: str | None  # None = the CLI's default model


def DefaultProviders() -> dict[str, ProviderSettings]:
    # Each provider's own defaults live in its registry entry (core/providers.py)
    return {
        name: ProviderSettings(smallModel=spec.smallModel, maxRetries=spec.maxRetries)
        for name, spec in PROVIDER_SPECS.items()
    }


@dataclass
class OrchConfig:
    source: str = f"built-in defaults (no {CONFIG_FILE_NAME})"  # where the values came from, shown by preview
    maxParallel: int = 0  # concurrent agents across all tasks; 0 = automatic
    maxRounds: int = 1  # review/fix rounds; 0 = no review
    budgetPerAgent: float = 2.0  # USD cap per agent call
    maxTurns: int = 40  # turn cap per agent call
    provider: str = "claude"  # every role without its own [roles.<role>] provider
    taskSource: str = SOURCE_FILE  # where the task list lives: TASKS.md or a Notion database
    followUps: bool = True  # let a finished task add the follow-up tasks its agents asked for to the task list
    providers: dict[str, ProviderSettings] = field(default_factory=DefaultProviders)
    notion: NotionSettings = field(default_factory=NotionSettings)
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
            target = SectionTarget(config, section)
            for key, value in ReadTable(f"[{section}]", table, SECTION_KEYS[section], errors).items():
                setattr(target, FIELDS[key], value)
        else:
            sections = ", ".join([*SECTION_KEYS, ROLES_SECTION])
            errors.append(f"{CONFIG_FILE_NAME}: unknown section [{section}] (use: {sections})")
    return config, errors


def SectionTarget(config: OrchConfig, section: str) -> object:
    """The object a section's keys are set on; [run], [agent] and [tasks] belong to the config itself."""
    if section in PROVIDERS:
        return config.providers[section]
    if section == NOTION_SECTION:
        return config.notion
    return config


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
        elif key == "source" and value not in TASK_SOURCES:
            problem = f"must be one of: {', '.join(TASK_SOURCES)}"
        if problem:
            errors.append(f"{CONFIG_FILE_NAME}: {where} {key} {problem}")
            continue
        values[key] = Normalize(value, keys[key])
    return values


def IsType(value: object, kind: type) -> bool:
    if kind is bool:
        return isinstance(value, bool)
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
        (config.taskSource in TASK_SOURCES, f"[tasks] source (--source) must be one of: {', '.join(TASK_SOURCES)}"),
    ]
    for name, spec in PROVIDER_SPECS.items():  # retryLimit: the most the provider's CLI accepts
        retries, top = config.providers[name].maxRetries, spec.retryLimit
        ok = retries is None or (retries >= 0 and (top is None or retries <= top))
        limits.append((ok, f"[{name}] max_retries must be " + (">= 0" if top is None else f"0-{top}")))
    return [message for ok, message in limits if not ok]


def ProvidersInUse(config: OrchConfig) -> list[str]:
    """The providers at least one role runs on, in registry order."""
    used = {config.AgentFor(role).provider for role in ROLES}
    return [name for name in PROVIDERS if name in used]


def AgentSummary(config: OrchConfig) -> list[str]:
    """One line per provider and model in use, with the roles that run on it."""
    groups: dict[AgentChoice, list[str]] = {}
    for role in ROLES:
        groups.setdefault(config.AgentFor(role), []).append(role)
    return [
        f"{choice.provider} {choice.model or '(CLI default model)'} - {', '.join(roles)}"
        for choice, roles in groups.items()
    ]
