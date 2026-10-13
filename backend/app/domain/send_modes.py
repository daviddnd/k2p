"""The send modes: who releases a message, per destination (scope §2.15;
data-model §2.9 ``TenantConfig``; contract §5.1.8's M2 note and §5.3).

The founder's ruling of 2026-09-24: **every sending path is the
Administrator's, and the default differs by destination.** Two modes:

- ``review_required`` — the message is drafted and held until a staff member
  reviews and sends it;
- ``automatic`` — it leaves on the configured date with no further staff
  action.

And two destinations, with the defaults that make the ruling a rule rather
than a preference: documents bound for **the client** default to ``automatic``
(for efficiency — 「發給客戶的...default 是自動以提升效率」), and documents bound for
**a government filing destination** — e-Registry, or any other portal the
product may later write through — default to ``review_required``.

**The government destination's mode is pinned, not merely defaulted.** AGENTS.md
is unambiguous — "No unattended submission to e-Registry or any government
portals. Human review and explicit sign-off are required for any filing
action." — and that constraint exists to keep the customer and its end users
out of liability for a wrong filing. A configuration write that asked for
``automatic`` there is therefore refused (`validate_send_modes`), and relaxing
it needs that constraint amended **and a legal review**, not a tenant setting.
The client destination carries no such constraint: it admits both modes.

The vocabulary is closed in both directions, and validation is total: a stored
block that is missing a destination, names an unknown one, or names an unknown
mode fails as a clean list of violations — never as a ``KeyError`` — so the
narrow reads that act on the block fail closed with something an operator can
read (the milestone block's own discipline, §5.3).

Pure and framework-free per ADR-0006: the standard library and nothing else.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

_PREFIX = "send_modes."


class SendMode(StrEnum):
    """How a destination's messages leave the deployment (the ruled pair)."""

    REVIEW_REQUIRED = "review_required"
    AUTOMATIC = "automatic"


class SendDestination(StrEnum):
    """Where a message bound for it goes, as the configuration names it.

    ``government`` is deliberately the *class* rather than one portal:
    AGENTS.md's constraint covers "e-Registry **or other government portals**",
    and a setting for one named portal would leave the next one unprotected.
    The confirmation request is a client-destination artifact; no
    government-destination send path exists yet (Phase 6 owns export, §5.1.8),
    which is why the client destination is the only one this slice acts on.
    """

    CLIENT = "client"
    GOVERNMENT = "government"


# The destinations whose mode a mode *admitted value* is pinned by a
# non-negotiable constraint rather than by a default. One entry today, and the
# entry is the whole of ruling 2.
PINNED_MODES: Mapping[SendDestination, tuple[SendMode, ...]] = {
    SendDestination.GOVERNMENT: (SendMode.REVIEW_REQUIRED,),
}


class SendModesValidationError(ValueError):
    """One or more send-mode rules failed.

    Carries every violation found (not just the first) so a PUT can report
    them all; each message names the block and the field it is about
    (``send_modes.<destination>: …``), the milestone block's own shape.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class SendModes:
    """The tenant's send modes, per destination.

    A value, built only through ``validate_send_modes`` (the write path and
    every read of a stored block) or ``default_send_modes`` — the fields are
    the validated ones and ``as_dict`` is the stored and returned shape.
    """

    client: SendMode
    government: SendMode

    def as_dict(self) -> dict[str, str]:
        """The block as the JSON column stores it and the API returns it.

        A fixed key set; ``validate_send_modes`` accepts its own output
        unchanged (a test pins the round trip).
        """
        return {
            SendDestination.CLIENT.value: self.client.value,
            SendDestination.GOVERNMENT.value: self.government.value,
        }

    def mode_for(self, destination: SendDestination | str) -> SendMode:
        """The mode this destination sends under, or a refusal.

        ``ValueError`` for a destination the vocabulary does not carry: a
        caller asking what mode an unknown destination sends under is a code
        bug, and answering ``review_required`` — the safe value — would hide
        it behind a plausible number.
        """
        try:
            known = SendDestination(destination)
        except ValueError as exc:
            raise ValueError(
                f"{destination!r} is not a send destination "
                f"(one of: {', '.join(sorted(item.value for item in SendDestination))})"
            ) from exc
        return self.client if known is SendDestination.CLIENT else self.government


def default_send_modes() -> SendModes:
    """The shipped defaults (founder-ruled 2026-09-24), already validated."""
    return DEFAULT_SEND_MODES


# The shipped defaults (founder-ruled 2026-09-24). They are the TCSP's to
# change — the panel exists for exactly that — but these are what a tenant
# without a stored block sends with: client mail goes out for efficiency, and
# anything bound for a government filing destination waits for a person.
DEFAULT_SEND_MODES = SendModes(client=SendMode.AUTOMATIC, government=SendMode.REVIEW_REQUIRED)


def _pinned(destination: SendDestination) -> tuple[SendMode, ...] | None:
    """The modes a constrained destination admits, or ``None`` when it is free."""
    return PINNED_MODES.get(destination)


def validate_send_modes(value: object) -> SendModes:
    """The block as a value, or ``SendModesValidationError`` listing every violation.

    Total: a stored row that is missing a destination, names an unknown one,
    or carries an unknown mode answers with a violation naming the field
    rather than raising ``KeyError`` — the read paths that derive from the
    block must fail closed with something an operator can act on.
    """
    errors: list[str] = []
    if not isinstance(value, Mapping):
        raise SendModesValidationError(["send_modes: must be an object"])
    modes: dict[SendDestination, SendMode] = {}
    for destination in SendDestination:
        raw = value.get(destination.value)
        if raw is None:
            errors.append(f"{_PREFIX}{destination.value}: is required")
            continue
        try:
            mode = SendMode(raw)
        except ValueError:
            errors.append(
                f"{_PREFIX}{destination.value}: unknown mode {raw!r} "
                f"(one of: {', '.join(sorted(item.value for item in SendMode))})"
            )
            continue
        admitted = _pinned(destination)
        if admitted is not None and mode not in admitted:
            errors.append(
                f"{_PREFIX}{destination.value}: {mode.value!r} is not admitted — documents "
                "bound for a government filing destination are always held for review "
                "(AGENTS.md's no-unattended-submission constraint; relaxing it needs that "
                "constraint amended and a legal review)"
            )
            continue
        modes[destination] = mode
    # The unknown keys last: a block's own fields are read first, so an
    # operator sees what is wrong with what they wrote before the surprises.
    for key in value:
        if not isinstance(key, str) or key not in {item.value for item in SendDestination}:
            errors.append(
                f"{_PREFIX}{key}: unknown destination "
                f"(one of: {', '.join(sorted(item.value for item in SendDestination))})"
            )
    if errors:
        raise SendModesValidationError(errors)
    return SendModes(
        client=modes[SendDestination.CLIENT], government=modes[SendDestination.GOVERNMENT]
    )


__all__ = [
    "DEFAULT_SEND_MODES",
    "PINNED_MODES",
    "SendDestination",
    "SendMode",
    "SendModes",
    "SendModesValidationError",
    "default_send_modes",
    "validate_send_modes",
]
