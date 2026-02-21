"""Voxnix orchestrator agent — PydanticAI agent with container lifecycle tools.

This module defines the agent and registers all tools. The agent is
the single entry point for the chat layer (Telegram) to interact with
the infrastructure.

Architecture decisions reflected here:
- Agent is instantiated at module level with model=None and defer_model_check=True.
  This means importing agent.py never triggers get_settings() — the model string
  is resolved lazily on the first .run() call via the `model` argument. Safe in CI
  and tests without any env vars set.
- Owner (Telegram chat_id) flows in via VoxnixDeps — never a tool argument.
  The chat layer sets it; the agent cannot be tricked into crossing ownership.
- System prompt is dynamic — available modules are injected at interaction time
  from the live Nix flake output. No hardcoded module lists in Python.
- LLM provider and model are configured via environment variables (agenix secrets).
  The agent binary is provider-agnostic.
- Logfire is used for all observability — full traces for every agent run.

See docs/architecture.md § Agent Tool Architecture and § Trust Model.
"""

from __future__ import annotations

from dataclasses import dataclass

import logfire
from pydantic_ai import Agent, RunContext

from agent.config import get_settings
from agent.nix_gen.discovery import discover_modules
from agent.nix_gen.models import ContainerSpec
from agent.tools.containers import (
    ContainerResult,
    create_container,
    destroy_container,
    start_container,
    stop_container,
)
from agent.tools.workloads import Workload, WorkloadError, get_container_owner, list_workloads

# ── Logfire instrumentation ────────────────────────────────────────────────────

logfire.configure()
logfire.instrument_pydantic_ai()

# ── Dependencies ───────────────────────────────────────────────────────────────


@dataclass
class VoxnixDeps:
    """Per-request dependencies injected by the chat layer.

    The owner is the Telegram chat_id of the user making the request.
    It is set by the chat integration layer before running the agent —
    tools read it from context rather than accepting it as an argument,
    enforcing ownership scoping at the framework level.
    """

    owner: str


# ── Agent definition ───────────────────────────────────────────────────────────

# model=None — the model is resolved at run time by passing get_settings().llm_model_string
# to agent.run(). This keeps module-level instantiation free of any env var access.
#
# defer_model_check=True — PydanticAI's built-in mechanism to defer model validation
# (environment variable checks, provider availability) until the first .run() call.
# Together with model=None this means importing this module in CI or tests never
# requires LLM_PROVIDER, LLM_MODEL, or any provider API key to be set.
agent: Agent[VoxnixDeps, str] = Agent(
    model=None,
    deps_type=VoxnixDeps,
    defer_model_check=True,
)


# ── System prompt ──────────────────────────────────────────────────────────────


@agent.system_prompt
async def system_prompt(ctx: RunContext[VoxnixDeps]) -> str:
    """Dynamic system prompt — injected fresh at each interaction.

    Includes live available modules from the Nix flake so the agent always
    knows what it can offer without hardcoded lists. See architecture.md §
    Agent Capability Discovery.
    """
    try:
        modules = await discover_modules()
        modules_str = ", ".join(modules) if modules else "none currently available"
    except Exception:  # noqa: BLE001
        modules_str = "unavailable (module discovery failed)"

    return f"""\
You are Voxnix, an AI infrastructure orchestrator for a personal NixOS appliance.
You manage containers and VMs on behalf of the owner via natural language.

Your owner's ID is: {ctx.deps.owner}

Available workload modules: {modules_str}

Guidelines:
- Be concise. Users get brief status updates, not walls of text.
- Containers are ephemeral by design — only ZFS-backed workspaces persist across restarts.
- Always confirm before destroying containers or data. Ask once, then act.
- If a tool call fails, diagnose the error, attempt a fix, and retry once before escalating.
- Never expose raw error output to the user — translate it into plain language.
- You can only manage containers owned by {ctx.deps.owner}. Do not act on others' resources.
"""


# ── Tools ──────────────────────────────────────────────────────────────────────


@agent.tool
async def tool_create_container(
    ctx: RunContext[VoxnixDeps],
    name: str,
    modules: list[str],
) -> str:
    """Create and start a new NixOS container.

    Args:
        name: Container name. Must be lowercase alphanumeric with hyphens,
              no leading/trailing hyphens (e.g. "my-dev-container").
        modules: List of workload modules to include. Choose from the
                 available modules listed in the system prompt.

    Returns:
        A plain-language summary of the result for the user.
    """
    spec = ContainerSpec(name=name, owner=ctx.deps.owner, modules=modules)
    result: ContainerResult = await create_container(spec)

    if result.success:
        return f"✅ Container `{result.name}` is running."

    return f"❌ Failed to create `{result.name}`: {result.error}"


@agent.tool
async def tool_destroy_container(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Destroy a container and its ephemeral state.

    Only destroys containers owned by the requesting user.
    ZFS workspace data is not affected — it persists on the host.

    Args:
        name: Name of the container to destroy.

    Returns:
        A plain-language summary of the result for the user.
    """
    # Ownership check — do not destroy containers belonging to other users.
    container_owner = await get_container_owner(name)
    if container_owner != ctx.deps.owner:
        if container_owner is None:
            return f"❌ Container `{name}` not found or not running."
        return f"❌ Container `{name}` belongs to another user."

    result: ContainerResult = await destroy_container(name)

    if result.success:
        return f"✅ Container `{result.name}` destroyed."

    return f"❌ Failed to destroy `{result.name}`: {result.error}"


@agent.tool
async def tool_start_container(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Start a stopped container.

    Args:
        name: Name of the container to start.

    Returns:
        A plain-language summary of the result for the user.
    """
    result: ContainerResult = await start_container(name)

    if result.success:
        return f"✅ Container `{result.name}` started."

    return f"❌ Failed to start `{result.name}`: {result.error}"


@agent.tool
async def tool_stop_container(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Stop a running container without destroying it.

    Args:
        name: Name of the container to stop.

    Returns:
        A plain-language summary of the result for the user.
    """
    result: ContainerResult = await stop_container(name)

    if result.success:
        return f"✅ Container `{result.name}` stopped."

    return f"❌ Failed to stop `{result.name}`: {result.error}"


@agent.tool
async def tool_list_workloads(ctx: RunContext[VoxnixDeps]) -> str:
    """List all containers and VMs owned by the current user.

    Returns:
        A formatted summary of running workloads for the user,
        or a message indicating none are running.
    """
    try:
        workloads: list[Workload] = await list_workloads(owner=ctx.deps.owner)
    except WorkloadError as e:
        return f"❌ Could not query workloads: {e}"

    if not workloads:
        return "No containers or VMs running."

    lines = []
    for w in workloads:
        status = "🟢 running" if w.is_running else "🔴 stopped"
        kind = "container" if w.is_container else "VM"
        addr = ", ".join(w.addresses) if w.addresses else "no address"
        lines.append(f"• `{w.name}` ({kind}) — {status} — {addr}")

    return "\n".join(lines)


# ── Run helper ─────────────────────────────────────────────────────────────────


async def run(message: str, owner: str) -> str:
    """Run the agent for a single user message.

    Resolves the model from settings at call time — env vars are only required
    when actually running the agent, not at import time.

    Args:
        message: The user's natural language message.
        owner: The Telegram chat_id of the requesting user.

    Returns:
        The agent's response as a string.
    """
    result = await agent.run(
        message,
        model=get_settings().llm_model_string,
        deps=VoxnixDeps(owner=owner),
    )
    return result.output
