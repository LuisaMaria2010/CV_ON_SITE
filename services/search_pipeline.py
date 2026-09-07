from __future__ import annotations

import math
import re
from typing import Any

from core.config import settings
from core.errors import InvalidInputError
from infra.mcflash_candidates import MCFlashApiError, MCFlashCandidatesClient
from infra.search_service import SearchService
from services.search_handler import (
    build_odata_filter,
    build_odata_filter_relaxed,
    enrich_hits_with_match_features,
    normalise_search_request,
    rerank,
    resolve_index,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _extract_availability_days(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n >= 0 else None
    if isinstance(value, str):
        m = re.search(r"(\d+)", value)
        if m:
            n = int(m.group(1))
            return n if n >= 0 else None
    return None


def _aggregate_semantic_evidence(top_chunks: list[dict[str, Any]]) -> str | None:
    """Evidence text populated only when the semantic search pass actually
    produced a caption/highlight for a retrieved chunk (`chunk["semantic_evidence"]`,
    set in SearchService.search_chunks step 2). No fallback to raw chunk
    content, the candidate's skill list, or `all_chunks` — if semantic search
    did not run (no free-text query) or found nothing, there is no semantic
    evidence to show, and the field must come back null rather than fabricated."""
    parts: list[str] = []

    for chunk in top_chunks[:4]:
        evidence = chunk.get("semantic_evidence")
        if isinstance(evidence, str) and evidence.strip():
            parts.extend([p.strip() for p in evidence.split("|") if p.strip()])

    deduped: list[str] = []
    seen: set[str] = set()
    for item in parts:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 8:
            break

    if not deduped:
        return None
    return " | ".join(deduped)


def _build_candidate_from_chunks(
    document_id: str,
    top_chunks: list[dict[str, Any]],
    all_chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    primary = top_chunks[0] if top_chunks else (all_chunks[0] if all_chunks else {})

    all_skill_values: list[str] = []
    all_cert_values: list[str] = []
    for chunk in [*top_chunks, *all_chunks]:
        all_skill_values.extend([_safe_str(v).lower() for v in (chunk.get("skills") or []) if _safe_str(v)])
        all_cert_values.extend([_safe_str(v) for v in (chunk.get("certifications") or []) if _safe_str(v)])

    aggregated_skills = sorted(set(v for v in all_skill_values if v))
    aggregated_certs = sorted(set(v for v in all_cert_values if v))

    semantic_score = max((_to_float(c.get("semantic_score"), 0.0) for c in top_chunks), default=0.0)
    vec_score = max((_to_float(c.get("vec_score"), 0.0) for c in top_chunks), default=0.0)
    lex_score = max((_to_float(c.get("lex_score"), 0.0) for c in top_chunks), default=0.0)
    candidate_score = max((_to_float(c.get("score"), 0.0) for c in top_chunks), default=0.0)

    availability_days = _extract_availability_days(
        _first_non_empty(
            primary.get("availability_days"),
            primary.get("availability"),
        )
    )

    evidence = _aggregate_semantic_evidence(top_chunks)

    return {
        "id": primary.get("id"),
        "candidate_id": _safe_str(_first_non_empty(primary.get("candidate_id"), document_id)),
        "document_id": document_id,
        "full_name": _first_non_empty(primary.get("full_name"), primary.get("name")),
        "name": _first_non_empty(primary.get("name"), primary.get("full_name")),
        "role": primary.get("role"),
        "location": primary.get("location"),
        "skills": aggregated_skills,
        "certifications": aggregated_certs,
        "seniority": primary.get("seniority"),
        "experience_years": primary.get("experience_years"),
        "language": primary.get("language"),
        "availability": primary.get("availability"),
        "availability_days": availability_days,
        "version": primary.get("version"),
        "source_path": primary.get("source_path"),
        "semantic_score": semantic_score,
        "vec_score": vec_score,
        "lex_score": lex_score,
        "score": round(candidate_score, 6),
        "semantic_evidence": evidence,
        "highlights": primary.get("highlights") or {},
    }


async def _aggregate_top_candidates(
    *,
    search: SearchService,
    reranked_chunks: list[dict[str, Any]],
    index_name: str,
    candidate_top_k: int,
) -> list[dict[str, Any]]:
    chunks_by_doc: dict[str, list[dict[str, Any]]] = {}
    for chunk in reranked_chunks:
        doc_id = _safe_str(_first_non_empty(chunk.get("document_id"), chunk.get("id")))
        if not doc_id:
            continue
        chunks_by_doc.setdefault(doc_id, []).append(chunk)

    if not chunks_by_doc:
        return []

    for doc_chunks in chunks_by_doc.values():
        doc_chunks.sort(key=lambda c: _to_float(c.get("score"), 0.0), reverse=True)

    ranked_docs = sorted(
        chunks_by_doc.items(),
        key=lambda it: _to_float(it[1][0].get("score"), 0.0),
        reverse=True,
    )

    top_doc_ids = [doc_id for doc_id, _ in ranked_docs[:candidate_top_k]]
    all_chunks_by_doc = await search.load_chunks_for_candidates(
        top_doc_ids,
        index_name=index_name,
        per_candidate_limit=40,
    )

    aggregated: list[dict[str, Any]] = []
    for doc_id in top_doc_ids:
        top_chunks = chunks_by_doc.get(doc_id, [])
        all_chunks = all_chunks_by_doc.get(doc_id, [])
        aggregated.append(_build_candidate_from_chunks(doc_id, top_chunks, all_chunks))

    return aggregated


def _normalise_person_key(value: Any) -> str:
    raw = _safe_str(value).lower()
    if not raw:
        return ""
    return re.sub(r"[^a-z0-9]+", "", raw)


def _select_mcflash_profile_for_hit(
    hit_role: Any,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    """
    Disambigua record MCFlash omonimi (stesso nome normalizzato) usando la similarita'
    di ruolo (tollerante a piccole variazioni: sinonimi IT/EN, forme parziali, ecc.
    tramite MCFlashCandidatesClient._role_matches).

    Ritorna (profile, ambiguous). `ambiguous` e' True quando esistono piu' omonimi e
    il ruolo da solo non ha permesso di isolare un candidato univoco: in tal caso si
    ricade sul primo match di ruolo disponibile, o sul primo omonimo se nessuno
    combacia, ma il flag segnala che l'identita' non e' stata risolta con certezza.
    """
    if not candidates:
        return None, False
    if len(candidates) == 1:
        return candidates[0], False

    hit_role_norm = _safe_str(hit_role)
    role_matches = [
        c for c in candidates
        if hit_role_norm and MCFlashCandidatesClient._role_matches(
            hit_role_norm,
            _safe_str(_first_non_empty(c.get("ruolo"), c.get("Ruolo"))),
        )
    ]

    if len(role_matches) == 1:
        return role_matches[0], False

    fallback = role_matches[0] if role_matches else candidates[0]
    return fallback, True


def _escape_odata_string(value: str) -> str:
    return value.replace("'", "''")


def _build_search_in_filter(field: str, values: set[str], *, chunk_size: int = 200) -> str | None:
    cleaned = [v for v in sorted({str(v).strip() for v in values if str(v).strip()}) if "," not in v]
    if not cleaned:
        return None

    clauses: list[str] = []
    for i in range(0, len(cleaned), chunk_size):
        chunk = cleaned[i:i + chunk_size]
        encoded = ",".join(_escape_odata_string(v) for v in chunk)
        clauses.append(f"search.in({field}, '{encoded}', ',')")

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " or ".join(clauses) + ")"


def _combine_odata_filters(*clauses: str | None) -> str | None:
    parts = [c for c in clauses if c and c.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return " and ".join(f"({p})" for p in parts)


def _relaxed_skill_thresholds(total_requested_skills: int) -> list[int]:
    if total_requested_skills <= 0:
        return []
    if total_requested_skills == 1:
        return [1]

    min_required = max(1, min(total_requested_skills - 1, math.ceil(total_requested_skills * 0.6)))
    return list(range(total_requested_skills - 1, min_required - 1, -1))


def _skill_overlap_count(candidate_skills: Any, requested_skills: list[str]) -> int:
    requested = {str(s).strip().lower() for s in requested_skills if str(s).strip()}
    candidate = {str(s).strip().lower() for s in _lower_list(candidate_skills) if str(s).strip()}
    if not requested or not candidate:
        return 0
    return len(requested & candidate)


def _escape_search_query_term(value: str) -> str:
    term = str(value or "").strip()
    if not term:
        return ""
    term = term.replace('"', ' ')
    term = re.sub(r"\s+", " ", term)
    return term.strip()


def _build_name_keyword_query(names: set[str], *, max_terms: int = 1000) -> str:
    """Nessun taglio di pertinenza qui: l'universo hard-select e' gia' il
    sotto-insieme corretto di MCFlash per questa richiesta, e va cercato per
    intero. Il precedente max_terms=60, combinato con l'ordinamento
    alfabetico di sorted(names), scartava sistematicamente i candidati il cui
    nome non rientrava tra i primi 60 in ordine alfabetico - indipendentemente
    dalla loro pertinenza. max_terms resta solo come protezione tecnica contro
    query eccessivamente lunghe: con l'universo MCFlash attuale (263
    candidati) non viene mai raggiunto.
    """
    cleaned: list[str] = []
    for raw in sorted(names):
        term = _escape_search_query_term(raw)
        if not term:
            continue
        cleaned.append(f'"{term}"')
        if len(cleaned) >= max_terms:
            break
    return " OR ".join(cleaned)


def _extract_availability_hint_from_query(query: str | None) -> str | None:
    text = _safe_str(query).lower()
    if not text:
        return None

    if any(token in text for token in ("immediat", "subito", "urgent")):
        return "immediata"

    patterns = [
        r"\bentro\s+\d+\s*(?:gg|giorn[oi]|settiman\w*)\b",
        r"\b(?:da|dal|dalla)\s+\d{1,2}\s+[a-zà-ù]+(?:\s+\d{2,4})?\b",
        r"\b(?:da|dal|dalla)\s+[a-zà-ù]+(?:\s+\d{2,4})?\b",
        r"\b(?:lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|giovedi|giovedì|venerdi|venerdì|sabato|domenica)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)

    return None

async def run_search_pipeline(payload: dict, *, get_mcflash_candidates_client, logger) -> dict:
    p = normalise_search_request(payload)

    if not p["query"] and not p["skills"] and not p["role"]:
        raise InvalidInputError("At least one of 'query', 'skills' or 'role' is required")

    hard_seniority = _safe_str(
        _first_non_empty(p.get("seniority"), payload.get("seniority"), p.get("seniority_explicit"))
    ) or None
    hard_role = _safe_str(
        _first_non_empty(payload.get("role"), p.get("role"))
    ) or None
    hard_sede = _safe_str(
        _first_non_empty(payload.get("sede"), payload.get("location"), p.get("location"))
    ) or None
    hard_language = _safe_str(
        _first_non_empty(payload.get("lingue"), payload.get("language"), p.get("language"))
    ) or None
    hard_work_mode_raw = _safe_str(
        _first_non_empty(payload.get("work_mode"), p.get("work_mode"))
    ).lower()
    hard_work_mode = hard_work_mode_raw if hard_work_mode_raw and hard_work_mode_raw not in {"unknown", "any"} else None
    hard_disponibilita = _safe_str(
        _first_non_empty(
            payload.get("disponibilita"),
            payload.get("availability"),
            payload.get("availability_date"),
            payload.get("availability_days"),
        )
    ) or None
    if not hard_disponibilita:
        hard_disponibilita = _extract_availability_hint_from_query(p.get("query"))
    hard_budget = _extract_budget_constraint(payload)
    hard_age = _extract_age_constraint(payload)

    hard_select_enabled = True
    hard_selected_total = 0
    hard_selected_nomi_keys: set[str] = set()
    hard_selected_full_names: set[str] = set()
    hard_selected_by_nome_key: dict[str, list[dict[str, Any]]] = {}

    client = get_mcflash_candidates_client()
    try:
        # Hard-select is always executed. Without filters we scan MCFlash pages and use
        # the full table (bounded by safety cap) as mandatory candidate universe.
        if any([hard_role, hard_seniority, hard_sede, hard_work_mode, hard_language, hard_disponibilita, hard_budget, hard_age]):
            hard_selected = await client.filter_candidates(
                limit=5000,
                offset=0,
                role=hard_role,
                seniority=hard_seniority,
                sede=hard_sede,
                work_mode=hard_work_mode,
                lingue=hard_language,
                disponibilita=hard_disponibilita,
                budget=hard_budget,
                age=hard_age,
                max_scan_records=20000,
                return_all_matches=True,
            )
        else:
            hard_selected = []
            offset = 0
            page_size = 1000
            max_scan = 20000
            while len(hard_selected) < max_scan:
                page = await client.fetch_page(limit=page_size, offset=offset)
                if not page:
                    break
                hard_selected.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
            hard_selected = hard_selected[:max_scan]
    except MCFlashApiError as exc:
        raise InvalidInputError(f"MCFlash hard select failed: {exc}") from exc

    hard_selected_total = len(hard_selected)
    if not hard_selected:
        return {
            "hits": [],
            "meta": {
                "total": 0,
                "top": p["top"],
                "relaxed": False,
                "relaxed_criteria": [],
                "hybrid": p["hybrid"],
                "work_mode": p["work_mode"],
                "ignored_constraints": [],
                "index": resolve_index(p["subco"]),
                "hard_select": {
                    "enabled": True,
                    "role": hard_role,
                    "seniority": hard_seniority,
                    "sede": hard_sede,
                    "work_mode": hard_work_mode,
                    "lingue": hard_language,
                    "disponibilita": hard_disponibilita,
                    "budget": hard_budget,
                    "age": hard_age,
                    "matched_candidates": 0,
                },
            },
            "suggestions": [],
        }

    for candidate in hard_selected:
        mcflash_nome = _safe_str(_first_non_empty(candidate.get("nome"), candidate.get("Nome")))

        # Solo il codice anonimizzato (es. "ELO") va usato per risolvere l'identita'
        # nell'indice CV: e' quello che l'indice usa come full_name (verificato sui
        # chunk reali). Il nome completo ("nomi", es. "Emanuele Maria Lobundo") non
        # compare nell'indice CV ed e' presente solo lato MCFlash - includerlo nella
        # query di risoluzione rischia solo falsi positivi (nomi/cognomi comuni che
        # matchano per caso il contenuto di CV di altri candidati).
        if mcflash_nome:
            hard_selected_full_names.add(mcflash_nome)

        nome_key = _normalise_person_key(mcflash_nome)
        if nome_key:
            hard_selected_nomi_keys.add(nome_key)
            # Keep every homonym (same normalized name): role-based disambiguation
            # happens later, at attach time, against the actual CV/index role.
            hard_selected_by_nome_key.setdefault(nome_key, []).append(candidate)

    name_keyword_query = _build_name_keyword_query(hard_selected_full_names)

    if not name_keyword_query:
        logger.warning("MCFlash universe could not be translated to name keyword query")
        return {
            "hits": [],
            "meta": {
                "total": 0,
                "top": p["top"],
                "relaxed": False,
                "relaxed_criteria": [],
                "hybrid": p["hybrid"],
                "work_mode": p["work_mode"],
                "ignored_constraints": [],
                "index": resolve_index(p["subco"]),
                "hard_select": {
                    "enabled": True,
                    "role": hard_role,
                    "seniority": hard_seniority,
                    "sede": hard_sede,
                    "work_mode": hard_work_mode,
                    "lingue": hard_language,
                    "disponibilita": hard_disponibilita,
                    "budget": hard_budget,
                    "age": hard_age,
                    "matched_candidates": hard_selected_total,
                },
            },
            "suggestions": [],
        }

    index_name = resolve_index(p["subco"])
    search = SearchService()

    # Risolve l'universo hard-select (nomi/codici MCFlash) in document_id
    # dell'indice CV. Il punteggio di questa ricerca viene scartato: serve solo
    # a stabilire CHI e' ammissibile (identita'), non quanto e' pertinente -
    # quella valutazione spetta solo alle ricerche vere piu' sotto, scoped da
    # un filtro invece che da testo libero in competizione con le skill.
    identity_doc_ids = await search.resolve_document_ids_by_name(
        name_keyword_query, index_name=index_name
    )
    if not identity_doc_ids:
        # Nessun document_id risolto per l'universo hard-select corrente (es. un
        # candidato MCFlash senza CV indicizzato). Non e' un caso da chiudere qui:
        # la ricerca "stretta" restera' semplicemente vuota e la logica di
        # second-pass piu' sotto (gia' esistente, allarga la ricerca quando i
        # candidati stretti sono troppo pochi) prendera' il suo corso, com'era
        # anche prima di questa modifica. Interrompere qui con un return
        # anticipato negherebbe al second-pass la possibilita' di intervenire.
        logger.warning("MCFlash universe did not resolve to any indexed CV; deferring to second-pass extension")
        identity_filter = None
    else:
        identity_filter = _build_search_in_filter("document_id", identity_doc_ids)

    # search_text non contiene piu' i nomi: l'ammissibilita' e' gia' garantita dal
    # filtro sopra, quindi qui c'e' solo cio' che e' realmente utile a valutare la
    # pertinenza (ruolo/skill esplicite).
    base_lexical_query = " ".join(filter(None, [
        p["role"] or "",
        " ".join(p["skills"]),
    ])).strip()
    lexical_query = base_lexical_query

    # Semantic search only makes sense over free-text intent. role/skills
    # keywords are already covered by the lexical pass — falling back to them
    # here would run the semantic reranker (and embedding) on keyword text, and
    # would never let semantic search stay off when the user gave no free text.
    semantic_query = (p["query"] or "").strip()

    embedding: list[float] | None = None
    if p["hybrid"] and semantic_query:
        try:
            from infra.llm_client import get_embedding_client
            emb_client = get_embedding_client()
            embedding = await emb_client.aembed_query(semantic_query)
        except Exception:
            logger.exception("Embedding generation failed, falling back to lexical-only search")

    candidate_top_k = min(6, max(10, p["top"]))
    chunk_top_k = max(candidate_top_k * 10, p["top"] * 5)
    # MCFlash hard-select (via identity_filter sopra) resta la fonte di verita' per
    # i vincoli di business (ruolo/seniority/sede/...). In index search aggiungiamo
    # solo il filtro skills, che MCFlash non conosce.
    eligibility_filter = build_odata_filter(
        skills=p["skills"],
        min_experience_years=None,
        max_experience_years=None,
    )
    odata_filter = _combine_odata_filters(identity_filter, eligibility_filter)

    if identity_doc_ids:
        raw_hits = await search.search_chunks(
            lexical_query=lexical_query,
            semantic_query=semantic_query,
            odata_filter=odata_filter,
            embedding=embedding,
            top=max(p["top"], chunk_top_k),
            index_name=index_name,
        )
    else:
        # Senza identity_doc_ids non c'e' nessun filtro di identita' da applicare:
        # cercare comunque significherebbe interrogare l'indice senza alcuna
        # restrizione di ammissibilita'. Meglio saltare la ricerca stretta (resta
        # vuota, come sarebbe comunque risultata dopo il filtro _is_hard_name_match
        # sotto) e lasciare che sia il second-pass a cercare in modo esplicitamente
        # allargato.
        raw_hits = []

    def _is_hard_name_match(hit: dict[str, Any]) -> bool:
        hit_full_name = _safe_str(_first_non_empty(hit.get("full_name"), hit.get("name")))
        hit_name_key = _normalise_person_key(hit_full_name)
        return bool(hit_name_key and hit_name_key in hard_selected_nomi_keys)

    strict_raw_hits = [h for h in raw_hits if isinstance(h, dict) and _is_hard_name_match(h)]

    reranked_chunks = rerank(
        strict_raw_hits,
        query_skills=p["skills"],
        query_role=p["role"],
        query_location=p["location"],
        top=chunk_top_k,
    )
    hits = await _aggregate_top_candidates(
        search=search,
        reranked_chunks=reranked_chunks,
        index_name=index_name,
        candidate_top_k=candidate_top_k,
    )

    # `_attach_mcflash_profile_by_nome` runs more than once on the same hit object
    # across passes (first-pass strict match, then the final re-attach loop). Role
    # disambiguation must always compare against the role as originally read from
    # the CV/index, not the MCFlash-overlaid value written back onto the hit by a
    # previous call — otherwise a wrong guess would "confirm" itself on re-entry.
    original_cv_role_by_hit: dict[int, Any] = {}

    def _attach_mcflash_profile_by_nome(hit: dict[str, Any]) -> bool:
        hit_full_name = _safe_str(_first_non_empty(hit.get("full_name"), hit.get("name")))
        hit_name_key = _normalise_person_key(hit_full_name)
        candidates = hard_selected_by_nome_key.get(hit_name_key) or []
        cache_key = id(hit)
        if cache_key not in original_cv_role_by_hit:
            original_cv_role_by_hit[cache_key] = hit.get("role")
        profile, ambiguous = _select_mcflash_profile_for_hit(original_cv_role_by_hit[cache_key], candidates)
        if profile is None or hit_name_key not in hard_selected_nomi_keys:
            hit["strict_mcflash_name_match"] = False
            return False

        hit["mcflash_profile"] = dict(profile)
        hit["mcflash_name_ambiguous"] = ambiguous

        def _profile_value(*keys: str) -> Any:
            for key in keys:
                if key not in profile:
                    continue
                value = profile.get(key)
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            return None

        # First-pass contract: keep profile fields from MCFlash and enrich with
        # skills/semantic evidence coming from index retrieval.
        role_value = _profile_value("ruolo", "Ruolo", "role", "Role")
        if role_value is not None:
            hit["role"] = role_value

        seniority_value = _profile_value("seniority", "Seniority")
        if seniority_value is not None:
            hit["seniority"] = seniority_value

        language_value = _profile_value("lingue", "Lingue", "language", "Language")
        if language_value is not None:
            hit["language"] = language_value

        location_value = _profile_value("sede", "Sede", "location", "Location")
        if location_value is not None:
            hit["location"] = location_value

        availability_value = _profile_value(
            "disponibilita",
            "Disponibilita",
            "availability",
            "Availability",
        )
        if availability_value is not None:
            # availability and disponibilita are aliases for the same value.
            hit["availability"] = availability_value
            hit["disponibilita"] = availability_value

        hit["strict_mcflash_name_match"] = True
        return True

    mcflash_full_name_cache: dict[str, list[dict[str, Any]]] = {}

    def _fill_missing_from_profile(hit: dict[str, Any], profile: dict[str, Any]) -> None:
        def _profile_value(*keys: str) -> Any:
            for key in keys:
                if key in profile and profile.get(key) is not None:
                    value = profile.get(key)
                    if not isinstance(value, str) or value.strip():
                        return value
            return None

        def _set_if_missing(field: str, *source_keys: str) -> None:
            if _safe_str(hit.get(field)):
                return
            value = _profile_value(*source_keys)
            if value is not None:
                hit[field] = value

        _set_if_missing("candidate_id", "id", "Id")
        _set_if_missing("full_name", "nome", "Nome", "nomi", "Nomi")
        _set_if_missing("name", "nome", "Nome")
        _set_if_missing("role", "ruolo", "Ruolo")
        _set_if_missing("seniority", "seniority", "Seniority")
        _set_if_missing("location", "sede", "Sede")
        _set_if_missing("language", "lingue", "Lingue")
        _set_if_missing("availability", "disponibilita", "Disponibilita")

    def _overlay_from_mcflash_for_second_pass(hit: dict[str, Any], profile: dict[str, Any], *, ambiguous: bool) -> None:
        def _profile_value(*keys: str) -> Any:
            for key in keys:
                if key not in profile:
                    continue
                value = profile.get(key)
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            return None

        hit["mcflash_profile"] = dict(profile)
        hit["mcflash_name_ambiguous"] = ambiguous

        role_value = _profile_value("ruolo", "Ruolo", "role", "Role")
        if role_value is not None:
            hit["role"] = role_value

        seniority_value = _profile_value("seniority", "Seniority")
        if seniority_value is not None:
            hit["seniority"] = seniority_value

        language_value = _profile_value("lingue", "Lingue", "language", "Language")
        if language_value is not None:
            hit["language"] = language_value

        availability_value = _profile_value(
            "disponibilita",
            "Disponibilita",
            "availability",
            "Availability",
        )
        if availability_value is not None:
            # Keep both aliases aligned.
            hit["availability"] = availability_value
            hit["disponibilita"] = availability_value

    async def _enrich_second_pass_from_mcflash(hit: dict[str, Any]) -> None:
        if not isinstance(hit, dict):
            return
        if not hit.get("second_pass_extension"):
            return
        if hit.get("strict_mcflash_name_match"):
            # Already resolved (and role-disambiguated, if needed) by
            # _attach_mcflash_profile_by_nome against the hard-select universe.
            # Re-running a live single-name MCFlash lookup here would only risk
            # overwriting a good match with an undisambiguated one.
            return

        full_name = _safe_str(_first_non_empty(hit.get("full_name"), hit.get("name")))
        if not full_name:
            if not _safe_str(hit.get("availability")):
                hit["availability"] = "non disponibile"
            return

        cache_key = full_name.lower()
        if cache_key not in mcflash_full_name_cache:
            mcflash_full_name_cache[cache_key] = await client.find_candidates(full_name)

        candidates = mcflash_full_name_cache.get(cache_key) or []
        profile, ambiguous = _select_mcflash_profile_for_hit(hit.get("role"), candidates)
        if profile is not None:
            _overlay_from_mcflash_for_second_pass(hit, profile, ambiguous=ambiguous)
            return

        if not _safe_str(hit.get("availability")):
            hit["availability"] = "non disponibile"
            hit["disponibilita"] = "non disponibile"

    strict_hits_initial: list[dict[str, Any]] = []

    for hit in hits:
        if not isinstance(hit, dict):
            continue
        if _attach_mcflash_profile_by_nome(hit):
            strict_hits_initial.append(hit)

    hits = strict_hits_initial[:candidate_top_k]

    relaxed = False
    relaxed_criteria = list(p.get("relaxed_criteria") or [])
    suggestions: list[str] = []
    fallback_min = 3

    if len(hits) < fallback_min and p["skills"]:
        relaxed = True
        if "skills" not in relaxed_criteria:
            relaxed_criteria.append("skills")
        relaxed_lexical_query = (p["role"] or "").strip()
        relaxed_semantic_query = (p["query"] or "").strip()

        relaxed_embedding: list[float] | None = None
        if p["hybrid"] and relaxed_semantic_query:
            try:
                from infra.llm_client import get_embedding_client
                emb_client = get_embedding_client()
                relaxed_embedding = await emb_client.aembed_query(relaxed_semantic_query)
            except Exception:
                logger.exception("Relaxed embedding generation failed, falling back to lexical-only search")

        relaxed_eligibility_filter = build_odata_filter_relaxed(
            min_experience_years=None,
            max_experience_years=None,
        )
        relaxed_odata_filter = _combine_odata_filters(identity_filter, relaxed_eligibility_filter)

        relaxed_hits = await search.search_chunks(
            lexical_query=relaxed_lexical_query,
            semantic_query=relaxed_semantic_query,
            odata_filter=relaxed_odata_filter,
            embedding=relaxed_embedding,
            top=max(chunk_top_k, fallback_min * 6),
            index_name=index_name,
        )
        relaxed_strict_raw_hits = [
            h for h in relaxed_hits
            if isinstance(h, dict) and _is_hard_name_match(h)
        ]
        relaxed_reranked_chunks = rerank(
            relaxed_strict_raw_hits,
            query_skills=p["skills"],
            query_role=p["role"],
            query_location=p["location"],
            top=chunk_top_k,
        )
        relaxed_candidates = await _aggregate_top_candidates(
            search=search,
            reranked_chunks=relaxed_reranked_chunks,
            index_name=index_name,
            candidate_top_k=candidate_top_k,
        )
        existing_ids = {_safe_str(h.get("document_id")) for h in hits}

        candidate_pool: list[dict[str, Any]] = []
        for h in relaxed_candidates:
            doc_id = _safe_str(h.get("document_id"))
            if not doc_id or doc_id in existing_ids:
                continue

            if _attach_mcflash_profile_by_nome(h):
                candidate_pool.append(h)

        thresholds = _relaxed_skill_thresholds(len(p["skills"]))
        selected_doc_ids: set[str] = set()

        for required_matches in thresholds:
            for candidate in candidate_pool:
                doc_id = _safe_str(candidate.get("document_id"))
                if not doc_id or doc_id in selected_doc_ids:
                    continue
                if _skill_overlap_count(candidate.get("skills"), p["skills"]) < required_matches:
                    continue

                candidate["is_relaxed_result"] = True
                hits.append(candidate)
                selected_doc_ids.add(doc_id)
                existing_ids.add(doc_id)

                if len(hits) >= candidate_top_k:
                    break
            if len(hits) >= candidate_top_k:
                break

        if len(hits) < fallback_min:
            for candidate in candidate_pool:
                doc_id = _safe_str(candidate.get("document_id"))
                if not doc_id or doc_id in existing_ids:
                    continue
                candidate["is_relaxed_result"] = True
                hits.append(candidate)
                existing_ids.add(doc_id)
                if len(hits) >= candidate_top_k:
                    break

        hits = hits[:candidate_top_k]
        suggestions = [
            f"{s} (not found as strict requirement, showing partial matches)"
            for s in p["skills"]
        ]

    strict_hits_final: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        if _attach_mcflash_profile_by_nome(hit):
            strict_hits_final.append(hit)
            continue

        await _enrich_second_pass_from_mcflash(hit)
        strict_hits_final.append(hit)
    hits = strict_hits_final[:candidate_top_k]

    # Second pass (extension only): keep first-pass MCFlash logic unchanged.
    # If strict MCFlash-compatible candidates are fewer than 6, extend the list
    # with a hybrid retrieval driven by hard-select keywords.
    target_min_candidates = 6
    has_second_pass_keywords = any([
        _safe_str(hard_role),
        _safe_str(hard_sede),
        _safe_str(hard_seniority),
        _safe_str(hard_language),
        bool(p["skills"]),
    ])

    if len(hits) < target_min_candidates and has_second_pass_keywords:
        keyword_parts = [
            _safe_str(hard_role),
            _safe_str(hard_sede),
            _safe_str(hard_seniority),
            _safe_str(hard_language),
            " ".join(p["skills"]),
        ]
        second_pass_lexical_query = " ".join(part for part in keyword_parts if part).strip()
        second_pass_semantic_query = (p["query"] or "").strip()

        second_pass_embedding: list[float] | None = None
        if second_pass_semantic_query:
            try:
                from infra.llm_client import get_embedding_client
                emb_client = get_embedding_client()
                second_pass_embedding = await emb_client.aembed_query(second_pass_semantic_query)
            except Exception:
                logger.exception("Second-pass embedding generation failed, using lexical-only fallback")

        second_pass_raw_hits = await search.search_chunks(
            lexical_query=second_pass_lexical_query,
            semantic_query=second_pass_semantic_query,
            odata_filter=None,
            embedding=second_pass_embedding,
            top=max(chunk_top_k, target_min_candidates * 8),
            index_name=index_name,
        )
        second_pass_reranked = rerank(
            second_pass_raw_hits,
            query_skills=p["skills"],
            query_role=p["role"],
            query_location=p["location"],
            top=max(chunk_top_k, target_min_candidates * 6),
        )
        second_pass_candidates = await _aggregate_top_candidates(
            search=search,
            reranked_chunks=second_pass_reranked,
            index_name=index_name,
            candidate_top_k=max(candidate_top_k, target_min_candidates * 2),
        )

        existing_doc_ids = {
            _safe_str(h.get("document_id"))
            for h in hits
            if isinstance(h, dict)
        }

        for candidate in second_pass_candidates:
            if len(hits) >= target_min_candidates:
                break
            if not isinstance(candidate, dict):
                continue

            doc_id = _safe_str(candidate.get("document_id"))
            if not doc_id or doc_id in existing_doc_ids:
                continue

            _attach_mcflash_profile_by_nome(candidate)
            candidate["second_pass_extension"] = True
            await _enrich_second_pass_from_mcflash(candidate)
            if not candidate.get("strict_mcflash_name_match"):
                candidate["non_perfect_match"] = True

            hits.append(candidate)
            existing_doc_ids.add(doc_id)

        if len(hits) > len(strict_hits_final):
            relaxed = True
            if "second_pass_hybrid" not in relaxed_criteria:
                relaxed_criteria.append("second_pass_hybrid")
            suggestions.append(
                "Extended results with hybrid index pass (role/location/seniority/language/skills keywords + vector query) to reach up to 6 candidates."
            )

        # If still below target, relax only skills in second pass while keeping
        # other hard-select keywords (role/location/seniority/language).
        if len(hits) < target_min_candidates:
            relaxed_keyword_parts = [
                _safe_str(hard_role),
                _safe_str(hard_sede),
                _safe_str(hard_seniority),
                _safe_str(hard_language),
            ]
            second_pass_relaxed_lexical_query = " ".join(
                part for part in relaxed_keyword_parts if part
            ).strip()
            second_pass_relaxed_semantic_query = (p["query"] or "").strip()

            second_pass_relaxed_embedding: list[float] | None = None
            if second_pass_relaxed_semantic_query:
                try:
                    from infra.llm_client import get_embedding_client
                    emb_client = get_embedding_client()
                    second_pass_relaxed_embedding = await emb_client.aembed_query(second_pass_relaxed_semantic_query)
                except Exception:
                    logger.exception("Second-pass skills-relaxed embedding generation failed, using lexical-only fallback")

            second_pass_relaxed_raw_hits = await search.search_chunks(
                lexical_query=second_pass_relaxed_lexical_query,
                semantic_query=second_pass_relaxed_semantic_query,
                odata_filter=None,
                embedding=second_pass_relaxed_embedding,
                top=max(chunk_top_k, target_min_candidates * 10),
                index_name=index_name,
            )
            second_pass_relaxed_reranked = rerank(
                second_pass_relaxed_raw_hits,
                query_skills=p["skills"],
                query_role=p["role"],
                query_location=p["location"],
                top=max(chunk_top_k, target_min_candidates * 8),
            )
            second_pass_relaxed_candidates = await _aggregate_top_candidates(
                search=search,
                reranked_chunks=second_pass_relaxed_reranked,
                index_name=index_name,
                candidate_top_k=max(candidate_top_k, target_min_candidates * 3),
            )

            for candidate in second_pass_relaxed_candidates:
                if len(hits) >= target_min_candidates:
                    break
                if not isinstance(candidate, dict):
                    continue

                doc_id = _safe_str(candidate.get("document_id"))
                if not doc_id or doc_id in existing_doc_ids:
                    continue

                _attach_mcflash_profile_by_nome(candidate)
                candidate["second_pass_extension"] = True
                candidate["second_pass_skills_relaxed"] = True
                await _enrich_second_pass_from_mcflash(candidate)
                if not candidate.get("strict_mcflash_name_match"):
                    candidate["non_perfect_match"] = True

                hits.append(candidate)
                existing_doc_ids.add(doc_id)

            if len(hits) > len(strict_hits_final):
                relaxed = True
                if "second_pass_skills_relaxed" not in relaxed_criteria:
                    relaxed_criteria.append("second_pass_skills_relaxed")
                suggestions.append(
                    "Still below 6 candidates: relaxed skills in second-pass hybrid retrieval while keeping role/location/seniority/language keywords."
                )

    hits = enrich_hits_with_match_features(
        hits,
        query_skills=p["skills"],
        query_role=p["role"],
        query_location=p["location"],
        query_language=p["language"],
        query_seniority=p["seniority"],
        query_availability_required=p["availability_required"],
        work_mode=p["work_mode"],
        relaxed_criteria=relaxed_criteria,
        query_availability_days=p["availability_days"],
    )

    ignored_constraints: list[str] = []

    logger.info(
        "Search completed query=%r index=%s hits=%s relaxed=%s hybrid=%s",
        p["query"], index_name, len(hits), relaxed, p["hybrid"],
    )

    return {
        "hits": hits,
        "meta": {
            "total": len(hits),
            "top": p["top"],
            "relaxed": relaxed,
            "relaxed_criteria": relaxed_criteria,
            "hybrid": p["hybrid"],
            "work_mode": p["work_mode"],
            "ignored_constraints": ignored_constraints,
            "index": index_name,
            "hard_select": {
                "enabled": hard_select_enabled,
                "role": hard_role,
                "seniority": hard_seniority,
                "sede": hard_sede,
                "work_mode": hard_work_mode,
                "lingue": hard_language,
                "disponibilita": hard_disponibilita,
                "budget": hard_budget,
                "age": hard_age,
                "matched_candidates": hard_selected_total,
            },
        },
        "suggestions": suggestions,
    }

def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _lower_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.split(",")]
        return [p for p in parts if p]
    return []


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _extract_budget_constraint(payload: dict[str, Any]) -> str | None:
    raw_budget = _first_non_empty(
        payload.get("budget"),
        payload.get("max_budget"),
        payload.get("budget_max"),
        payload.get("budget_limit"),
        payload.get("maxBudget"),
    )

    if isinstance(raw_budget, dict):
        raw_budget = _first_non_empty(
            raw_budget.get("max"),
            raw_budget.get("value"),
            raw_budget.get("amount"),
        )

    value = _safe_str(raw_budget)
    return value or None


def _extract_age_constraint(payload: dict[str, Any]) -> str | None:
    """
    Estrae il vincolo di eta' testuale (es. "under 30", "over 40") dal payload
    per l'hard-select su MariaDB. Il parsing della direzione (min/max) avviene
    in MCFlashCandidatesClient._extract_requested_age_bounds; qui recuperiamo
    solo il testo grezzo, come per budget/disponibilita'.
    """
    raw_age = _first_non_empty(
        payload.get("age"),
        payload.get("eta"),
        payload.get("min_age"),
        payload.get("max_age"),
    )

    if isinstance(raw_age, dict):
        raw_age = _first_non_empty(
            raw_age.get("min"),
            raw_age.get("max"),
            raw_age.get("value"),
        )

    value = _safe_str(raw_age)
    return value or None


def _candidate_fields_for_evaluator(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep only candidate fields consumed by deterministic evaluator logic."""
    allowed_keys = {
        "id",
        "candidate_id",
        "document_id",
        "full_name",
        "name",
        "role",
        "location",
        "skills",
        "seniority",
        "language",
        "availability_days",
        "semantic_score",
        "source_path",
        "match_features",
        "relaxed_criteria",
    }
    return {k: v for k, v in candidate.items() if k in allowed_keys}


def _extract_evaluator_candidates(payload: dict) -> list[dict[str, Any]]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        return [
            _candidate_fields_for_evaluator(c)
            for c in candidates
            if isinstance(c, dict)
        ]

    search_response = payload.get("search_response")
    if isinstance(search_response, dict):
        if isinstance(search_response.get("hits"), list):
            return [
                _candidate_fields_for_evaluator(c)
                for c in search_response["hits"]
                if isinstance(c, dict)
            ]
        data = search_response.get("data")
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            return [
                _candidate_fields_for_evaluator(c)
                for c in data["hits"]
                if isinstance(c, dict)
            ]

    return []



