"""
Water Disconnection Monitor Module
"""

import re
import os
import json
import aiohttp
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from loguru import logger

from src.core.config import LOGS_DIR

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass


@dataclass
class WaterStatus:
    ok: bool = True
    last_check: Optional[datetime] = None
    matches: list = field(default_factory=list)
    error: Optional[str] = None


WATER_CONFIG = {
    "base_url": os.getenv("WATER_FEED_URL", ""),
    "address_pattern": os.getenv("WATER_ADDRESS_PATTERN", r"Main\s+Street\s*,?\s*1\b"),
    "check_interval": int(os.getenv("WATER_CHECK_INTERVAL", 3600)),
    "timeout": 30,
}

def _email_config() -> dict:
    """Read notification config live from the runtime store (with env fallback)."""
    from src.web.runtime_config import get_notification_config, get_notification_recipients
    cfg = dict(get_notification_config("water"))
    cfg["to"] = get_notification_recipients("water")
    return cfg


def _water_pattern() -> str:
    """Read the Water address pattern live from the runtime store."""
    from src.web.runtime_config import get_water_pattern
    return get_water_pattern()

CACHE_FILE = LOGS_DIR / "water_status.json"
NOTIFIED_FILE = LOGS_DIR / "water_notified.json"

_cached_status: Optional[WaterStatus] = None
_last_check_time: Optional[datetime] = None


def send_email_notification(matches: list) -> bool:

    EMAIL_CONFIG = _email_config()
    if not EMAIL_CONFIG["enabled"]:
        logger.debug("Water email notifications disabled")
        return False

    if not matches:
        return False

    logger.info(f"Water: Sending email notification to {EMAIL_CONFIG['to']}")

    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG["from"]
        msg['To'] = ', '.join(EMAIL_CONFIG["to"])
        _domain = EMAIL_CONFIG["from"].split("@")[-1] if "@" in EMAIL_CONFIG["from"] else None
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid(domain=_domain)
        msg['Subject'] = f"⚠️ Possible water disconnection ({datetime.now().strftime('%d.%m.%Y')})"

        body = f"""⚠️ A water disconnection matches a monitored address.

Checked: {datetime.now().strftime('%d.%m.%Y %H:%M')}
Address pattern: {_water_pattern()}

{'='*50}
FOUND:
{'='*50}

"""
        for match in matches:
            body += f"{match}\n\n"

        body += f"""
{'='*50}
Details on the provider's site:
{WATER_CONFIG['base_url']}

--
NetOps Control Plane
"""

        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"], timeout=30)
        if EMAIL_CONFIG.get("smtp_tls"):
            server.starttls()
        if EMAIL_CONFIG.get("smtp_user"):
            server.login(EMAIL_CONFIG["smtp_user"], EMAIL_CONFIG.get("smtp_password", ""))
        server.sendmail(EMAIL_CONFIG["from"], EMAIL_CONFIG["to"], msg.as_string())
        server.quit()

        logger.info("Water: Email sent successfully!")
        return True

    except Exception as e:
        logger.error(f"Water: Email error: {e}")
        return False


def _match_signature(match: str) -> str:
    """Stable id for one disconnection: its first line."""
    return match.split('\n', 1)[0].strip()


def _match_end(match: str) -> Optional[datetime]:
    """Parse the restoration time ('until: dd.mm.yyyy hh:mm') if present."""
    m = re.search(r'until[^0-9]*(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2}))?', match)
    if not m:
        return None
    try:
        return datetime(
            int(m.group(3)), int(m.group(2)), int(m.group(1)),
            int(m.group(4) or 23), int(m.group(5) or 59),
        )
    except ValueError:
        return None


def filter_current_matches(matches: list) -> list:
    """Drop disconnections whose restoration time has already passed. Entries
    without a parseable end time (plain notifications) are kept for the day and
    expire via the day-level cache reset."""
    now = datetime.now()
    out = []
    for m in matches:
        end = _match_end(m)
        if end and now > end:
            continue
        out.append(m)
    return out


