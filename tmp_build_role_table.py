import re, json, statistics
from collections import defaultdict
from infra.mcflash_candidates import MCFlashCandidatesClient

client = MCFlashCandidatesClient(base_url='https://mcflashtest.mcengineering.eu/api/Candidati', api_key='MCFlash-Candidati-2026!')

def norm(x):
    return (x or '').strip().lower()

def parse_budget(v):
    if v is None:
        return None
    s = str(v).strip().lower().replace(',', '.')
    nums = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', s)]
    if not nums:
        return None
    return max(nums)

def norm_seniority(s):
    s = norm(s)
    if s in {'middle', 'intermediate', 'medior'}:
        return 'mid'
    if s in {'staff', 'expert'}:
        return 'principal'
    if s.startswith('senior'):
        return 'senior'
    if s.startswith('junior'):
        return 'junior'
    if s.startswith('lead'):
        return 'lead'
    if s.startswith('princip'):
        return 'principal'
    if s.startswith('mid'):
        return 'mid'
    return s or 'unknown'

rows = []
offset = 0
page_size = 1000
while True:
    p = client._request_json({'limit': page_size, 'offset': offset})
    if isinstance(p, dict):
        p = p.get('items', [])
    if not isinstance(p, list) or not p:
        break
    rows.extend([r for r in p if isinstance(r, dict)])
    offset += len(p)

roles = defaultdict(lambda: defaultdict(list))
all_roles = set()
for r in rows:
    role = norm(r.get('Ruolo') or r.get('ruolo') or r.get('ROLE'))
    sen = norm_seniority(r.get('Seniority') or r.get('seniority'))
    b = parse_budget(r.get('Budget') or r.get('budget'))
    if role:
        all_roles.add(role)
        if b is not None:
            roles[role][sen].append(b)

all_budgets = []
for role in roles:
    for sen in roles[role]:
        all_budgets.extend(roles[role][sen])

global_median = statistics.median(all_budgets) if all_budgets else 320.0

out = {}
for role in sorted(all_roles):
    role_entry = {}
    role_vals = []
    for svals in roles.get(role, {}).values():
        role_vals.extend(svals)
    role_median = statistics.median(role_vals) if role_vals else global_median
    for sen in ['junior', 'mid', 'senior', 'lead', 'principal']:
        vals = roles.get(role, {}).get(sen, [])
        if len(vals) >= 2:
            vals2 = sorted(vals)
            idx = int(0.75 * (len(vals2) - 1))
            cap = vals2[idx]
        elif len(vals) == 1:
            cap = vals[0]
        else:
            mult = {'junior': 0.85, 'mid': 1.0, 'senior': 1.15, 'lead': 1.30, 'principal': 1.45}[sen]
            cap = role_median * mult
        role_entry[sen] = round(float(cap), 2)
    out[role] = role_entry

print(json.dumps({'total_candidates': len(rows), 'roles_count': len(out), 'global_median': round(global_median, 2)}, ensure_ascii=False))
with open('tmp_mcflash_role_budget_table.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('WROTE tmp_mcflash_role_budget_table.json')
