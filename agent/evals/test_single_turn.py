"""Single-turn evals — one agent call per scenario, all assertions from the spec.

The test runner is deliberately dumb. Every decision about what to evaluate
and how strictly to enforce it is declared in the ``Scenario`` dataclass
(see ``scenarios.py``). This file just executes:

  1. Run the agent once per scenario.
  2. Evaluate tool correctness (from ``scenario.expected_tools`` / ``scenario.tier``).
  3. Evaluate each response quality check (from ``scenario.quality_checks``).
  4. Assert or warn based on the tier declared in the scenario.

To add a new eval, add a ``Scenario`` to ``scenarios.py`` with appropriate
``quality_checks``. Nothing in this file needs to change.

One model at a time: ``LLM_PROVIDER`` / ``LLM_MODEL`` control which model
is tested (defaults to openrouter / anthropic/claude-haiku-4.5). To test
a different model, override those env vars in ``.env.eval`` or CI secrets.

All tests are marked with ``@pytest.mark.evals`` (via module-level
``pytestmark``) and excluded from default pytest runs.

See docs/handoff.md § Agent evals (#51) for design rationale.
"""

from __future__ import annotations

import warnings

import pytest
from deepeval.evaluate.configs import AsyncConfig, DisplayConfig
from deepeval.evaluate.evaluate import evaluate as deepeval_evaluate
from deepeval.metrics import GEval, ToolCorrectnessMetric
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from agent.agent import run
from agent.evals.conftest import FAKE_MODULES
from agent.evals.helpers import build_test_case, extract_tool_names
from agent.evals.scenarios import (
    ALL_SCENARIOS,
    CONCISENESS,
    CONTAINER_NAME,
    EMOJI_INDICATORS,
    NO_HALLUCINATED_MODULES,
    NO_MARKDOWN,
    NO_RAW_ERRORS,
    Metric,
    Scenario,
)

pytestmark = pytest.mark.evals

# ── Shared evaluate() config ──────────────────────────────────────────────────
_ASYNC_CFG = AsyncConfig(run_async=False)
_DISPLAY_CFG = DisplayConfig(print_results=False, show_indicator=False)

# ── Metric instances ───────────────────────────────────────────────────────────
# Keyed by the same constant strings used in scenario quality_checks.
# The test runner looks up metrics by name — no if/elif chains.

_tool_correctness = ToolCorrectnessMetric(threshold=0.7)