def get_notified_signatures() -> dict:
    """Return {signature: date} of disconnections already emailed about."""
    try:
        if NOTIFIED_FILE.exists():
            with open(NOTIFIED_FILE, 'r') as f:
                data = json.load(f)
            sigs = data.get('sigs')
            if isinstance(sigs, dict):
                return sigs
    except Exception:
        pass
    return {}


def mark_notified(matches: list):
    """Record disconnections as emailed; prune entries older than today."""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        sigs = get_notified_signatures()
        for m in matches:
            sigs[_match_signature(m)] = today
        sigs = {s: d for s, d in sigs.items() if d >= today}

        NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTIFIED_FILE, 'w') as f:
            json.dump({'sigs': sigs, 'updated': datetime.now().isoformat()}, f)
    except Exception as e:
        logger.debug(f"Water: failed to mark notified: {e}")


def invalidate():
    """Drop the in-memory cache so the next check re-fetches immediately.
    Called when the monitored address pattern changes."""
    global _cached_status, _last_check_time
    _cached_status = None
    _last_check_time = None
    logger.info("Water: cache invalidated (pattern changed)")


async def fetch_water_page(date_str: str, tab: int = 2) -> str:
    url = f"{WATER_CONFIG['base_url']}?date={date_str}&tab={tab}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=WATER_CONFIG['timeout'])) as response:
                if response.status == 200:
                    return await response.text()
                return ""
    except Exception as e:
        logger.warning(f"Water fetch error: {e}")
        return ""


