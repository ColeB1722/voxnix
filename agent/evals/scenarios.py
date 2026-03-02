"""Eval scenario definitions — structured data for all eval test cases.

Each scenario is a self-contained spec describing one eval interaction.
Reading a scenario tells you everything the test will assert — no need to
trace through test runner logic to understand what's being evaluated.

This is the TDD-style contract: write the scenario (spec) first, then
make the agent pass it. Adding a new eval means adding a new Scenario
with its quality checks — the test runner just executes them.

Fields:
  - input: what the user says to the agent
  - expected_tools: which tool(s) the agent should call
  - accept_alternatives: optional alternative tools that are also acceptable
  - quality_checks: which response quality criteria to evaluate, and at what tier
  - category: grouping for reporting (lifecycle, query, diagnostics, etc.)
  - tier: enforcement level for tool routing (must, must_not, should)
  - description: human-readable explanation of what this scenario tests

Multi-turn scenarios are separate dataclasses with a list of turns.

Scenarios are pure data — no LLM calls, no deepeval imports, no fixtures.
Test modules import these and parametrize over them.

See docs/handoff.md § Agent evals (#51) for the scenario design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

# ── Quality check definitions ──────────────────────────────────────────────────

# Enforcement tiers for response quality metrics:
#   must     — fails CI if the metric scores below threshold
#   should   — logs a warning but does not fail CI
Tier = Literal["must", "should"]


class Metric(StrEnum):
    """Metric names — map 1:1 to GEval instances in the test runner.

    StrEnum so values work as dict keys, GEval ``name=`` params, and
    f-string interpolation with zero conversion.
    """

    NO_RAW_ERRORS = "NoRawErrors"
    NO_MARKDOWN = "NoMarkdown"
    CONCISENESS = "Conciseness"
    CONTAINER_NAME = "ContainerNamePresent"
    EMOJI_INDICATORS = "EmojiIndicators"
    NO_HALLUCINATED_MODULES = "NoHallucinatedModules"


# Re-export members at module level for backward compatibility.
# Existing imports like ``from scenarios import NO_RAW_ERRORS`` keep working.
NO_RAW_ERRORS = Metric.NO_RAW_ERRORS
NO_MARKDOWN = Metric.NO_MARKDOWN
CONCISENESS = Metric.CONCISENESS
CONTAINER_NAME = Metric.CONTAINER_NAME
EMOJI_INDICATORS = Metric.EMOJI_INDICATORS
NO_HALLUCINATED_MODULES = Metric.NO_HALLUCINATED_MODULES


@dataclass(frozen=True)
class QualityCheck:
    """A single response quality expectation attached to a scenario.

    Each check names a metric and the enforcement tier. The test runner
    looks up the corresponding GEval instance by name and applies the tier
    to decide assert vs warn.
    """

    metric: Metric
    tier: Tier = "must"


# ── Reusable check sets ────────────────────────────────────────────────────────
# Most scenarios share the same baseline checks. These helpers keep the
# scenario definitions DRY without hiding what's being tested — every
# scenario still lists its checks explicitly via _checks() or _checks_with().


def _baseline() -> list[QualityCheck]:
    """Checks that apply to every scenario: no errors, no markdown, concise, no hallucinations."""
    return [
        QualityCheck(NO_RAW_ERRORS, "must"),
        QualityCheck(NO_MARKDOWN, "must"),
        QualityCheck(CONCISENESS, "must"),
        QualityCheck(NO_HALLUCINATED_MODULES, "should"),
    ]


def _with_container_name() -> list[QualityCheck]:
    """Baseline + container name must be present in the response."""
    return [
        *_baseline(),
        QualityCheck(CONTAINER_NAME, "must"),
    ]


def _with_container_name_and_emoji() -> list[QualityCheck]:
    """Baseline + container name + emoji indicators (lifecycle operations)."""
    return [
        *_baseline(),
        QualityCheck(CONTAINER_NAME, "must"),
        QualityCheck(EMOJI_INDICATORS, "should"),
    ]


def _with_emoji() -> list[QualityCheck]:
    """Baseline + emoji indicators (lifecycle without a specific container query)."""
    return [
        *_baseline(),
        QualityCheck(EMOJI_INDICATORS, "should"),
    ]


# ── Single-turn scenarios ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    """A single-turn eval scenario — the complete spec for one test.

    To add a new eval:
      1. Define a Scenario with input, expected_tools, and quality_checks.
      2. The test runner handles everything else.

    Reading this dataclass tells you exactly what the test will assert:
      - Which tool(s) the agent should call (expected_tools)
      - Whether alternative tools are acceptable (accept_alternatives)
      - Which response quality metrics apply and at what tier (quality_checks)
      - Whether tool routing failure is hard (must) or soft (should)
    """

    id: str
    input: str
    expected_tools: list[str]
    quality_checks: list[QualityCheck]
    accept_alternatives: list[list[str]] = field(default_factory=list)
    category: str = "general"
    tier: Tier = "must"  # enforcement level for tool routing
    description: str = ""


# ── Query vs status vs list disambiguation ─────────────────────────────────────

QUERY_VS_STATUS = [
    Scenario(
        id="query-tell-me-about",
        input="Tell me about dev",
        expected_tools=["tool_query_container"],
        quality_checks=_with_container_name(),
        category="query",
        description="Broad 'tell me about' should route to query, not status or list",
    ),
    Scenario(
        id="query-modules",
        input="What modules does dev have?",
        expected_tools=["tool_query_container"],
        quality_checks=_with_container_name(),
        category="query",
        description="Module question is a query concern, not status",
    ),
    Scenario(
        id="status-is-running",
        input="Is dev running?",
        expected_tools=["tool_get_container_status"],
        quality_checks=_with_container_name(),
        category="diagnostics",
        description="Direct running/stopped question is a status check",
    ),
    Scenario(
        id="list-my-containers",
        input="What containers do I have?",
        expected_tools=["tool_list_workloads"],
        quality_checks=_baseline(),
        category="query",
        description="Listing all containers, not querying one specific container",
    ),
    Scenario(
        id="query-how-is-doing",
        input="How is dev doing?",
        expected_tools=["tool_query_container"],
        quality_checks=_with_container_name(),
        accept_alternatives=[["tool_get_container_status"]],
        category="query",
        description="Broad question — query preferred, status acceptable",
    ),
    Scenario(
        id="query-tailscale-ip",
        input="What's the Tailscale IP for dev?",
        expected_tools=["tool_query_container"],
        quality_checks=_with_container_name(),
        accept_alternatives=[["tool_get_tailscale_status"]],
        category="query",
        description="Tailscale IP is a query facet, but tailscale_status is also reasonable",
    ),
]

# ── Diagnostics routing ────────────────────────────────────────────────────────

DIAGNOSTICS = [
    Scenario(
        id="diag-host-health",
        input="Container ops keep failing, what's wrong?",
        expected_tools=["tool_check_host_health"],
        quality_checks=_baseline(),
        category="diagnostics",
        description="General infrastructure failure → host health check",
    ),
    Scenario(
        id="diag-container-logs",
        input="Show me the logs for dev",
        expected_tools=["tool_get_container_logs"],
        quality_checks=_with_container_name(),
        category="diagnostics",
        description="Explicit log request maps directly to log tool",
    ),
    Scenario(
        id="diag-tailscale-status",
        input="Is Tailscale working on dev?",
        expected_tools=["tool_get_tailscale_status"],
        quality_checks=_with_container_name(),
        category="diagnostics",
        description="Tailscale connectivity question → tailscale status tool",
    ),
    Scenario(
        id="diag-service-check",
        input="Is the nix-daemon service running?",
        expected_tools=["tool_check_service"],
        quality_checks=_baseline(),
        category="diagnostics",
        description="Named host service check → service status tool",
    ),
    Scenario(
        id="diag-why-cant-create",
        input="Why can't I create containers?",
        expected_tools=["tool_check_host_health"],
        quality_checks=_baseline(),
        category="diagnostics",
        description="Creation failure diagnosis → host health check",
    ),
]

# ── Lifecycle commands ─────────────────────────────────────────────────────────

LIFECYCLE = [
    Scenario(
        id="lifecycle-create",
        input="Create a dev container with git and fish",
        expected_tools=["tool_create_container"],
        quality_checks=_with_emoji(),
        category="lifecycle",
        description="Unambiguous create request with module list",
    ),
    Scenario(
        id="lifecycle-destroy",
        input="Destroy the dev container",
        expected_tools=["tool_destroy_container"],
        quality_checks=_with_container_name_and_emoji(),
        category="lifecycle",
        description="Unambiguous destroy — should act immediately, no confirmation",
    ),
    Scenario(
        id="lifecycle-stop",
        input="Stop dev",
        expected_tools=["tool_stop_container"],
        quality_checks=_with_container_name_and_emoji(),
        category="lifecycle",
        description="Short stop command with container name",
    ),
    Scenario(
        id="lifecycle-start",
        input="Start dev",
        expected_tools=["tool_start_container"],
        quality_checks=_with_container_name_and_emoji(),
        category="lifecycle",
        description="Short start command with container name",
    ),
]

# ── Storage ────────────────────────────────────────────────────────────────────

STORAGE = [
    Scenario(
        id="storage-usage",
        input="How much storage am I using?",
        expected_tools=["tool_storage_usage"],
        quality_checks=_baseline(),
        category="storage",
        description="Storage/disk usage question → storage usage tool",
    ),
]

# ── Vague / edge cases ─────────────────────────────────────────────────────────

EDGE_CASES = [
    Scenario(
        id="edge-whats-happening",
        input="What's happening on my server?",
        expected_tools=["tool_list_workloads"],
        quality_checks=_baseline(),
        category="query",
        description="Vague overview question — list is the safe default",
    ),
]

# ── All single-turn scenarios ──────────────────────────────────────────────────

ALL_SCENARIOS: list[Scenario] = [
    *QUERY_VS_STATUS,
    *DIAGNOSTICS,
    *LIFECYCLE,
    *STORAGE,
    *EDGE_CASES,
]

# Convenience lookups
SCENARIO_BY_ID: dict[str, Scenario] = {s.id: s for s in ALL_SCENARIOS}
SCENARIOS_BY_CATEGORY: dict[str, list[Scenario]] = {}
for _s in ALL_SCENARIOS:
    SCENARIOS_BY_CATEGORY.setdefault(_s.category, []).append(_s)


# ── Multi-turn scenarios ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Turn:
    """A single turn in a multi-turn conversation."""

    input: str
    expected_tools: list[str]
    accept_alternatives: list[list[str]] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class MultiTurnScenario:
    """A multi-turn eval scenario — tests conversation context retention."""

    id: str
    turns: list[Turn]
    category: str = "multi_turn"
    description: str = ""


MULTI_TURN_SCENARIOS: list[MultiTurnScenario] = [
    MultiTurnScenario(
        id="multi-create-then-query",
        turns=[
            Turn(
                input="Create a dev container with git and fish",
                expected_tools=["tool_create_container"],
                description="First turn: create a container",
            ),
            Turn(
                input="What modules does it have?",
                expected_tools=["tool_query_container"],
                description=(
                    "Second turn: 'it' refers to the container just created. "
                    "Agent must retain context to resolve the pronoun and pick "
                    "query over list."
                ),
            ),
        ],
        description=(
            "Knowledge retention: agent remembers what it just created "
            "and resolves 'it' to the container name"
        ),
    ),
    MultiTurnScenario(
        id="multi-status-then-logs",
        turns=[
            Turn(
                input="Is dev running?",
                expected_tools=["tool_get_container_status"],
                description="First turn: check container status",
            ),
            Turn(
                input="Show me the logs",
                expected_tools=["tool_get_container_logs"],
                description=(
                    "Second turn: 'the logs' implicitly refers to the same "
                    "container from the previous turn. Agent must carry context "
                    "forward for diagnostic escalation."
                ),
            ),
        ],
        description=(
            "Diagnostic escalation: status check followed by implicit log "
            "request for the same container"
        ),
    ),
]
