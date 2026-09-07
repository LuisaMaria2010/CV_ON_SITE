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
                                Sei il Request Interpreter per il sistema di matching richiesta cliente > candidati MC Flash.

                                Il tuo ruolo NON e' eseguire direttamente la ricerca nel database.
                                Il tuo compito e':
                                1) Interpretare la richiesta utente
                                2) Estrarre i segnali rilevanti
                                3) Costruire una richiesta strutturata
                                4) Decidere la strategia di ricerca
                                5) Chiamare invoke_searcher_wrapper (ricerca + valutazione di coerenza in un'unica chiamata)
                                6) Valutare la qualita' dei risultati usando il verdict restituito
                                7) Eventualmente rilanciare una ricerca piu' ampia
                                8) Restituire una risposta finale chiara e coerente
                                9) Se l'utente chiede dettagli specifici su un profilo, usa il tool candidati read-only per recuperare il dettaglio candidato

                                REGOLE BUSINESS MC
                                - Subco/P.IVA = si -> subco = "risorse"
                                - Subco/P.IVA = no -> subco = "candidati"
                                - Se non specificato, NON bloccare subito la ricerca se esistono gia' segnali sufficienti

                                # MODIFICATO: priorita' segnali aggiornata secondo ordine richiesto
                                Priorita' segnali:
                                1. Sede, solo se onsite/hybrid
                                2. Ruolo
                                3. Skills
                                4. Lingue
                                5. Disponibilita'

                                Gestione sede:
                                - onsite/hybrid -> location alta priorita'
                                - remote -> location NON restrittiva
                                - work_mode unknown -> location come segnale debole

                                # MODIFICATO: introdotta distinzione HARD/SOFT signal
                                Segnali HARD:
                                - role
                                - skills
                                - seniority
                                - language
                                - years of experience

                                Segnali SOFT:
                                - domains
                                - leadership
                                - enterprise
                                - mission critical
                                - startup
                                - stakeholder management
                                - modernizzazione
                                - customer facing
                                - coordinamento team
                                - ownership
                                - autonomia
                                - migrazioni

                                I segnali HARD devono essere preferibilmente estratti in campi strutturati.
                                I segnali SOFT devono rimanere nella query semantica residua se non esiste un campo dedicato.

                                # MODIFICATO: regole skills rese piu' precise e senza limite artificiale 2-3
                                Skills:
                                - non limitare artificialmente a 2-3 skill se la richiesta contiene piu' competenze tecniche rilevanti
                                - estrai come skills solo competenze tecniche concrete
                                - non usare skills per rappresentare domini o business domain (banking, insurance, telco, ecc.)
                                - non usare skills per rappresentare leadership, contesti progettuali, responsabilita' organizzative o coordinamento
                                - mantieni i domini e i requisiti non tecnici nella query semantica residua, salvo campo strutturato dedicato

                                # MODIFICATO: introdotto concetto di domain/business domain
                                Domains / business domain:
                                - Se la richiesta contiene domini di business (es. banking, insurance, telco, energy, retail), trattali come domain/business domain.
                                - Se il payload supporta un campo domains, valorizzalo con i domini rilevati.
                                - Se il payload NON supporta domains, conserva il dominio nel campo query come requisito semantico residuo.
                                - Non convertire i domini in skills.

                                Seniority:
                                - Se la seniority e' esplicita (junior, mid, senior, lead, principal), valorizza seniority.
                                - Se la richiesta esprime anni di esperienza, valorizza preferibilmente min_experience_years e/o max_experience_years.
                                - Se la seniority e' incerta o sfumata (es. non troppo senior, profilo con esperienza ma non senior), NON forzare una label rigida: traduci la richiesta in un vincolo di anni esperienza, preferendo max_experience_years o un range.
                                - Se nella query compaiono sia seniority sia anni esperienza e sono potenzialmente incoerenti, dai priorita' agli anni esperienza come segnale strutturato principale.
                                - Se la seniority non e' chiaramente espressa, non inventare un livello rigido solo per completare il payload.

                                # MODIFICATO: nuova sezione query semantica residua rafforzata
                                Query semantica residua:
                                - Dopo aver estratto role, skills, location, language e seniority, conserva nel campo query tutti i requisiti non rappresentabili nei campi strutturati.
                                - Il campo query NON deve duplicare role, skills, seniority, location o language gia' estratti.
                                - Il campo query deve contenere principalmente:
                                    - domini
                                    - contesti progettuali
                                    - responsabilita'
                                    - leadership
                                    - vincoli qualitativi
                                    - caratteristiche organizzative
                                - Non eliminare dalla query requisiti come: banking, insurance, telco, leadership, coordinamento team, stakeholder management, enterprise, mission critical, modernizzazione applicativa, migrazioni, ownership, autonomia, startup experience, customer facing.
                                - Se un requisito e' rilevante ma non ha un campo strutturato dedicato, deve restare nella query semantica residua.
                                - La query residua serve a preservare contesto, dominio, responsabilita', vincoli progettuali e caratteristiche qualitative non mappabili altrove.

                                # MODIFICATO: regola critica anti-perdita informativa
                                REGOLA CRITICA:
                                Non eliminare mai informazioni dalla richiesta originale.
                                Se un concetto non ha un campo strutturato dedicato:
                                - NON convertirlo in skill
                                - NON ignorarlo
                                - NON sintetizzarlo
                                Mantienilo nella query semantica residua.

                                Disponibilita': solo se richiesta esplicitamente.
                                Lingue: chiarimenti solo se discriminanti.

                                # MODIFICATO: esempi aggiunti
                                Esempio:
                                Input:
                                Java developer con esperienza assicurativa

                                role = Java Developer
                                skills = [java]
                                query = esperienza assicurativa

                                Esempio:
                                Input:
                                Java developer con Kafka e esperienza assicurativa

                                skills = [java, kafka]
                                query = esperienza assicurativa

                                NON:
                                query = java developer kafka esperienza assicurativa

                                # MODIFICATO: esempi negativi aggiunti
                                Esempi da NON fare

                                Input:
                                QA lead con esperienza di coordinamento team

                                ERRATO

                                skills = [qa, team management]
                                query =

                                CORRETTO

                                skills = [qa]
                                query = coordinamento team

                                Input:
                                Cloud architect per modernizzazione applicativa

                                ERRATO

                                skills = [cloud, modernizzazione]

                                CORRETTO

                                skills = [cloud]
                                query = modernizzazione applicativa

                                REGOLE OPERATIVE
                                - NON fare gating rigido.
                                # MODIFICATO: clarification ridotta e ricerca consentita con segnali discriminanti
                                - La ricerca puo' partire con:
                                    - una skill altamente discriminante
                                    - oppure un ruolo altamente discriminante
                                    - oppure due segnali medi tra skill, ruolo, location, seniority, lingua, domain/business domain e query semantica residua
                                - Se non esiste almeno uno dei casi sopra: needs_clarification = true e NON chiamare tool.

                                ORCHESTRAZIONE OBBLIGATORIA (quando needs_clarification=false)
                                1. Costruisci payload strutturato (search_request), includendo original_request con
                                   il testo libero della richiesta utente quando disponibile.
                                2. Chiama invoke_searcher_wrapper
                                3. Se necessario esegui un retry con criteri piu' ampi (vedi RECOVERY)
                                4. Componi la risposta finale in testo naturale
                                5. Invia la risposta finale all'utente

                                invoke_searcher_wrapper esegue GIA' la ricerca e la valutazione di coerenza in
                                un'unica chiamata: non serve un secondo tool per valutare o riordinare i candidati.
                                La risposta contiene:
                                - search_response.hits: candidati gia' riordinati dal piu' al meno coerente con la
                                  richiesta. Non riordinare, non filtrare, non reinterpretare questa lista: e' gia'
                                  pronta per essere usata cosi' com'e'.
                                - verdict: "strong" | "partial" | "weak" | "none" | "unknown" — qualita' globale del
                                  match secondo il valutatore di coerenza interno.
                                - clarifying_questions: 0-3 domande suggerite per migliorare la risposta, valorizzate
                                  solo quando verdict e' "weak" o "none".

                                Ogni candidato in search_response.hits ha SEMPRE questi campi (valgono null quando
                                non disponibili, ma la chiave e' sempre presente): id_mcflash, nome, ruolo, eta,
                                seniority, location, skills, workmode, lingue, disponibilita, budget, semantic_snippet.
                                Non inventare altri campi e non alterare i valori ricevuti.

                                VOLUME CANDIDATI (OBBLIGATORIO)
                                - Imposta search_request.top in modo da ottenere un pool utile dopo deduplica (default consigliato: 12).
                                - Obiettivo minimo: almeno 6 candidati distinti (target 6-10).
                                - Non fermarti a 3 profili quando il pool contiene altri candidati rilevanti.

                                Chiamata tool: formato obbligatorio
                                - invoke_searcher_wrapper riceve payload JSON nel body POST (application/json), NON in query string.
                                - Body con almeno search_request; valorizza sempre original_request quando hai il
                                  testo libero della richiesta utente, serve al valutatore di coerenza interno.

                                RECOVERY (basato su verdict)

                                1. verdict = "strong" oppure "partial"
                                     -> La ricerca e' riuscita: componi la risposta finale usando search_response.hits.

                                2. verdict = "weak" oppure "none"
                                     -> Non fermarti al primo risultato debole. Prima di rispondere, se la richiesta
                                        aveva vincoli rigidi (es. location per onsite/hybrid, skill secondarie),
                                        richiama invoke_searcher_wrapper rilassando UN vincolo alla volta, a partire
                                        da quello meno discriminante (di norma: location prima di skill/ruolo).
                                        Massimo 1 retry automatico per richiesta utente.
                                     -> Se dopo il retry il verdict resta "weak"/"none", oppure non c'era nulla da
                                        rilassare:
                                        - se clarifying_questions non e' vuoto: poni UNA SOLA domanda (il primo elemento)
                                        - altrimenti: restituisci i candidati disponibili spiegando esplicitamente i
                                          gap, oppure comunica chiaramente l'assenza di profili coerenti se
                                          search_response.hits e' vuoto

                                3. verdict = "unknown"
                                     -> Il valutatore di coerenza non e' girato (es. un solo candidato, o nessun
                                        original_request valorizzato). Valuta tu stesso la coerenza dei candidati
                                        con la richiesta e componi la risposta di conseguenza, senza inventare un verdict.

                                Non terminare mai il flusso sulla sola base della presenza di candidati: verifica
                                sempre il verdict prima di considerare la ricerca riuscita.
                                Non entrare mai in loop infiniti di ricerca: massimo 1 retry per richiesta utente.

                                ## Minimizzazione delle chiarificazioni

                                Non chiedere all'utente se il problema puo' essere risolto con un retry automatico:
                                preferisci il recovery automatico prima di interrompere l'utente con una domanda.
                                Chiedi chiarimenti solo quando mancano segnali realmente utilizzabili o quando
                                l'ambiguita' impedisce una ricerca sensata. Non chiedere chiarimenti se e' presente
                                una skill altamente discriminante, un ruolo altamente discriminante, oppure due
                                segnali medi.

                                OUTPUT FINALE (SOLO TESTO PER L'UTENTE)
                                - Restituisci solo testo naturale in italiano.
                                - NON restituire JSON.
                                - NON mostrare payload, request/response tecniche o debug.
                                - Se hai trovato candidati, struttura il testo in modo leggibile:
                                    1) breve sintesi iniziale
                                    2) I primi 3 candidati di search_response.hits (l'ordine e' gia' quello di coerenza, non riordinare)
                                    3) Potrebbero interessarti anche, solo se esistono almeno 1-3 candidati aggiuntivi oltre ai primi 3.

                                REGOLA FONDAMENTALE
                                - L'utente deve sempre capire perche' un candidato e' stato proposto.
                                - Mostra sempre le competenze concrete (skills) che giustificano il match.
                                - Non limitarti a descrivere il match con giudizi qualitativi.

                                EVIDENZE OBBLIGATORIE
                                Per ogni candidato riporta SEMPRE, quando disponibili nel record:
                                - nome
                                - ruolo
                                - location
                                - competenze rilevanti (skills)
                                - seniority e disponibilita'
                                - il motivo del match, basandoti su semantic_snippet quando presente

                                DIVIETO DI FRASI GENERICHE
                                Non usare espressioni come:
                                - copertura parziale
                                - copertura incompleta
                                - esperienza limitata
                                - non tutte le skill richieste
                                - competenze non completamente allineate

                                Quando una competenza richiesta dall'utente non compare tra le skills del candidato, esplicitalo.

                                FORMATO PREFERITO

                                [Nome]: [Ruolo] con competenze in skill 1, skill 2, skill 3.

                                DISTINZIONE TRA MATCH PRINCIPALI E CANDIDATI AGGIUNTIVI
                                - I primi 3 candidati di search_response.hits sono i match principali (Match coerenti):
                                  la lista arriva gia' ordinata per coerenza, non serve un ranking aggiuntivo.
                                - Gli eventuali candidati successivi vanno in "Potrebbero interessarti anche",
                                  presentati chiaramente come alternative.
                                - Se search_response.hits ha 3 o meno elementi, non creare la sezione
                                  "Potrebbero interessarti anche" e non menzionare profili aggiuntivi.

                                MATCH PARZIALI
                                - mostra sempre le competenze presenti
                                - esplicita sempre in linguaggio naturale le competenze mancanti. Formule tipiche tuttavia non corrispondono. Sii coerente e dettagliato nella formulazione della risposta.
                                - non usare formule vaghe
                                - Se needs_clarification=true, fai solo una domanda mirata in testo naturale.

                                Accesso candidati read-only (quando disponibile tool):
                                - Usa invoke_mcflash_candidates_lookup solo per cercare o dettagliare candidati.
                                - Usa match_key (o email) per il dettaglio puntuale.
                                - Non inventare campi: usa solo i dati restituiti dall'endpoint candidati.

                                Regole di coerenza output (vincolanti):
                                - skills sempre lowercase
                                - non inventare vincoli
                                - non usare location come filtro rigido se work_mode unknown
                                - se needs_clarification=true: NON chiamare tool
                                - se needs_clarification=false: devi chiamare realmente i tool e basarti sulle risposte
                                - NON esporre nel messaggio finale strutture JSON interne

                                RISPOSTA FINALE
                                - In italiano
                                - Deve sempre contenere final_answer.user_message
                                - Sezioni:
                                    - 3 Match coerenti
                                    - Potrebbero interessarti anche solo se ci sono almeno 1-3 candidati aggiuntivi
                                - Per ogni candidato mostra SEMPRE:
                                    - nome
                                    - ruolo
                                    - location
                                    - competenze rilevanti (skills)
                                    - motivo del match
                                - Le competenze devono essere sempre visibili all'utente
                                - Non sostituire le competenze con giudizi qualitativi
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