def decode_html_entities(text: str) -> str:
    replacements = {
        '&nbsp;': ' ',
        '&ndash;': '–',
        '&mdash;': '—',
        '&Icirc;': 'Î',
        '&icirc;': 'î',
        '&bdquo;': '„',
        '&rdquo;': '"',
        '&ldquo;': '"',
        '&laquo;': '«',
        '&raquo;': '»',
        '&amp;': '&',
        '&lt;': '<',
        '&gt;': '>',
        '&quot;': '"',
        '&apos;': "'",
        '&#39;': "'",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    return text


def parse_notifications(html: str, regex) -> list:
    """Parse tab=1 notifications (text format with planned works)."""
    matches = []

    time_match = re.search(
        r'intervalul[^<]*<em><strong>([^<]+)</strong></em>.*?următoarele străzi.*?</p>\s*<p[^>]*><span[^>]*><strong><em>([^<]+)</em></strong>',
        html,
        re.DOTALL | re.IGNORECASE
    )

    if time_match:
        time_date = decode_html_entities(time_match.group(1).strip())
        streets = decode_html_entities(time_match.group(2).strip())

        if regex.search(streets):
            match_str = f"[Notice] ⚠️ Planned disconnection\n   📅 {time_date}\n   📍 Streets: {streets}"
            matches.append(match_str)

    return matches


def parse_table_rows(html: str, regex) -> list:
    """Parse tab=2 HTML table and find matching addresses."""
    matches = []

    current_sector = ""

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)

    for row in rows:
        sector_match = re.search(r"<td colspan=['\"]?\d+['\"]?>([^<]+)</td>", row, re.IGNORECASE)
        if 'sector_title' in row and sector_match:
            current_sector = decode_html_entities(sector_match.group(1).strip())
            continue

        tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        if len(tds) < 4:
            continue

        def clean_td(td):
            text = re.sub(r'<[^>]+>', '', td)
            text = decode_html_entities(text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text

        tds_clean = [clean_td(td) for td in tds]
        full_row_text = ' '.join(tds_clean)

        if regex.search(full_row_text):
            status = tds_clean[0] if tds_clean[0] else ""
            street = tds_clean[2] if len(tds_clean) > 2 else ""
            number = tds_clean[3] if len(tds_clean) > 3 else ""
            problem = tds_clean[4] if len(tds_clean) > 4 else ""

            dates = re.findall(r'\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2})?', full_row_text)
            date_info = ""
            if dates:
                if len(dates) >= 2:
                    date_info = f"From: {dates[0]}, until: {dates[1]}"
                else:
                    date_info = f"Date: {dates[0]}"

            match_str = f"[Works] 📍 {current_sector}: {street} {number}"
            if status:
                match_str += f" [{status}]"
            if problem:
                match_str += f"\n   Cause: {problem}"
            if date_info:
                match_str += f"\n   {date_info}"

            matches.append(match_str)

    return matches


async def check_water_status(force: bool = False) -> WaterStatus:
    global _cached_status, _last_check_time

    if not force and _cached_status and _last_check_time:
        elapsed = (datetime.now() - _last_check_time).total_seconds()
        if elapsed < WATER_CONFIG['check_interval']:
            _cached_status.matches = filter_current_matches(_cached_status.matches)
            _cached_status.ok = len(_cached_status.matches) == 0
            return _cached_status

    if not WATER_CONFIG['base_url']:
        logger.warning("Water: WATER_FEED_URL not set - skipping")
        return WaterStatus(ok=True, last_check=datetime.now(), matches=[], error="not configured")

    logger.info(f"Water: checking for '{_water_pattern()}'...")

    today = datetime.now().strftime("%Y-%m-%d")
    pattern = _water_pattern()

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.error(
            f"Water: invalid address pattern {pattern!r}: {e}. "
            "Fix it in the admin panel (Water outage pattern), e.g. 'Main\\s+Street\\s*,?\\s*1\\b'."
        )
        status = WaterStatus(ok=True, last_check=datetime.now(), matches=[], error=f"invalid pattern: {e}")
        _cached_status = status
        _last_check_time = datetime.now()
        return status

    all_matches = []

    # Tab 1 = Planned notifications
    content1 = await fetch_water_page(today, tab=1)
    if content1:
        matches1 = parse_notifications(content1, regex)
        all_matches.extend(matches1)

    # Tab 2 = Current works
    content2 = await fetch_water_page(today, tab=2)
    if content2:
        matches2 = parse_table_rows(content2, regex)
        all_matches.extend(matches2)

    all_matches = filter_current_matches(all_matches)

    # Send email for disconnections we haven't emailed about yet (by content,
    # so a newly added address still triggers even if today was already notified)
    if all_matches:
        notified = get_notified_signatures()
        new_matches = [m for m in all_matches if _match_signature(m) not in notified]
        if new_matches and send_email_notification(new_matches):
            mark_notified(new_matches)

    status = WaterStatus(
        ok=len(all_matches) == 0,
        last_check=datetime.now(),
        matches=all_matches,
    )

    _cached_status = status
    _last_check_time = datetime.now()

    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump({
                "ok": status.ok,
                "last_check": status.last_check.isoformat() if status.last_check else None,
                "matches": status.matches,
                "date_checked": today,
            }, f)
    except Exception as e:
        logger.debug(f"Water: failed to save cache: {e}")

    if all_matches:
        logger.warning(f"Water: FOUND {len(all_matches)} matches!")
    else:
        logger.info("Water: no disconnections found")

    return status


def load_cached_status() -> Optional[WaterStatus]:
    global _cached_status, _last_check_time

    if _cached_status:
        _cached_status.matches = filter_current_matches(_cached_status.matches)
        _cached_status.ok = len(_cached_status.matches) == 0
        return _cached_status

    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, 'r') as f:
                data = json.load(f)

                cached_date = data.get('date_checked')
                today = datetime.now().strftime("%Y-%m-%d")
                if cached_date != today:
                    return None

                matches = filter_current_matches(data.get('matches', []))
                _cached_status = WaterStatus(
                    ok=len(matches) == 0,
                    last_check=datetime.fromisoformat(data['last_check']) if data.get('last_check') else None,
                    matches=matches,
                )
                if _cached_status.last_check:
                    _last_check_time = _cached_status.last_check
                return _cached_status
    except Exception as e:
        logger.debug(f"Water: failed to load cache: {e}")
    return None


def get_status_dict() -> dict:
    status = load_cached_status()
    if status:
        return {
            "ok": status.ok,
            "last_check": status.last_check.isoformat() if status.last_check else None,
            "matches_count": len(status.matches),
            "matches": status.matches,
        }
    return {
        "ok": True,
        "last_check": None,
        "matches_count": 0,
        "matches": [],
    }
