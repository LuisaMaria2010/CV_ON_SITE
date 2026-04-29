# Implementation Plan - CV_ON_SITE Ingestion Pipeline

## Scope

Target flow:

1. Upload original CV via API and store in `incoming-cv-originals`
2. Extract candidate data (upload + extract path)
3. For internal ingestion, read files from `incoming-cv`
4. Convert to markdown
5. Normalize content
6. Index chunks in Azure AI Search

Reference model: AgentIngestionPipeline.

---

## Status snapshot (Dev)

### Azure resources provisioned

- [x] Blob container `incoming-cv-originals` available
- [x] Blob container `incoming-cv` available
- [x] Blob container `normalized-cv-md` created
- [x] Queue `document-processing` created
- [x] Queue `document-indexing` created
- [x] Queue `document-processing-deadletter` created
- [x] Queue `document-indexing-deadletter` created
- [x] Table `DocumentRegistry` created
- [x] Search index `cv-doc-chunks` created on `as-ai-sitemc-dev`

### Code/config aligned

- [x] Updated `core/config.py` with ingestion/search settings
- [x] Updated `local.settings.json` with Dev resource names
- [x] Updated plan with Azure-valid table name (`DocumentRegistry`)

### Still pending

- [x] Blob trigger and queue handlers implementation in `function_app.py`
- [x] Registry module implementation in `infra/document_registry.py`
- [x] Processor implementation for markdown (`services/document_processor.py`)
- [ ] Indexer implementation for markdown chunks (`services.document_indexer.py`)
- [x] Integrate `DocumentRegistry` updates into the processing pipeline (register / mark_status)
- [x] Image enrichment (upload extracted images + AI descriptions)
- [x] Language detection, element ordering and OCR-duplicate suppression
- [x] Generate YAML front matter and upload normalized markdown to `normalized-cv-md`
- [ ] End-to-end tests (upload → queue → markdown → index)



---

## Phase checklist

## Phase 0 - Configuration baseline

- [x] Add ingestion/index settings in `core/config.py`
- [x] Use Azure-valid names (`DocumentRegistry`, `normalized-cv-md`, `cv-doc-chunks`)
- [x] Add local settings entries in `local.settings.json`
- [ ] Validate runtime reads all new settings during function startup

## Phase 0.5 - Initial backfill for existing files

- [x] Add bootstrap function (HTTP or timer) for existing blobs
- [x] List blobs from `incoming-cv`
- [x] Enqueue using same contract as live trigger
- [x] Add filters (`prefix`, `max_items`, `dry_run`)
- [x] Run bootstrap once in Dev and verify backlog queued (real enqueue, controlled run validated)

## Phase 1 - Trigger and message contract

- [x] Add blob trigger on `incoming-cv/{name}`
- [x] Enqueue contract to `document-processing`
- [x] Include `correlation_id` in message and logs
- [ ] Verify one uploaded PDF -> one queue message

Notes:

- `incoming-cv-originals` is used by API upload/extract to store original files.
- `incoming-cv` remains the internal ingestion container for trigger/backfill flow.

Message contract:

```json
{
  "blob": "incoming-cv/<path>",
  "filename": "<file>.pdf",
  "source_path": "/incoming-cv/<path>",
  "last_modified": "<iso8601>",
  "correlation_id": "<id>"
}
```

## Phase 2 - Document registry and idempotency

- [x] Create `infra/document_registry.py`
- [x] Implement lookup/register/status update on `DocumentRegistry`
- [x] Implement logic: new -> process, same hash -> skip, changed hash -> reprocess + version++
- [x] Verify dedup on same file upload
- [ ] Verify version increment on modified file
 - [x] Integrate registry `register` and `mark_status` calls into `CVPipeline.process` (or ensure consumer always performs full lifecycle)

## Phase 3 - PDF to structured markdown

- [x] Add `domain/document_elements.py`
- [x] Add `services/document_parser.py` (PDF-first)
- [x] Add `services/document_processor.py`
- [x] Implement element ordering (heading/paragraph/list/table)
- [x] Produce deterministic markdown output
 - [x] Extend `services/document_processor.py` to include image extraction hooks and pass image elements to enrichment

