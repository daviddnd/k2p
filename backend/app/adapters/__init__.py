"""External touchpoints, each behind a Protocol/ABC (ADR-0006).

LLM providers (ADR-0003), email providers (ADR-0005), object storage, PDF
renderers (ADR-0004), messaging channels, and the vendor control-plane client
(``adapters/control_plane/``, ADR-0012). Adapters depend on domain interfaces,
never the reverse. The adapter seam is where PII minimization and per-company
AI-disabled checks are enforced before any LLM call.
"""
