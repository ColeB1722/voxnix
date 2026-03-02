"""Multi-turn conversational evals — does the agent retain context across turns?

Uses deepeval's ``ConversationalTestCase`` with ``KnowledgeRetentionMetric``
to evaluate whether the agent carries context forward between turns in a
conversation. These are INFORMATIONAL — they log scores and warn on failure
but do not fail CI.

Multi-turn evals are more expensive (2+ LLM calls per scenario) and more
prone to flakiness than single-turn evals. The goal here is to establish
baseline scores, not to gate PRs. Thresholds can be tightened in a follow-up
once we see score distributions.

Test flow for each multi-turn scenario:
  1. Run the agent through all turns, threading message history forward
  2. Check tool routing correctness per turn (warn-only)
  3. Construct a ``ConversationalTestCase`` from all turns
  4. Evaluate with ``KnowledgeRetentionMetric`` (warn-only)

The conversation is run exactly once per scenario — the previous design had
two separate test functions that each ran the full conversation independently,
doubling the agent calls for no additional coverage.

The agent runs against a real LLM with fully mocked infrastructure.
Conversation history threading uses PydanticAI's native ``message_history``
parameter — the same mechanism the Telegram chat layer uses in production.

All tests are marked with ``@pytest.mark.evals`` (via module-level
``pytestmark``) and excluded from default pytest runs.

See docs/handoff.md § Agent evals (#51) for design rationale.
"""

from __future__ import annotations

import warnings

import pytest
from deepeval.evaluate.configs import AsyncConfig, DisplayConfig
from deepeval.evaluate.evaluate import evaluate as deepeval_evaluate
from deepeval.metrics import KnowledgeRetentionMetric
from deepeval.test_case import ConversationalTestCase, Turn

from agent.agent import run
from agent.evals.helpers import extract_tool_names
from agent.evals.scenarios import MULTI_TURN_SCENARIOS, MultiTurnScenario

pytestmark = pytest.mark.evals

# ── Shared evaluate() config ──────────────────────────────────────────────────
# Run synchronously and suppress deepeval's built-in table output.
_ASYNC_CFG = AsyncConfig(run_async=False)
_DISPLAY_CFG = DisplayConfig(print_results=False, show_indicator=False)

# ── Metric instances ───────────────────────────────────────────────────────────

# Informational — low threshold, warn-only enforcement.
# KnowledgeRetentionMetric evaluates whether the agent retains facts from
# earlier turns (e.g. the container name it just created) and uses them
# correctly in subsequent turns.
_knowledge_retention = KnowledgeRetentionMetric(threshold=0.5)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _scenario_ids(scenarios: list[MultiTurnScenario]) -> list[str]:
    return [s.id for s in scenarios]


def _tool_selection_is_acceptable(
    actual_tools: list[str],
    expected_tools: list[str],
    accept_alternatives: list[list[str]],
) -> bool:
    """Check if the actual tool selection is acceptable."""
    if set(actual_tools) == set(expected_tools):
        return True
    return any(set(actual_tools) == set(alt) for alt in accept_alternatives)


# ── Multi-turn tests ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scenario",
    MULTI_TURN_SCENARIOS,
    ids=_scenario_ids(MULTI_TURN_SCENARIOS),
)
async def test_multi_turn(
    scenario: MultiTurnScenario,
    eval_owner: str,
    eval_model: str,
):
    """Run a multi-turn conversation once and evaluate both tool routing and knowledge retention.

    The conversation is executed exactly once — message history threads forward
    through all turns. The results are then evaluated for:
      - Tool routing correctness per turn (warn-only)
      - Knowledge retention across the full conversation (warn-only via deepeval)

    Does NOT fail CI — logs results and warns on low scores.
    """
    message_history = None
    conversation_turns: list[Turn] = []
    turn_results: list[dict] = []
    all_tools_acceptable = True

    # ── Run the conversation ───────────────────────────────────────────
    for i, turn in enumerate(scenario.turns):
        output, new_messages = await run(
            turn.input,
            owner=eval_owner,
            model=eval_model,
            message_history=message_history,
        )

        actual_tools = extract_tool_names(new_messages)
        acceptable = _tool_selection_is_acceptable(
            actual_tools,
            turn.expected_tools,
            turn.accept_alternatives,
        )

        if not acceptable:
            all_tools_acceptable = False

        turn_results.append(
            {
                "turn": i + 1,
                "input": turn.input,
                "output": output,
                "expected_tools": turn.expected_tools,
                "actual_tools": actual_tools,
                "acceptable": acceptable,
                "description": turn.description,
            }
        )

        # Accumulate history for the next turn.
        if message_history is None:
            message_history = list(new_messages)
        else:
            message_history.extend(new_messages)

        # Build the deepeval Turn for the conversational test case.
        conversation_turns.append(Turn(role="user", content=turn.input))
        conversation_turns.append(Turn(role="assistant", content=output))

    # ── Log results ────────────────────────────────────────────────────
    print(f"\n  Multi-turn scenario: {scenario.id}")
    print(f"  Description: {scenario.description}")
    for tr in turn_results:
        status = "✓" if tr["acceptable"] else "✗"
        print(f"  Turn {tr['turn']} [{status}]: {tr['input']!r}")
        print(f"    Expected tools: {tr['expected_tools']}")
        print(f"    Actual tools:   {tr['actual_tools']}")
        print(f"    Output: {tr['output'][:150]}{'...' if len(tr['output']) > 150 else ''}")

    # ── Tool routing check (warn-only) ─────────────────────────────────
    for tr in turn_results:
        if not tr["acceptable"]:
            warnings.warn(
                f"Multi-turn '{scenario.id}' turn {tr['turn']}: "
                f"expected {tr['expected_tools']}, got {tr['actual_tools']}. "
                f"({tr['description']})",
                stacklevel=2,
            )

    if all_tools_acceptable:
        print("  Tool routing: all turns correct")
    else:
        print("  Tool routing: one or more turns mismatched (see warnings)")

    # ── Knowledge retention evaluation ─────────────────────────────────
    convo_test_case = ConversationalTestCase(turns=conversation_turns)

    result = deepeval_evaluate(
        test_cases=[convo_test_case],
        metrics=[_knowledge_retention],
        async_config=_ASYNC_CFG,
        display_config=_DISPLAY_CFG,
    )

    test_results = result.test_results
    assert test_results, f"No test results returned for scenario '{scenario.id}'"
    metrics_data = test_results[0].metrics_data
    assert metrics_data, f"No metrics data returned for scenario '{scenario.id}'"
    metric_data = metrics_data[0]
    score = metric_data.score if metric_data.score is not None else 0.0
    passed = metric_data.success
    reason = metric_data.reason or ""

    print(f"  KnowledgeRetention: score={score:.2f} passed={passed}")
    if reason:
        print(f"  Reason: {reason}")

    # Informational only — warn but don't fail CI.
    if not passed:
        warnings.warn(
            f"Multi-turn knowledge retention low for '{scenario.id}' "
            f"(score={score:.2f}, threshold=0.5): {reason}",
            stacklevel=1,
        )