## Phase 4 - Normalization and output storage

- [x] Add normalization pipeline (cleanup/dedup)
- [x] Write markdown into `normalized-cv-md`
- [x] Add YAML front matter metadata (`document_id`, `source_path`, `version`, `hash`, `processed_at`, `language`, `element_count`, `image_count`)
- [x] Upload normalized markdown from pipeline and include metadata in blob metadata
- [x] Ensure pipeline removes raw image bytes and replaces them with blob references after enrichment
- [x] Validate one processed message -> one `.md` blob

## Phase 5 - Chunking, embeddings, search indexing

- [x] Create Search index `cv-doc-chunks`
- [x] Add `services/document_indexer.py`
- [x] Extend `infra/search_service.py` for chunk documents
- [x] Implement delete old chunks (trigger calls `delete_chunks` before re-index)
- [x] Implement upsert latest chunks (indexer + upsert flow)
- [ ] Verify retrieval by `document_id` and latest version

Notes: chunking should preserve `document_id` + `version` and allow idempotent upserts; embedding generation should be cached where possible.

### Chunk schema (Phase 5 — original minimal)

| Campo | Tipo | Note |
|---|---|---|
| `id` | string | key — `{doc_id}-v{version}-{idx:05d}` |
| `document_id` | string | filterable |
| `version` | int | filterable, sortable |
| `chunk_index` | int | retrievable |
| `section` | string | retrievable |
| `content` | string | searchable |
| `source_path` | string | retrievable |
| `content_hash` | string | retrievable |
| `processed_at` | datetime | filterable, sortable |

→ **Esteso in Phase A-C** con metadati candidato e vettore embedding (vedi sotto).

---

## Phase A — Schema e modelli Pydantic normalizzati

> **Dipendenze:** nessuna. Da implementare prima di B, C, D.

### Obiettivo

Aggiungere in `core/schema.py` i modelli che rappresentano il front matter YAML esteso e la struttura metadati del candidato. Questi modelli sono usati sia dal processor (serializzazione → markdown) sia dall'indexer (deserializzazione → campi Search).

### Modelli da aggiungere

```python
class CandidateInfo(BaseModel):
    full_name: str | None
    role: str | None
    location: str | None
    email: str | None
    phone: str | None
    birth_date: str | None
    age: int | None
    availability: str | None
    seniority: str | None          # junior/mid/senior/lead/principal

class LanguageEntry(BaseModel):
    lang: str                      # es: "italiano", "inglese"
    level: str | None              # es: "madrelingua", "B2", "C1"

class ImageRef(BaseModel):
    blob_path: str                 # path nel container extracted-images
    description: str | None        # AI-generated caption opzionale

class NormalizedCVMetadata(BaseModel):
    document_id: str
    source_paths: list[str]        # alias (stessi hash, più sorgenti)
    version: int
    hash: str
    processed_at: str              # ISO datetime
    language: str | None           # lingua del CV (iso 639-1: it/en/...)
    candidate: CandidateInfo
    skills: list[str]              # normalizzate lowercase
    certifications: list[str]
    education_titles: list[str]
    languages_spoken: list[LanguageEntry]
    experience_years: float | None
    employment_dates: list[WorkExperienceRaw]
    images: list[ImageRef]
    element_count: int
    image_count: int
```

### Checklist

- [ ] Aggiungere `CandidateInfo`, `LanguageEntry`, `ImageRef`, `NormalizedCVMetadata` in `core/schema.py`
- [ ] Mantenere retrocompatibilità con `LLMExtractionRaw`, `CVExtraction`, `ExtractHttpResponse`

---

## Phase B — Front matter esteso (`services/document_processor.py`)

> **Dipendenze:** Phase A.

### Obiettivo

Il processor attualmente scrive solo 7 campi nel front matter. Va esteso per serializzare `NormalizedCVMetadata` completo nel blob `.md` uploadato su `normalized-cv-md`.

### Front matter PRIMA (attuale — 7 campi)

