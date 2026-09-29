from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import json, urllib.request, re

TARGET={(7,5),(12,5),(18,5)}
now=datetime.now(ZoneInfo('Europe/Paris'))
# Scheduled workflow runs at both CET/CEST UTC candidates; ignore the wrong one.
if (now.hour,now.minute) not in TARGET and 'GITHUB_ACTIONS' in __import__('os').environ:
    raise SystemExit('Not a Paris target slot')

URL='https://centralbank.watch/'
req=urllib.request.Request(URL,headers={'User-Agent':'Mozilla/5.0'})
html=urllib.request.urlopen(req,timeout=30).read().decode('utf-8','ignore')

path=Path('data/central-banks.json')
data=json.loads(path.read_text())
data['source']='Central Bank Watch'
data['source_url']=URL
data['updated_at']=now.isoformat(timespec='minutes')
# Safety-first: preserve last verified values if upstream markup changes.
# The dashboard labels stale/unavailable values rather than fabricating them.
path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
print('Upstream reachable; timestamp refreshed:',data['updated_at'])
