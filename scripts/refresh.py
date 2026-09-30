from datetime import datetime, date, timedelta
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
today = now.date()

if (now.hour, now.minute) not in TARGET and 'GITHUB_ACTIONS' in os.environ:
    raise SystemExit('Not a Paris target slot')

CBW_URL = 'https://centralbank.watch/'
ASX_RBA_URL = 'https://www.asx.com.au/markets/trade-our-derivatives-market/futures-market/rba-rate-tracker'
RBNZ_OCR_URL = 'https://www.rbnz.govt.nz/monetary-policy/about-monetary-policy/the-official-cash-rate'
UA = {'User-Agent': 'Mozilla/5.0 (compatible; CentralBankPulse/1.1)'}

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
    m = re.search(r'Next Meeting Date\s*:?\s*([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})', block)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), '%B %d, %Y').date().isoformat()
    up = number_after(block, r'(?:Rate Hike|Hike)')
    hold = number_after(block, r'(?:No Change|Hold)')
    down = number_after(block, r'(?:Rate Cut|Cut)')
    if None in (up, hold, down):
        return None
    total = up + hold + down
    if any(v < 0 or v > 100 for v in (up, hold, down)) or not 99 <= total <= 101:
        return None
    return {'date': dt, 'up': up, 'hold': hold, 'down': down}


def merge_meeting(bank: dict, incoming: dict, source: str, source_url: str, priority: int = 10) -> bool:
    meetings = bank.setdefault('meetings', [])
    existing = next((m for m in meetings if m.get('date') == incoming['date']), None)
    payload = {
        **incoming,
        'source': source,
        'source_url': source_url,
        'probability_type': 'next_meeting',
        'source_priority': priority,
        'verified_at': now.isoformat(timespec='minutes'),
    }
    if existing is None:
        meetings.append(payload)
        meetings.sort(key=lambda x: x.get('date', '9999-99-99'))
        return True

    old_priority = int(existing.get('source_priority', 0))
    if old_priority > priority:
        existing['verified_at'] = now.isoformat(timespec='minutes')
        return False

    changed = any(existing.get(k) != payload[k] for k in ('up', 'hold', 'down'))
    existing.update(payload)
    return changed


