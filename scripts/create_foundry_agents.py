from __future__ import annotations

import argparse
import json
import os
from textwrap import dedent
from typing import Any
from urllib.parse import parse_qs, urlparse


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def _require(value: str | None, message: str) -> str:
    if value:
        return value
    raise SystemExit(message)


def _build_classifier_instructions() -> str:
    return dedent(
        """
                Sei il Request Interpreter per il sistema di matching richiesta cliente > DB MC Flash.

                Il tuo ruolo NON e' eseguire direttamente la ricerca nel database.
                Il tuo compito e':
                1) Interpretare la richiesta utente
                2) Estrarre i segnali rilevanti
                3) Costruire una richiesta strutturata
                4) Decidere la strategia di ricerca
                5) Chiamare il Search Agent
                6) Valutare la qualita' dei risultati
                7) Eventualmente rilanciare una ricerca piu' ampia
                8) Chiamare il Match Evaluator
                9) Restituire una risposta finale chiara e coerente
                10) Se l'utente chiede dettagli specifici su un profilo, usa il tool DB read-only per recuperare il dettaglio candidato

                REGOLE BUSINESS MC
                - Subco/P.IVA = si -> subco = "risorse"
                - Subco/P.IVA = no -> subco = "candidati"
                - Se non specificato, NON bloccare subito la ricerca se esistono gia' segnali sufficienti

                Priorita' segnali:
                1. Sede
                2. Skills
                3. Ruolo
                4. Lingue
                5. Disponibilita'

                Gestione sede:
                - onsite/hybrid -> location alta priorita'
                - remote -> location NON restrittiva
                - work_mode unknown -> location come segnale debole

                Skills:
                - massimo 2-3 skill principali
                - priorita' maggiore del ruolo

                Seniority:
                - Se la seniority e' esplicita (junior, mid, senior, lead, principal), valorizza `seniority`.
                - Se la richiesta esprime anni di esperienza, valorizza preferibilmente `min_experience_years` e/o `max_experience_years`.
                - Se la seniority e' incerta o sfumata (es. "non troppo senior", "profilo con esperienza ma non senior"), NON forzare una label rigida: traduci la richiesta in un vincolo di anni esperienza, preferendo `max_experience_years` o un range.
                - Se nella query compaiono sia seniority sia anni esperienza e sono potenzialmente incoerenti, dai priorita' agli anni esperienza come segnale strutturato principale.
                - Se la seniority non e' chiaramente espressa, non inventare un livello rigido solo per completare il payload.

                Disponibilita': solo se richiesta esplicitamente.
                Lingue: chiarimenti solo se discriminanti.

                REGOLE OPERATIVE
                - NON fare gating rigido.
                - La ricerca parte con almeno 2 segnali utili tra skill, ruolo, location, seniority, lingua.
                - Se i segnali utili sono meno di 2: `needs_clarification = true` e NON chiamare tool.

                ORCHESTRAZIONE OBBLIGATORIA (quando needs_clarification=false)
                1. Costruisci payload strutturato
                2. Chiama `invoke_searcher_wrapper`
                3. Se necessario esegui step relaxed
                4. Prepara `candidates_compact` (ridotti)
                5. Chiama `invoke_match_evaluator` (valutazione deterministica: score, coverage, recovery_strategy)
                6. Applica recovery_strategy (vedi sezione dedicata)
                7. Componi la risposta finale in testo naturale
                8. Chiama `invoke_response_judger` con `original_request` e `final_answer` (testo che stai per inviare)
                9. Se `compatible=false` o `verdict=mismatch`: rivedi la risposta e rispondi diversamente
                10. Invia la risposta finale all'utente

                Chiamata tool: formato obbligatorio
                                - Entrambi i tool ricevono payload JSON nel body POST (application/json), NON in query string.
                                - Per `invoke_searcher_wrapper`: body con almeno `search_request`.
                                - Per `invoke_match_evaluator`: body con almeno `original_request`, `interpreted_request`, `candidates` (oppure `search_response`).
                                - Per `invoke_response_judger`: body con `original_request` (testo richiesta utente) e `final_answer` (testo risposta che stai per inviare).
                                - Mantieni comunque un payload pulito:
                                    - per ogni candidato includi: `document_id`, `full_name`, `role`, `location`, `skills`, `seniority`, `language`, `retrieval_score`, `source_path`, `match_features`
                                    - `skills` massimo 2 elementi
                                    - NON includere campi pesanti come `content`, `highlights`, `certifications`, testi lunghi

                EVALUATION-DRIVEN RECOVERY (OBBLIGATORIO)

                REGOLA ASSOLUTA: dopo ogni chiamata a `invoke_searcher_wrapper` (con o senza risultati),
                DEVI chiamare `invoke_match_evaluator` prima di generare qualsiasi risposta.
                L'unica eccezione e' needs_clarification=true stabilita PRIMA della ricerca (query insufficiente).

                `invoke_match_evaluator` e' deterministico: calcola score e segnali di recovery senza LLM.
                Usa i suoi output (verdict, recovery_strategy, coverage, relaxation_suggestions, best_candidates)
                per decidere cosa fare.

                VERIFICA FINALE RISPOSTA (OBBLIGATORIA se response_judger disponibile)

                Prima di inviare la risposta all'utente:
                1. Componi il testo finale da inviare (user_message)
                2. Chiama `invoke_response_judger` con:
                   - `original_request`: la richiesta originale dell'utente
                   - `final_answer`: il testo che stai per inviare
                3. Se `compatible=false` o `verdict=mismatch`:
                   - Leggi `issues` e `notes` per capire il problema
                   - Rivedi la risposta per correggere l'incoerenza
                   - NON rieseguire la ricerca: e' un problema di formulazione della risposta, non di dati
                4. Se `compatible=true` o `verdict=ok|partial`: invia la risposta senza modifiche.

                `invoke_response_judger` NON rifà il ranking ne' valuta i candidati:
                verifica solo se il tuo testo risponde adeguatamente alla domanda posta.
                - verdict              — qualita' globale del match
                - failure_type         — causa strutturata del problema (es. poor_skill_coverage, location_mismatch)
                - recovery_strategy    — azione obbligatoria da eseguire (vedi tabella sotto)
                - improved_queries     — query alternative per retry automatico
                - missing_entities     — informazioni mancanti nella query utente
                - needs_clarification  — booleano
                - clarifying_questions — domande suggerite da porre all'utente
                - coverage             — copertura per dimensione (skills/role/location/seniority/language): high|medium|low|unknown
                - critical_gaps        — lista gap critici identificati dal judge
                - relaxation_suggestions — dimensioni da rilassare in caso di retry (es. ["location", "seniority"])
                - best_candidates      — lista ordinata dei migliori profili con why_fit e risk gia' scritti dal judge
                - candidate_evaluations — valutazioni complete per profilo (strengths, weaknesses, missing_requirements)

                ## Regola prioritaria: needs_clarification sovrasta il recovery

                - Se needs_clarification=true E recovery_strategy NON e' RETURN_ANSWER:
                  → Poni all'utente UNA SOLA domanda (primo elemento di clarifying_questions).
                  → Non ritentare la ricerca: la causa e' che la query e' ambigua o incompleta.
                  → Prefer la chiarificazione rispetto al recovery automatico quando la query e' ambigua.

                - Se needs_clarification=true E recovery_strategy=RETURN_ANSWER:
                  → Rispondi normalmente (il match e' passato, il flag puo' essere ignorato).

                - Se needs_clarification=false:
                  → Applica il recovery_strategy come descritto di seguito.

                ## Recovery behavior

                1. RETURN_ANSWER
                   → Verdict strong_match o partial_match: genera la risposta finale normale.

                2. RELAX_AND_RETRY
                   → Richiama `invoke_searcher_wrapper` con criteri piu' ampi:
                     - usa `relaxation_suggestions` del valutatore per sapere cosa rilassare
                     - usa `coverage` per capire quale dimensione e' piu' carente (es. coverage.location = "low" → rilassa location)
                     - aggiungi `relaxed_criteria` nel payload (es. ["location", "seniority"])
                     - usa `improved_queries` se disponibili come nuova query
                   → Poi chiama di nuovo `invoke_match_evaluator` sul nuovo risultato.

                3. REWRITE_QUERY
                   → Richiama `invoke_searcher_wrapper` usando gli `improved_queries` del valutatore come query.
                   → Non riscrivere ulteriormente la query del valutatore.
                   → Poi chiama di nuovo `invoke_match_evaluator`.

                4. ASK_USER_CLARIFICATION
                   → Poni all'utente UNA SOLA domanda (usa `clarifying_questions`, primo elemento).
                   → Se `missing_entities` e' disponibile, privilegia l'entita' con impatto maggiore sul retrieval.
                   → Non fare piu' domande nello stesso messaggio.

                5. RETURN_PARTIAL_ANSWER
                   → Restituisci solo i candidati supportati (anche se pochi o con gap).
                   → Indica esplicitamente cosa manca o perche' il match e' parziale.

                6. SAFE_REFUSAL
                   → Comunica chiaramente che non ci sono profili coerenti.
                   → Suggerisci di riformulare la richiesta o ampliare i criteri.

                ## Loop di recovery sicuro

                - Massimo 1 retry di ricerca automatico (RELAX_AND_RETRY o REWRITE_QUERY).
                - Dopo un retry: se il nuovo verdict e' ancora no_match o weak_match con recovery!=RETURN_ANSWER,
                  applica RETURN_PARTIAL_ANSWER o ASK_USER_CLARIFICATION — non ritentare ulteriormente.
                - Non entrare mai in loop infiniti di ricerca.

                ## Minimizzazione delle chiarificazioni

                Non chiedere all'utente se il problema puo' essere risolto con retry automatico
                (RELAX_AND_RETRY o REWRITE_QUERY). Preferisci il recovery automatico prima di interrompere
                l'utente con una domanda.

                OUTPUT FINALE (SOLO TESTO PER L'UTENTE)
                - Restituisci solo testo naturale in italiano.
                - NON restituire JSON.
                - NON mostrare payload, request/response tecniche o debug.
                - Se hai trovato candidati, struttura il testo in modo leggibile:
                  1) breve sintesi iniziale
                  2) Match coerenti
                  3) Potrebbero interessarti anche (se presenti)
                - Per ogni profilo usa DIRETTAMENTE i campi del valutatore:
                  - nome, ruolo, location da `best_candidates` o `candidate_evaluations`
                  - motivazione: usa `why_fit` cosi' com'e' (non riscrivere)
                  - caveat/rischio: aggiungi `risk` se presente e non nullo
                  - per match parziali puoi citare `missing_requirements` o `weaknesses` in modo sintetico
                - NON riscrivere o parafrasare le motivazioni gia' generate dal valutatore.
                - Dopo aver composto la risposta, chiama `invoke_response_judger` prima di inviarla.
                - Se `needs_clarification=true`, fai solo una domanda mirata in testo naturale.

                Accesso DB read-only (quando disponibile tool):
                - Usa `invoke_db_candidates_lookup` solo per cercare o dettagliare candidati gia' persistiti.
                - Usa `match_key` (o email) per il dettaglio puntuale.
                - Non inventare campi: usa solo i dati restituiti dall'endpoint DB.

                Regole di coerenza output (vincolanti):
                - skills sempre lowercase
                - non inventare vincoli
                - non usare location come filtro rigido se work_mode unknown
                - se needs_clarification=true: NON chiamare tool
                - se needs_clarification=false: devi chiamare realmente i tool e basarti sulle risposte
                - NON esporre nel messaggio finale strutture JSON interne

                RISPOSTA FINALE
                - In italiano
                - Deve sempre contenere `final_answer.user_message` con testo naturale (2-5 frasi), non solo liste
                - Due sezioni: Match coerenti, Potrebbero interessarti anche
                - Spiega eventuali criteri rilassati
                - Evita output rumorosi

                PRINCIPIO GUIDA:
                se esiste abbastanza segnale utile, prova prima la ricerca.
        """
    ).strip()


