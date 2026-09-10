"""Entry point della Function App: crea `app` e registra i blueprint.

Nessuna logica qui dentro. Ogni gruppo di route vive nel proprio modulo sotto
`functions/`, l'ingestion asincrona in `ingestion_triggers.py`.

Route esposte:
- blobTrigger  incoming-cv/{name}        -> functions.cv_ingest
- queueTrigger document-processing       -> ingestion_triggers
- POST /api/extract                      -> functions.cv_extract
- POST /api/search                       -> functions.search_api
- POST /api/searcher-wrapper   (ANON)    -> functions.search_api
- POST /api/ai-matcher-wrapper (ANON)    -> functions.matcher_api
- POST /api/backfill/incoming-cv         -> functions.backfill_api
"""
from __future__ import annotations

import azure.functions as func

from ingestion_triggers import bp as ingestion_bp
from functions.backfill_api import bp as backfill_bp
from functions.cv_extract import bp as cv_extract_bp
from functions.cv_ingest import bp as cv_ingest_bp
from functions.matcher_api import bp as matcher_bp
from functions.search_api import bp as search_bp

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

for _bp in (
    ingestion_bp,
    cv_ingest_bp,
    cv_extract_bp,
    search_bp,
    matcher_bp,
    backfill_bp,
):
    app.register_functions(_bp)
