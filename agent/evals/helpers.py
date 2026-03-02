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
from pydantic_ai.messages import (  # pyright: ignore[reportMissingModuleSource]
    ModelResponse,
    ToolCallPart,
)

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


def extract_tool_calls(messages: list[ModelMessage]) -> list[ToolCall]:
    """Extract tool calls from PydanticAI messages into deepeval ToolCall format.

    Iterates through all ``ModelResponse`` messages and finds ``ToolCallPart``
    entries. Each is converted to a deepeval ``ToolCall`` with the tool name
    and arguments (if present).

    Args:
        messages: List of ``ModelMessage`` objects from
                  ``result.new_messages()`` or ``result.all_messages()``.

    Returns:
        List of deepeval ``ToolCall`` objects, in the order they were called.
        Empty list if no tool calls were made.
    """
    calls: list[ToolCall] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    calls.append(
                        ToolCall(
                            name=part.tool_name,
                            input_parameters=part.args_as_dict() if part.args is not None else None,
                        )
                    )
    return calls


def extract_tool_names(messages: list[ModelMessage]) -> list[str]:
    """Extract just the tool names from PydanticAI messages.

    Convenience wrapper over ``extract_tool_calls`` for assertions that
    only care about which tools were called, not their arguments.

    Args:
        messages: List of ``ModelMessage`` objects.

    Returns:
        List of tool name strings, in call order. May contain duplicates
        if the same tool was called multiple times.
    """
    return [call.name for call in extract_tool_calls(messages)]


def extract_final_output(messages: list[ModelMessage]) -> str:
    """Extract the agent's final text output from PydanticAI messages.

    Walks the messages in reverse to find the last ``ModelResponse`` that
    contains a text part (not a tool call). This is the agent's final
    user-facing response.

    Args:
        messages: List of ``ModelMessage`` objects.

    Returns:
        The final text response string, or empty string if none found.
    """
    for msg in reversed(messages):
        if isinstance(msg, ModelResponse):
            text = msg.text
            if text:
                return text
    return ""


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
