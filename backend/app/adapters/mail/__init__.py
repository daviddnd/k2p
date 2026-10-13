"""The dedicated mailbox's provider I/O (ADR-0005).

Gmail first: the provider-API path for a TCSP on Google Workspace. The SMTP
fallback ADR-0005 names is its own adapter (``smtp_client``) and lands with
the sending slice, which is also what this package's third module holds: the
one typed failure a *send* raises (``errors``).

Three modules, one seam each:

- ``gmail_client`` — the wire to Google's token endpoint and the Gmail API;
- ``smtp_client`` — the same mailbox over SMTP with an app password (path A);
- ``credential_store`` — the local file the tokens live in, at mode 0600
  (ADR-0014 custody), never the database.
"""
