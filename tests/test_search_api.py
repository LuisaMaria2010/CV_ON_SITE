"""
Tests per POST /api/search (blueprint functions/search_api.py).

Strategia: chiamiamo direttamente la funzione `search_candidates` importata dal
blueprint, monkeypatchando SearchService e get_embedding_client per evitare
dipendenze di rete/Azure.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import azure.functions as func

import infra.search_service as search_mod
import functions.search_api as search_api


# =========================================================
# Helpers
# =========================================================

def _make_request(body: dict | None, method: str = "POST") -> func.HttpRequest:
    # Ogni ricerca richiede il site: i test che non lo dichiarano usano il default.
    if body:
        body = {"site": "mcflashcandidati", **body}
    raw = json.dumps(body).encode() if body is not None else b""
    return func.HttpRequest(
        method=method,
        url="/api/search",
        body=raw,
        headers={"content-type": "application/json"},
        params={},
        route_params={},
    )


def _parse_envelope(response) -> dict:
    """Return the raw envelope {data, error, request_id, success} from the handler."""
    if isinstance(response, dict):
        return response
    body = response.get_body().decode()
    return json.loads(body)


def _parse_response(response) -> dict:
    """
    Unwrap the inner 'data' payload from the http_error_handler envelope.
    For success responses, returns envelope["data"].
    For error responses (data=None), returns the envelope itself so callers
    can inspect 'error'.
    """
    envelope = _parse_envelope(response)
    if isinstance(envelope.get("data"), dict):
        return envelope["data"]
    return envelope


def _make_fake_hit(
    document_id: str = "doc1",
    lex_score: float = 0.8,
    vec_score: float = 0.7,
    skills: list[str] | None = None,
    role: str = "developer",
    location: str = "Milano",
    processed_at: str = "2026-04-01T00:00:00+00:00",
    full_name: str = "Test User",
) -> dict:
    # `full_name` e' la chiave di raggruppamento per persona in
    # _aggregate_top_candidates: due hit con lo stesso nome collassano in un
    # solo candidato (piu' CV della stessa persona). I test che vogliono N
    # candidati distinti devono passare nomi distinti.
    return {
        "document_id": document_id,
        "full_name": full_name,
        "role": role,
        "location": location,
        "skills": skills or ["python"],
        "certifications": [],
        "seniority": "senior",
        "experience_years": 5.0,
        "language": "it",
        "availability": None,
        "version": 1,
        "source_path": "incoming/cv.pdf",
        "chunk_index": 0,
        "content": "content text",
        "highlights": {},
        "lex_score": lex_score,
        "vec_score": vec_score,
        "score": max(lex_score, vec_score),
        "processed_at": processed_at,
    }


class FakeSearchService:
    """Returns configurable hits for each search_chunks call."""

    def __init__(
        self,
        hits_sequence: list[list[dict]] | None = None,
        document_ids: set[str] | None = None,
    ):
        self._hits_sequence = hits_sequence or [[]]
        self._call_count = 0
        self._last_hits: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.identity_calls: list[dict[str, Any]] = []
        # Universo identita' esplicito: serve quando la ricerca non restituisce
        # nulla ma l'hard-select deve comunque risolvere dei document_id.
        self._document_ids = document_ids

    def people(self) -> dict[str, str]:
        """document_id -> full_name per ogni hit configurato."""
        out: dict[str, str] = {}
        for batch in self._hits_sequence:
            for hit in (batch or []):
                if not isinstance(hit, dict):
                    continue
                doc_id = str(hit.get("document_id") or hit.get("id") or "").strip()
                name = str(hit.get("full_name") or hit.get("name") or "").strip()
                if doc_id and name:
                    out.setdefault(doc_id, name)
        return out

    def _all_document_ids(self) -> set[str]:
        ids: set[str] = set()
        for batch in self._hits_sequence:
            for hit in (batch or []):
                if not isinstance(hit, dict):
                    continue
                doc_id = str(hit.get("document_id") or hit.get("id") or "").strip()
                if doc_id:
                    ids.add(doc_id)
        return ids

    async def resolve_document_ids_by_name(
        self,
        name_query: str,
        *,
        top: int = 5000,
        index_name=None,
    ) -> set[str]:
        """Identity resolution: l'universo hard-select MCFlash -> document_id.

        Il fake risolve sull'insieme dei document_id presenti negli hit
        configurati, cosi' che l'identity filter sia non vuoto e la pipeline
        prosegua fino alle vere ricerche di rilevanza.
        """
        self.identity_calls.append(
            {"name_query": name_query, "top": top, "index_name": index_name}
        )
        if not str(name_query or "").strip():
            return set()
        if self._document_ids is not None:
            return set(self._document_ids)
        return self._all_document_ids()

    async def search_chunks(
        self,
        query=None,
        *,
        lexical_query=None,
        semantic_query=None,
        odata_filter=None,
        embedding=None,
        top=10,
        index_name=None,
    ):
        self.calls.append({
            "query": query,
            "lexical_query": lexical_query,
            "semantic_query": semantic_query,
            "odata_filter": odata_filter,
            "top": top,
            "index_name": index_name,
        })
        if self._call_count < len(self._hits_sequence):
            hits = self._hits_sequence[self._call_count]
        else:
            hits = []
        self._call_count += 1
        self._last_hits = list(hits or [])
        return hits

    async def load_chunks_for_candidates(
        self,
        document_ids: list[str],
        *,
        index_name=None,
        per_candidate_limit: int = 40,
    ):
        _ = index_name
        grouped: dict[str, list[dict[str, Any]]] = {
            str(doc_id): []
            for doc_id in (document_ids or [])
            if str(doc_id).strip()
        }
        if not grouped:
            return {}

        limit = max(1, int(per_candidate_limit or 1))
        for hit in self._last_hits:
            if not isinstance(hit, dict):
                continue
            doc_id = str(hit.get("document_id") or hit.get("id") or "").strip()
            if not doc_id or doc_id not in grouped:
                continue
            if len(grouped[doc_id]) >= limit:
                continue
            grouped[doc_id].append(dict(hit))

        return grouped


async def _call_handler(
    req: func.HttpRequest,
    monkeypatch,
    fake_service: FakeSearchService,
    mcflash_client=None,
    cv_map: dict[str, str] | None = None,
):
    """Wire fake SearchService and call the handler.

    `cv_map` e' la tabella CV -> MCFlash (document_id -> id MCFlash). Di default
    ogni CV configurato e' collegato al record MCFlash con Id uguale al suo
    document_id, cioe' alle righe di default del client MCFlash finto.
    """
    monkeypatch.setattr(search_mod, "SearchService", lambda: fake_service)

    import services.search_pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "SearchService", lambda: fake_service)

    # L'universo hard-select MCFlash di default copre tutte le persone presenti
    # negli hit configurati, una riga per CV con Id = document_id.
    _default_rows = [
        {"Id": doc_id, "Nome": name}
        for doc_id, name in sorted(fake_service.people().items())
    ] or [{"Id": "doc1", "Nome": "Test User"}]
    if cv_map is None:
        cv_map = {str(r["Id"]): str(r["Id"]) for r in _default_rows}
    monkeypatch.setattr(pipeline_mod, "get_cv_mcflash_map", lambda: dict(cv_map))

    class _FakeMCFlashClient:
        async def filter_candidates(self, **kwargs):
            _ = kwargs
            return list(_default_rows)

        async def fetch_page(self, *, limit: int, offset: int):
            _ = (limit, offset)
            return list(_default_rows)

    monkeypatch.setattr(search_api, "_get_mcflash_candidates_client", lambda: (mcflash_client or _FakeMCFlashClient()))

    # Disable embedding to keep tests fast and network-free
    async def fake_embed(text):
        return [0.1] * 1536

    class FakeEmbClient:
        async def aembed_query(self, text):
            return [0.1] * 1536

    import infra.llm_client as llm_mod
    monkeypatch.setattr(llm_mod, "get_embedding_client", lambda: FakeEmbClient())

    result = await search_api.search_candidates(req)
    return result


# =========================================================
# Validation / error cases
# =========================================================

class TestSearchValidation:

    def test_empty_body_raises_400(self, monkeypatch):
        req2 = func.HttpRequest(method="POST", url="/api/search", body=b"",
                                headers={}, params={}, route_params={})

        async def _run():
            return await _call_handler(req2, monkeypatch, FakeSearchService())

        result = asyncio.run(_run())
        envelope = _parse_envelope(result)
        # http_error_handler wraps InvalidInputError → 400
        assert result.status_code == 400
        assert envelope.get("error") is not None

    def test_no_query_or_skills_raises_400(self, monkeypatch):
        req = _make_request({"top": 5})

        async def _run():
            return await _call_handler(req, monkeypatch, FakeSearchService())

        result = asyncio.run(_run())
        envelope = _parse_envelope(result)
        assert result.status_code == 400
        assert envelope.get("error") is not None


# =========================================================
# Happy path — basic
# =========================================================

class TestSearchHappyPath:

    def test_basic_query_returns_hits(self, monkeypatch):
        # Nomi distinti: hit con lo stesso full_name sono la stessa persona e
        # verrebbero raggruppati in un unico candidato.
        fake = FakeSearchService(hits_sequence=[[
            _make_fake_hit("doc1", full_name="Test User"),
            _make_fake_hit("doc2", full_name="Second User"),
        ]])
        req = _make_request({"query": "python developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert "hits" in data
        assert len(data["hits"]) == 2

    def test_semantic_query_prefers_explicit_query(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({
            "query": "python engineer milano",
            "role": "developer",
            "skills": ["azure"],
            "hybrid": False,
        })

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert "developer azure" in fake.calls[0]["lexical_query"]
        assert fake.calls[0]["semantic_query"] == "python engineer milano"

    def test_semantic_query_empty_when_no_free_text(self, monkeypatch):
        # Semantic search only makes sense over free-text intent. With no
        # `query`, role/skills still drive the lexical pass, but semantic
        # search must stay off rather than run on keyword text.
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({
            "query": "",
            "role": "developer",
            "skills": ["python"],
            "hybrid": False,
        })

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert "developer python" in fake.calls[0]["lexical_query"]
        assert fake.calls[0]["semantic_query"] == ""

    def test_meta_fields_present(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        meta = data["meta"]
        assert "total" in meta
        assert "top" in meta
        assert "relaxed" in meta
        assert "hybrid" in meta
        assert "index" in meta

    def test_meta_relaxed_false_when_enough_results(self, monkeypatch):
        # With top=2 and fallback_threshold=0.20, fallback_min=ceil(0.20*2)=1
        # Provide 2 hits → relaxed must stay False
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1"), _make_fake_hit("doc2")]])
        req = _make_request({"query": "developer", "top": 2, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["relaxed"] is False

    def test_meta_total_matches_hits_length(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit(f"doc{i}") for i in range(3)]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["total"] == len(data["hits"])

    def test_suggestions_empty_when_not_relaxed(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")] * 5])
        req = _make_request({"query": "developer", "hybrid": False, "top": 2})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert isinstance(data["suggestions"], list)


# =========================================================
# OData filter wiring
# =========================================================

class TestODataFilterWiring:

    def test_skills_filter_sent_to_search(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")] * 5])
        req = _make_request({"query": "dev", "skills": ["python"], "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert fake.calls[0]["odata_filter"] is not None
        assert "python" in fake.calls[0]["odata_filter"]

    def test_no_skill_filter_when_no_constraints(self, monkeypatch):
        # L'identity filter (search.in su document_id, dall'hard-select MCFlash)
        # e' SEMPRE applicato: "nessun vincolo" significa nessuna clausola
        # skills/any(...), non assenza totale di filtro OData.
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        odata = fake.calls[0]["odata_filter"]
        assert "skills/any" not in (odata or "")
        assert "search.in(document_id" in (odata or "")


# =========================================================
# Fallback relaxation
# =========================================================

class TestFallbackRelaxation:

    def test_relaxed_triggered_when_too_few_hits(self, monkeypatch):
        # top=10, fallback_min=ceil(0.20*10)=2
        # First call returns 0 hits, second (relaxed) returns 3
        relaxed_hits = [_make_fake_hit(f"rdoc{i}") for i in range(3)]
        fake = FakeSearchService(hits_sequence=[[], relaxed_hits])
        req = _make_request({"query": "developer", "skills": ["python"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["relaxed"] is True
        assert len(data["hits"]) > 0

    def test_relaxed_suggestions_not_empty(self, monkeypatch):
        relaxed_hits = [_make_fake_hit("rdoc1")]
        fake = FakeSearchService(hits_sequence=[[], relaxed_hits])
        req = _make_request({"query": "developer", "skills": ["python", "azure"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert len(data["suggestions"]) > 0

    def test_relaxed_not_triggered_without_skills(self, monkeypatch):
        # No skills -> no skill-relax branch.
        # `document_ids` esplicito: la ricerca non restituisce nulla, ma
        # l'hard-select deve comunque risolvere un'identita', altrimenti la
        # pipeline salta del tutto search_chunks.
        fake = FakeSearchService(hits_sequence=[[]], document_ids={"doc1"})
        req = _make_request({"query": "developer", "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["relaxed"] is False
        # Only 1 search call (no fallback)
        assert len(fake.calls) == 1

    def test_relaxed_deduplicates_results(self, monkeypatch):
        # First search returns doc1, relaxed search also returns doc1 + doc2
        first_hits = [_make_fake_hit("doc1")]
        relaxed_hits = [_make_fake_hit("doc1"), _make_fake_hit("doc2")]
        # top=10, fallback_min=2, first returns only 1 → relaxed triggered
        fake = FakeSearchService(hits_sequence=[first_hits, relaxed_hits])
        req = _make_request({"query": "developer", "skills": ["python"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        doc_ids = [h["document_id"] for h in data["hits"]]
        assert len(doc_ids) == len(set(doc_ids)), "Duplicate document_ids in results"

    def test_relaxed_semantic_query_stays_empty_without_free_text(self, monkeypatch):
        # query empty + role empty -> relaxed_lexical_query empty; semantic search
        # must stay off (no fallback to joined skills) since there is no free text.
        relaxed_hits = [_make_fake_hit("rdoc1")]
        fake = FakeSearchService(hits_sequence=[[], relaxed_hits])
        req = _make_request({"query": "", "skills": ["python", "azure"], "top": 10, "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        asyncio.run(_run())
        assert len(fake.calls) >= 2
        assert fake.calls[1]["semantic_query"] == ""


# =========================================================
# Hybrid flag
# =========================================================

class TestHybridFlag:

    def test_hybrid_true_recorded_in_meta(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": True})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["hybrid"] is True

    def test_hybrid_false_recorded_in_meta(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[_make_fake_hit("doc1")]])
        req = _make_request({"query": "developer", "hybrid": False})

        async def _run():
            return await _call_handler(req, monkeypatch, fake)

        result = asyncio.run(_run())
        data = _parse_response(result)
        assert data["meta"]["hybrid"] is False


# =========================================================
# Homonym disambiguation (name + role)
# =========================================================

def _hit(document_id: str, full_name: str, role: str) -> dict:
    return {
        "document_id": document_id,
        "full_name": full_name,
        "role": role,
        "location": "Milano",
        "skills": ["python"],
        "certifications": [],
        "seniority": "senior",
        "experience_years": 5.0,
        "language": "it",
        "availability": None,
        "version": 1,
        "source_path": "incoming/cv.pdf",
        "chunk_index": 0,
        "content": "content text",
        "highlights": {},
        "lex_score": 0.8,
        "vec_score": 0.7,
        "score": 0.8,
        "processed_at": "2026-04-01T00:00:00+00:00",
    }


class _HomonymMCFlashClient:
    """Two MCFlash rows sharing the same normalized name but different roles.

    `rows` e' cio' che restituisce la hard select con filtri di business;
    `site_rows` e' il perimetro del site (solo filtro site), usato quando la
    ricerca si allarga e dal second pass. Di default coincidono.
    """

    _NON_FILTER_KWARGS = {"limit", "offset", "site", "max_scan_records", "return_all_matches"}

    def __init__(self, rows, site_rows: list[dict] | None = None):
        self._rows = rows
        self._site_rows = site_rows if site_rows is not None else rows

    async def filter_candidates(self, **kwargs):
        business_filters = any(v for k, v in kwargs.items() if k not in self._NON_FILTER_KWARGS)
        return list(self._rows if business_filters else self._site_rows)


class TestCvMcflashMap:
    """L'identita' di un CV viene SOLO dalla tabella CV -> MCFlash, mai dal codice."""

    _HOMONYMS = [
        {"Id": "mc1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"},
        {"Id": "mc2", "Nome": "Mario Rossi", "Ruolo": "Data Analyst"},
    ]

    def _run(self, monkeypatch, body, fake, mcflash, cv_map):
        req = _make_request(body)

        async def _go():
            return await _call_handler(req, monkeypatch, fake, mcflash_client=mcflash, cv_map=cv_map)

        return _parse_response(asyncio.run(_go()))

    def test_homonyms_resolved_by_the_map(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[
            _hit("doc1", "Mario Rossi", "java developer"),
            _hit("doc2", "Mario Rossi", "data analyst"),
        ]])
        data = self._run(monkeypatch, {"query": "mario rossi", "hybrid": False}, fake,
                         _HomonymMCFlashClient(self._HOMONYMS), {"doc1": "mc1", "doc2": "mc2"})
        by_doc = {h["document_id"]: h for h in data["hits"]}

        assert by_doc["doc1"]["mcflash_profile"]["Id"] == "mc1"
        assert by_doc["doc2"]["mcflash_profile"]["Id"] == "mc2"

    def test_the_map_wins_over_the_cv_role(self, monkeypatch):
        """Il ruolo estratto dal CV non conta: decide la tabella."""
        fake = FakeSearchService(hits_sequence=[[_hit("doc1", "Mario Rossi", "java developer")]])
        data = self._run(monkeypatch, {"query": "mario rossi", "hybrid": False}, fake,
                         _HomonymMCFlashClient(self._HOMONYMS), {"doc1": "mc2"})

        assert [h["mcflash_profile"]["Id"] for h in data["hits"]] == ["mc2"]

    def test_cv_outside_the_map_is_out_of_perimeter(self, monkeypatch):
        """Stesso codice di un record MCFlash, ma CV non in tabella: mai restituito."""
        fake = FakeSearchService(hits_sequence=[[
            _hit("doc1", "Mario Rossi", "java developer"),
            _hit("doc9", "Mario Rossi", "rust developer"),
        ]] * 4)
        data = self._run(monkeypatch, {"query": "mario rossi", "skills": ["python"], "hybrid": False}, fake,
                         _HomonymMCFlashClient(self._HOMONYMS), {"doc1": "mc1"})

        assert {h["document_id"] for h in data["hits"]} == {"doc1"}

    def test_cvs_of_the_same_record_are_one_candidate(self, monkeypatch):
        fake = FakeSearchService(hits_sequence=[[
            _hit("doc1", "Mario Rossi", "java developer"),
            _hit("doc1b", "Mario Rossi", "backend developer"),
        ]])
        data = self._run(monkeypatch, {"query": "mario rossi", "hybrid": False}, fake,
                         _HomonymMCFlashClient(self._HOMONYMS), {"doc1": "mc1", "doc1b": "mc1"})

        assert [h["mcflash_profile"]["Id"] for h in data["hits"]] == ["mc1"]

    def test_second_pass_resolves_identity_with_the_map(self, monkeypatch):
        # I filtri di business selezionano solo un'altra persona: i CV di Mario
        # Rossi entrano dal second pass, collegati tramite la tabella ai record
        # del site.
        mcflash = _HomonymMCFlashClient(
            rows=[{"Id": "other", "Nome": "Altra Persona"}],
            site_rows=[{"Id": "other", "Nome": "Altra Persona"}, *self._HOMONYMS],
        )
        fake = FakeSearchService(hits_sequence=[
            [],
            [_hit("doc1", "Mario Rossi", "java developer"), _hit("doc2", "Mario Rossi", "data analyst")],
        ])
        data = self._run(monkeypatch, {"query": "mario rossi", "role": "java developer", "hybrid": False},
                         fake, mcflash, {"doc1": "mc1", "doc2": "mc2"})
        by_doc = {h["document_id"]: h for h in data["hits"]}

        assert by_doc["doc1"]["mcflash_profile"]["Id"] == "mc1"
        assert by_doc["doc2"]["mcflash_profile"]["Id"] == "mc2"


# =========================================================
# Perimetro del site
# =========================================================

class TestSitePerimeter:
    """La ricerca resta sempre dentro i record MCFlash del site richiesto."""

    def _run(self, monkeypatch, body: dict, fake: FakeSearchService, mcflash, cv_map) -> dict:
        req = _make_request(body)

        async def _go():
            return await _call_handler(req, monkeypatch, fake, mcflash_client=mcflash, cv_map=cv_map)

        return _parse_response(asyncio.run(_go()))

    def test_second_pass_drops_cvs_outside_the_site(self, monkeypatch):
        mcflash = _HomonymMCFlashClient(
            rows=[{"Id": "other", "Nome": "Altra Persona"}],
            site_rows=[
                {"Id": "other", "Nome": "Altra Persona"},
                {"Id": "mc1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"},
            ],
        )
        fake = FakeSearchService(hits_sequence=[
            [],  # first pass: hard select contains nobody from the index
            [
                _hit("doc1", "Mario Rossi", "java developer"),   # record del site
                _hit("doc8", "Mario Rossi", "java developer"),   # record dell'altro site
                _hit("doc9", "Luigi Verdi", "java developer"),   # fuori tabella
            ],
        ])
        data = self._run(monkeypatch, {"query": "java", "role": "java developer", "hybrid": False}, fake, mcflash,
                         {"doc1": "mc1", "doc8": "mc-altro-site"})

        assert [h["document_id"] for h in data["hits"]] == ["doc1"]
        assert data["hits"][0]["mcflash_profile"]["Id"] == "mc1"

    def test_second_pass_searches_only_the_site_cvs(self, monkeypatch):
        mcflash = _HomonymMCFlashClient(
            rows=[{"Id": "other", "Nome": "Altra Persona"}],
            site_rows=[
                {"Id": "other", "Nome": "Altra Persona"},
                {"Id": "mc1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"},
            ],
        )
        fake = FakeSearchService(hits_sequence=[[], [_hit("doc1", "Mario Rossi", "java developer")]])
        self._run(monkeypatch, {"query": "java", "role": "java developer", "hybrid": False}, fake, mcflash,
                  {"doc1": "mc1", "doc8": "mc-altro-site"})

        assert len(fake.calls) >= 2
        assert all(call["odata_filter"] for call in fake.calls), "nessuna ricerca sull'intero indice"
        assert all("doc8" not in call["odata_filter"] for call in fake.calls)

    @pytest.mark.parametrize("body", [
        {"query": "java", "role": "java developer", "hybrid": False},
        {"query": "java", "skills": ["python"], "hybrid": False},                        # rilassamento skill
        {"query": "java", "role": "cobol developer", "skills": ["python"], "hybrid": False},  # hard select allargata
    ])
    def test_every_index_search_is_restricted_to_mapped_cvs(self, monkeypatch, body):
        """Nessuna ricerca sull'indice senza filtro sui document_id della tabella."""
        mcflash = _HomonymMCFlashClient(
            rows=[{"Id": "other", "Nome": "Altra Persona"}],
            site_rows=[{"Id": "other", "Nome": "Altra Persona"},
                       {"Id": "mc1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"}],
        )
        fake = FakeSearchService(hits_sequence=[[_hit("doc9", "Mario Rossi", "java developer")]] * 5)
        data = self._run(monkeypatch, body, fake, mcflash, {"doc1": "mc1", "doc8": "mc-altro-site"})

        assert fake.calls, "almeno una ricerca attesa"
        for call in fake.calls:
            assert call["odata_filter"] and "search.in(document_id" in call["odata_filter"]
            assert "doc8" not in call["odata_filter"]
        assert all(h["document_id"] in {"doc1"} for h in data["hits"])

    def test_second_pass_skipped_when_site_has_no_indexed_cv(self, monkeypatch):
        mcflash = _HomonymMCFlashClient(rows=[{"Id": "other", "Nome": "Altra Persona"}])
        fake = FakeSearchService(hits_sequence=[[], [_hit("doc9", "Luigi Verdi", "java developer")]])
        data = self._run(monkeypatch, {"query": "java", "role": "java developer", "hybrid": False}, fake, mcflash, {})

        assert data["hits"] == []
        assert all(call["odata_filter"] for call in fake.calls)

    def test_widening_keeps_the_site(self, monkeypatch):
        """Filtri di business senza esito: si tolgono i filtri, non il site."""
        mcflash = _HomonymMCFlashClient(
            rows=[],
            site_rows=[{"Id": "mc1", "Nome": "Mario Rossi", "Ruolo": "Java Developer"}],
        )
        fake = FakeSearchService(hits_sequence=[[_hit("doc1", "Mario Rossi", "java developer")]])
        data = self._run(monkeypatch, {"query": "java", "role": "cobol developer", "hybrid": False}, fake, mcflash,
                         {"doc1": "mc1"})

        assert data["meta"]["hard_select"]["widened"] is True
        assert data["meta"]["hard_select"]["site"] == "mcflashcandidati"
        assert [h["mcflash_profile"]["Id"] for h in data["hits"]] == ["mc1"]

    def test_site_is_passed_to_every_mcflash_selection(self, monkeypatch):
        seen: list[dict] = []

        class _Recording(_HomonymMCFlashClient):
            async def filter_candidates(self, **kwargs):
                seen.append(kwargs)
                return await super().filter_candidates(**kwargs)

        mcflash = _Recording(rows=[], site_rows=[{"Id": "mc1", "Nome": "Mario Rossi"}])
        fake = FakeSearchService(hits_sequence=[[_hit("doc1", "Mario Rossi", "java developer")]])
        self._run(
            monkeypatch,
            {"site": "mcflashrisorse", "query": "java", "role": "java developer", "hybrid": False},
            fake, mcflash, {"doc1": "mc1"},
        )

        assert seen and all(kw.get("site") == "mcflashrisorse" for kw in seen)

    @pytest.mark.parametrize("site", [None, "", "altro"])
    def test_missing_or_unknown_site_is_rejected(self, monkeypatch, site):
        body = {"query": "java", "site": site}
        req = func.HttpRequest(
            method="POST", url="/api/search", body=json.dumps(body).encode(),
            headers={"content-type": "application/json"}, params={}, route_params={},
        )
        fake = FakeSearchService(hits_sequence=[[_hit("doc1", "Mario Rossi", "java developer")]])

        async def _go():
            return await _call_handler(req, monkeypatch, fake)

        assert asyncio.run(_go()).status_code == 400
