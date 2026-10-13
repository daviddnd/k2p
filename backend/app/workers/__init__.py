"""Thin job wrappers (email ingestion, parsing, reminders, scheduled jobs).

Jobs only call application services; no business logic here. The Celery-vs-RQ
choice (ADR-0001 allows either) is deferred until the first real job lands —
do not add a queue dependency before then.
"""
