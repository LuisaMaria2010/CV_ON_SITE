import requests, json

BASE = 'http://localhost:7071/api'

def post_search():
    url = f"{BASE}/search"
    payload = {'query': 'data scientist', 'top': 5, 'hybrid': False}
    try:
        r = requests.post(url, json=payload, timeout=30)
        print('SEARCH status', r.status_code)
        print(r.text)
    except Exception as e:
        print('SEARCH error:', e)


def post_backfill():
    url = f"{BASE}/backfill/incoming-cv"
    payload = {'dry_run': True, 'max_items': 5}
    try:
        r = requests.post(url, json=payload, timeout=30)
        print('BACKFILL status', r.status_code)
        print(r.text)
    except Exception as e:
        print('BACKFILL error:', e)


if __name__ == '__main__':
    post_search()
    print('\n---\n')
    post_backfill()
