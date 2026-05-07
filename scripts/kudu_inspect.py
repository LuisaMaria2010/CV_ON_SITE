#!/usr/bin/env python3
"""Inspect Kudu wwwroot and latest deployment using publishing credentials from az CLI."""
import json
import subprocess
import sys
import os
from urllib.parse import urljoin

try:
    import requests
except Exception:
    print('requests library is required. Install with: pip install requests', file=sys.stderr)
    raise

RG = 'rg_ai_SiteMC_Dev'
APP = 'dev-function-ai-mc'

def get_publishing_creds():
    # prefer pre-saved file `pub.json` (created by az) to avoid calling az from within python
    pub_file = os.path.join(os.path.dirname(__file__), 'pub.json')
    if os.path.exists(pub_file):
        with open(pub_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data['publishingUserName'], data['publishingPassword'], data.get('scmUri')
    out = subprocess.check_output([
        'az', 'functionapp', 'deployment', 'list-publishing-credentials',
        '--name', APP, '--resource-group', RG, '-o', 'json'
    ])
    data = json.loads(out)
    return data['publishingUserName'], data['publishingPassword'], data.get('scmUri')

def fetch_kudu(path, auth):
    base = f'https://{APP}.scm.azurewebsites.net'
    url = urljoin(base, path)
    r = requests.get(url, auth=auth, timeout=30)
    r.raise_for_status()
    return r.text

def main():
    user, pwd, scm = get_publishing_creds()
    auth = (user, pwd)
    print('Fetching wwwroot listing...')
    try:
        www = fetch_kudu('/api/vfs/site/wwwroot/?recursive=true', auth)
        print(www)
    except Exception as e:
        print('Failed to fetch wwwroot:', e, file=sys.stderr)
    print('\n---LATEST DEPLOYMENT---\n')
    try:
        dep = fetch_kudu('/api/deployments/latest', auth)
        print(dep)
    except Exception as e:
        print('Failed to fetch deployments/latest:', e, file=sys.stderr)

if __name__ == '__main__':
    main()
