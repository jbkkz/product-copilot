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

    def __init__(self, client=None):
        self.client = client or new_client()

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
                               reuse_system=reuse_system)
        return run(self.client, [{"role": "user", "content": request}], only=only,
                   reuse_system=reuse_system)

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs):
        """`**kwargs` carries the few per-artifact options a generator takes (release notes accept a
        `version` to stamp); an option a generator does not know is a TypeError, not a silent no-op."""
        try:
            fn = _GENERATORS[artifact_type]
        except KeyError as e:
            raise EngineError(f"unknown artifact type for the Anthropic provider: {artifact_type!r}") from e
        return fn(self.client, model, only=only, **kwargs)

    def model_name(self) -> str:
        return current_model_name()

    def provenance(self, op: str, *, only: list[str] | None = None) -> dict:
        """Who reasoned, with what, against which prompt — the fields a revision records. The service
        asks the provider for this instead of assembling it, so a second provider cannot silently
        stamp revisions as `anthropic`, and the prompt identity comes from the layer that owns it."""
        return {"provider": self.name, "model_name": self.model_name(),
                "prompt_version": prompt_version(op, only)}