def _build_search_instructions() -> str:
    return dedent(
        """
          Sei il Search Agent del sistema MC Flash.

          Il tuo ruolo e' eseguire retrieval di profili candidati tramite il tool di search disponibile.

          NON devi:
          - interpretare richieste utente libere
          - fare domande
          - decidere business policy conversazionali
          - valutare qualitativamente i candidati
          - generare ranking finale semantico
          - fare reasoning complesso

          Il tuo compito e':
          1. Ricevere una richiesta strutturata
          2. Costruire la query di ricerca
          3. Chiamare il tool search
          4. Applicare filtri e pesi richiesti
          5. Restituire risultati ordinati
          6. Supportare strict e relaxed search

          INPUT

          Ricevi sempre un payload strutturato.
          Esempio:
          {
             "query": "backend developer java spring",
             "skills": ["java", "spring"],
             "role": "backend developer",
             "location": "milano",
             "work_mode": "hybrid",
             "subco": "candidati",
             "top": 10,
             "strict": true,
             "relaxed_criteria": []
          }

          Contratto input operativo:
          - query: string (obbligatoria)
          - skills: array string (opzionale)
          - role: string|null (opzionale)
          - location: string|null (opzionale)
          - work_mode: remote|hybrid|onsite|unknown (opzionale, default unknown)
          - subco: risorse|candidati|null (opzionale)
          - top: integer 1..20 (opzionale, default 10)
          - strict: boolean (opzionale, default true)
          - relaxed_criteria: array tra [availability, languages, role, location] (opzionale)
          - availability_required: boolean (opzionale)
          - language: string|null (opzionale)

          REGOLE SEARCH

          Routing dataset:
          - subco = "risorse"
          - subco = "candidati"

          Gestione location:
          - onsite/hybrid: location alta priorita'
          - remote: NON usare location come filtro restrittivo
          - work_mode unknown: usa location come segnale debole

          Skills:
          - Le skills sono il segnale piu' importante
          - Dai maggiore peso alle skill rispetto al ruolo

          Ruolo:
          - Segnale secondario
          - Usato per affinare ranking e retrieval

          Lingue:
          - Applica filtri lingua solo se esplicitamente presenti

          Disponibilita':
          - Applica solo se richiesta

          STRICT VS RELAXED SEARCH

          STRICT SEARCH:
          - usa tutti i criteri ricevuti
          - massima precisione

          RELAXED SEARCH:
          - ignora i criteri presenti in relaxed_criteria
          - esempio: ["location"] -> non usare location come filtro forte

          NON decidere autonomamente cosa rilassare.
          Usa solo cio' che ricevi nel payload.

          COMPORTAMENTO

          Devi:
          - eseguire retrieval
          - massimizzare pertinenza
          - evitare rumore eccessivo
          - restituire risultati consistenti

          NON devi:
          - chiedere chiarimenti
          - bloccare la ricerca
          - inferire requisiti mancanti
          - modificare il payload ricevuto

          OUTPUT

                    Restituisci SOLO JSON valido.
          Formato:
          {
             "strategy": "strict|relaxed",
             "total_results": 0,
             "applied_filters": {
                "skills": ["java", "spring"],
                "location": "milano",
                "role": "backend developer"
             },
             "ignored_filters": [],
             "results": [
                {
                  "candidate_id": "123",
                  "name": "Mario Rossi",
                  "role": "Java Developer",
                  "location": "Milano",
                  "skills": ["java", "spring boot", "kafka"],
                  "availability_days": 15,
                  "language": "it",
                  "retrieval_score": 0.81,
                                    "source_path": "/profiles/mario_rossi.pdf",
                                    "match_features": {
                                        "skills": {
                                            "requested": ["java", "spring", "docker"],
                                            "matched": ["java"],
                                            "semantic_matches": ["spring boot"],
                                            "missing": ["docker"]
                                        },
                                        "role": {
                                            "requested": "backend developer",
                                            "candidate": "java developer",
                                            "score": 0.74
                                        },
                                        "location": {
                                            "requested": "milano",
                                            "candidate": "milano",
                                            "match": "exact"
                                        },
                                        "language": {
                                            "requested": "english",
                                            "candidate": "b2",
                                            "match": true
                                        },
                                        "relaxed_criteria": [],
                                        "matched_on": ["skills", "role", "location"]
                                    }
                }
             ]
          }

          Coerenza output con il giro:
          - strategy = "strict" se strict=true e relaxed_criteria vuoto
          - strategy = "relaxed" se relaxed_criteria non vuoto o strict=false
          - applied_filters deve includere solo filtri effettivamente applicati
          - ignored_filters deve riflettere relaxed_criteria effettivamente ignorati
          - retrieval_score in [0.0, 1.0]
          - ordinamento results per retrieval_score decrescente

          RETRIEVAL SCORE

          Il retrieval_score rappresenta:
          - similarita' query/profilo
          - compatibilita' metadata
          - ranking retrieval

          NON rappresenta il match finale business.
          Il Match Evaluator si occupera' della valutazione finale usando soprattutto `match_features`.

          REQUISITO CRITICO:
          - Ogni risultato DEVE includere `match_features` completi e coerenti.
          - NON lasciare `match_features` vuoto quando sono presenti segnali nel profilo.

          VINCOLI IMPORTANTI

          NON:
          - fare explainability business
          - classificare il match finale
          - inventare dati mancanti
          - alterare il payload
          - trasformare il retrieval in reasoning conversazionale

          Tu sei un motore di retrieval strutturato.
        """
    ).strip()


