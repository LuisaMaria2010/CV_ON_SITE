# Implementation Plan - CV_ON_SITE Ingestion Pipeline

## Scope

Target flow:

1. Read PDF from `incoming-cv`
2. Convert to markdown
3. Normalize content
4. Index chunks in Azure AI Search

Reference model: AgentIngestionPipeline.

---

## Status snapshot (Dev)

### Azure resources provisioned

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
- [ ] Processor/indexer implementation for markdown + chunks
- [ ] End-to-end tests

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

## Phase 3 - PDF to structured markdown

- [x] Add `domain/document_elements.py`
- [x] Add `services/document_parser.py` (PDF-first)
- [x] Add `services/document_processor.py`
- [x] Implement element ordering (heading/paragraph/list/table)
- [x] Produce deterministic markdown output

## Phase 4 - Normalization and output storage

- [ ] Add normalization pipeline (cleanup/dedup)
- [x] Write markdown into `normalized-cv-md`
- [ ] Add YAML front matter metadata (`document_id`, `source_path`, `version`, `hash`, `processed_at`)
- [x] Validate one processed message -> one `.md` blob

## Phase 5 - Chunking, embeddings, search indexing

- [x] Create Search index `cv-doc-chunks`
- [ ] Add `services/document_indexer.py`
- [ ] Extend `infra/search_service.py` for chunk documents
- [ ] Implement delete old chunks + upsert latest chunks
- [ ] Verify retrieval by `document_id` and latest version

Chunk schema target:

- [x] `id`
- [x] `document_id`
- [x] `version`
- [x] `chunk_index`
- [x] `section`
- [x] `content`
- [x] `source_path`
- [x] `content_hash`
- [x] `processed_at`
- [x] `content_vector` (1536)

## Phase 6 - Reliability hardening

- [x] Provision DLQ queues in Dev
- [ ] Implement DLQ output bindings in function handlers
- [ ] Add bounded retry policy in processing/indexing
- [ ] Add structured events (`enqueued`, `processing_start`, `skipped`, `processed`, `indexed`, `failed`)
- [ ] Validate poison message flow to DLQ

## Phase 7 - Test plan

- [ ] Unit tests: registry decision logic
- [ ] Unit tests: markdown generation
- [ ] Unit tests: chunking deterministic ids
- [ ] Integration tests: upload -> queue -> markdown blob
- [ ] Integration tests: modified upload -> version increment + reindex
- [ ] Smoke test end-to-end on Dev sample PDFs

---

## File roadmap

### New files

- [ ] `domain/document_elements.py`
- [ ] `services/document_parser.py`
- [ ] `services/document_processor.py`
- [ ] `services/document_indexer.py`
- [x] `infra/document_registry.py`
- [x] `infra/backfill_enqueuer.py`
- [ ] `tests/test_document_registry.py`
- [ ] `tests/test_markdown_generation.py`
- [ ] `tests/test_indexer_chunking.py`

### Existing files to update

- [x] `function_app.py`
- [x] `core/config.py`
- [ ] `infra/search_service.py`
- [ ] `infra/blob_storage.py`
- [ ] `requirements.txt`
- [x] `local.settings.json`

---

## Definition of done

- [ ] Upload PDF in `incoming-cv` triggers pipeline automatically
- [ ] Existing backlog can be enqueued with bootstrap and processed safely
- [ ] Same file unchanged is skipped by registry hash
- [ ] Updated file increments version and replaces chunks
- [ ] Normalized markdown is written to `normalized-cv-md`
- [ ] Search index `cv-doc-chunks` serves latest chunks for retrieval
- [ ] End-to-end correlation and DLQ diagnostics are available

