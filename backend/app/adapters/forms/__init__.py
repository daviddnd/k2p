"""Form-registry readers (ADR-0006/0010) and the prior-filing PDF extractor.

Data-only registry access (``registry.py``) and the digital-shape reader
(``pdf/extractor.py``). Both are adapters: they may import ``app.domain``,
never ``app.application`` or ``app.api``.
"""