```yaml
document_id: mario-rossi
source_path: /incoming-cv/mario-rossi.docx
version: 1
hash: abc123...
processed_at: "2026-04-20T15:33:34+00:00"
element_count: 6
image_count: 0
```

### Front matter DOPO (proposto — completo)

```yaml
document_id: mario-rossi
source_paths:
  - /incoming-cv/mario-rossi.docx
version: 1
hash: abc123...
processed_at: "2026-04-20T15:33:34+00:00"
language: it
candidate:
  full_name: Mario Rossi
  role: Senior Software Engineer
  location: Milano, Italia
  email: mario.rossi@example.com
  phone: "+39 333 1234567"
  birth_date: null
  age: null
  availability: null
  seniority: senior
skills:
  - python
  - azure
  - docker
certifications:
  - AZ-900
education_titles:
  - "MSc Computer Science"
languages_spoken:
  - {lang: italiano, level: madrelingua}
  - {lang: inglese, level: B2}
experience_years: 10.5
employment_dates:
  - {start_date: "2020-05-01", end_date: null}
images:
  - {blob_path: "extracted-images/mario-rossi/foto.jpg", description: "Foto profilo"}
element_count: 6
image_count: 1
```

### Checklist

- [ ] Aggiornare il metodo di generazione front matter nel processor per produrre `NormalizedCVMetadata`
- [ ] Alimentare `candidate` con i dati da `CVExtraction` (già calcolati dal pipeline LLM)
- [ ] Sostituire `source_path: str` con `source_paths: list[str]` (backward-compat: append se già esistente)
- [ ] Serializzare `NormalizedCVMetadata` con `model.model_dump()` + `yaml.dump()`
- [ ] Aggiornare unit test su generazione front matter

---

## Phase C — Indexer con metadati candidato + embedding

> **Dipendenze:** Phase A, Phase B.

### Obiettivo

Estendere `DocumentIndexer.build_chunk_documents` per includere i metadati strutturati del candidato in ogni chunk, e aggiungere il campo `embedding` (vettore 1536-dim) generato tramite Azure OpenAI Embeddings.

### Chunk schema esteso (Phase C — completo)

| Campo | Tipo Search | Proprietà |
|---|---|---|
| `id` | Edm.String | **key**, retrievable |
| `document_id` | Edm.String | filterable, retrievable |
| `full_name` | Edm.String | searchable, retrievable |
| `role` | Edm.String | searchable, filterable, facetable |
| `location` | Edm.String | filterable, facetable |
| `skills` | Collection(Edm.String) | searchable, filterable, facetable |
| `certifications` | Collection(Edm.String) | filterable, facetable |
| `seniority` | Edm.String | filterable, facetable |
| `experience_years` | Edm.Double | filterable, sortable |
| `language` | Edm.String | filterable |
| `availability` | Edm.String | filterable |
| `version` | Edm.Int32 | filterable, sortable |
| `chunk_index` | Edm.Int32 | retrievable |
| `section` | Edm.String | retrievable |
| `content` | Edm.String | searchable, retrievable |
| `source_path` | Edm.String | retrievable |
| `processed_at` | Edm.DateTimeOffset | filterable, sortable |
| `content_hash` | Edm.String | retrievable |
| `embedding` | Collection(Edm.Single) | vectorSearchable, 1536 dims, algoritmo HNSW |

### Firma aggiornata `build_chunk_documents`

```python
# PRIMA
def build_chunk_documents(
    self,
    markdown_text: str,
    document_id: str,
    version: int,
    source_path: str,
    processed_at: str | None = None,
) -> list[dict]

# DOPO
def build_chunk_documents(
    self,
    markdown_text: str,
    metadata: NormalizedCVMetadata,          # porta tutti i campi strutturati
) -> list[dict]

async def index_async(
    self,
    markdown_text: str,
    metadata: NormalizedCVMetadata,
    embedding_fn: Callable[[str], Awaitable[list[float]]] | None = None,
) -> list[dict]
```

### Embedding client (`infra/llm_client.py`)

Aggiungere funzione:

