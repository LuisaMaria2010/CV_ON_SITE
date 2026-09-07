import asyncio
import ast
import json
from pathlib import Path

import pandas as pd

import function_app as fa
from infra.search_service import SearchService
from services.search_handler import build_odata_filter, normalise_search_request, resolve_index


def parse_skills(value):
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(v).strip().lower() for v in parsed if str(v).strip()]
        except Exception:
            pass
    return [p.strip().lower() for p in text.split(",") if p.strip()]


def as_opt_str(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


async def inspect_case(case_name: str, request: dict):
    p = normalise_search_request(request)

    hard_seniority = fa._safe_str(
        fa._first_non_empty(p.get("seniority"), request.get("seniority"), p.get("seniority_explicit"))
    ) or None
    hard_role = fa._safe_str(fa._first_non_empty(request.get("role"), p.get("role"))) or None
    hard_sede = fa._safe_str(fa._first_non_empty(request.get("sede"), request.get("location"), p.get("location"))) or None
    hard_language = fa._safe_str(
        fa._first_non_empty(request.get("lingue"), request.get("language"), p.get("language"))
    ) or None
    hard_work_mode_raw = fa._safe_str(fa._first_non_empty(request.get("work_mode"), p.get("work_mode"))).lower()
    hard_work_mode = hard_work_mode_raw if hard_work_mode_raw and hard_work_mode_raw not in {"unknown", "any"} else None
    hard_disponibilita = fa._safe_str(
        fa._first_non_empty(
            request.get("disponibilita"),
            request.get("availability"),
            request.get("availability_date"),
            request.get("availability_days"),
        )
    ) or None
    hard_budget = fa._safe_str(fa._first_non_empty(request.get("budget"), request.get("max_budget"), request.get("budget_max"))) or None

    client = fa._get_mcflash_candidates_client()
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
        max_scan_records=20000,
        return_all_matches=True,
    )

    hard_selected_ids = set()
    hard_selected_full_names = set()
    for candidate in hard_selected:
        candidate_id = fa._safe_str(
            fa._first_non_empty(
                candidate.get("candidate_id"),
                candidate.get("candidateId"),
                candidate.get("id"),
                candidate.get("Id"),
            )
        ).lower()
        if candidate_id:
            hard_selected_ids.add(candidate_id)
        for key in ("nome", "Nome", "nomi", "Nomi", "full_name", "name"):
            raw_name = fa._safe_str(candidate.get(key))
            if raw_name:
                hard_selected_full_names.add(raw_name)

    id_universe_filter = fa._combine_odata_filters(
        fa._build_search_in_filter("candidate_id", hard_selected_ids),
        fa._build_search_in_filter("id", hard_selected_ids),
        fa._build_search_in_filter("document_id", hard_selected_ids),
    )
    id_universe_filter_no_candidate_id = fa._combine_odata_filters(
        fa._build_search_in_filter("id", hard_selected_ids),
        fa._build_search_in_filter("document_id", hard_selected_ids),
    )
    name_universe_filter = fa._build_search_in_filter("full_name", hard_selected_full_names)
    universe_filter = id_universe_filter or name_universe_filter

    eligibility_filter = build_odata_filter(
        skills=p["skills"],
        min_experience_years=p.get("min_experience_years"),
        max_experience_years=p.get("max_experience_years"),
    )
    odata_filter = fa._combine_odata_filters(universe_filter, eligibility_filter)

    lexical_query = " ".join(filter(None, [p["role"] or "", " ".join(p["skills"])] )).strip()
    semantic_query = (p["query"] or "").strip() or lexical_query

    search = SearchService()
    index_name = resolve_index(p.get("subco"))

    result = {
        "case": case_name,
        "request": request,
        "hard": {
            "role": hard_role,
            "seniority": hard_seniority,
            "sede": hard_sede,
            "work_mode": hard_work_mode,
            "lingue": hard_language,
            "disponibilita": hard_disponibilita,
            "budget": hard_budget,
        },
        "mcflash": {
            "matched_candidates": len(hard_selected),
            "sample_candidates": [
                {
                    "id": c.get("id") or c.get("Id"),
                    "nome": c.get("nome") or c.get("Nome"),
                    "sede": c.get("sede") or c.get("Sede"),
                    "seniority": c.get("seniority") or c.get("Seniorita"),
                    "ruolo": c.get("ruolo") or c.get("Ruolo"),
                }
                for c in hard_selected[:3]
            ],
        },
        "filters": {
            "id_universe_filter": id_universe_filter,
            "name_universe_filter": name_universe_filter,
            "eligibility_filter": eligibility_filter,
            "odata_filter": odata_filter,
        },
        "search": {},
    }

    try:
        hits = await search.search_chunks(
            lexical_query=lexical_query,
            semantic_query=semantic_query,
            odata_filter=odata_filter,
            embedding=None,
            top=15,
            index_name=index_name,
        )
        result["search"]["with_candidate_id_clause"] = {
            "ok": True,
            "hits": len(hits),
        }
    except Exception as exc:
        result["search"]["with_candidate_id_clause"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        odata_no_candidate_id = fa._combine_odata_filters(id_universe_filter_no_candidate_id or name_universe_filter, eligibility_filter)
        hits2 = await search.search_chunks(
            lexical_query=lexical_query,
            semantic_query=semantic_query,
            odata_filter=odata_no_candidate_id,
            embedding=None,
            top=15,
            index_name=index_name,
        )
        result["search"]["without_candidate_id_clause"] = {
            "ok": True,
            "hits": len(hits2),
            "odata_filter": odata_no_candidate_id,
        }
    except Exception as exc:
        result["search"]["without_candidate_id_clause"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return result


async def main():
    df = pd.read_excel(Path("eval_classifier_20260610152836.xlsx"), sheet_name="eval")
    rows = df[["query", "classified_skills", "classified_role", "classified_location", "classified_seniority", "classified_language", "classified_work_mode"]].iloc[[1, 2]].to_dict(orient="records")

    requests = []
    for row in rows:
        requests.append({
            "query": as_opt_str(row.get("query")) or "",
            "skills": parse_skills(row.get("classified_skills")),
            "role": as_opt_str(row.get("classified_role")),
            "location": as_opt_str(row.get("classified_location")),
            "seniority": as_opt_str(row.get("classified_seniority")),
            "language": as_opt_str(row.get("classified_language")),
            "work_mode": as_opt_str(row.get("classified_work_mode")) or "unknown",
            "top": 3,
            "hybrid": False,
        })

    out = []
    out.append(await inspect_case("case_2", requests[0]))
    out.append(await inspect_case("case_3", requests[1]))

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
