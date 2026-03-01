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
- Conversation history is managed by the chat layer (ConversationStore) and
  threaded into agent.run() via message_history. The agent itself is stateless —
  the chat layer owns persistence and TTL. See #48, #62.

See docs/architecture.md § Agent Tool Architecture and § Trust Model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import logfire
from pydantic_ai import Agent, RunContext

from agent.config import VoxnixSettings, get_settings

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

from agent.chat.history import DEFAULT_MAX_TURN_MESSAGES
from agent.nix_gen.discovery import discover_modules
from agent.nix_gen.models import ContainerSpec, validate_container_name
from agent.tools.containers import (
    ContainerResult,
    create_container,
    destroy_container,
    start_container,
    stop_container,
)
from agent.tools.diagnostics import (
    DiagnosticResult,
    check_host_health,
    get_container_logs,
    get_container_status,
    get_service_status,
    get_tailscale_status,
)
from agent.tools.query import ContainerInfo, query_container
from agent.tools.workloads import Workload, WorkloadError, get_container_owner, list_workloads
from agent.tools.zfs import get_user_storage_info

# ── Logfire instrumentation ────────────────────────────────────────────────────

# logfire.configure() is intentionally absent here — it is called in
# agent/chat/__main__.py with the actual token from VoxnixSettings.
# A module-level configure() without credentials would race (and win over)
# the entry-point call, discarding the token before it can be set.
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


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _check_ownership(name: str, owner: str) -> str | None:
    """Verify the requesting user owns the named container.

    Returns an error string if the check fails (caller should return it directly),
    or None if the caller is the owner and the operation should proceed.

    Args:
        name: Container name to check.
        owner: The requesting user's chat_id.
    """
    container_owner = await get_container_owner(name)
    if container_owner == owner:
        return None
    if container_owner is None:
        return f"❌ Container `{name}` not found or not running."
    return f"❌ Container `{name}` belongs to another user."


# ── History processor ──────────────────────────────────────────────────────────


