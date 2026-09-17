"""Shared fixtures. Tests run against a disposable copy of server/.

Nothing here touches the real server/data/ tree, and no test makes a network
call: the LLM and Gmail layers are always faked.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from harness import FakeGmail, FakeLLM, bootstrap  # noqa: E402


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run coroutine tests without requiring pytest-asyncio.

    Each test gets a fresh event loop, which also keeps loop-latency assertions
    independent between tests.
    """
    test_fn = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_fn):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
        if name in pyfuncitem.funcargs
    }
    asyncio.run(test_fn(**kwargs))
    return True


@pytest.fixture(scope="session")
def sandbox():
    """Working tree by default; OPENPOKE_TEST_REF pins a git ref instead.

    Running the suite against main is the negative control: these tests must
    fail there, or they are not testing the fix.
    """
    import os

    sb = bootstrap(ref=os.environ.get("OPENPOKE_TEST_REF") or None)
    yield sb
    sb.cleanup()


@pytest.fixture
def fake_gmail(sandbox):
    """Install a fake Gmail layer; returns a factory so tests pick the latency."""

    def _install(latency_seconds: float = 0.2, mode: str = "blocking") -> FakeGmail:
        gmail = FakeGmail(latency_seconds, mode=mode)
        gmail.install(sandbox)
        return gmail

    return _install


@pytest.fixture
def fake_llm(sandbox):
    """Install a fake LLM; returns a factory taking a responder callable."""

    def _install(responder) -> FakeLLM:
        llm = FakeLLM(responder)
        llm.install(sandbox)
        return llm

    return _install