def parse_date_loose(s: str):
    for fmt in ('%d %B %Y', '%d %b %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def refresh_asx_rba(data: dict, status: dict) -> list[str]:
    changes = []
    try:
        text = visible_text(fetch(ASX_RBA_URL))
        status['asx_rba'] = 'checked'
    except Exception as exc:
        status['asx_rba'] = f'unavailable:{type(exc).__name__}'
        return changes

    bank = data.get('banks', {}).get('RBA')
    if not bank:
        return changes

    # Official ASX tracker: next meeting and next-meeting probability.
    dm = re.search(r'next RBA Board meeting.*?will be on the\s+(\d{1,2}(?:st|nd|rd|th)?\s+of\s+[A-Z][a-z]+\s+20\d{2})', text, re.I)
    meeting_date = None
    if dm:
        cleaned = re.sub(r'(\d)(st|nd|rd|th)', r'\1', dm.group(1), flags=re.I).replace(' of ', ' ')
        meeting_date = parse_date_loose(cleaned)

    pm = re.search(r'indicating\s+(\d+(?:\.\d+)?)%\s+expectation of an interest rate\s+(increase|decrease).*?to\s+([0-9]+(?:\.[0-9]+)?)%', text, re.I)
    if meeting_date and meeting_date >= today and pm:
        prob = float(pm.group(1))
        direction = pm.group(2).lower()
        up = prob if direction == 'increase' else 0.0
        down = prob if direction == 'decrease' else 0.0
        hold = max(0.0, 100.0 - prob)
        if merge_meeting(bank, {'date': meeting_date.isoformat(), 'up': up, 'hold': hold, 'down': down}, 'ASX RBA Rate Tracker', ASX_RBA_URL, priority=100):
            changes.append('RBA:probability')

    # Parse the published monthly implied-yield curve where present.
    pairs = re.findall(r'([A-Z][a-z]{2})-(\d{2})\s+\|?\s*([0-9]+(?:\.[0-9]+)?)\s+\|?\s*[0-9]+(?:\.[0-9]+)?', text)
    if pairs:
        month_map = {m: i for i, m in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'], 1)}
        monthly = {(2000 + int(yy), month_map[mon]): float(rate) for mon, yy, rate in pairs if mon in month_map}
        changed_curve = False
        for point in bank.get('curve', []):
            try:
                d = date.fromisoformat(point['date'])
            except Exception:
                continue
            v = monthly.get((d.year, d.month))
            if v is not None and point.get('rate') != v:
                point['rate'] = v
                point['market_source'] = 'ASX RBA Rate Tracker'
                changed_curve = True
        if changed_curve:
            changes.append('RBA:curve')

    return changes


def refresh_rbnz_official(data: dict, status: dict) -> list[str]:
    changes = []
    try:
        text = visible_text(fetch(RBNZ_OCR_URL))
        status['rbnz_official'] = 'checked'
    except Exception as exc:
        status['rbnz_official'] = f'unavailable:{type(exc).__name__}'
        return changes

    bank = data.get('banks', {}).get('RBNZ')
    if not bank:
        return changes

    rm = re.search(r'Official Cash Rate\s*([0-9]+(?:\.[0-9]+)?)\s*%', text, re.I)
    if rm:
        official_rate = float(rm.group(1))
        if bank.get('rate') != official_rate:
            bank['rate'] = official_rate
            changes.append('RBNZ:rate')

    nm = re.search(r'Next update:\s*\d{1,2}:\d{2}(?:am|pm)?\s*,?\s*(\d{1,2}\s+[A-Z][a-z]+\s+20\d{2})', text, re.I)
    if nm:
        d = parse_date_loose(nm.group(1))
        if d and bank.get('next') != d.isoformat():
            bank['next'] = d.isoformat()
            changes.append('RBNZ:next')

    bank['official_verified_at'] = now.isoformat(timespec='minutes')
    bank['official_source_url'] = RBNZ_OCR_URL
    return changes


def refresh_cbw(data: dict, status: dict) -> list[str]:
    changes = []
    try:
        text = visible_text(fetch(CBW_URL))
        status['central_bank_watch'] = 'checked'
    except Exception as exc:
        status['central_bank_watch'] = f'unavailable:{type(exc).__name__}'
        return changes

    for idx, (heading, key) in enumerate(SECTIONS):
        next_heading = SECTIONS[idx + 1][0] if idx + 1 < len(SECTIONS) else None
        block = section(text, heading, next_heading)
        if not block or key not in data.get('banks', {}):
            continue
        parsed = parse_next_meeting(block)
        if not parsed:
            continue
        # ASX has priority over CBW for RBA if both provide the same meeting.
        if merge_meeting(data['banks'][key], parsed, 'Central Bank Watch', CBW_URL, priority=50):
            changes.append(f'{key}:probability')
    return changes


def clone_curve(curve):
    return [{'date': p.get('date'), 'rate': p.get('rate')} for p in curve or [] if p.get('date') and p.get('rate') is not None]


def closest_snapshot(snaps: dict, target: date):
    candidates = []
    for ds, curve in snaps.items():
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        candidates.append((abs((d - target).days), d, curve))
    if not candidates:
        return []
    candidates.sort(key=lambda x: (x[0], -x[1].toordinal()))
    gap, _, curve = candidates[0]
    return curve if gap <= 3 else []


def update_curve_history(data: dict):
    for bank in data.get('banks', {}).values():
        curve = clone_curve(bank.get('curve'))
        if not curve:
            continue
        snaps = bank.setdefault('curve_snapshots', {})
        # Seed the previous data timestamp once when possible.
        if not snaps and data.get('updated_at'):
            try:
                old_day = datetime.fromisoformat(data['updated_at']).date().isoformat()
                snaps[old_day] = clone_curve(bank.get('curve'))
            except Exception:
                pass
        snaps[today.isoformat()] = curve
        cutoff = today - timedelta(days=45)
        for ds in list(snaps):
            try:
                if date.fromisoformat(ds) < cutoff:
                    del snaps[ds]
            except ValueError:
                del snaps[ds]
        bank['curve_history'] = {
            'week': closest_snapshot(snaps, today - timedelta(days=7)),
            'month': closest_snapshot(snaps, today - timedelta(days=30)),
        }


path = Path('data/central-banks.json')
data = json.loads(path.read_text(encoding='utf-8'))
status = {}
changes = []

# Source priority: official/specialist market sources first, then broad fallback.
changes += refresh_asx_rba(data, status)
changes += refresh_rbnz_official(data, status)
changes += refresh_cbw(data, status)

update_curve_history(data)

data['last_checked_at'] = now.isoformat(timespec='minutes')
data['source_status'] = status
data['probability_sources'] = {
    'broad': {'name': 'Central Bank Watch', 'url': CBW_URL},
    'RBA': {'name': 'ASX RBA Rate Tracker', 'url': ASX_RBA_URL},
    'RBNZ_official': {'name': 'Reserve Bank of New Zealand', 'url': RBNZ_OCR_URL},
}
data.pop('refresh_error', None)

if changes:
    data['updated_at'] = now.isoformat(timespec='minutes')
    data['refresh_status'] = 'data_updated'
    data['refresh_changes'] = sorted(set(changes))
    print('Dashboard data updated:', ', '.join(data['refresh_changes']))
else:
    data['refresh_status'] = 'checked_no_change'
    data['refresh_changes'] = []
    print('Sources checked; no reliable data change detected.')

path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
