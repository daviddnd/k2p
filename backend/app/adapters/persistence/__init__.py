"""Relational persistence: SQLAlchemy models and session plumbing.

ORM models are adapter-layer details (ADR-0006); the domain layer stays
framework-free. Alembic diffs against ``base.Base.metadata`` (see
``alembic/env.py``).
"""
