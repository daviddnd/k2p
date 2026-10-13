"""The LLM transport port (M6.4): the seam between the gateway and a provider.

Everything the gateway knows about *where* a composed snippet goes is this
Protocol — the control-plane channel (Standard tier, ADR-0014's vendor
custody) and the local OpenAI-compatible provider (Enterprise Offline) both
satisfy it structurally, and the application layer's ``llm_client()`` seam is
what tests replace. Neither implementation is imported here: the channel
client deliberately imports nothing from ``app.*`` so the cross-deployable
integration driver can load it standalone, and duck typing is what keeps
that possible.

``TransportResult`` carries the decoded response **envelope** — the exact
shape the domain's ``response_envelope_violations`` validates — rather than
extracted fields, because validation is a domain rule (ADR-0006): a
transport delivers bytes or a typed error; it never decides what may be
trusted.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class TransportResult:
    """The decoded response envelope of one completed call, unvalidated.

    ``envelope`` is the JSON object the provider answered with — for the
    control-plane channel, the response envelope of the inference protocol;
    for the local provider, the same envelope built from the endpoint's
    OpenAI-compatible answer. The gateway validates it before a single
    field is trusted (fail closed, ``app.domain.ai_gateway``).
    """

    envelope: Mapping[str, Any]


class LlmTransport(Protocol):
    """One inference call over one custody option.

    ``task_type`` is the wire vocabulary value (``reply_parsing``, …),
    ``language`` the request's language code, and ``snippet`` the composed,
    scrubbed, tokenized outbound text — the only client-derived bytes any
    implementation ever receives. Implementations raise their typed
    transport errors (``InferenceChannelUnavailable`` /
    ``InferenceChannelRefused`` for the channel); the gateway books the
    audit entry and raises ``InferenceUnavailable``.
    """

    async def complete(
        self, *, task_type: str, language: str, snippet: str
    ) -> TransportResult:
        """Send one snippet; return the decoded envelope or raise typed."""
        ...
