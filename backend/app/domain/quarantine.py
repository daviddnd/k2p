"""The §2.5 quarantine vocabularies (scope §2.5; data-model §2.9, migrations
``0016`` and ``0017``).

Scope §2.5: "Replies from unregistered addresses are quarantined for staff
review." A quarantined message is one the ingestion cycle could not attribute
to exactly one company; the record staff work from is
``quarantined_messages`` (``app.adapters.persistence.quarantine``), and these
are its two closed sets.

The **reason** follows the sender, not the resolver's case. No company
registered for the address is ``unregistered_sender`` — the remedy is to
register the sender. An address that *is* registered but reaches more than
one company is ``multiple_companies`` — the remedy is to work out which
company the message is about (or to ask the client), whatever the resolver's
own case was: a message naming none of the sender's companies is still
``multiple_companies``, because "we do not know this address" would send
staff down a different remedy than the one they need. The resolver's case
(``no_match`` versus ``ambiguous``, ``app.domain.reply_resolver``) is
diagnostic, and the §2.7 assessment records it.

The **status** is what the review surface moves, and it has two terminal
states rather than one. ``pending`` until the message is attributed and
ingested — when the row points at the ``Communication`` it became and records
``resolved_at`` — or until staff close it as needing no further action
(``dismissed``, with ``dismissed_at``), which is the only way out for a
message the mailbox no longer has. The two are deliberately distinct: a
resolved row has a record behind it, a dismissed one is a decision not to
look further, and a reader has to be able to tell which.

Mirrors the DB CHECKs of ``quarantined_messages`` — the database refuses a
value outside these sets, and refuses a terminal row without the timestamp
that says when it closed. Pure and framework-free per ADR-0006.
"""

from enum import StrEnum


class QuarantineReason(StrEnum):
    """Why a message could not be attributed (scope §2.5)."""

    UNREGISTERED_SENDER = "unregistered_sender"
    MULTIPLE_COMPANIES = "multiple_companies"


class QuarantineStatus(StrEnum):
    """Where a quarantined message stands (scope §2.5's staff review)."""

    PENDING = "pending"
    RESOLVED = "resolved"
    # Closed by staff who decided no further action is needed — the message is
    # gone from the mailbox, or it never needed attributing. The row keeps the
    # reason it was quarantined with; it just stops being a to-do.
    DISMISSED = "dismissed"
