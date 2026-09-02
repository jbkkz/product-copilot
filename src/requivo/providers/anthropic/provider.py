"""`AnthropicProvider` — the `ReasoningProvider` face over the functions in this package."""

from __future__ import annotations

from requivo.core.contracts import EngineOutput
from requivo.providers.anthropic.client import current_model_name, new_client
from requivo.providers.anthropic.generators import _GENERATORS, answer_turn, prompt_version, run
from requivo.providers.errors import EngineError


class AnthropicProvider:
    """`ReasoningProvider` over the Anthropic SDK. Holds a client so the free functions in
    `generators.py` (which tests still exercise directly with a fake client) stay the single
    implementation — the object is a thin, uniform face over them for callers that want the provider
    seam."""

    name = "anthropic"

    def __init__(self, client=None, model: str | None = None):
        """`model` is an optional fixed model id for this instance (#434). `None` (the default)
        preserves the pre-existing behaviour byte-for-byte: every call resolves its model through
        `current_model_name()`'s `REQUIVO_MODEL`/`MODEL` env chain, per call, as it always did.

        Set explicitly, it is threaded into every completion call, `model_name()` and `provenance()`
        instead — and it wins outright, with **no env read at all** on that path: two providers
        constructed with two different ids in one process each call, price and record independently,
        which an ambient env var can never give you (one process, one mutable variable). Stored
        privately (`self._model`, not `self.model`) because `generate()`'s own `model` parameter
        already names the *requirements* model (an `EngineOutput`) — the two are unrelated concepts
        that happen to share a word, and a public `self.model` sitting next to that parameter would
        invite exactly that confusion."""
        self.client = client or new_client()
        self._model = model

    def analyze(self, request: str, *, current_model: EngineOutput | None = None,
                answers: str | None = None, only: list[str] | None = None,
                reuse_system: bool = False) -> EngineOutput:
        """One reasoning turn, on either branch — **one call per operation by default**, which is why
        the default is `reuse_system=False` (#58).

        This is where the caching question is actually decidable, and the answer is per *operation*,
        not per function. `DiscoveryService.start`, `run_discovery` and `answer` each reach this
        once, so the system block was being written to cache at 1.25x and never read back. The free
        functions in `generators.py` cannot know that, which is why `run()` keeps its own `True`
        default for the callers that genuinely loop it — the golden harness sends the identical
        prompt K times.

        The one looping caller *inside* the seam is `DiscoveryService.draft_turn`, the CLI's
        interactive `discover` loop: up to 8 turns off one system prompt, so it passes
        `reuse_system=True` and the breakpoint earns its write there. That loop used to call `run()`
        directly from `cli.py` and keep the breakpoint by accident of the default; since #77 it says
        so through the seam instead, which is why the parameter is on the protocol rather than
        hard-coded here."""
        if current_model is not None and answers is not None:
            return answer_turn(self.client, current_model, request, answers, only=only,
                               reuse_system=reuse_system, model=self._model)
        return run(self.client, [{"role": "user", "content": request}], only=only,
                   reuse_system=reuse_system, model=self._model)

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs):
        """`**kwargs` carries the few per-artifact options a generator takes (release notes accept a
        `version` to stamp); an option a generator does not know is a TypeError, not a silent no-op.

        `model` here is the *requirements* model (this method's own parameter, inherited from the
        protocol) — not to be confused with `self._model`, the constructed LLM id (#434) forwarded
        below as the generator functions' own `model=` keyword. The two never collide in the call:
        `model` is bound positionally (to the callee's `out`/`model` requirements-model parameter),
        the keyword `model=self._model` is bound by name to the callee's own `model:str|None`."""
        try:
            fn = _GENERATORS[artifact_type]
        except KeyError as e:
            raise EngineError(f"unknown artifact type for the Anthropic provider: {artifact_type!r}") from e
        return fn(self.client, model, only=only, model=self._model, **kwargs)

    def model_name(self) -> str:
        return self._model if self._model is not None else current_model_name()

    def provenance(self, op: str, *, only: list[str] | None = None) -> dict:
        """Who reasoned, with what, against which prompt — the fields a revision records. The service
        asks the provider for this instead of assembling it, so a second provider cannot silently
        stamp revisions as `anthropic`, and the prompt identity comes from the layer that owns it."""
        return {"provider": self.name, "model_name": self.model_name(),
                "prompt_version": prompt_version(op, only)}