```python
def get_embedding_client() -> AzureOpenAIEmbeddings:
    """Ritorna client embeddings per azure-openai. Usa stesse credenziali del LLM."""
```

### Checklist

- [ ] Aggiornare firma e implementazione `build_chunk_documents` in `services/document_indexer.py`
- [ ] Ogni chunk doc include `full_name`, `role`, `location`, `skills`, `certifications`, `seniority`, `experience_years`, `language`, `availability`
- [ ] Aggiungere `index_async` con injection `embedding_fn`
- [ ] Aggiungere `get_embedding_client()` in `infra/llm_client.py`
- [ ] Aggiornare chiamate al indexer nel trigger/pipeline per passare `NormalizedCVMetadata`
- [ ] Aggiornare unit tests `tests/test_indexer_chunking.py`

---

## Phase D — Azure Search schema + ricerca ibrida (`infra/search_service.py`)

> **Dipendenze:** Phase C.

### Obiettivo

1. Aggiungere `create_or_update_index()` per definire il mapping dello schema in Azure Search (eseguito una volta in setup/deploy).
2. Implementare `search_chunks()` per ricerca ibrida (lexical + vector).

### `create_or_update_index()`

```python
async def create_or_update_index(self) -> None:
    """
    Crea o aggiorna il mapping dell'indice su Azure Search.
    Da eseguire in deploy/setup, non ad ogni upsert.
    Definisce campi, vector profile (HNSW 1536), e semantic configuration.
    """
```

### `search_chunks()` — ricerca ibrida

```python
async def search_chunks(
    self,
    query: str,
    odata_filter: str | None = None,
    embedding: list[float] | None = None,
    top: int = 10,
    highlight_fields: str = "content",
    index_name: str | None = None,
) -> list[dict]:
    """
    Esegue ricerca ibrida (lexical + vector) su chunk index.
    - Se embedding presente: VectorizedQuery su campo 'embedding'
    - Se query presente: search_text full-text
    - Merge e deduplica per document_id tenendo lo score migliore
    Ritorna lista normalizzata di hit con: document_id, full_name, role,
    location, skills, seniority, experience_years, score, highlights.
    """
```

### Algoritmo merge lexical + vector

```
results_lex  = client.search(search_text=query, filter=odata_filter, top=top*3, highlight_fields=...)
results_vec  = client.search(search_text=None,  vector_queries=[VectorizedQuery(...)], top=top*3)

merged = {}
for hit in results_lex:
    merged[hit["document_id"]] = {"lex_score": hit["@search.score"], **hit}
for hit in results_vec:
    if hit["document_id"] in merged:
        merged[hit["document_id"]]["vec_score"] = hit["@search.score"]
    else:
        merged[hit["document_id"]] = {"vec_score": hit["@search.score"], **hit}

# score composito calcolato nel reranker (Phase E)
```

### Checklist

- [ ] Aggiungere `create_or_update_index()` in `infra/search_service.py`
- [ ] Aggiungere `search_chunks()` con merge lexical+vector in `infra/search_service.py`
- [ ] Aggiungere `azure-search-documents>=11.6` in `requirements.txt` se mancante (per `VectorizedQuery`)
- [ ] Aggiungere unit tests con fake search client

---

## Phase E — HTTP API `/api/search` (`function_app.py`)

> **Dipendenze:** Phase D.

### Struttura richiesta

```json
POST /api/search
{
  "query": "sviluppatore python azure",
  "skills": ["python", "azure"],
  "role": "software engineer",
  "location": "Milano",
  "seniority": "senior",
  "min_experience_years": 5,
  "max_experience_years": null,
  "language": "it",
  "availability_required": false,
  "top": 10,
  "hybrid": true,
  "subco": null
}
```

### Struttura risposta

```json
{
  "hits": [
    {
      "document_id": "mario-rossi",
      "full_name": "Mario Rossi",
      "role": "Senior Software Engineer",
      "location": "Milano, Italia",
      "skills": ["python", "azure", "docker"],
      "seniority": "senior",
      "experience_years": 10.5,
      "score": 0.87,
      "highlights": { "content": ["...azure <em>python</em>..."] },
      "source_path": "/incoming-cv/mario-rossi.docx",
      "version": 1
    }
  ],
  "meta": {
    "total": 3,
    "top": 10,
    "relaxed": false,
    "hybrid": true,
    "index": "cv-doc-chunks"
  },
  "suggestions": ["azure developer", "cloud engineer"]
}
```

