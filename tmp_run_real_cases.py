import asyncio
import ast
import json
from pathlib import Path

import pandas as pd

import function_app as fa


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


async def run_case(row):
    request = {
        "query": as_opt_str(row.get("query")) or "",
        "skills": parse_skills(row.get("classified_skills")),
        "role": as_opt_str(row.get("classified_role")),
        "location": as_opt_str(row.get("classified_location")),
        "seniority": as_opt_str(row.get("classified_seniority")),
        "language": as_opt_str(row.get("classified_language")),
        "work_mode": as_opt_str(row.get("classified_work_mode")) or "unknown",
        "top": 3,
        "hybrid": False,
    }

    try:
        out = await asyncio.wait_for(fa._run_search_pipeline(request), timeout=45)
        return {
            "ok": True,
            "request": request,
            "hits": len(out.get("hits", [])),
            "sample_hit": (out.get("hits") or [None])[0],
            "meta": out.get("meta", {}),
            "suggestions": out.get("suggestions", []),
        }
    except Exception as exc:
        return {
            "ok": False,
            "request": request,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def main():
    path = Path("eval_classifier_20260610152836.xlsx")
    df = pd.read_excel(path, sheet_name="eval")
    rows = df[["query", "classified_skills", "classified_role", "classified_location", "classified_seniority", "classified_language", "classified_work_mode"]].head(3).to_dict(orient="records")

    results = []
    for row in rows:
        res = await run_case(row)
        results.append(res)

    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