def _build_evaluator_instructions() -> str:
        return dedent(
                """
                Sei il Match Evaluator del sistema MC Flash.

                Il tuo compito NON e' eseguire la ricerca.
                Ricevi:
                - la richiesta originale del cliente
                - il payload interpretato
                - i profili restituiti dal motore di ricerca

                Devi classificare la qualita' del match tra richiesta e candidato.

                Il tuo obiettivo e':
                - spiegare perche' un candidato e' coerente o meno
                - assegnare un match score realistico
                - evidenziare gap o mismatch
                - separare match forti da match estesi

                INPUT CANONICO ATTESO:
                {
                    "original_request": "string",
                    "interpreted_request": { ... },
                    "candidates": [ ... ]
                }

                Dove:
                1) original_request = richiesta originale utente
                2) interpreted_request = payload strutturato interpretato
                3) candidates = lista candidati trovati

                Fallback compatibilita':
                - Se `candidates` non e' presente ma arriva `search_response.data.hits`, usa `search_response.data.hits` come sorgente candidati.
                - Se sono presenti entrambi, usa `candidates` come fonte primaria e `search_response.data.hits` solo come supporto.

                Regole di valutazione (priorita'):
                1. Skills
                2. Compatibilita' sede/work mode
                3. Ruolo
                4. Seniority
                5. Lingue
                6. Disponibilita'

                Vincoli di valutazione:
                - Le skills hanno peso maggiore del ruolo.
                - La location e' importante solo per onsite/hybrid.
                - La location NON penalizza fortemente richieste remote.
                - La disponibilita' pesa solo se esplicitamente richiesta.
                - Le lingue pesano solo se richieste.

                MATCH SCORE:
                - Genera un match_score da 0.0 a 1.0.
                - 0.90-1.00: match eccellente
                - 0.75-0.89: match forte
                - 0.55-0.74: match buono ma con gap
                - 0.35-0.54: match debole
                - 0.00-0.34: poco coerente
                - NON assegnare score artificialmente alti.

                MATCH TYPE per candidato:
                - "strong": alta coerenza reale
                - "good": match valido con piccoli gap
                - "weak": match limitato
                - "extended": risultato ottenuto tramite relaxation

                Spiegazione per candidato:
                - reasons
                - missing_requirements
                - strengths
                - weaknesses
                Le motivazioni devono essere sintetiche, concrete e leggibili da recruiter/sales.
                NON inventare informazioni mancanti.

                Gestione relaxation:
                - Se il candidato arriva da ricerca rilassata, valorizza relaxed_criteria (es. ["location"]).

                Se manca sia `candidates` sia `search_response.data.hits`, oppure il payload e' non valido, imposta verdict = "invalid_input" e confidence bassa.

                OUTPUT JSON richiesto (nessun markdown), mantieni questo formato:
                {
                    "verdict": "strong_match|partial_match|weak_match|no_match|invalid_input",
                    "confidence": 0.0,
                    "summary": "stringa breve",
                    "failure_type": "no_matches|poor_skill_coverage|location_mismatch|seniority_mismatch|ambiguous_query|invalid_input|none",
                    "recovery_strategy": "RETURN_ANSWER|RELAX_AND_RETRY|REWRITE_QUERY|ASK_USER_CLARIFICATION|RETURN_PARTIAL_ANSWER|SAFE_REFUSAL",
                    "needs_clarification": false,
                    "clarifying_questions": [],
                    "improved_queries": [],
                    "missing_entities": [],
                    "search_evaluation": {
                        "quality": "excellent|good|fair|poor|insufficient_data",
                        "summary": "stringa breve",
                        "coverage": {
                            "skills": "high|medium|low|unknown",
                            "role": "high|medium|low|unknown",
                            "location": "high|medium|low|unknown",
                            "seniority": "high|medium|low|unknown",
                            "language": "high|medium|low|unknown"
                        },
                        "critical_gaps": ["..."]
                    },
                    "relaxation_suggestions": ["availability", "languages", "role", "skills", "location"],
                    "candidate_evaluations": [
                        {
                            "candidate_id": "string",
                            "full_name": "string",
                            "role": "string|null",
                            "location": "string|null",
                            "match_score": 0.0,
                            "match_type": "strong|good|weak|extended",
                            "why_fit": "string",
                            "risk": "string|null",
                            "matched_on": ["skills", "role", "location"]
                        }
                    ]
                }

                ## Come derivare i segnali di recovery

                ### failure_type
                - "none"                → verdict strong_match o partial_match
                - "poor_skill_coverage" → le skill richieste non sono presenti o scarse nei candidati
                - "location_mismatch"   → location richiesta non coperta dai candidati disponibili
                - "seniority_mismatch"  → seniority/anni di esperienza non coerenti con i candidati
                - "ambiguous_query"     → la query utente e' troppo vaga per restituire risultati coerenti
                - "no_matches"          → nessuna causa identificabile, semplicemente nessun profilo coerente
                - "invalid_input"       → payload mancante o non valido

                ### recovery_strategy
                - "RETURN_ANSWER"           → verdict strong_match (top score ≥ 0.75) o partial_match con buona copertura
                - "RELAX_AND_RETRY"         → verdict weak_match (prima ricerca, non ancora rilassata) o no_match con cause strutturali chiare
                - "REWRITE_QUERY"           → query molto vaga o mal formulata; i candidati esistono ma non vengono raggiunti
                - "ASK_USER_CLARIFICATION"  → ricerca gia' rilassata e ancora no_match, o query ambigua senza segnali sufficienti
                - "RETURN_PARTIAL_ANSWER"   → ricerca gia' rilassata e verdict weak_match: mostra i risultati parziali con caveats
                - "SAFE_REFUSAL"            → invalid_input o zero candidati anche dopo relaxation

                ### needs_clarification
                - true solo se recovery_strategy = ASK_USER_CLARIFICATION

                ### clarifying_questions
                - Genera 1-2 domande concrete basate su failure_type e missing_entities.
                - Usa "tu" (informale), in italiano.
                - Esempio per poor_skill_coverage: "Puoi specificare le skill tecniche prioritarie che cerchi?"
                - Esempio per location_mismatch: "Il profilo deve essere in sede o accetti anche modalita' remota?"
                - Esempio per seniority_mismatch: "Puoi indicare gli anni di esperienza o la seniority che cerchi?"

                ### improved_queries
                - Se recovery_strategy e' RELAX_AND_RETRY o REWRITE_QUERY: genera 1-2 query alternative.
                - Usa le stesse keyword ma con focus diverso (es. solo skill, senza location, ruolo piu' generico).
                - Non inventare skill non presenti nella richiesta originale.

                ### missing_entities
                - Lista delle entita' assenti nell'interpreted_request che avrebbero migliorato il retrieval.
                - Esempi: "skills", "role", "location", "seniority", "language".

                Regola fondamentale:
                - Usa `match_features` del search come fonte primaria.
                - Evita inferenze arbitrarie quando le feature sono disponibili.

                VINCOLI IMPORTANTI:
                - NON fare retrieval.
                - NON modificare la query.
                - NON inventare skill.
                - NON assegnare score casuali.
                - NON promuovere tutti i candidati.
                Il tuo ruolo e' valutare criticamente la qualita' del match.
                """
        ).strip()


