"""The mail adapters' shared failure vocabulary, with the wire's own meaning.

Two kinds of failure, one per direction. A **read** that fails is a read: the
probe reports it and the cycle aborts, and nothing outside the deployment has
changed — so the two read legs (``gmail_client``, ``imap_client``) raise the
two errors below, naming a provider that could not be reached and a provider
that answered with a refusal. A **send** that fails is different in kind — the
question "did the message leave?" has to be answerable — so the two outbound
adapters raise ``MailDeliveryFailed``, carrying the domain's ``DeliveryFailure``
classification with it.

**Why the adapter classifies.** Only the wire knows what its own answer means.
An HTTP 5xx and an SMTP 5xx are the same three digits and opposite things: the
first is a server that may recover (retry), the second is a refused message
that will be refused again (do not retry). A shared numeric rule would be
wrong, so each adapter reads its own protocol and names the verdict with
``app.domain.mail_send.DeliveryFailure`` — the vocabulary the retry policy and
the outbox row's fate are defined in.

``reason`` is a short code, never a provider body: a refusal's text can echo
the request, and no error in this package is allowed to carry a credential, a
recipient's message, or a message body.
"""

from app.domain.mail_send import DeliveryFailure

# Refusals are recorded and rendered; keep them short and free of anything the
# provider echoed (the ``gmail_client`` precedent).
_MAX_REASON_LENGTH = 120


class MailProviderUnavailable(Exception):
    """The provider could not be reached or did not answer in time."""


class MailProviderRefused(Exception):
    """The provider answered with a refusal, or with no usable body.

    ``reason`` is the provider's error *code* — Gmail's ``invalid_grant``,
    ``UNAUTHENTICATED``; IMAP's bracketed ``AUTHENTICATIONFAILED``, or a
    bounded code of the leg's own where the server sent none — never its
    description or message, and never the request that produced it. ``status``
    is the HTTP status when the refusal came back as one (``None`` when the
    body could not be read at all, and on the IMAP leg, which has no status); a
    *send* reads it to decide whether another attempt could work, and a read
    has no use for it.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        self.reason = reason[:_MAX_REASON_LENGTH]
        self.status = status
        super().__init__(self.reason)


class MailDeliveryFailed(Exception):
    """A send attempt did not land, and the wire says which kind of failure it was.

    ``failure`` is the domain's classification (``transient`` — nothing left
    the deployment; ``unconfirmed`` — the outcome is unknown; ``permanent`` —
    the provider refused the message), and ``reason`` is a bounded code with no
    message content in it.
    """

    def __init__(self, *, failure: DeliveryFailure, reason: str) -> None:
        self.failure = failure
        self.reason = reason[:_MAX_REASON_LENGTH]
        super().__init__(f"{failure.value}: {self.reason}")
