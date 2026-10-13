"""The malware scanner's I/O (ADR-0009; scope §8 inbound file safety).

ClamAV is the scanner ADR-0009 names, and ``clamd`` — its daemon — is the way
the execution plane reaches it: the scanner runs as its own container in the
deployment (``deploy/docker-compose.yml``), so no virus database and no
scanning process live inside the application image, and the application only
ever holds the bytes it is about to store.

One module, one seam: ``clamd``. A deployment that points the seam at nothing
reachable gets failures, not exceptions — and a failure is never clean.
"""