def _build_search_openapi_spec(search_url: str) -> dict[str, Any]:
    parsed = urlparse(search_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("SEARCH_API_URL deve essere un URL assoluto verso POST /api/search")

    server_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/api/search"
    query_params = parse_qs(parsed.query)
    function_code = (query_params.get("code") or [None])[0]

    parameters: list[dict] = []
    if function_code:
        parameters.append(
            {
                "name": "code",
                "in": "query",
                "required": True,
                "description": "Function key richiesta dall'endpoint Azure Functions.",
                "schema": {
                    "type": "string",
                    "enum": [function_code],
                    "default": function_code,
                },
            }
        )

    request_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": "Query di ricerca libera o semanticamente arricchita.",
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Massimo 2-3 skill principali.",
            },
            "role": {"type": ["string", "null"]},
            "location": {"type": ["string", "null"]},
            "seniority": {
                "type": ["string", "null"],
                "enum": ["junior", "mid", "senior", "lead", "principal", None],
            },
            "min_experience_years": {"type": ["number", "null"]},
            "max_experience_years": {"type": ["number", "null"]},
            "language": {"type": ["string", "null"]},
            "availability_required": {"type": "boolean", "default": False},
            "top": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            "hybrid": {"type": "boolean", "default": True},
            "subco": {
                "type": ["string", "null"],
                "enum": ["risorse", "candidati", None],
            },
        },
        "required": ["query"],
    }

    spec = {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash Candidate Search",
            "version": "1.0.0",
            "description": "Ricerca profili CV su Azure AI Search via Azure Functions POST /api/search.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            path: {
                "post": {
                    "operationId": "searchCandidates",
                    "summary": "Search candidate profiles",
                    "description": "Esegue la ricerca profili MC Flash applicando i filtri strutturati.",
                    "parameters": parameters,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": request_schema,
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Risultati della ricerca candidati.",
                        }
                    },
                }
            }
        },
    }

    return spec


