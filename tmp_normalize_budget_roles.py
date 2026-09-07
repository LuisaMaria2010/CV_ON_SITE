import json
import re
import statistics
from pathlib import Path

path = Path('infra/mcflash_budget_placeholders.json')
raw = json.loads(path.read_text(encoding='utf-8'))

separators = re.compile(r"\s*(?:/|\||,|;|&|\+|\b e \b|\b and \b|\s-\s)\s*", re.IGNORECASE)


def explode_role_tokens(role: str) -> list[str]:
    text = re.sub(r"\s+", " ", (role or '').strip().lower())
    if not text:
        return []
    parts = separators.split(text)
    out = []
    seen = set()
    for p in parts:
        t = re.sub(r"\s+", " ", p.strip())
        if len(t) < 3:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if not out:
        return []
    return out

acc: dict[str, dict[str, list[float]]] = {}
composite_before = []
for role, row in raw.items():
    role_norm = re.sub(r"\s+", " ", str(role).strip().lower())
    tokens = explode_role_tokens(role_norm)
    if len(tokens) > 1:
        composite_before.append(role_norm)
    for token in tokens:
        by_s = acc.setdefault(token, {})
        for s_key, cap in (row or {}).items():
            s = str(s_key).strip().lower()
            try:
                v = float(cap)
            except Exception:
                continue
            by_s.setdefault(s, []).append(v)

normalized: dict[str, dict[str, float]] = {}
for role in sorted(acc.keys()):
    vals = acc[role]
    entry = {}
    for s in ('junior','mid','senior','lead','principal'):
        caps = vals.get(s, [])
        if caps:
            entry[s] = round(float(statistics.median(caps)), 2)
    if entry:
        normalized[role] = entry

path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding='utf-8')

# verify remaining composites in keys
remaining_composites = [
    k for k in normalized.keys()
    if separators.search(k)
]

print({
    'roles_before': len(raw),
    'roles_after': len(normalized),
    'composite_keys_before': len(composite_before),
    'composite_keys_after': len(remaining_composites),
})
if composite_before:
    print('sample_composites_before:', composite_before[:8])
if remaining_composites:
    print('sample_composites_after:', remaining_composites[:8])
