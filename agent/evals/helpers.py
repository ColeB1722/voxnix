"""Helper utilities for extracting eval data from PydanticAI agent runs.

The eval suite runs the real Voxnix agent against a real LLM, then needs to
extract tool call information from PydanticAI's native message protocol to
construct deepeval ``LLMTestCase`` objects.

This module bridges PydanticAI's message format and deepeval's ``ToolCall``
format without introducing any deepeval imports into the agent's production
code.

Design decision: Option B from the eval plan — plain ``LLMTestCase``
construction from PydanticAI messages, not deepeval's ``@observe`` tracing.
PydanticAI is the source of truth for tool calls; deepeval only sees the
extracted data. See docs/handoff.md § Architecture decisions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepeval.test_case import LLMTestCase, ToolCall
from pydantic_ai.messages import ModelResponse  # pyright: ignore[reportMissingModuleSource]

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


def extract_tool_calls(messages: list[ModelMessage]) -> list[ToolCall]:
    """Convert PydanticAI tool calls into deepeval ``ToolCall`` objects.

    Uses ``ModelResponse.tool_calls`` (which already filters to
    ``ToolCallPart`` instances) rather than manually walking message parts.

    Args:
        messages: List of ``ModelMessage`` objects from
                  ``result.new_messages()`` or ``result.all_messages()``.

    Returns:
        List of deepeval ``ToolCall`` objects, in the order they were called.
        Empty list if no tool calls were made.
    """
    return [
        ToolCall(
            name=part.tool_name,
            input_parameters=part.args_as_dict() if part.args is not None else None,
        )
        for msg in messages
        if isinstance(msg, ModelResponse)
        for part in msg.tool_calls
    ]


def extract_tool_names(messages: list[ModelMessage]) -> list[str]:
    """Extract just the tool names from PydanticAI messages.

    Args:
        messages: List of ``ModelMessage`` objects.

    Returns:
        List of tool name strings, in call order. May contain duplicates
        if the same tool was called multiple times.
    """
    return [
        part.tool_name
        for msg in messages
        if isinstance(msg, ModelResponse)
        for part in msg.tool_calls
    ]


def build_test_case(
    *,
    input_text: str,
    actual_output: str,
    messages: list[ModelMessage],
    expected_tools: list[str] | None = None,
    expected_output: str | None = None,
) -> LLMTestCase:
    """Build a deepeval ``LLMTestCase`` from a PydanticAI agent run.

    Convenience function that extracts tool calls from messages and
    constructs a fully populated test case ready for metric evaluation.

    Args:
        input_text: The user's natural language input to the agent.
        actual_output: The agent's text response (``result.output``).
        messages: The ``result.new_messages()`` from the agent run.
        expected_tools: Optional list of tool names the agent should have called.
                        Used by ``ToolCorrectnessMetric``.
        expected_output: Optional expected output string.
                         Used by ``GEval`` when criteria reference expected output.

    Returns:
        A populated ``LLMTestCase`` with tool call data extracted from
        the PydanticAI messages.
    """
    tools_called = extract_tool_calls(messages)

    expected: list[ToolCall] | None = None
    if expected_tools is not None:
        expected = [ToolCall(name=name, input_parameters=None) for name in expected_tools]

    kwargs: dict = {
        "input": input_text,
        "actual_output": actual_output,
        "tools_called": tools_called,
        "expected_tools": expected,
    }
    if expected_output is not None:
        kwargs["expected_output"] = expected_output

    return LLMTestCase(**kwargs)