### Flusso interno dell'handler

```
1. Validazione e normalizzazione input
   - lowercase skills, trim, deduplica
   - default: top=10, hybrid=true

2. Routing indice per subco
   - "risorse"   → settings.search_subco_risorse_index
   - "candidati" → settings.search_subco_candidati_index
   - null        → settings.document_search_index_name (default)

3. Build filtri OData (hard constraints)
   - skills:       skills/any(s: s eq 'python') and skills/any(s: s eq 'azure')
   - seniority:    seniority eq 'senior'
   - experience:   experience_years ge 5
   - language:     language eq 'it'

4. Build query aumentata per embedding
   augmented = f"{query} {' '.join(skills)} {role or ''} {seniority or ''}"
   embedding = await embedding_fn(augmented)   # solo se hybrid=true

5. Ricerca ibrida via SearchService.search_chunks
   Ottieni lista hit grezza (lexical + vector merge)

6. Reranking (scoring custom)
   score = lex_score  * 0.40
         + vec_score  * 0.60
         + Σ(0.10 per skill che matcha nel candidato)
         + 0.05 se role contiene keyword richiesta
         + 0.05 se location corrisponde
         + 0.02 se processed_at < 6 mesi
   Ordina per score DESC, prendi top N

7. Fallback relaxation (se len(hits) < ceil(top * 0.2))
   - Rimuove skill filter, mantiene seniority + experience
   - Re-esegue search_chunks
   - Appende risultati mancanti, setta meta.relaxed = true
   - Aggiunge campo "suggestions" con skill simili non trovate

8. Risposta finale
   - Ritorna hits + meta + suggestions
   - Highlight preservati dall'API Search
```

### Configurazione aggiuntiva in `core/config.py`

```python
search_subco_risorse_index: str = "cv-risorse-chunks"
search_subco_candidati_index: str = "cv-candidati-chunks"
search_reranker_lex_weight: float = 0.40
search_reranker_vec_weight: float = 0.60
search_reranker_skill_boost: float = 0.10
search_reranker_role_boost: float = 0.05
search_reranker_location_boost: float = 0.05
search_reranker_recency_boost: float = 0.02
search_fallback_threshold: float = 0.20    # se hits < top * threshold → relaxation
```

### Checklist

- [ ] Aggiungere `POST /api/search` handler in `function_app.py`
- [ ] Implementare build filtri OData in funzione helper separata
- [ ] Implementare reranker come funzione pura (testabile isolatamente)
- [ ] Implementare fallback relaxation
- [ ] Aggiungere configurazione `subco` + pesi reranker in `core/config.py`
- [ ] Unit tests per: OData builder, reranker, fallback, handler con fake SearchService

---

## Phase F — Test copertura nuove fasi

- [ ] `tests/test_schema_normalized.py` — NormalizedCVMetadata serialization/validation
- [ ] `tests/test_processor_front_matter.py` — front matter esteso con candidato completo
- [ ] `tests/test_indexer_extended.py` — chunk con metadati candidato + embedding injection
- [ ] `tests/test_search_service.py` — search_chunks mock, create_or_update_index no-op
- [ ] `tests/test_search_api.py` — handler POST /api/search con fake search service
- [ ] `tests/test_reranker.py` — scoring formula, fallback threshold logic

---

## Phase 6 - Reliability hardening

- [x] Provision DLQ queues in Dev
- [ ] Implement DLQ output bindings in function handlers
- [ ] Add bounded retry policy in processing/indexing
- [ ] Add structured events (`enqueued`, `processing_start`, `skipped`, `processed`, `indexed`, `failed`)
- [ ] Validate poison message flow to DLQ
 - [ ] Add structured logging context and telemetry for pipeline steps (image_enrichment, llm call, registry events, upload)

