"""Test-wide configuration.

THE TEST SUITE MUST NEVER LOAD A MODEL. `create_app`'s lifespan picks the best engines it can
find, and on a developer machine that means Qwen2.5-1.5B onto the GPU and Kokoro into an ONNX
session — about six seconds of loading, several gigabytes of memory, and a warm-up generation,
paid by every test that constructs a `TestClient`. The first run of this suite after the live
call landed took over two minutes for that reason alone.

Worse than slow, it is wrong: a test that exercises the real model is testing the model. What
the WebSocket tests are for is the protocol — that a `say` produces tokens, then clips, then a
`done` with a budget — and the scripted engines produce exactly that shape, deterministically,
in milliseconds.

Set at import rather than in a fixture because the environment must be in place before any test
module imports `rainmaker.app`, and imports happen at collection.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("RAINMAKER_BRAIN", "scripted")
os.environ.setdefault("RAINMAKER_VOICE", "browser")


# ── how many tests are in this run ───────────────────────────────────────────
# `test_readme.py` compares the README's published count against reality, and reality is only
# knowable once collection has finished. Stashing it here is the smallest way to get it: the
# alternative is a test that shells out to `pytest --collect-only`, which re-imports every test
# module inside a test run.
_COLLECTED = 0


def pytest_collection_modifyitems(session, config, items) -> None:  # noqa: ARG001
    global _COLLECTED
    _COLLECTED = len(items)


# NOT named `pytest_collected`: pluggy treats every `pytest_*` name in a conftest as a hook
# implementation and refuses to start when it does not recognise one.
@pytest.fixture
def collected_test_count() -> int:
    return _COLLECTED
