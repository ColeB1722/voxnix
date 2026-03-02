"""Voxnix agent LLM evals — deepeval-based quality evaluation suite.

This package tests the agent's tool selection, response quality, and
conversation context retention against real LLMs with mocked infrastructure.

Evals are excluded from default pytest runs via the ``evals`` marker
(see pyproject.toml ``addopts``). Run them explicitly with::

    just eval
    # or
    uv run pytest -m evals agent/evals/ -x --tb=short

All evals require API keys configured in ``.env.eval`` — see
``.env.eval.example`` for the template.

See docs/handoff.md § Agent evals (#51) for architecture decisions.
"""