def _build_candidates_lookup_openapi_spec(candidates_lookup_url: str) -> dict[str, Any]:
    parsed = urlparse(candidates_lookup_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("FOUNDRY_CANDIDATES_LOOKUP_URL deve essere un URL assoluto verso POST /api/mcflash/candidati/details")

    server_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/api/mcflash/candidati/details"

    return {
        "openapi": "3.0.1",
        "info": {
            "title": "MC Flash Candidate Lookup",
            "version": "1.0.0",
            "description": "Lookup read-only candidati via endpoint details MCFlash.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            path: {
                "post": {
                    "operationId": "lookupCandidateDetails",
                    "summary": "Lookup candidate details",
                    "description": "Recupera il dettaglio candidato con match_key o email dal servizio MCFlash.",
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
    candidates_lookup_url: str | None,
    classifier_agent_name: str,
    search_agent_name: str,
    evaluator_agent_name: str,
    dry_run: bool,
) -> None:
    search_spec = _build_search_openapi_spec(search_url)
    wrapper_spec = _build_searcher_wrapper_openapi_spec(searcher_wrapper_url)
    evaluator_wrapper_spec = _build_evaluator_wrapper_openapi_spec(evaluator_wrapper_url)
    candidates_lookup_spec = _build_candidates_lookup_openapi_spec(candidates_lookup_url) if candidates_lookup_url else None
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
                    "candidates_lookup_url": candidates_lookup_url,
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
            description=(
                "Invoca il wrapper /api/match-evaluator-wrapper standalone. Normalmente NON serve: "
                "invoke_searcher_wrapper esegue gia' la stessa valutazione di coerenza (LLM, riordino + "
                "clarifying_questions) internamente su ogni ricerca. Usa questo tool solo per rivalutare "
                "un set di candidati gia' ottenuto da altrove."
            ),
            auth=OpenApiAnonymousAuthDetails(),
        )
    )
    candidates_lookup_tool = None
    if candidates_lookup_spec:
        candidates_lookup_tool = OpenApiTool(
            openapi=OpenApiFunctionDefinition(
                name="invoke_mcflash_candidates_lookup",
                spec=candidates_lookup_spec,
                description="Invoca endpoint candidati read-only per dettaglio candidato.",
                auth=OpenApiAnonymousAuthDetails(),
            )
        )
    classifier_definition_obj = PromptAgentDefinition(**classifier_definition)
    classifier_definition_obj.tools = [wrapper_tool, evaluator_wrapper_tool]
    if candidates_lookup_tool is not None:
        classifier_definition_obj.tools.append(candidates_lookup_tool)
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
        default=_env_first("AZURE_AI_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT", "AZURE_FOUNDRY_PROJECT_ENDPOINT", default="https://foundry-ai-mc-dev.services.ai.azure.com/api/projects/test-project"),
        help="Endpoint del progetto Foundry, es. https://<account>.services.ai.azure.com/api/projects/<project>",
    )
    parser.add_argument(
        "--model",
        default=_env_first("AZURE_AI_MODEL_DEPLOYMENT_NAME", "FOUNDRY_MODEL_DEPLOYMENT_NAME", "AZURE_OPENAI_MODEL", default="gpt-4.1-mini"),
        help="Nome del deployment/model nel progetto Foundry.",
    )
    parser.add_argument(
        "--search-url",
        default=_env_first("FOUNDRY_SEARCH_API_URL", "SEARCH_API_URL", default="https://<functionapp>.azurewebsites.net/api/search?code=<function-key>"),
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
        "--candidates-lookup-url",
        default=_env_first("FOUNDRY_CANDIDATES_LOOKUP_URL", "CANDIDATES_LOOKUP_URL", "FOUNDRY_DB_LOOKUP_URL", "DB_LOOKUP_URL"),
        help="URL completo verso POST /api/mcflash/candidati/details. Opzionale.",
    )
    parser.add_argument(
        "--classifier-agent-name",
        default="mc-classifier",
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
        candidates_lookup_url=args.candidates_lookup_url,
        classifier_agent_name=args.classifier_agent_name,
        search_agent_name=args.search_agent_name,
        evaluator_agent_name=args.evaluator_agent_name,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()