"""M16 regular NSE equity-session guard, independent of feed availability.

2026 EQUITIES TRADING holidays (not the longer clearing-holiday list):
https://www.nseindia.com/resources/exchange-communication-holidays
Verified against the published 2026 table; no runtime NSE request is needed.
Maintain this list when exchange circulars announce changes/new years. Special
sessions outside 09:15-15:30 are not supported by M16's regular-session rules.
Never infer an exchange holiday from a missing Yahoo response.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo('Asia/Kolkata')
HOLIDAYS = {
    '2026-01-15': 'Municipal Corporation Election - Maharashtra',
    '2026-01-26': 'Republic Day',
    '2026-03-03': 'Holi',
    '2026-03-26': 'Shri Ram Navami',
    '2026-03-31': 'Shri Mahavir Jayanti',
    '2026-04-03': 'Good Friday',
    '2026-04-14': 'Dr. Baba Saheb Ambedkar Jayanti',
    '2026-05-01': 'Maharashtra Day',
    '2026-05-28': 'Bakri Id',
    '2026-06-26': 'Muharram',
    '2026-09-14': 'Ganesh Chaturthi',
    '2026-10-02': 'Mahatma Gandhi Jayanti',
    '2026-10-20': 'Dussehra',
    '2026-11-10': 'Diwali-Balipratipada',
    '2026-11-24': 'Prakash Gurpurb Sri Guru Nanak Dev',
    '2026-12-25': 'Christmas',
}
COVERED_YEARS = {2026}


def session_status(now):
    """Return (open/closed/unknown, reason) for a regular day, not clock hours.

    Unknown weekdays remain eligible, preserving outage detection and avoiding
    silently suppressing real entries when the maintained calendar expires.
    """
    if now.tzinfo is None:
        raise ValueError('Calendar requires a timezone-aware clock')
    local = now.astimezone(IST)
    day = local.strftime('%Y-%m-%d')
    if day in HOLIDAYS:
        return 'closed', HOLIDAYS[day]
    if local.weekday() >= 5:
        return 'closed', 'No regular weekend session (special sessions unsupported)'
    if local.year not in COVERED_YEARS:
        return 'unknown', 'Calendar year not maintained; weekday monitoring remains enabled'
    return 'open', 'Regular NSE equity trading day'


if __name__ == '__main__':
    import sys
    status, reason = session_status(datetime.now(IST))
    print(f'regular_session={str(status != "closed").lower()}')
    print(f'M16 calendar: {status}: {reason}', file=sys.stderr)
