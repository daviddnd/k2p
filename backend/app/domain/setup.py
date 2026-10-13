"""The bootstrap guard decision (opt-in).

``POST /api/v1/tenants`` creates the deployment's tenant and first
Administrator and is unauthenticated by necessity — until it has run,
whoever reaches it first becomes the Administrator. Arming the guard (a
non-empty ``AICS_SETUP_TOKEN``) closes that first-run race on an exposed
deployment; disarmed, the deployment behaves exactly as without the guard.
One decision in one place; the seam that calls it is
``app.api.deps.require_setup_token``.
"""

from hmac import compare_digest


def setup_token_allows(*, configured: str, presented: str | None) -> bool:
    """Whether the bootstrap may proceed.

    Disarmed (no token configured) → allow: the pre-guard behaviour, so
    local trials and every existing flow are untouched. Armed → allow only
    on a constant-time match of the presented token.

    Compared as bytes: ``compare_digest`` refuses two strings holding
    non-ASCII characters, and the presented value is whatever a caller put
    in a header — a comparison that raised on it would turn a hostile
    header into a 500 instead of a refusal (the
    ``app.domain.mail.state_matches`` precedent).
    """
    if not configured:
        return True
    if presented is None:
        return False
    return compare_digest(configured.encode("utf-8"), presented.encode("utf-8"))