def _build_searcher_wrapper_openapi_spec(wrapper_url: str) -> dict[str, Any]:
    parsed = urlparse(wrapper_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("FOUNDRY_SEARCHER_WRAPPER_URL deve essere un URL assoluto verso POST /api/searcher-wrapper")

    server_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/api/searcher-wrapper"
    parameters: list[dict] = []

    return {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash Searcher Wrapper",
            "version": "1.0.0",
            "description": "Wrapper API che inoltra il payload classificato al searcher e restituisce i risultati.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            path: {
                "post": {
                    "operationId": "invokeSearcherWrapper",
                    "summary": "Invoke searcher wrapper",
                    "description": "Invoca il wrapper /api/searcher-wrapper con payload JSON nel body POST.",
                    "parameters": parameters,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SearcherWrapperRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Risultato completo con classificazione e search_response.",
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "SearcherWrapperRequest": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "search_request": {
                            "type": "object",
                            "description": "Richiesta strutturata da inoltrare al motore search.",
                            "additionalProperties": True,
                        }
                    },
                }
            }
        },
    }


def _build_evaluator_wrapper_openapi_spec(wrapper_url: str) -> dict[str, Any]:
    parsed = urlparse(wrapper_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("FOUNDRY_EVALUATOR_WRAPPER_URL deve essere un URL assoluto verso POST /api/match-evaluator-wrapper")

    server_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/api/match-evaluator-wrapper"
    parameters: list[dict] = []

    return {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash Match Evaluator Wrapper",
            "version": "1.0.0",
            "description": "Wrapper API che valuta i risultati search tramite il Match Evaluator.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            path: {
                "post": {
                    "operationId": "invokeMatchEvaluator",
                    "summary": "Invoke match evaluator",
                    "description": "Invoca il wrapper del Match Evaluator con payload JSON nel body POST.",
                    "parameters": parameters,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/MatchEvaluatorRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Valutazione match strutturata.",
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "MatchEvaluatorRequest": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "original_request": {"type": "string"},
                        "interpreted_request": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                        "candidates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        "search_response": {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    },
                }
            }
        },
    }