_METRICS: dict[Metric, GEval] = {
    NO_RAW_ERRORS: GEval(
        name=NO_RAW_ERRORS,
        criteria=(
            "The response must not contain raw error output, stack traces, Python "
            "tracebacks, CLI stderr strings, file paths like /nix/store/..., or "
            "technical error codes. The response should translate errors into "
            "plain language the user can understand. A response that says something "
            "like 'Failed to create container: permission denied' is acceptable. "
            "A response that dumps 'subprocess.CalledProcessError' or "
            "'Read-only file system' verbatim is not."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.7,
    ),
    NO_MARKDOWN: GEval(
        name=NO_MARKDOWN,
        criteria=(
            "The response must not use Markdown formatting. Specifically: "
            "no **bold** markers, no *italic* markers, no `backtick` inline code, "
            "no ```code blocks```, no [links](url), no # headings. "
            "Plain dashes for lists are acceptable. Plain text emphasis "
            "(like capitalisation or quotes) is acceptable. "
            "The response is displayed in Telegram which does not render Markdown — "
            "raw markers appear as-is and look broken."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.7,
    ),
    CONCISENESS: GEval(
        name=CONCISENESS,
        criteria=(
            "The response should be concise and to the point. For simple operations "
            "(creating, starting, stopping, destroying a container, listing workloads, "
            "checking storage), the response should be 5 sentences or fewer. "
            "Longer responses are acceptable only when the user asked a complex "
            "diagnostic question or when the response includes structured data "
            "like a list of containers or log lines."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.7,
    ),
    CONTAINER_NAME: GEval(
        name=CONTAINER_NAME,
        criteria=(
            "When the user's request involves a specific container by name, "
            "the response must include that container name. For example, if the "
            "user says 'create a dev container' or 'stop dev', the response must "
            "mention 'dev' somewhere. This helps the user confirm which container "
            "the action was applied to."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.7,
    ),
    EMOJI_INDICATORS: GEval(
        name=EMOJI_INDICATORS,
        criteria=(
            "Success responses should include a positive indicator like a checkmark "
            "emoji or similar success symbol. Failure responses should include a "
            "negative indicator like a cross emoji or warning symbol. This provides "
            "instant visual feedback in Telegram. The absence of emoji is not a "
            "critical failure, but their presence improves the user experience."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.5,
    ),
    NO_HALLUCINATED_MODULES: GEval(
        name=NO_HALLUCINATED_MODULES,
        criteria=(
            "If the response mentions workload module names, they must be from the "
            f"available set: {', '.join(FAKE_MODULES)}. The response must not "
            "invent or hallucinate module names that don't exist (e.g. 'docker', "
            "'nodejs', 'python', 'code-server'). Mentioning no modules at all is "
            "fine. Mentioning the correct available modules is fine. Inventing "
            "modules that aren't available is not."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.5,
    ),
}

# ── Helpers ────────────────────────────────────────────────────────────────────


def _scenario_ids(scenarios: list[Scenario]) -> list[str]:
    return [s.id for s in scenarios]


def _tool_selection_is_acceptable(
    actual_tools: list[str],
    scenario: Scenario,
) -> bool:
    """Check if the actual tool selection matches expected or alternatives."""
    if set(actual_tools) == set(scenario.expected_tools):
        return True
    return any(set(actual_tools) == set(alt) for alt in scenario.accept_alternatives)


def _run_scenario(
    scenario: Scenario,
    output: str,
    messages: list,
) -> None:
    """Evaluate a single scenario's output against all its declared checks.

    Called after the agent has already run. Handles:
      - Tool correctness via deepeval ToolCorrectnessMetric
      - Response quality via GEval metrics listed in scenario.quality_checks
      - Tier-based enforcement (must → assert, should → warn)
    """
    actual_tools = extract_tool_names(messages)
    acceptable = _tool_selection_is_acceptable(actual_tools, scenario)

    # ── Log ────────────────────────────────────────────────────────────
    print(f"\n  Scenario: {scenario.id}")
    print(f"  Input: {scenario.input!r}")
    print(f"  Expected tools: {scenario.expected_tools}")
    if scenario.accept_alternatives:
        print(f"  Alternatives: {scenario.accept_alternatives}")
    print(f"  Actual tools: {actual_tools}")
    print(f"  Tool match: {acceptable}")
    print(f"  Output: {output[:200]}{'...' if len(output) > 200 else ''}")

    # ── Tool correctness ───────────────────────────────────────────────
    tool_test_case = build_test_case(
        input_text=scenario.input,
        actual_output=output,
        messages=messages,
        expected_tools=scenario.expected_tools,
    )

    tool_result = deepeval_evaluate(
        test_cases=[tool_test_case],
        metrics=[_tool_correctness],
        async_config=_ASYNC_CFG,
        display_config=_DISPLAY_CFG,
    )

    tool_test_results = tool_result.test_results
    assert tool_test_results, f"No tool results for '{scenario.id}'"
    tool_metrics_data = tool_test_results[0].metrics_data
    assert tool_metrics_data, f"No tool metrics for '{scenario.id}'"
    tool_passed = tool_metrics_data[0].success

    if not tool_passed and scenario.accept_alternatives and acceptable:
        tool_passed = True

    print(f"  ToolCorrectness: passed={tool_passed}")

    if scenario.tier == "must":
        assert tool_passed or acceptable, (
            f"Tool routing failed for '{scenario.id}': "
            f"expected {scenario.expected_tools}, got {actual_tools}"
        )
    elif scenario.tier == "should" and not (tool_passed or acceptable):
        warnings.warn(
            f"SHOULD: tool routing suboptimal for '{scenario.id}': "
            f"expected {scenario.expected_tools}, got {actual_tools}",
            stacklevel=2,
        )

    # ── Response quality (from scenario.quality_checks) ────────────────
    if not scenario.quality_checks:
        return

    geval_metrics = []
    check_tiers: list[str] = []
    for check in scenario.quality_checks:
        metric = _METRICS.get(check.metric)
        assert metric, (
            f"Unknown metric '{check.metric}' in scenario '{scenario.id}'. "
            f"Known metrics: {sorted(_METRICS.keys())}"
        )
        geval_metrics.append(metric)
        check_tiers.append(check.tier)

    quality_test_case = LLMTestCase(
        input=scenario.input,
        actual_output=output,
    )

    quality_result = deepeval_evaluate(
        test_cases=[quality_test_case],
        metrics=geval_metrics,
        async_config=_ASYNC_CFG,
        display_config=_DISPLAY_CFG,
    )

    quality_test_results = quality_result.test_results
    assert quality_test_results, f"No quality results for '{scenario.id}'"
    quality_metrics_data = quality_test_results[0].metrics_data
    assert quality_metrics_data, f"No quality metrics for '{scenario.id}'"

    failures: list[str] = []

    for md, check in zip(quality_metrics_data, scenario.quality_checks, strict=True):
        score = md.score if md.score is not None else 0.0
        passed = md.success
        reason = md.reason or ""

        print(f"  {check.metric}: score={score:.2f} passed={passed}")

        if passed:
            continue

        if check.tier == "must":
            failures.append(f"{check.metric} (score={score:.2f}): {reason}")
        elif check.tier == "should":
            warnings.warn(
                f"SHOULD: {check.metric} for '{scenario.id}' (score={score:.2f}): {reason}",
                stacklevel=2,
            )

    if failures:
        detail = "; ".join(failures)
        pytest.fail(f"Response quality failed for '{scenario.id}': {detail}")


# ── Primary single-model tests ─────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=_scenario_ids(ALL_SCENARIOS))
async def test_scenario(scenario: Scenario, eval_owner: str, eval_model: str):
    """Run the agent once and evaluate all checks declared in the scenario.

    This function is intentionally minimal — all logic about what to
    evaluate and how strictly is declared in the Scenario dataclass.
    """
    output, messages = await run(
        scenario.input,
        owner=eval_owner,
        model=eval_model,
    )
    _run_scenario(scenario, output, messages)
