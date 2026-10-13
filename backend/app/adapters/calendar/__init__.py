"""Calendar provider adapters (slice M8; ADR-0013 item 3, 2026-10-03 amendment).

The push leg's provider I/O (the ``app.application.calendar_sync.
CalendarProvider`` seam) and the ADR-0014 credential custody it reads. Five
modules, one seam each:

- ``microsoft_client`` — the wire to the tenant's v2.0 token endpoint and the
  delegated Graph calendar surface (create/update/delete on the target
  calendar, the ruling-R5 probe);
- ``google_client`` — the same over Google's token endpoint and the Calendar
  API v3 (``events.insert``/``patch``/``delete`` on the target calendar, the
  same probe);
- ``errors`` — the shared typed failure family both adapters raise (auth vs
  transient), catchable without httpx;
- ``probe`` — the probe report's shared shape (four legs, per-leg outcomes);
- ``credential_store`` — the local file the provider credential lives in, at
  mode 0600 (ADR-0014 custody), never the database.
"""
