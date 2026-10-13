"""Execution-plane backend application (modular monolith, ADR-0006).

Layer dependency direction (enforced by import-linter, see pyproject.toml):

    api/  →  application/  →  domain/
                   ↓
               adapters/

    workers/ sits beside api/ and likewise only calls application services.
    core/ holds shared configuration and may be imported by any layer.
"""
