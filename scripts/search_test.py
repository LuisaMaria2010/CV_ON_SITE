import json
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient

ls = json.load(open('local.settings.json'))
vals = ls['Values']
endpoint = vals.get('SEARCH_ENDPOINT') or vals.get('AZURE_SEARCH_SERVICE_ENDPOINT')
key = vals.get('AZURE_SEARCH_API_KEY')
index = vals.get('DOCUMENT_SEARCH_INDEX_NAME') or 'cv-doc-chunks'
print('endpoint=', endpoint)
print('index=', index)
client = SearchClient(endpoint=endpoint, index_name=index, credential=AzureKeyCredential(key))
query = 'data scientist'
print('Querying for:', query)
results = client.search(query, top=5)
count = 0
for r in results:
    count += 1
    print('--- result', count, '---')
    try:
        print(json.dumps(r, ensure_ascii=False, default=str, indent=2))
    except Exception:
        print(dict(r))
print('done, results returned:', count)
