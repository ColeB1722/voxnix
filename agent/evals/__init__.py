"""Voxnix agent LLM evals — deepeval-based quality evaluation suite.

This package tests the agent's tool selection, response quality, and
conversation context retention against real LLMs with mocked infrastructure.

Evals are excluded from default pytest runs via the ``evals`` marker
(see pyproject.toml ``addopts``). Run them explicitly with::

    just eval
    # or
    uv run pytest -m evals -o "addopts=" agent/evals/ -x --tb=short

Model under test:
    ``LLM_PROVIDER`` and ``LLM_MODEL`` control which single model is
    evaluated. They default to ``openrouter`` / ``anthropic/claude-haiku-4.5``
    so that CI only needs one secret (``OPENROUTER_API_KEY``) to
    authenticate both the test model and the judge. Override these in
    ``.env.eval`` or CI env vars to test a different model.

Judge model (gpt-4.1):
    deepeval's LLM-as-judge uses the OpenAI client internally. The eval
    conftest auto-detects judge credentials in priority order:

    1. ``OPENAI_API_KEY`` — used directly (native OpenAI path).
    2. ``OPENROUTER_API_KEY`` — aliased to ``OPENAI_API_KEY`` with
       ``OPENAI_BASE_URL`` pointed at OpenRouter's OpenAI-compatible
       endpoint.

    With just ``OPENROUTER_API_KEY`` set, both the test model and the
    judge route through OpenRouter — zero additional configuration.

Local setup:
    Copy ``.env.eval.example`` to ``.env.eval``, add your API key,
    and run ``direnv allow``. The ``.envrc`` sources ``.env.eval``
    automatically when present.

CI:
    The eval workflow (``.github/workflows/eval.yml``) runs post-merge
    on main. Set ``OPENROUTER_API_KEY`` as a repository secret and
    everything works out of the box. To use OpenAI directly instead,
    set ``OPENAI_API_KEY`` plus override ``LLM_PROVIDER`` / ``LLM_MODEL``
    in the workflow env.

See docs/handoff.md § Agent evals (#51) for architecture decisions.
"""
