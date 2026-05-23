"""Anthropic client wrapper with structured output.

Every agent LLM call goes through :meth:`LLMClient.structured_call`. The
wrapper:

- forces JSON via a single tool use schema derived from a Pydantic model
- retries once on parse failure, raises on the second
- counts input and output tokens per call
- records prompt version so tests can pin behavior

Phase 1 ships this as a working facade over the ``anthropic`` SDK. Real
caching (agent-response, embedding) lands in later phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from codereview.config import Settings, get_settings
from codereview.graph.embeddings import EmbeddingProvider
from codereview.logging import get_logger

T = TypeVar("T", bound=BaseModel)

_log = get_logger(__name__)


class LLMClientError(RuntimeError):
    """Raised when the LLM client exhausts retries."""


@dataclass(slots=True)
class TokenUsage:
    """Running totals across a single client instance (one per job)."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    by_prompt_version: dict[str, int] = field(default_factory=dict)

    def record(self, input_tokens: int, output_tokens: int, prompt_version: str) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1
        self.by_prompt_version[prompt_version] = self.by_prompt_version.get(prompt_version, 0) + 1


class LLMClient:
    """Thin wrapper around Anthropic messages with structured outputs.

    Parameters
    ----------
    settings:
        Optional override. Defaults to :func:`get_settings`.
    client:
        Optional pre-built ``AsyncAnthropic`` instance. Tests inject fakes here.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: AsyncAnthropic | None = None,
        *,
        prose_embedder: EmbeddingProvider | None = None,
        prose_embedding_model: str = "text-embedding-3-large",
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or self._build_client()
        self._prose_embedder = prose_embedder
        self._prose_embedding_model = prose_embedding_model
        self.usage = TokenUsage()

    def _build_client(self) -> AsyncAnthropic:
        kwargs: dict[str, Any] = {}
        if self._settings.anthropic_api_key:
            kwargs["api_key"] = self._settings.anthropic_api_key
        if self._settings.anthropic_base_url:
            kwargs["base_url"] = self._settings.anthropic_base_url
        return AsyncAnthropic(**kwargs)

    async def structured_call(
        self,
        *,
        prompt: str,
        output_schema: type[T],
        prompt_version: str,
        system: str | None = None,
        max_tokens: int = 4000,
    ) -> T:
        """Call Claude and parse the response as ``output_schema``.

        Uses a single tool as the coerced JSON surface. Retries once on
        parse failure. On the second failure raises :class:`LLMClientError`.
        """

        tool_name = "emit_" + output_schema.__name__.lower()
        schema = output_schema.model_json_schema()
        tools = [
            {
                "name": tool_name,
                "description": f"Return a {output_schema.__name__} object.",
                "input_schema": schema,
            }
        ]

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.messages.create(
                    model=self._settings.anthropic_model,
                    max_tokens=max_tokens,
                    system=system or "",
                    tools=tools,
                    tool_choice={"type": "tool", "name": tool_name},
                    messages=[{"role": "user", "content": prompt}],
                )
            except Exception as exc:  # pragma: no cover - network error path
                last_error = exc
                _log.warning(
                    "llm_call_error",
                    attempt=attempt,
                    prompt_version=prompt_version,
                    error=str(exc),
                )
                continue

            usage = getattr(response, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "input_tokens", 0))
                output_tokens = int(getattr(usage, "output_tokens", 0))
                self.usage.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    prompt_version=prompt_version,
                )
                # Emit to Prometheus too. Kept isolated so test runs
                # without prometheus_client installed still work on
                # downstream forks — an ImportError is swallowed.
                try:
                    from codereview.metrics import LLM_TOKENS

                    model = self._settings.anthropic_model
                    LLM_TOKENS.labels(model=model, type="input").inc(input_tokens)
                    LLM_TOKENS.labels(model=model, type="output").inc(output_tokens)
                except Exception:  # pragma: no cover - metrics are best-effort
                    pass

            tool_input = _extract_tool_input(response, tool_name)
            if tool_input is None:
                last_error = LLMClientError("model did not emit tool use")
                _log.warning("llm_no_tool_use", attempt=attempt, prompt_version=prompt_version)
                continue

            try:
                return output_schema.model_validate(tool_input)
            except ValidationError as exc:
                last_error = exc
                _log.warning(
                    "llm_parse_error",
                    attempt=attempt,
                    prompt_version=prompt_version,
                    errors=exc.errors(),
                )
                continue

        raise LLMClientError(
            f"structured_call failed after 2 attempts: {last_error!r}"
        ) from last_error

    async def embed_prose(self, text: str) -> list[float]:
        """Return a single prose embedding for ``text``.

        Thin wrapper around the configured prose provider. The Conflict
        Detector and the Context Packager both duck-type this method on
        :class:`AgentContext`; agents that run without an embedder (unit
        tests, minimal CI configurations) should fall back to a degraded
        path rather than calling this method.

        Raises
        ------
        LLMClientError
            If no prose embedder was configured at construction time, or
            the provider returned an empty response.
        """

        if self._prose_embedder is None:
            raise LLMClientError(
                "LLMClient has no prose embedder configured; "
                "pass prose_embedder=... when constructing the client."
            )
        vectors = await self._prose_embedder([text], self._prose_embedding_model)
        if not vectors:
            raise LLMClientError(
                "prose embedder returned no vectors for input"
            )
        return list(vectors[0])


def _extract_tool_input(response: Any, tool_name: str) -> dict[str, Any] | None:
    """Pull the first matching tool-use block from an Anthropic response."""

    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        name = getattr(block, "name", None)
        if block_type == "tool_use" and name == tool_name:
            value = getattr(block, "input", None)
            if isinstance(value, dict):
                return value
    return None
