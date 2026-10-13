# Execution-plane backend image. Build context is the repository root so the
# pinned form registry (backend/forms/, ADR-0006/0010) ships in the image.
FROM python:3.12-slim

WORKDIR /app

# Local OCR engine for scanned prior filings (founder sign-off 2026-09-21).
# Tesseract is a system package, not a Python one; chi-tra carries the
# Traditional-Chinese data the bilingual CR forms need. Cloud OCR is excluded
# by ADR-0012's local-vault rule, so the engine ships in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-tra \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir .

COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/forms ./forms
# Dated policy data (ADR-0010; contract §5.1.4 slice L) — the fee schedule the
# compliance reads evaluate, beside the registry it sits next to in the repo.
COPY backend/rules ./rules
# The draft filler's embedded Traditional-Chinese font (ADR-0004 amendment;
# founder sign-off 2026-09-28) — next to the registry it fills.
COPY backend/assets ./assets

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
