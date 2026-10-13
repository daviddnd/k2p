"""Pure business logic: no I/O, no framework imports (ADR-0006 deterministic core).

Home of the effective-dated master model (ADR-0007), workflow state machines
(ADR-0008), deadline and risk calculation, and the diff engine. Anything with
legal or financial consequence lives here as deterministic, exhaustively
unit-tested code — never behind an LLM call.
"""
