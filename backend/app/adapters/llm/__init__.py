"""The LLM seam (M6.4): the port the gateway speaks and the two custody
implementations behind it (ADR-0014).

``port.py`` is the Protocol — the only thing ``app.application.ai_gateway``
knows about a transport. ``control_plane_channel.py`` is the Standard tier's
path to the vendor control plane (activation-token bearer, standalone-
loadable for the cross-deployable driver). ``local_provider.py`` is the
Enterprise Offline path: a hand-rolled OpenAI-compatible chat-completions
client over httpx pointed at a locally configured endpoint.
"""
