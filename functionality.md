# FlashCV — Architettura e Funzionalità

## 📌 Obiettivo del progetto

FlashCV è un sistema serverless su Azure per:

* caricare CV (PDF/DOCX/TXT)
* estrarre automaticamente i dati tramite LLM (Azure OpenAI + LangChain)
* normalizzare e arricchire le informazioni
* salvare i risultati su database
* indicizzare i CV per ricerca (Search Index)

Il sistema è progettato per:

* **scalare automaticamente**
* **pagare solo per uso**
* **essere completamente async**
* **separare dominio, infrastruttura e API**

---

# 🧱 Architettura generale

```
Client → HTTP Function (extract)
      → Pipeline CV
      → Cache Blob
      → Azure OpenAI (LLM)
      → Queue persist
          → DB Function (upsert)
          → Queue index
              → Search Function
```

---

# ⚙️ Componenti principali

## 1️⃣ HTTP Function — `extract`

**Responsabilità**

* riceve CV via POST
* valida dimensione e input
* chiama pipeline di estrazione
* restituisce JSON standard

**Output envelope**

```json
{
  "data": {...},
  "error": null,
  "request_id": "..."
}
```

---

## 2️⃣ Pipeline CV (`CVPipeline`)

Flusso:

```
bytes
→ SHA256 hash
→ cache lookup testo
→ estrazione testo
→ cache save testo
→ cache lookup JSON LLM
→ chiamata LLM (LangChain)
→ mapping dominio
→ enrichment business
→ risultato finale
```

**Responsabilità**

* orchestrazione pura
* nessuna logica Azure/HTTP
* nessun accesso diretto DB

---

## 3️⃣ Text Extraction (`extract.py`)

Supporta:

* PDF → PyMuPDF
* DOCX → python-docx
* TXT → decode utf-8/latin-1

Pulizia:

* trim righe
* rimozione spazi multipli
* newline normalizzati
* limite caratteri sicurezza LLM

---

## 4️⃣ Cache Layer (`TextCache`)

Blob containers:

```
incoming-cv       → file originali
raw-text-cache    → testo estratto + JSON LLM
```

Funzioni:

* `get(hash)` → testo
* `save(hash,text)`
* `get_json(hash)`
* `save_json(hash,json)`

Evita:

* doppie chiamate LLM
* parsing ripetuti

---

## 5️⃣ LLM Chain (`CVExtractionChain`)

Tecnologie:

* LangChain
* Azure OpenAI
* PydanticOutputParser

Funzioni:

* carica prompt da file
* genera output strutturato
* valida schema
* traccia metriche:

```
llm_call_start
llm_processing_ms
llm_call_success
llm_call_error
```

Output:

```
LLMExtractionRaw
```

---

## 6️⃣ Schema dati

### 🔹 RAW LLM

`LLMExtractionRaw`

* solo stringhe
* robusto per il modello
* nessuna logica business

### 🔹 Domain model

`CVExtraction`

* tipizzato
* validato
* pronto DB/API
* include campi calcolati:

```
age
experience_years
seniority
```

---

## 7️⃣ Mapper (`mapper.py`)

Converte:

```
LLMExtractionRaw → CVExtraction
```

Operazioni:

* trim stringhe
* parsing date ISO
* costruzione WorkExperience

Nessun enrichment.

---

## 8️⃣ Postprocess (`postprocess.py`)

Arricchimenti:

* normalizzazione skills
* calcolo età
* anni esperienza
* determinazione seniority

---

# 🗄️ Persistenza DB

## Queue Function — `persist_candidate`

Trigger:

```
queue: cv-persist
```

Responsabilità:

* leggere payload
* costruire CVExtraction
* upsert DB

Semantica:

```
1 persona = 1 riga
match_key UNIQUE
```

Se:

* esiste → UPDATE campi cambiati
* non esiste → INSERT

---

## Repository (`CandidateRepository`)

Operazioni:

```
upsert(match_key, cv)
```

Supporta:

* insert nuovi record
* update parziale record esistenti

Nessuna logica business.

---

# 🔎 Search Index

## Queue Function — `index_candidate`

Trigger:

```
queue: cv-index
```

Responsabilità:

* riceve CV dal persist
* aggiorna Azure Search Index

Pipeline:

```
extract → persist → index
```

---

# 📦 Storage

## Blob Storage

| Container      | Uso               |
| -------------- | ----------------- |
| incoming-cv    | upload temporaneo |
| raw-text-cache | testo e JSON LLM  |

Accesso:

* Managed Identity in Azure
* Azure CLI login in locale

---

# 📡 Observability

Modulo: `observability.py`

Supporta:

* eventi custom
* metriche
* durata operazioni
* correlazione tramite request_id

Esempi:

```
http_request_start
cache_hit_text
llm_call_success
db_upsert_ms
```

---

# 🧾 Request Context

Modulo: `request_context.py`

Gestisce:

```
ContextVar request_id
```

Permette:

* propagazione automatica
* logging coerente
* tracing cross-service

---

# 🌐 HTTP Layer

## Error handler decorator

`@http_error_handler`

Gestisce:

* envelope JSON standard
* mapping errori dominio → HTTP
* tracking eventi
* request duration

Error mapping:

| Errore              | HTTP |
| ------------------- | ---- |
| InvalidInputError   | 400  |
| FileTooLargeError   | 413  |
| TextExtractionError | 422  |
| LLMProcessingError  | 502  |
| CVError             | 500  |

---

# 🧪 Testing

Test previsti:

* hashing deterministico
* parsing file
* mapper date
* enrichment logico
* LLM chain mock
* pipeline completa senza Azure
* queue function simulate

---

# 🚀 Deployment

Componenti Azure:

* Function App (Flex Consumption)
* Blob Storage
* Azure OpenAI
* Storage Queue
* MySQL
* Search Index
* Application Insights

---

# 🎯 Principi architetturali

* **Serverless first**
* **Async everywhere**
* **Core indipendente da Azure**
* **LLM output sempre validato**
* **Idempotenza tramite SHA256**
* **Cache per ridurre costi LLM**
* **Event-driven via Queue**
* **Observability built-in**

---

# 📌 Stato attuale

✔ pipeline completa
✔ caching blob
✔ LLM extraction
✔ enrichment business
✔ DB upsert via queue
✔ search indexing pipeline
✔ error handling standard
✔ request tracing

---

# 🔮 Possibili evoluzioni

* deduplicazione CV multi-upload
* matching candidato-posizione
* embeddings + semantic search
* scoring CV automatico
* dashboard analytics
* feedback loop per prompt tuning

---

**FlashCV — Serverless CV Intelligence Pipeline**