## Phase 7 - Test plan

- [ ] Unit tests: registry decision logic
- [ ] Unit tests: markdown generation
- [ ] Unit tests: chunking deterministic ids
- [ ] Integration tests: upload -> queue -> markdown blob
- [ ] Integration tests: modified upload -> version increment + reindex
- [ ] Smoke test end-to-end on Dev sample PDFs

Additional tests recommended:
- [ ] Integration test: image enrichment uploads and image description present in output
- [ ] Integration test: registry lifecycle (register → processed/failed) when running under Functions host
- [ ] Backfill test: end-to-end bootstrap that performs real enqueue and validates registry entries

---

## File roadmap

### New files

| File | Stato | Fase |
|---|---|---|
| `domain/document_elements.py` | ✅ | Phase 3 |
| `services/document_parser.py` | ✅ | Phase 3 |
| `services/document_processor.py` | ✅ | Phase 3-4 |
| `services/document_indexer.py` | ✅ | Phase 5 |
| `infra/document_registry.py` | ✅ | Phase 2 |
| `infra/backfill_enqueuer.py` | ✅ | Phase 0.5 |
| `core/schema.py` → `NormalizedCVMetadata` + sub-models | ⬜ | **Phase A** |
| `tests/test_schema_normalized.py` | ⬜ | **Phase F** |
| `tests/test_processor_front_matter.py` | ⬜ | **Phase F** |
| `tests/test_indexer_extended.py` | ⬜ | **Phase F** |
| `tests/test_search_service.py` | ⬜ | **Phase F** |
| `tests/test_search_api.py` | ⬜ | **Phase F** |
| `tests/test_reranker.py` | ⬜ | **Phase F** |

### Existing files to update

| File | Stato | Fase |
|---|---|---|
| `function_app.py` | ✅ (trigger+backfill) → ⬜ (POST /api/search) | Phase E |
| `core/config.py` | ✅ (env alias) → ⬜ (subco indexes + reranker weights) | Phase E |
| `infra/search_service.py` | ✅ (upsert/delete) → ⬜ (create_or_update_index, search_chunks) | Phase D |
| `services/document_processor.py` | ✅ → ⬜ (front matter esteso da NormalizedCVMetadata) | Phase B |
| `services/document_indexer.py` | ✅ → ⬜ (metadati candidato nei chunk, index_async, embedding) | Phase C |
| `infra/llm_client.py` | ✅ → ⬜ (get_embedding_client) | Phase C |
| `requirements.txt` | ⬜ (azure-search-documents>=11.6 se mancante) | Phase D |
| `local.settings.json` | ✅ | — |

### Ordine di implementazione consigliato

```
Phase A  →  Phase B  →  Phase C  →  Phase D  →  Phase E  →  Phase F
(schema)    (processor)  (indexer)   (search)    (api)       (tests)
```

Ogni fase è bloccante per la successiva; Phase F può procedere in parallelo per le fasi A-D completate.
4. Add language inference and element ordering/OCR deduplication
5. Wire `document_indexer` and chunking, then add tests and run a real backfill

---

## Definition of done

 - [x] Upload + extract path stores originals in `incoming-cv-originals`
 - [ ] Upload PDF in `incoming-cv` triggers pipeline automatically
 - [ ] Existing backlog can be enqueued with bootstrap and processed safely
 - [x] Same file unchanged is skipped by registry hash
 - [x] Updated file increments version and old chunks are deleted (upsert pending)
 - [x] Normalized markdown is written to `normalized-cv-md`
 - [ ] Search index `cv-doc-chunks` serves latest chunks for retrieval
 - [ ] End-to-end correlation and DLQ diagnostics are available



Additional acceptance criteria:

- Pipeline performs `register` and `mark_status` calls so registry reflects processing lifecycle.
- Images extracted from documents are uploaded to a dedicated container and referenced from the markdown output.
- Markdown blobs include YAML front matter with required metadata and blob metadata set.
- Language and element ordering improvements are applied and validated in unit tests.
- Backfill run (non-dry-run) produces registry entries for processed blobs.


