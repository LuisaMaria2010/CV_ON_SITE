import urllib.request
import json
import urllib.error

url='http://127.0.0.1:7071/api/backfill/incoming-cv'
data=json.dumps({'dry_run':True,'max_items':10}).encode('utf-8')
req=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req) as r:
        print('STATUS', r.status)
        print(r.read().decode())
except urllib.error.HTTPError as e:
    print('STATUS', e.code)
    try:
        print(e.read().decode())
    except Exception as ex:
        print('ERROR_READING_BODY', ex)
except Exception as e:
    print('EXC', e)