def _build_db_lookup_openapi_spec(db_lookup_url: str) -> dict[str, Any]:
    parsed = urlparse(db_lookup_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("FOUNDRY_DB_LOOKUP_URL deve essere un URL assoluto verso POST /api/db/candidates/details")

    server_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/api/db/candidates/details"

    return {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash DB Candidate Lookup",
            "version": "1.0.0",
            "description": "Lookup read-only candidati nel DB MC Flash via endpoint details.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            path: {
                "post": {
                    "operationId": "lookupCandidateDetails",
                    "summary": "Lookup candidate details",
                    "description": "Recupera il dettaglio candidato con match_key o email.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "match_key": {"type": "string"},
                                        "email": {"type": "string"},
                                        "include_payload": {"type": "boolean", "default": True},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Dettaglio candidato o found=false.",
                        }
                    },
                }
            }
        },
    }

def _build_response_judger_openapi_spec(judger_url: str) -> dict[str, Any]:
    parsed = urlparse(judger_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("FOUNDRY_RESPONSE_JUDGER_URL deve essere un URL assoluto verso POST /api/response-judger")

    server_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/api/response-judger"

    return {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash Response Judger",
            "version": "1.0.0",
            "description": "Verifica la compatibilita' della risposta finale rispetto alla richiesta originale.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            path: {
                "post": {
                    "operationId": "invokeResponseJudger",
                    "summary": "Verify response compatibility",
                    "description": (
                        "Invia la risposta finale e la richiesta originale per verificare la coerenza. "
                        "NON rifa' ranking ne' search. Restituisce compatible, verdict, issues, notes."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ResponseJudgerRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Giudizio di compatibilita' risposta/richiesta.",
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "ResponseJudgerRequest": {
                    "type": "object",
                    "required": ["original_request", "final_answer"],
                    "properties": {
                        "original_request": {
                            "type": "string",
                            "description": "La richiesta originale dell'utente in testo libero.",
                        },
                        "final_answer": {
                            "type": "string",
                            "description": "Il testo della risposta finale generata dal bot.",
                        },
                    },
                }
            }
        },
    }


def _create_definition_payload(model: str, instructions: str, tools: list | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
    }
    if tools:
        payload["tools"] = tools
    return payload


def create_agents(
    project_endpoint: str,
    model: str,
    search_url: str,
    searcher_wrapper_url: str,
    evaluator_wrapper_url: str,
    response_judger_url: str | None,
    db_lookup_url: str | None,
    classifier_agent_name: str,
    search_agent_name: str,
    evaluator_agent_name: str,
    dry_run: bool,
) -> None:
    search_spec = _build_search_openapi_spec(search_url)
    wrapper_spec = _build_searcher_wrapper_openapi_spec(searcher_wrapper_url)
    evaluator_wrapper_spec = _build_evaluator_wrapper_openapi_spec(evaluator_wrapper_url)
    response_judger_spec = _build_response_judger_openapi_spec(response_judger_url) if response_judger_url else None
    db_lookup_spec = _build_db_lookup_openapi_spec(db_lookup_url) if db_lookup_url else None
    classifier_definition = _create_definition_payload(
        model=model,
        instructions=_build_classifier_instructions(),
    )
    classifier_definition["tool_choice"] = "required"
    classifier_definition["temperature"] = 0.0
    search_definition = _create_definition_payload(
        model=model,
        instructions=_build_search_instructions(),
        tools=[
            {
                "type": "openapi",
                "name": "search_candidates_api",
                "description": "Chiama l'endpoint /api/search della pipeline CV_ON_SITE.",
                "spec": search_spec,
            }
        ],
    )
    evaluator_definition = _create_definition_payload(
        model=model,
        instructions=_build_evaluator_instructions(),
    )

    if dry_run:
        preview = {
            "project_endpoint": project_endpoint,
            "model": model,
            "agents": [
                {
                    "name": classifier_agent_name,
                    "type": "classifier",
                    "instructions_preview": classifier_definition["instructions"][:280],
                    "wrapper_url": searcher_wrapper_url,
                    "wrapper_openapi_server": wrapper_spec["servers"][0]["url"],
                    "evaluator_wrapper_url": evaluator_wrapper_url,
                    "evaluator_wrapper_openapi_server": evaluator_wrapper_spec["servers"][0]["url"],
                    "db_lookup_url": db_lookup_url,
                },
                {
                    "name": search_agent_name,
                    "type": "search",
                    "instructions_preview": search_definition["instructions"][:280],
                    "search_url": search_url,
                    "openapi_server": search_spec["servers"][0]["url"],
                },
                {
                    "name": evaluator_agent_name,
                    "type": "evaluator",
                    "instructions_preview": evaluator_definition["instructions"][:280],
                },
            ],
        }
        print(json.dumps(preview, indent=2, ensure_ascii=True))
        return

    try:
        from azure.ai.projects import AIProjectClient
        from azure.ai.projects.models import (
            OpenApiAnonymousAuthDetails,
            OpenApiFunctionDefinition,
            OpenApiTool,
            PromptAgentDefinition,
        )
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise SystemExit(
            "Per creare realmente gli agenti serve installare azure-ai-projects in un ambiente dedicato. "
            "Il runtime principale di questa Function app usa openai<2 tramite langchain-openai, mentre azure-ai-projects richiede openai>=2.8. "
            "Usa un helper venv separato e rilancia questo script senza --dry-run."
        ) from exc

    openapi_tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="search_candidates_api",
            spec=search_spec,
            description="Chiama l'endpoint /api/search della pipeline CV_ON_SITE.",
            auth=OpenApiAnonymousAuthDetails(),
        )
    )
    wrapper_tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="invoke_searcher_wrapper",
            spec=wrapper_spec,
            description="Invoca il wrapper /api/searcher-wrapper con payload classificato.",
            auth=OpenApiAnonymousAuthDetails(),
        )
    )
    evaluator_wrapper_tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="invoke_match_evaluator",
            spec=evaluator_wrapper_spec,
            description="Invoca il wrapper /api/match-evaluator-wrapper con richiesta e candidati. Valutazione deterministica del match.",
            auth=OpenApiAnonymousAuthDetails(),
        )
    )
    response_judger_tool = None
    if response_judger_spec:
        response_judger_tool = OpenApiTool(
            openapi=OpenApiFunctionDefinition(
                name="invoke_response_judger",
                spec=response_judger_spec,
                description="Verifica la compatibilita' della risposta finale con la richiesta originale. NON rifa' ranking. Solo coerenza risposta/domanda.",
                auth=OpenApiAnonymousAuthDetails(),
            )
        )
    db_lookup_tool = None
    if db_lookup_spec:
        db_lookup_tool = OpenApiTool(
            openapi=OpenApiFunctionDefinition(
                name="invoke_db_candidates_lookup",
                spec=db_lookup_spec,
                description="Invoca endpoint DB read-only per dettaglio candidato persistito.",
                auth=OpenApiAnonymousAuthDetails(),
            )
        )
    classifier_definition_obj = PromptAgentDefinition(**classifier_definition)
    classifier_definition_obj.tools = [wrapper_tool, evaluator_wrapper_tool]
    if response_judger_tool is not None:
        classifier_definition_obj.tools.append(response_judger_tool)
    if db_lookup_tool is not None:
        classifier_definition_obj.tools.append(db_lookup_tool)
    search_definition_obj = PromptAgentDefinition(
        model=search_definition["model"],
        instructions=search_definition["instructions"],
        tools=[openapi_tool],
    )
    evaluator_definition_obj = PromptAgentDefinition(**evaluator_definition)

    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=project_endpoint,
        credential=credential,
    ) as project_client:
        classifier_agent = project_client.agents.create_version(
            agent_name=classifier_agent_name,
            definition=classifier_definition_obj,
        )
        search_agent = project_client.agents.create_version(
            agent_name=search_agent_name,
            definition=search_definition_obj,
        )
        evaluator_agent = project_client.agents.create_version(
            agent_name=evaluator_agent_name,
            definition=evaluator_definition_obj,
        )

    result = {
        "classifier_agent": {
            "name": classifier_agent.name,
            "id": classifier_agent.id,
            "version": classifier_agent.version,
        },
        "search_agent": {
            "name": search_agent.name,
            "id": search_agent.id,
            "version": search_agent.version,
        },
        "evaluator_agent": {
            "name": evaluator_agent.name,
            "id": evaluator_agent.id,
            "version": evaluator_agent.version,
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crea tre Foundry agents: classificatore query, search agent e valutatore output per MC Flash.",
    )
    parser.add_argument(
        "--project-endpoint",
        default=_env_first("AZURE_AI_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT", "AZURE_FOUNDRY_PROJECT_ENDPOINT"),
        help="Endpoint del progetto Foundry, es. https://<account>.services.ai.azure.com/api/projects/<project>",
    )
    parser.add_argument(
        "--model",
        default=_env_first("AZURE_AI_MODEL_DEPLOYMENT_NAME", "FOUNDRY_MODEL_DEPLOYMENT_NAME", "AZURE_OPENAI_MODEL", default="gpt-4.1-mini"),
        help="Nome del deployment/model nel progetto Foundry.",
    )
    parser.add_argument(
        "--search-url",
        default=_env_first("FOUNDRY_SEARCH_API_URL", "SEARCH_API_URL"),
        help="URL completo verso POST /api/search. Può includere ?code=<function-key>.",
    )
    parser.add_argument(
        "--searcher-wrapper-url",
        default=_env_first("FOUNDRY_SEARCHER_WRAPPER_URL", "SEARCHER_WRAPPER_URL"),
        help="URL completo verso POST /api/searcher-wrapper. Può includere ?code=<function-key>.",
    )
    parser.add_argument(
        "--evaluator-wrapper-url",
        default=_env_first("FOUNDRY_EVALUATOR_WRAPPER_URL", "EVALUATOR_WRAPPER_URL"),
        help="URL completo verso POST /api/match-evaluator-wrapper. Può includere ?code=<function-key>.",
    )
    parser.add_argument(
        "--response-judger-url",
        default=_env_first("FOUNDRY_RESPONSE_JUDGER_URL", "RESPONSE_JUDGER_URL"),
        help="URL completo verso POST /api/response-judger. Opzionale.",
    )
    parser.add_argument(
        "--db-lookup-url",
        default=_env_first("FOUNDRY_DB_LOOKUP_URL", "DB_LOOKUP_URL"),
        help="URL completo verso POST /api/db/candidates/details. Opzionale.",
    )
    parser.add_argument(
        "--classifier-agent-name",
        default="mc-query-classifier-agent",
        help="Nome logico dell'agente classificatore.",
    )
    parser.add_argument(
        "--search-agent-name",
        default="mc-profile-search-agent",
        help="Nome logico dell'agente di search.",
    )
    parser.add_argument(
        "--evaluator-agent-name",
        default="mc-search-evaluator-agent",
        help="Nome logico dell'agente valutatore dell'output del searcher.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Non crea agenti: stampa solo il payload che verrebbe creato.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_endpoint = _require(
        args.project_endpoint,
        "Manca il project endpoint. Imposta AZURE_AI_PROJECT_ENDPOINT o passa --project-endpoint.",
    )
    search_url = _require(
        args.search_url,
        "Manca la search URL. Imposta FOUNDRY_SEARCH_API_URL o passa --search-url.",
    )
    searcher_wrapper_url = _require(
        args.searcher_wrapper_url,
        "Manca la wrapper URL. Imposta FOUNDRY_SEARCHER_WRAPPER_URL o passa --searcher-wrapper-url.",
    )
    evaluator_wrapper_url = _require(
        args.evaluator_wrapper_url,
        "Manca la evaluator wrapper URL. Imposta FOUNDRY_EVALUATOR_WRAPPER_URL o passa --evaluator-wrapper-url.",
    )
    create_agents(
        project_endpoint=project_endpoint,
        model=args.model,
        search_url=search_url,
        searcher_wrapper_url=searcher_wrapper_url,
        evaluator_wrapper_url=evaluator_wrapper_url,
        response_judger_url=args.response_judger_url,
        db_lookup_url=args.db_lookup_url,
        classifier_agent_name=args.classifier_agent_name,
        search_agent_name=args.search_agent_name,
        evaluator_agent_name=args.evaluator_agent_name,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()