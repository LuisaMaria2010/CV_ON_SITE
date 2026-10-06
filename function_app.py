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
- GET  /api/cv/filename                  -> functions.dashboard_api
- GET  /api/roles                        -> functions.dashboard_api
- GET  /api/candidates                   -> functions.dashboard_api
- GET  /api/candidates/{id}              -> functions.dashboard_api
- POST /api/candidates/match             -> functions.dashboard_api
- GET  /api/mc-levels                    -> functions.dashboard_api
- GET  /api/drafts                       -> functions.drafts_api
- PUT/GET/DELETE /api/drafts/{id}        -> functions.drafts_api
- GET  /api/drafts/{id}/original         -> functions.drafts_api
- POST /api/cv/render                    -> functions.cv_render_api
- POST /api/cv/commit                    -> functions.operations_api
- GET  /api/operations[/{id}]            -> functions.operations_api
- POST /api/operations/{id}/retry        -> functions.operations_api
"""
from __future__ import annotations

import azure.functions as func

from functions.backfill_api import bp as backfill_bp
from functions.cv_extract import bp as cv_extract_bp
from functions.cv_ingest import bp as cv_ingest_bp
from functions.cv_render_api import bp as cv_render_bp
from functions.dashboard_api import bp as dashboard_bp
from functions.drafts_api import bp as drafts_bp
from functions.matcher_api import bp as matcher_bp
from functions.operations_api import bp as operations_bp
from functions.search_api import bp as search_bp
from ingestion_triggers import bp as ingestion_bp

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

for _bp in (
    ingestion_bp,
    cv_ingest_bp,
    cv_extract_bp,
    search_bp,
    matcher_bp,
    backfill_bp,
    dashboard_bp,
    drafts_bp,
    cv_render_bp,
    operations_bp,
):
    app.register_functions(_bp)
