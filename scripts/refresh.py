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
TMX_BOC_URL = 'https://www.m-x.ca/en/trading/tools/canadian-interest-rate-expectations'
BNZ_RBNZ_MARKET_URL = 'https://www.bnz.co.nz/institutional-banking/research/publications/outlook-for-borrowers'
UA = {'User-Agent': 'Mozilla/5.0 (compatible; CentralBankPulse/1.2)'}

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

MONTHS = {m: i for i, m in enumerate([
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
], 1)}


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


def upsert_curve_point(bank: dict, meeting_date: str, rate: float, source: str) -> bool:
    curve = bank.setdefault('curve', [])
    point = next((p for p in curve if p.get('date') == meeting_date), None)
    rate = round(float(rate), 3)
    if point is None:
        curve.append({'date': meeting_date, 'rate': rate, 'market_source': source})
        curve.sort(key=lambda p: p.get('date', '9999-99-99'))
        return True
    changed = point.get('rate') != rate or point.get('market_source') != source
    point['rate'] = rate
    point['market_source'] = source
    return changed


def interpolate(anchors: list[tuple[date, float]], target: date):
    anchors = sorted(anchors, key=lambda x: x[0])
    if not anchors:
        return None
    if target <= anchors[0][0]:
        return anchors[0][1]
    if target >= anchors[-1][0]:
        return anchors[-1][1]
    for (d1, r1), (d2, r2) in zip(anchors, anchors[1:]):
        if d1 <= target <= d2:
            span = (d2 - d1).days
            if span <= 0:
                return r2
            w = (target - d1).days / span
            return r1 + (r2 - r1) * w
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


def refresh_boc_tmx(data: dict, status: dict) -> list[str]:
    changes = []
    bank = data.get('banks', {}).get('BoC')
    if not bank:
        return changes
    try:
        text = visible_text(fetch(TMX_BOC_URL))
        status['tmx_boc'] = 'checked'
    except Exception as exc:
        status['tmx_boc'] = f'unavailable:{type(exc).__name__}'
        return changes

    coa_block = section(text, 'One Month CORRA Futures (COA)', 'Daily Compounded CORRA Implied by COA Prices')
    cra_block = section(text, 'Three Month CORRA Futures (CRA)', 'Daily Compounded CORRA Implied by CRA Prices')

    monthly = {}
    for mon, yr, rate in re.findall(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2}).{0,60}?COA[A-Z0-9]+.{0,45}?([0-9]+(?:\.[0-9]+)?)%', coa_block, re.I):
        monthly[(int(yr), MONTHS[mon.capitalize()])] = float(rate)

    quarter_anchors = []
    for mon, yr, rate in re.findall(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2}).{0,60}?CRA[A-Z0-9]+.{0,45}?([0-9]+(?:\.[0-9]+)?)%', cra_block, re.I):
        quarter_anchors.append((date(int(yr), MONTHS[mon.capitalize()], 15), float(rate)))

    if not monthly and not quarter_anchors:
        status['tmx_boc'] = 'checked_no_curve_parse'
        return changes

    changed = False
    for ds in bank.get('meeting_calendar', []):
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        if d < today or d.year > 2027:
            continue
        rate = monthly.get((d.year, d.month))
        source = 'TMX 1M CORRA futures'
        if rate is None:
            rate = interpolate(quarter_anchors, d)
            source = 'TMX 3M CORRA futures term structure'
        if rate is not None:
            changed |= upsert_curve_point(bank, ds, rate, source)

    if changed:
        bank['market_source'] = 'TMX Montréal Exchange COA/CRA CORRA futures'
        bank['method_note'] = 'Near meetings use 1M CORRA futures where available. Longer 2027 points are interpolated from the public 3M CORRA futures term structure; starred probabilities are derived estimates, not exact TMX meeting odds.'
        changes.append('BoC:curve')
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


def refresh_rbnz_market(data: dict, status: dict) -> list[str]:
    changes = []
    bank = data.get('banks', {}).get('RBNZ')
    if not bank:
        return changes
    try:
        text = visible_text(fetch(BNZ_RBNZ_MARKET_URL))
        status['bnz_rbnz_market'] = 'checked'
    except Exception as exc:
        status['bnz_rbnz_market'] = f'unavailable:{type(exc).__name__}'
        return changes

    m = re.search(r'Market pricing implies an OCR near\s*([0-9]+(?:\.[0-9]+)?)%\s*by year-end,?\s*rising to around\s*([0-9]+(?:\.[0-9]+)?)%\s*by December 2027', text, re.I)
    if not m:
        status['bnz_rbnz_market'] = 'checked_no_anchor_parse'
        return changes

    end_2026 = float(m.group(1))
    end_2027 = float(m.group(2))
    anchors = [(date(2026, 12, 9), end_2026), (date(2027, 12, 8), end_2027)]
    changed = False
    for ds in bank.get('meeting_calendar', []):
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        if d < date(2026, 12, 9) or d > date(2027, 12, 8):
            continue
        rate = interpolate(anchors, d)
        if rate is not None:
            changed |= upsert_curve_point(bank, ds, rate, 'BNZ public OIS market-pricing anchors')

    if changed:
        bank['market_source'] = 'RBNZ / BNZ public OIS market-pricing anchors'
        bank['method_note'] = f'Published market pricing anchors are near {end_2026:.1f}% at end-2026 and around {end_2027:.1f}% by Dec-2027. Intermediate 2027 meeting rates are linear interpolation for dashboard continuity; starred probabilities are derived estimates, not exact OIS odds for each meeting.'
        changes.append('RBNZ:curve')
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

changes += refresh_asx_rba(data, status)
changes += refresh_boc_tmx(data, status)
changes += refresh_rbnz_official(data, status)
changes += refresh_rbnz_market(data, status)
changes += refresh_cbw(data, status)

update_curve_history(data)

data['last_checked_at'] = now.isoformat(timespec='minutes')
data['source_status'] = status
data['probability_sources'] = {
    'broad': {'name': 'Central Bank Watch', 'url': CBW_URL},
    'RBA': {'name': 'ASX RBA Rate Tracker', 'url': ASX_RBA_URL},
    'BoC': {'name': 'TMX Montréal Exchange CORRA futures', 'url': TMX_BOC_URL},
    'RBNZ_official': {'name': 'Reserve Bank of New Zealand', 'url': RBNZ_OCR_URL},
    'RBNZ_market': {'name': 'BNZ public OIS market-pricing anchors', 'url': BNZ_RBNZ_MARKET_URL},
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
