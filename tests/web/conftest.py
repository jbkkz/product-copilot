"""Shared fixtures for the Requivo Web tests — no network, no real provider.

Each test gets an isolated workspace (its own `.requivo/`), a `TestClient`, and a helper to swap in a
fake provider so discovery/generation run offline. The fake returns canned JSON replies in call order,
exactly like the CLI test's `FakeClient`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.services.discovery import DiscoveryService
from requivo.web.app import create_app
from requivo.web.dependencies import get_discovery
from requivo.web.security import CSRF_HEADER, csrf_token


def full_model(**overrides) -> dict:
    """A complete required-slot model (empty/low by default), with per-slot overrides — what a real
    discovery turn emits, so it satisfies the completeness invariant `run()` enforces."""
    _, required = schema_slot_ids()
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low"}
             for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def engine_reply(*, converged: bool = False, **slot_overrides) -> str:
    questions = [] if converged else [
        {"q": "How are exceptions handled?", "slot": "business_rules", "why": "uncertainty × impact"}]
    return json.dumps({
        "model": full_model(**slot_overrides),
        "questions": questions,
        "summary": {"objective": "A leave approval system"},
    })


BRIEF_REPLY = json.dumps({"complexity": "medium", "problem": "P", "solution": "S",
                          "risks": ["a race on approval"], "next_steps": ["confirm exceptions"]})
PRD_REPLY = json.dumps({"title": "Leave approval — PRD", "summary": "A leave system"})
CRITERIA_REPLY = json.dumps({"title": "Leave approval — acceptance criteria"})


class FakeClient:
    """Returns canned JSON replies in order; records each create() call so a test can assert a key was
    never sent to the provider."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls = []
        self.messages = self  # client.messages.create → self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0))


class _FakeResponse:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.stop_reason = "end_turn"
        self.usage = None


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Isolate every test's sessions under a fresh temp workspace; no API key by default."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def raw_client(app):
    """A client that sends nothing a browser wouldn't: loopback host, no request token. Everything a
    real page carries has to be added explicitly — which is what makes the guard tests meaningful."""
    return TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


@pytest.fixture
def client(raw_client):
    """The everyday client: same as `raw_client` plus the cross-site request token every rendered form
    carries as a hidden field. Sent as a header here so tests can keep posting plain `data=` dicts."""
    raw_client.headers[CSRF_HEADER] = csrf_token()
    return raw_client


@pytest.fixture
def with_provider(app):
    """Swap in a DiscoveryService backed by a FakeClient (shared across requests, so replies pop in
    order over a multi-step flow). Returns a function taking the reply sequence."""
    def _install(*replies):
        fake = FakeClient(*replies)
        disco = DiscoveryService(client=fake)
        app.dependency_overrides[get_discovery] = lambda: disco
        return fake
    yield _install
    app.dependency_overrides.clear()
