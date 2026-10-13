"""PDF adapters: reading the digital shapes of a filing.

The prior-filing extractor lives here (``extractor.py``). It owns every
use of pdfplumber in the codebase and hands the application layer plain
per-target values, so a later OCR reader (E2) can join it behind the same
entry point without the callers knowing which reader ran.
"""
