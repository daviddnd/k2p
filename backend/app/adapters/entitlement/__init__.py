"""Entitlement source (ADR-0012/0016; entitlement design D2).

The single seam that tells the execution plane what the tenant is licensed
for. Phase 1 has exactly one implementation — deployment configuration
(``config_source.py``), defaulting to Free — and deliberately no stub or
second path: the control-plane slice replaces that implementation with the
activation-lease client without changing the seam, the payload shape, or any
caller. The vendor control-plane client itself belongs in
``adapters/control_plane`` when it lands.
"""
