from infra.mcflash_candidates import MCFlashCandidatesClient as C

base = C._load_role_budget_placeholders()
red = C._build_reduced_role_budget_placeholders()

print('BASE_COUNT', len(base))
print('REDUCED_COUNT', len(red))

print('\n=== BASE TABLE SAMPLE (first 15 roles) ===')
for role in sorted(base.keys())[:15]:
    print(f"{role} => {base[role]}")

probes = [
    ('java developer', 'senior'),
    ('.net developer', 'senior'),
    ('data scientist/ai engineer', 'senior'),
    ('front end/mobile developer', 'mid'),
    ('devops engineer', 'lead'),
    ('qa guru', 'senior'),
]

print('\n=== LOOKUP PROBES (resolved cap) ===')
for role, sen in probes:
    cap = C._resolve_placeholder_budget_cap(role=role, seniority=sen)
    print({'role': role, 'seniority': sen, 'resolved_cap': cap})

print('\n=== REDUCED TOKENS CONTAINING java (up to 20) ===')
java_tokens = [k for k in sorted(red.keys()) if 'java' in k][:20]
for token in java_tokens:
    print(f"{token} => {red[token]}")
