"""Runtime configuration via environment variables (prefix ``AICS_``)."""

from datetime import datetime
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The build's version, reported to the control plane in ``ping``'s telemetry
# and on the API. Kept here so the two cannot drift apart.
APP_VERSION = "0.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AICS_", env_file=".env", extra="ignore")

    # Defaults target the local docker-compose services in deploy/.
    # The secret default is a development placeholder only — real deployments
    # must override it via the environment.
    database_url: str = "postgresql+psycopg://aics:aics-dev-only@localhost:5432/aics"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "aics-documents"
    s3_access_key: str = "aics"
    s3_secret_key: str = "aics-dev-only"

    # Entitlement provisioning (ADR-0012/0016; entitlement design D2/D5):
    # deployment configuration is the entitlement source until the deployment
    # activates, and the only source after it deactivates. An unprovisioned
    # deployment runs Free — no licensed automation. Unknown tier values fail
    # closed to Free in the domain rule, not here.
    license_tier: str = "free"
    license_expires_at: datetime | None = None

    # Control-plane activation and the entitlement lease (ADR-0016; CP1b).
    # Empty means the vendor service is not configured for this deployment:
    # activation refuses (503) and nothing else changes — a deployment that
    # never activates keeps reading the configuration above. The default is
    # deliberately empty rather than a guess: there is no "the vendor" URL,
    # only the one this customer was told.
    cp_base_url: str = ""
    # Where the browser that just left the vendor site comes back to. Must be
    # reachable by that browser (loopback by default, per ADR-0016).
    cp_activation_redirect_uri: str = (
        "http://localhost:8000/api/v1/entitlement/activation/callback"
    )
    # The deployment's credential file — the activation token, its identity,
    # and the pending attempt. A credential under ADR-0014 custody: 0600, in a
    # gitignored path, never in the database and never in the image.
    cp_activation_token_file: Path = Path("var/control-plane-activation-token")
    # Refresh policy, job-free (``app/workers`` is still deliberately empty):
    # the read path attempts a refresh once the cached lease is older than the
    # interval, at most once per backoff window, with a short timeout. Every
    # failure is non-fatal — the cached lease keeps ageing through §4.3.
    cp_refresh_interval_hours: int = 24
    cp_refresh_backoff_minutes: int = 30
    cp_request_timeout_seconds: float = 5.0

    # The bootstrap guard (opt-in). ``POST /api/v1/tenants`` is open by
    # necessity and one-shot, but while the deployment is uninitialised,
    # whoever reaches it first becomes the Administrator. Set this and the
    # bootstrap requires the caller to present it (``X-Setup-Token``); leave
    # it empty and the deployment behaves exactly as without the guard —
    # the right state for local trials only.
    setup_token: str = ""

    # Licensed-pack signature verification (CP2 signing seam; ADR-0012). The
    # vendor signs each release's SHA-256 digest with Ed25519 at publish; this
    # deployment verifies the signature before installing anything that
    # carries one. The vendor public key is pinned here — a PEM file, one per
    # deployment, distributed with the licence. Empty means no key is pinned:
    # signed releases are then refused ``signature_unverifiable`` (fail
    # closed), and unsigned ones still install on the digest alone.
    pack_vendor_public_key_file: str = ""

    # The per-tenant encryption key (scope §8; Phase 7 slice D1). The one
    # key at-rest secrets — TOTP seeds today (E1-E3) — are sealed with. A
    # credential under ADR-0014 custody, exactly like the activation token:
    # 0600, in a gitignored path, never in the database, never a log line,
    # never an audit entry — the audit chain records only the key's id.
    tenant_key_file: Path = Path("var/tenant-key")

    # The command worker (ADR-0012 open action 3, contract §5.2). The
    # deployment polls the control plane for command envelopes, executes
    # them, and reports the outcomes; the queue and the audit trail are local
    # either way. **Off by default**: a background loop is a deployment-level
    # choice, and nothing that runs on its own should have to be turned off
    # first in a test or a pre-pilot install. The synchronous trigger
    # (``POST /api/v1/commands/run``) works whether or not the loop runs.
    command_worker_enabled: bool = False
    command_worker_interval_seconds: float = 30.0

    # The dedicated client mailbox (ADR-0005; Phase 3). One mailbox, the
    # TCSP's own, connected by an Administrator through Google's consent
    # screen; the tokens then live in a 0600 file (ADR-0014 custody), never
    # in the database. Empty client credentials refuse the connect flow (503)
    # and change nothing else: without them the deployment can still send over
    # SMTP (the fallback below) and still needs ``mail_address`` to draft.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Where Google sends the browser back to. Must be reachable by that
    # browser: loopback by default, exactly like the activation callback
    # (ADR-0016).
    google_redirect_uri: str = "http://localhost:8000/api/v1/mail/oauth/callback"
    # The mailbox this deployment expects. The connect flow refuses an
    # account that does not match, because consent is granted by whoever is
    # signed in to the browser — connecting the wrong mailbox would send
    # clients' mail from an account nobody chose.
    mail_address: str = ""
    # The mailbox credential file — access token, refresh token, address, and
    # any pending attempt. A credential under ADR-0014 custody: 0600, in a
    # gitignored path, never in the database and never in the image.
    mail_credential_file: Path = Path("var/mail-credential")
    mail_request_timeout_seconds: float = 10.0

    # Inbound mail ingestion (ADR-0005; Phase 3 ingestion slice). The cycle
    # reads the connected mailbox from its stored cursor and turns each new
    # message into a Communication row and stored documents; the trigger is
    # gated per company (``EMAIL_INGESTION``), and reads of what has already
    # been ingested stay ungated. **Off by default**, like the command worker:
    # a background loop is a deployment-level choice, and the synchronous
    # trigger (``POST /api/v1/mail/ingest``) works whether or not it runs.
    mail_ingest_enabled: bool = False
    mail_ingest_interval_seconds: float = 60.0
    # The per-cycle bound. Messages are examined oldest first, and the cursor
    # only advances over what a cycle finished, so a burst larger than this is
    # picked up by the next cycle rather than skipped.
    mail_ingest_max_messages: int = 50

    # The attribution asks' anti-abuse ceiling (ADR-0019 §1, ruling R4): how
    # many registration/confirmation asks one tenant may send in a rolling
    # hour before further asks are suppressed. A tenant that trips it keeps
    # its cases — they stay open with no ask, visible to staff — and the
    # suppression is silent by design (a cycle must not fail because a bot is
    # looping). ``0`` means the deployment never asks automatically; the asks'
    # other guards (the automatic-submission headers) are deliberately not a
    # setting.
    attribution_ask_hourly_cap: int = 30
    # The clarification loop's convenience asks (the which-company and
    # first-contact asks, rulings D3/D7) carry their own ceiling so a burst
    # of cold or ambiguous mail cannot starve ①'s authority asks above: the
    # two pools are counted and capped separately. The same shape applies —
    # a tenant that trips the cap keeps its cases, open and staff-visible,
    # and the suppression is silent by design. ``0`` means the deployment
    # never asks automatically; company-less asks consult only this cap and
    # ①'s anti-abuse headers, never the per-company automation gate (D3 —
    # gating the replacement on paid automation would regress Free tenants).
    clarification_ask_hourly_cap: int = 20

    # The multi-company split's bounds (workstream ③, ADR-0019 §3, rulings
    # D5): a parent whose involved set exceeds ``split_max_companies`` parks
    # for staff rather than splitting — the provider divides content, and a
    # very wide division is staff work, not an automation. The daily cap
    # bounds how many automatic splits one sender may drive per day — the
    # cold-mail abuse surface the split opens — and ``0`` means the
    # deployment never splits automatically (every parent parks). A parent
    # the bounds park stays staff-visible on the split surface; nothing is
    # guessed.
    split_max_companies: int = 10
    split_daily_cap_per_sender: int = 5

    # Outbound mail: the send outbox's worker (ADR-0011 §1; Phase 3 sending
    # slice). The cycle delivers the pending sends — the release gate first,
    # then the provider — and the same cycle runs from
    # ``POST /api/v1/mail/send``. **Off by default**, like the other two loops,
    # and **a deployment that wants ``automatic`` sending has to enable it**:
    # the send route releases and delivers one request on demand, while an
    # automatic draft waits for a cycle. Each message is attempted
    # ``mail_send_interval_seconds`` apart at the loop's own backoff, so the
    # setting is both the cadence and the retry pause.
    mail_send_enabled: bool = False
    mail_send_interval_seconds: float = 60.0
    mail_send_max_messages: int = 50

    # The reminder sweep's loop (contract §5.1.12; slice R). The cycle runs
    # the same sweep ``POST /api/v1/mail/reminders`` runs synchronously —
    # every due, unanswered ``sent`` request gets a stored letter and an
    # outbox row with ``not_before``. **Off by default**, like the other
    # loops: a deployment that wants reminders with no staff action enables
    # it, and one that does not presses the route — or acts on the cadence
    # due dates by hand, ADR-0005's manual path, which every tier keeps.
    mail_reminders_enabled: bool = False
    mail_reminders_interval_seconds: float = 60.0

    # The reply parser's loop (slice M5b; contract §5.1.13). The cycle is the
    # same ``run_cycle`` ``POST /api/v1/mail/parse-replies`` runs synchronously:
    # every ingested inbound reply with no parse row gets the §5.1 gate per
    # company and then the single gateway call. **Off by default**, like the
    # other loops: parsing client replies with no staff action is a deployment
    # choice, and one that leaves the loop off presses the route — or never
    # parses, which the Free tier's posture allows.
    mail_parse_replies_enabled: bool = False
    mail_parse_replies_interval_seconds: float = 60.0

    # The confirmation-discovery loop (the discovery slice). The cycle runs the
    # same sweep ``POST /api/v1/mail/confirmation-discovery`` runs synchronously:
    # every company whose data-confirmation ask date has arrived with no
    # confirmation request yet gets the generation trigger's decision — a
    # drafted (and, under ``automatic``, queued) request, or the trigger's own
    # refusal, named in the cycle's report. **Off by default**, like the other
    # loops: asking clients with no staff action is a deployment choice, and
    # one that leaves the loop off presses the route — or drafts per company by
    # hand, ADR-0005's manual path, which every tier keeps.
    mail_confirmation_discovery_enabled: bool = False
    mail_confirmation_discovery_interval_seconds: float = 60.0

    # The calendar-push reconciliation loop (slice M8; ADR-0013 item 3's
    # 2026-10-03 amendment; ruling R4). The cycle reconciles the registered
    # tenant's provider calendar with the same projection the feed serves —
    # writes that move a deadline (a change event's apply, an
    # anniversary/type correction) wake it early, and the interval is the
    # backstop. **Off by default**, like the other loops: a deployment that
    # has connected no provider has nothing to reconcile, the `.ics` feed
    # needs no loop at all, and a tenant whose credential's probe never
    # passed is swept as an honest no-op that leaves it on the feed.
    calendar_push_enabled: bool = False
    calendar_push_interval_seconds: float = 60.0

    # The integrity-verification loop (ADR-0009/0011; slice PH7-B). The cycle
    # runs the same check POST /api/v1/admin/integrity-check runs synchronously
    # — the tenant's full audit chain verified and every registered document
    # re-hashed against the records registry — and records the one
    # integrity_check audit entry (actor NULL = system). **Off by default**,
    # like the other loops: a deployment that wants continuous verification
    # enables it; one that does not presses the route — and ADR-0011's
    # post-restore verification is the same check run once, by hand or by
    # cron, before the instance returns to service.
    integrity_check_enabled: bool = False
    integrity_check_interval_hours: float = 24.0

    # The SMTP fallback (ADR-0005 path A): the same mailbox, with an app
    # password instead of OAuth. Empty means "not configured" — the provider
    # rule then falls back from a connected Gmail credential to these settings
    # and refuses with ``mail_not_configured`` when neither is there. The
    # password is a credential under ADR-0014 custody: environment only, never
    # the database, never a log line, never an audit entry.
    smtp_host: str = ""
    smtp_port: int = 587
    mail_app_password: str = ""

    # The IMAP half of the same fallback: the read side of path A. The host is
    # what makes the leg configured — the account is ``mail_address`` and the
    # password the app password above, the pair the SMTP leg sends with,
    # because it is the same mailbox. The read rule then falls back from a
    # connected Gmail credential to these settings, and a deployment with
    # neither has no mailbox to read. **TLS is the transport on every port**
    # (``IMAP4_SSL``, verified), because a port is not a security decision: a
    # server whose only protocol is cleartext is refused before the password
    # is offered. ``imap_allow_plaintext`` is the one exception and it is
    # development-only — a local test server, never a mailbox with client
    # data, never a deployment. The password is ADR-0014 custody, as above.
    imap_host: str = ""
    imap_port: int = 993
    imap_allow_plaintext: bool = False

    # The calendar push's provider-credential directory (slice M8; ADR-0014
    # custody, the mailbox store's pattern): one file per tenant holding that
    # tenant's Google or Microsoft app-registration credential — the tokens
    # the push authenticates with. A 0700 directory of 0600 files in a
    # gitignored path, never the database; an empty directory means no tenant
    # has connected a provider, and every such tenant stays on the `.ics` feed.
    calendar_credential_dir: Path = Path("var/calendar-credentials")

    # The calendar adapters' per-call HTTP timeout (slice M8; one sweep or
    # probe's client): a provider that does not answer this fast is the
    # transient failure the typed error family names, retried on the next
    # sweep — never a hang inside a request or a cycle.
    calendar_request_timeout_seconds: float = 10.0

    # Malware scanning (ADR-0009; scope §8 inbound file safety). clamd runs as
    # its own service (``deploy/docker-compose.yml``); nothing is served or
    # processed until it says clean, and a deployment that cannot reach it
    # stores files as ``pending`` — never clean, never usable — which the
    # retro-scan resolves.
    scan_host: str = "localhost"
    scan_port: int = 3310
    scan_timeout_seconds: float = 10.0

    # The draft filler's embedded font (ADR-0004 as amended; Phase 5 slice
    # F4b). Empty means "the packaged asset" — ``backend/assets/NotoSansHK.ttf``
    # in the repo, ``/app/assets/`` in the image — and a deployment that points
    # it at a path without a readable font refuses the release loudly
    # (``draft_font_unavailable``) rather than rendering Latin-only text on a
    # statutory form. A string, like the other "not configured" settings: an
    # empty value is the absence, never a path.
    draft_font_path: str = ""

    # The Q1.3 deployment gate (ADR-0014 open action 2; slice M6): counsel's
    # resolution of the provider-terms question that gates every production
    # inference call. Closed vocabulary — ``enterprise_dpa`` (an archived
    # enterprise DPA with explicit zero-retention/no-training clauses),
    # ``fallback_provider`` (a provider passing R1-R6), or
    # ``residual_risk_accepted`` (the founder's recorded acceptance of the
    # reviewed provider terms' residual risk — ``research/legal/pia-register.md``;
    # a counsel letter is deferred to the funded scale-up, ADR-0003's 2026-10-06
    # amendment).
    # The default is deliberately empty: the gate fails closed, the per-
    # company opt-in register refuses new entries while it is unset, and
    # ``decide_call`` refuses ``ai_gate_unset`` at every call. Anything
    # outside the closed vocabulary reads as unset, in config as in the rule.
    ai_q13_gate: str = ""
    # The bounded reference that evidences the resolution above — the letter
    # or DPA's archive id, the counsel memo's reference: a short string a
    # reader can chase, never the document itself. Recorded in the audit
    # chain with every opt-in register write, so the entry names the legal
    # basis under which the notice was accepted.
    ai_q13_evidence: str = ""

    # The local LLM endpoint (Enterprise Offline; ADR-0014's own-key custody,
    # slice M6): a hand-rolled OpenAI-compatible client posts the composed
    # snippet to ``{AICS_LLM_BASE_URL}/chat/completions`` with
    # ``AICS_LLM_API_KEY`` as the bearer credential. Empty base URL means
    # "not configured" — the pre-flight rule then refuses ``ai_not_configured``
    # for Enterprise Offline and the deployment stays manual. The key is
    # ADR-0014 custody: environment only, never the database, never a log
    # line, never an audit entry. **TLS is the transport**: a cleartext
    # ``http://`` base URL is refused (``plaintext_not_allowed``) before the
    # key is offered; ``llm_allow_insecure`` is the one exception and it is
    # development-only — a local test server, never a deployment with client
    # data (the IMAP leg's rule).
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 30.0
    llm_allow_insecure: bool = False


settings = Settings()
