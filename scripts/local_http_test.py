import requests, json, sys, time

BASE = 'http://localhost:7071/api'

def post(path, payload=None, files=None):
    url = BASE + path
    try:
        if files:
            r = requests.post(url, files=files, timeout=30)
        else:
            r = requests.post(url, json=payload or {}, timeout=30)
        print(f"POST {url} -> {r.status_code}")
        try:
            print(r.json())
        except Exception:
            print(r.text[:1000])
    except Exception as e:
        print('ERROR CALL', url, e)

# 1) search
print('\n== SEARCH test ==')
post('/search', {'query': 'data scientist', 'top': 5, 'hybrid': False})

# 2) backfill dry run
print('\n== BACKFILL test (dry_run=true) ==')
post('/backfill/incoming-cv', {'dry_run': True, 'max_items': 10})

# 3) extract small text via raw bytes
print('\n== EXTRACT test (small text) ==')
sample = b"Hello world\nThis is a quick test CV text."
files = {'file': ('test.txt', sample, 'text/plain')}
post('/extract', None, files=files)
