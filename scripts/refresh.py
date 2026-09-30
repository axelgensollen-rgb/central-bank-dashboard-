from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from html import unescape
import json
import os
import re
import urllib.request

TARGET = {(7, 5), (12, 5), (18, 5)}
TZ = ZoneInfo('Europe/Paris')
now = datetime.now(TZ)

# GitHub cron runs CET + CEST candidates; only the intended Paris slot continues.
if (now.hour, now.minute) not in TARGET and 'GITHUB_ACTIONS' in os.environ:
    raise SystemExit('Not a Paris target slot')

HOME_URL = 'https://centralbank.watch/'
UA = {'User-Agent': 'Mozilla/5.0 (compatible; CentralBankPulse/1.0)'}

# Heading used on Central Bank Watch -> dashboard key.
SECTIONS = [
    ('Federal Reserve', 'FED'),
    ('European Central Bank', 'BCE'),
    ('Bank of England', 'BoE'),
    ('Bank of Japan', 'BoJ'),
    ('Bank of Canada', 'BoC'),
    ('Reserve Bank of Australia', 'RBA'),
    ('Reserve Bank of New Zealand', 'RBNZ'),
    ('Swiss National Bank', 'BNS'),
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'ignore')


def visible_text(raw_html: str) -> str:
    raw_html = re.sub(r'(?is)<script.*?</script>', ' ', raw_html)
    raw_html = re.sub(r'(?is)<style.*?</style>', ' ', raw_html)
    raw_html = re.sub(r'(?s)<[^>]+>', ' ', raw_html)
    return re.sub(r'\s+', ' ', unescape(raw_html)).strip()


def section(text: str, heading: str, next_heading: str | None) -> str:
    start = text.find(heading)
    if start < 0:
        return ''
    end = text.find(next_heading, start + len(heading)) if next_heading else -1
    return text[start:end if end >= 0 else len(text)]


def number_after(block: str, label_pattern: str):
    m = re.search(label_pattern + r'\s*([0-9]+(?:\.[0-9]+)?)\s*%', block, re.I)
    return float(m.group(1)) if m else None


def parse_next_meeting(block: str):
    """Parse only the explicitly labelled next-meeting hike/hold/cut odds.

    We deliberately do NOT convert later cumulative rate-level probabilities into
    per-meeting probabilities. Those remain N/D unless a reliable source provides
    the meeting-specific odds.
    """
    date_match = re.search(
        r'Next Meeting Date\s*:?\s*([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})', block
    )
    if not date_match:
        return None

    dt = datetime.strptime(date_match.group(1), '%B %d, %Y').date().isoformat()
    up = number_after(block, r'(?:Rate Hike|Hike)')
    hold = number_after(block, r'(?:No Change|Hold)')
    down = number_after(block, r'(?:Rate Cut|Cut)')
    if None in (up, hold, down):
        return None

    total = up + hold + down
    if any(v < 0 or v > 100 for v in (up, hold, down)) or not 99.0 <= total <= 101.0:
        return None
    return {'date': dt, 'up': up, 'hold': hold, 'down': down}


def merge_meeting(bank: dict, incoming: dict) -> bool:
    meetings = bank.setdefault('meetings', [])
    existing = next((m for m in meetings if m.get('date') == incoming['date']), None)
    payload = {
        **incoming,
        'source': 'Central Bank Watch',
        'source_url': HOME_URL,
        'probability_type': 'next_meeting',
        'verified_at': now.isoformat(timespec='minutes'),
    }
    if existing is None:
        meetings.append(payload)
        meetings.sort(key=lambda x: x.get('date', '9999-99-99'))
        return True

    changed = any(existing.get(k) != payload[k] for k in ('up', 'hold', 'down'))
    # Source metadata / verification time can refresh without counting as market-data change.
    existing.update(payload)
    return changed


path = Path('data/central-banks.json')
data = json.loads(path.read_text(encoding='utf-8'))

try:
    text = visible_text(fetch(HOME_URL))
except Exception as exc:
    data['last_checked_at'] = now.isoformat(timespec='minutes')
    data['refresh_status'] = 'source_unavailable'
    data['refresh_error'] = type(exc).__name__
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Market source unavailable; preserved last verified values.')
    raise SystemExit(0)

changes = []
for idx, (heading, key) in enumerate(SECTIONS):
    next_heading = SECTIONS[idx + 1][0] if idx + 1 < len(SECTIONS) else None
    block = section(text, heading, next_heading)
    if not block or key not in data.get('banks', {}):
        continue
    parsed = parse_next_meeting(block)
    if not parsed:
        continue
    if merge_meeting(data['banks'][key], parsed):
        changes.append(key)

# Always record that the source was checked. Only move updated_at when actual
# market probabilities changed. This prevents a fake 'data updated' timestamp.
data['last_checked_at'] = now.isoformat(timespec='minutes')
data['probability_live_source'] = 'Central Bank Watch'
data['probability_live_source_url'] = HOME_URL
data.pop('refresh_error', None)

if changes:
    data['updated_at'] = now.isoformat(timespec='minutes')
    data['refresh_status'] = 'market_data_updated'
    data['refresh_changes'] = changes
    print('Market probabilities updated:', ', '.join(changes))
else:
    data['refresh_status'] = 'checked_no_change'
    data['refresh_changes'] = []
    print('Source checked; no probability change detected.')

path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