async def keep_recent_turns(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Trim conversation history to the most recent turns before sending to the LLM.

    This is a PydanticAI history_processor — it runs inside agent.run() on every
    model request, trimming what the model sees without affecting what the
    ConversationStore persists. The store accumulates the full raw history;
    this processor keeps the context window manageable.

    The cap is DEFAULT_MAX_TURN_MESSAGES (max_turns * 2). When the history
    exceeds this, the oldest messages are dropped.

    See PydanticAI docs § Messages and Chat History § history_processors.
    """
    if len(messages) > DEFAULT_MAX_TURN_MESSAGES:
        return messages[-DEFAULT_MAX_TURN_MESSAGES:]
    return messages


# ── Agent definition ───────────────────────────────────────────────────────────

# model=None — the model is resolved at run time by passing get_settings().llm_model_string
# to agent.run(). This keeps module-level instantiation free of any env var access.
#
# defer_model_check=True — PydanticAI's built-in mechanism to defer model validation
# (environment variable checks, provider availability) until the first .run() call.
# Together with model=None this means importing this module in CI or tests never
# requires LLM_PROVIDER, LLM_MODEL, or any provider API key to be set.
#
# history_processors — delegates context window trimming to PydanticAI's pipeline.
# The ConversationStore handles persistence and TTL; the history_processor handles
# "what does the LLM actually see." Clean separation of concerns.
agent: Agent[VoxnixDeps, str] = Agent(
    model=None,
    deps_type=VoxnixDeps,
    defer_model_check=True,
    history_processors=[keep_recent_turns],
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
- Do not use Markdown formatting in responses. Respond in plain text only.
  Telegram does not render Markdown — raw markers like **bold** and `backticks` appear as-is.
  Use plain dashes for lists, plain text for emphasis, and spell out code references naturally.
- Containers are ephemeral by design — only ZFS-backed workspaces persist across restarts.
- Containers with the tailscale module get a hostname on the tailnet matching the container name.
  Users can SSH in via Tailscale SSH (e.g. ssh root@<container-name>). Always include the tailscale
  module unless the user explicitly asks for a container without remote access.
- Container names must be 11 characters or fewer (network interface name limit). Choose short names.
- Destroy containers immediately when explicitly requested — do not ask for confirmation first.
  Trust that an explicit destroy request is intentional.
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
    # If the tailscale module is requested, inject the auth key from settings.
    # Refuse early if the key isn't configured — a container with the tailscale
    # module but no auth key would start but never connect to the tailnet.
    tailscale_auth_key: str | None = None
    if "tailscale" in modules:
        settings: VoxnixSettings = get_settings()
        if settings.tailscale_auth_key is None:
            return (
                "❌ The tailscale module requires a Tailscale auth key, "
                "but TAILSCALE_AUTH_KEY is not configured on the appliance. "
                "Ask the admin to add it via agenix."
            )
        tailscale_auth_key = settings.tailscale_auth_key.get_secret_value()

    spec = ContainerSpec(
        name=name,
        owner=ctx.deps.owner,
        modules=modules,
        tailscale_auth_key=tailscale_auth_key,
    )
    result: ContainerResult = await create_container(spec)

    if result.success:
        return f"✅ Container `{result.name}` is running."

    return f"❌ Failed to create `{result.name}`: {result.error}"


@agent.tool
async def tool_destroy_container(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Destroy a container, its ephemeral state, and its ZFS dataset.

    Only destroys containers owned by the requesting user.
    The container's ZFS dataset (persistent workspace) is cleaned up
    after the container itself is torn down.

    Args:
        name: Name of the container to destroy.

    Returns:
        A plain-language summary of the result for the user.
    """
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    if denied := await _check_ownership(name, ctx.deps.owner):
        return denied

    result: ContainerResult = await destroy_container(name, owner=ctx.deps.owner)

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
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    if denied := await _check_ownership(name, ctx.deps.owner):
        return denied

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
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    if denied := await _check_ownership(name, ctx.deps.owner):
        return denied

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


@agent.tool
async def tool_storage_usage(ctx: RunContext[VoxnixDeps]) -> str:
    """Show storage usage and quota for the current user.

    Reports how much disk space the user's container workspaces are
    consuming and how much remains under their quota.

    Returns:
        A plain-language summary of the user's storage usage.
    """
    info = await get_user_storage_info(ctx.deps.owner)

    if not info.success:
        return f"❌ Could not query storage: {info.error}"

    return info.message


@agent.tool
async def tool_query_container(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Get detailed information about a specific container.

    Returns rich metadata including installed modules, Tailscale IP and
    hostname, storage usage, uptime, and state. Use this when the user
    asks things like "tell me about the dev container" or "what modules
    does dev have".

    Args:
        name: Name of the container to query.

    Returns:
        A plain-language summary of the container's metadata.
    """
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    info: ContainerInfo = await query_container(name, owner=ctx.deps.owner)

    if not info.exists:
        return f"❌ Container `{name}` does not exist."

    if info.error and "another user" in info.error:
        return f"❌ Container `{name}` belongs to another user."

    return info.format_summary()


@agent.tool
async def tool_check_host_health(ctx: RunContext[VoxnixDeps]) -> str:
    """Run a health check on the host infrastructure.

    Checks whether key components are available and functioning:
    extra-container, machinectl, container service template, and ZFS.
    Use this to diagnose why container operations might be failing.

    Returns:
        A checklist of host health indicators with pass/fail status.
    """
    result: DiagnosticResult = await check_host_health()
    return result.output


@agent.tool
async def tool_get_container_logs(
    ctx: RunContext[VoxnixDeps],
    name: str,
    lines: int = 50,
) -> str:
    """Retrieve recent log entries from a container.

    Reads the container's systemd journal. Useful for diagnosing why a
    container failed to start, why a service inside it is misbehaving,
    or checking recent activity.

    Args:
        name: Name of the container to read logs from.
        lines: Number of recent log lines to retrieve (default 50, max 200).

    Returns:
        Recent log lines from the container, or an error message.
    """
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    if denied := await _check_ownership(name, ctx.deps.owner):
        return denied

    result: DiagnosticResult = await get_container_logs(name, lines=lines)

    if result.success:
        return result.output

    return f"❌ {result.error}"


@agent.tool
async def tool_get_container_status(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Get detailed systemd and machine status for a container.

    Shows whether the container is running, its resource usage, and
    systemd unit state. Use this for deeper diagnostics than list_workloads.

    Args:
        name: Name of the container to check.

    Returns:
        Detailed status information for the container.
    """
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    if denied := await _check_ownership(name, ctx.deps.owner):
        return denied

    result: DiagnosticResult = await get_container_status(name)

    if result.success:
        return result.output

    return f"❌ {result.error}"


@agent.tool
async def tool_get_tailscale_status(
    ctx: RunContext[VoxnixDeps],
    name: str,
) -> str:
    """Check Tailscale connectivity status for a container.

    Queries Tailscale inside the container to report its IP, hostname,
    and whether it is connected to the tailnet. Use this to diagnose
    Tailscale enrollment or connectivity issues.

    Args:
        name: Name of the container to check Tailscale status for.

    Returns:
        Tailscale status output from inside the container.
    """
    if name_error := validate_container_name(name):
        return f"❌ {name_error}"

    if denied := await _check_ownership(name, ctx.deps.owner):
        return denied

    result: DiagnosticResult = await get_tailscale_status(name)

    if result.success:
        return result.output

    return f"❌ {result.error}"


@agent.tool
async def tool_check_service(
    ctx: RunContext[VoxnixDeps],
    service_name: str,
) -> str:
    """Check the status of a host-level systemd service.

    Only allows querying a fixed set of infrastructure services:
    voxnix-agent, tailscaled, nix-daemon, systemd-machined,
    systemd-networkd, sshd.

    Args:
        service_name: Name of the service to check (without .service suffix).

    Returns:
        Service status information or an error if the service is not allowed.
    """
    result: DiagnosticResult = await get_service_status(service_name)

    if result.success:
        return result.output

    return f"❌ {result.error}"


# ── Run helper ─────────────────────────────────────────────────────────────


async def run(
    message: str,
    owner: str,
    message_history: list[ModelMessage] | None = None,
) -> tuple[str, list[ModelMessage]]:
    """Run the agent for a single user message, optionally with conversation history.

    Resolves the model from settings at call time — env vars are only required
    when actually running the agent, not at import time.

    When ``message_history`` is provided, PydanticAI prepends it to the
    conversation so the agent has context from prior turns. The chat layer
    (ConversationStore) is responsible for managing, persisting, and expiring
    histories — this function just threads them through.

    Args:
        message: The user's natural language message.
        owner: The Telegram chat_id of the requesting user.
        message_history: Optional list of messages from previous turns in this
                         conversation. Pass the output of ``ConversationStore.get()``
                         here. If None or empty, the agent runs statelessly.

    Returns:
        A tuple of ``(output, new_messages)`` where:
        - ``output`` is the agent's response as a string.
        - ``new_messages`` is the list of new ModelMessage objects from this turn,
          suitable for passing to ``ConversationStore.append()``.
    """
    result = await agent.run(
        message,
        model=get_settings().llm_model_string,
        deps=VoxnixDeps(owner=owner),
        message_history=message_history or [],
    )
    return result.output, result.new_messages()
