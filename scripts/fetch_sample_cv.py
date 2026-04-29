#!/usr/bin/env python3
"""
Scarica il primo blob .md trovato in `normalized-cv-md` e stampa il front matter YAML.
Usa la connection string presente in local.settings.json.
"""
import json
import re
from pathlib import Path
from azure.storage.blob import BlobServiceClient

ROOT = Path(__file__).resolve().parents[1]
LS = ROOT / 'local.settings.json'
if not LS.exists():
    print('local.settings.json not found')
    raise SystemExit(1)

cfg = json.loads(LS.read_text(encoding='utf-8'))
vals = cfg.get('Values', {}) or {}
conn = vals.get('AzureWebJobsStorage')
if not conn:
    print('AzureWebJobsStorage connection string not set in local.settings.json')
    raise SystemExit(1)

container = vals.get('STORAGE_CONTAINER_NORMALIZED_MARKDOWN') or 'normalized-cv-md'

client = BlobServiceClient.from_connection_string(conn)
container_client = client.get_container_client(container)
print('Listing blobs in', container)
blobs = list(container_client.list_blobs())
if not blobs:
    print('No blobs found in container')
    raise SystemExit(0)

# prefer .md blobs
md_blob = None
for b in blobs:
    if b.name.lower().endswith('.md'):
        md_blob = b
        break
if not md_blob:
    md_blob = blobs[0]

print('Selected blob:', md_blob.name)
blob_client = container_client.get_blob_client(md_blob.name)
data = blob_client.download_blob().readall()
try:
    text = data.decode('utf-8')
except Exception:
    text = None

if text and text.strip().startswith('---'):
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.S)
    if m:
        front = m.group(1)
        body = m.group(2)[:500]
        print('\n--- FRONT MATTER ---')
        print(front)
        print('--- END FRONT MATTER ---\n')
        print('Body (preview):')
        print(body)
    else:
        print('No YAML front matter found; printing first 500 chars:')
        print(text[:500])
else:
    print('Blob is binary or not UTF-8; saving to disk:')
    out = ROOT / 'downloaded_sample_blob'
    out.write_bytes(data)
    print('Saved to', out)

print('\nDone')
