"""Voxnix agent LLM evals — deepeval-based quality evaluation suite.

This package tests the agent's tool selection, response quality, and
conversation context retention against real LLMs with mocked infrastructure.

Evals are excluded from default pytest runs via the ``evals`` marker
(see pyproject.toml ``addopts``). Run them explicitly with::

    just eval
    # or
    uv run pytest -m evals -o "addopts=" agent/evals/ -x --tb=short

Provider auto-detection:
    The eval conftest auto-detects which LLM provider to use based on
    which API keys are present in the environment. Priority order:

    1. ``OPENAI_API_KEY`` — preferred; deepeval's judge model uses
       OpenAI natively, so both agent-under-test and judge work with
       zero aliasing.
    2. ``OPENROUTER_API_KEY`` — fallback; aliased to ``OPENAI_API_KEY``
       with ``OPENAI_BASE_URL`` pointed at OpenRouter's OpenAI-compatible
       endpoint so the judge routes through OpenRouter too.
    3. Other provider keys (``ANTHROPIC_API_KEY``, ``GOOGLE_API_KEY``, etc.)

    You can also set ``LLM_PROVIDER`` / ``LLM_MODEL`` explicitly in
    ``.env.eval`` to override auto-detection.

Local setup:
    Copy ``.env.eval.example`` to ``.env.eval``, add your API key,
    and run ``direnv allow``. The ``.envrc`` sources ``.env.eval``
    automatically when present.

CI:
    The eval workflow (``.github/workflows/eval.yml``) runs post-merge
    on main. It accepts either ``OPENAI_API_KEY`` or ``OPENROUTER_API_KEY``
    as a repository secret — at least one must be configured.

See docs/handoff.md § Agent evals (#51) for architecture decisions.
"""
