"""The provider-registration onboarding guides (slice M8, ruling R6).

One static source, two guides — the exact steps a TCSP's administrator
performs at its own Microsoft or Google app registration, then where in this
app the resulting values get pasted and the probe run. The Phase-6
e-Registry-helper pattern: static operator-facing English text, no LLM call,
no per-tenant copy, and the guide is "done" when a valid credential exists
AND the probe passes (ruling R7) — never when the text was merely shown.
The route (``app.api.calendar_sync``) serves these verbatim; the wizard
(Task 4) renders one step per numbered action.

Both guides end at the same place — this app's calendar-sync settings, the
credential form, the probe — because the app's half of the walk is identical
whichever provider the TCSP registered. What differs is only the provider's
half: Entra application vs Google Cloud project.

Pure and framework-free per ADR-0006: stdlib only.
"""

from app.domain.calendar_push import CalendarProviderName

# The app's half, identical for both providers: where the values land. Kept
# as one block so the two guides cannot drift apart about this deployment.
_APP_STEPS = """\
## In this app

1. Open **Settings → Calendar sync → Connect provider** (Administrator only).
2. Paste the values above into the provider-credential form and save — the
   client secret and refresh token are stored in a mode-0600 file on this
   deployment (ADR-0014 custody), never in the database.
3. Press **Run probe**: it refreshes the token, lists the target calendar,
   writes a probe event and deletes it (ruling R5). Every leg must pass.
4. Only then does the push start: until the probe passes, this tenant stays
   on the `.ics` feed, which every tier keeps."""

_MICROSOFT_GUIDE = f"""\
# Register a Microsoft (Microsoft 365 / Entra) application

Performed once, by the TCSP's own Microsoft 365 administrator, against the
tenant's own Entra directory — the push authenticates as the TCSP's own app,
never a shared vendor registration.

1. Sign in to the **Microsoft Entra admin center** (entra.microsoft.com) with
   an account that can register applications in the TCSP's tenant.
2. **Identity → Applications → App registrations → New registration**: give
   it a name (for example "K2P calendar push"); supported account types:
   **Accounts in this organizational directory only** (single tenant).
3. Open the app's **Overview** page and copy two values: the **Application
   (client) ID** and the **Directory (tenant) ID**.
4. **Certificates & secrets → New client secret**: create a secret and copy
   its **Value** immediately — Entra shows it only once.
5. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions**: add **Calendars.ReadWrite** (the push writes and deletes
   events on the target calendar; a read-only scope cannot work), then press
   **Grant admin consent** for the tenant.
6. Obtain a **refresh token** for the mailbox/calendar account the deadlines
   should land in: one manual OAuth consent against the v2.0 authorize
   endpoint with scopes `offline_access` and `Calendars.ReadWrite`, and
   capture the refresh token from the code exchange. This is the one step a
   person performs in a browser; the app uses the refresh token from then on.
7. Note the target **calendar id**: in Outlook, right-click the calendar →
   Properties, or read it from Graph (`GET /me/calendars`).

You now have five values: Directory (tenant) ID, Application (client) ID,
client secret, refresh token, and calendar id.

{_APP_STEPS}"""

_GOOGLE_GUIDE = f"""\
# Register a Google (Google Workspace) application

Performed once, by the TCSP's own Google Cloud administrator, inside the
TCSP's own Google Cloud project — the push authenticates as the TCSP's own
app, never a shared vendor registration.

1. Open the **Google Cloud Console** (console.cloud.google.com) and create
   (or select) the project the registration belongs to.
2. **APIs & Services → Library**: enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: configure it for the project
   and add the scope **.../auth/calendar** (the push writes and deletes
   events on the target calendar; a read-only scope cannot work).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   create the client and copy the **client ID** and **client secret**.
5. Obtain a **refresh token** for the account whose calendar should hold the
   deadlines: one manual OAuth consent with scopes `offline_access` and
   `.../auth/calendar`, and capture the refresh token from the code exchange.
   This is the one step a person performs in a browser; the app uses the
   refresh token from then on.
6. Note the target **calendar ID**: `primary` for the account's own calendar,
   or the calendar's id from its settings page for any other calendar.

You now have four values: client ID, client secret, refresh token, and
calendar ID.

{_APP_STEPS}"""

GUIDES: dict[CalendarProviderName, str] = {
    CalendarProviderName.MICROSOFT: _MICROSOFT_GUIDE,
    CalendarProviderName.GOOGLE: _GOOGLE_GUIDE,
}


def guide_for(provider: CalendarProviderName) -> str:
    """The step-by-step registration guide for one provider (static text)."""
    return GUIDES[provider]
