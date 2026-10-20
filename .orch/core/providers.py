"""The agent providers: what settings, preview and the runner need to know about each one. SDK-free.

Every provider has a backend module in agents/ with the same entry point,
    async def Run(label, prompt, spec, ctx, model) -> AgentResult
imported on its first call, so a provider no role uses needs neither its SDK nor its CLI.
Adding a provider = an entry here (orch.toml then accepts its [<name>] section) + its backend module.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    module: str  # backend module in agents/, e.g. "agents.claude"
    smallModel: str | None  # default small_model (namer, reporter); None = model
    maxRetries: int | None = None  # default max_retries; None = the CLI's own default
    retryLimit: int | None = None  # highest max_retries the CLI accepts; None = no limit
    notes: tuple[str, ...] = ()  # how its agents differ from the others, shown by preview


PROVIDER_SPECS = {
    "claude": ProviderSpec(module="agents.claude", smallModel="haiku"),
    # Fails fast by default: the CLI's own 10 attempts per rate-limited request can stall an agent for many minutes
    "gemini": ProviderSpec(
        module="agents.gemini",
        smallModel="flash-lite",
        maxRetries=0,
        retryLimit=9,  # the Gemini CLI allows at most 10 attempts per request
        notes=("cost not reported ($0 in the summary, tokens in run.log); max_turns applies, budget_per_agent does not",),
    ),
}
PROVIDERS = tuple(PROVIDER_SPECS)
