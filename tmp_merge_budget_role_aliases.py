import json
import re
import statistics
from collections import defaultdict

path = 'infra/mcflash_budget_placeholders.json'
data = json.load(open(path, encoding='utf-8'))

replacements = {
    'artificial intelligence': 'ai',
    'machine learning': 'ml',
    'cyber security': 'cybersecurity',
    'full stack': 'fullstack',
    'front end': 'frontend',
    'back end': 'backend',
    'help desk': 'helpdesk',
    'data base': 'database',
    'phyton': 'python',
}

def canon_role(role: str) -> str:
    r = (role or '').strip().lower()
    for old, new in replacements.items():
        r = r.replace(old, new)
    r = re.sub(r'\s+', ' ', r).strip()
    return r

acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
for role, row in data.items():
    role_c = canon_role(role)
    if not role_c or not isinstance(row, dict):
        continue
    for s, v in row.items():
        s_norm = str(s).strip().lower()
        try:
            acc[role_c][s_norm].append(float(v))
        except Exception:
            pass

merged = {}
for role in sorted(acc.keys()):
    out = {}
    for s in ('junior','mid','senior','lead','principal'):
        vals = acc[role].get(s, [])
        if vals:
            out[s] = round(float(statistics.median(vals)), 2)
    if out:
        merged[role] = out

json.dump(merged, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

# report duplicate groups against canon
reverse = defaultdict(list)
for role in data.keys():
    reverse[canon_role(role)].append(role)
merged_groups = {k:v for k,v in reverse.items() if len(v) > 1}
print({'roles_before': len(data), 'roles_after': len(merged), 'merged_groups': len(merged_groups)})
for k, vals in sorted(merged_groups.items()):
    print(k, '=>', vals)
