#!/usr/bin/env python3
"""Dart Chat daily storage report.

Once a day, at the time set in `storage_report_time`, this measures how much
space the chat server uses (photos and files, the message database and the
rest of the add-on's data) and how much room is left on the disk. Every
measurement is kept in /data/storage_history.json, so the report can say how
much it grew over the last day, month and year. The report is emailed to
`storage_report_email` through Resend, the same way invitations are sent.

Uses only the Python standard library.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DATA = os.environ.get("DARTCHAT_DATA", "/data")
DB = os.path.join(DATA, "db")
MEDIA = os.path.join(DB, "media")
COUNTS = os.environ.get(
    "DARTCHAT_MESSAGE_COUNTS", os.path.join(DATA, "message_counts.json")
)
HISTORY = os.environ.get(
    "DARTCHAT_STORAGE_HISTORY", os.path.join(DATA, "storage_history.json")
)
SERVER_NAME = os.environ.get("DARTCHAT_SERVER_NAME", "")
RESEND_KEY = os.environ.get("DARTCHAT_RESEND_KEY", "")
RESEND_BASE = os.environ.get("DARTCHAT_RESEND_BASE", "https://api.resend.com")
EMAIL_FROM = os.environ.get("DARTCHAT_INVITE_FROM", "")
TO = os.environ.get("DARTCHAT_STORAGE_REPORT_EMAIL", "").strip()
REPORT_TIME = os.environ.get("DARTCHAT_STORAGE_REPORT_TIME", "") or "08:00"
TIMEZONE = os.environ.get("DARTCHAT_TIMEZONE", "") or "Europe/Belgrade"

# Measurements older than this are thinned to one per month.
KEEP_DAILY_DAYS = 400


def log(message):
    print(f"[dartchat-storage] {message}", flush=True)


# -- measuring ---------------------------------------------------------------


def folder_size(path, skip=None):
    """Bytes used by the files under path (actual disk blocks)."""
    total = 0
    for root, dirs, files in os.walk(path):
        if skip:
            dirs[:] = [d for d in dirs if os.path.join(root, d) != skip]
        for name in files:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            blocks = getattr(info, "st_blocks", None)
            total += blocks * 512 if blocks is not None else info.st_size
    return total


def measure():
    media = folder_size(MEDIA) if os.path.isdir(MEDIA) else 0
    database = folder_size(DB, skip=MEDIA)
    everything = folder_size(DATA)
    disk = os.statvfs(DATA)
    return {
        "media": media,
        "database": database,
        "other": max(everything - media - database, 0),
        "free": disk.f_bavail * disk.f_frsize,
        "disk": disk.f_blocks * disk.f_frsize,
    }


def used(entry):
    return entry["media"] + entry["database"] + entry["other"]


# -- history -----------------------------------------------------------------


def load_history(path=HISTORY):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_history(history, path=HISTORY):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=0, sort_keys=True)
    os.replace(tmp, path)


def thin(history, today):
    """Keeps daily entries for KEEP_DAILY_DAYS, then the first of each month."""
    limit = (today - timedelta(days=KEEP_DAILY_DAYS)).isoformat()
    kept, months = {}, set()
    for day in sorted(history):
        if day >= limit:
            kept[day] = history[day]
        elif day[:7] not in months:
            months.add(day[:7])
            kept[day] = history[day]
    return kept


def on_or_before(history, day):
    """The latest measurement taken on or before day, as (day, entry)."""
    days = [d for d in history if d <= day.isoformat()]
    if not days:
        return None
    best = max(days)
    return best, history[best]


# -- the report --------------------------------------------------------------


def size(n):
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{sign}{n:.0f} bytes"
            return f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} TB"


def growth(n):
    if n == 0:
        return "no change"
    return ("+" if n > 0 else "") + size(n)


def since(history, today, start, label):
    """Growth from the measurement at start (or the oldest one) to today."""
    now = history[today.isoformat()]
    found = on_or_before(history, start)
    note = ""
    if found is None:
        first = min(history)
        found = first, history[first]
        if first == today.isoformat():
            return label, "measuring starts today", ""
        note = f" (since {date.fromisoformat(first):%d %b %Y}, first measurement)"
    return label, growth(used(now) - used(found[1])), note


def baseline(history, start, end):
    """The measurement a period [start, end] is compared with: the last one
    before it, or else the first one inside it."""
    found = on_or_before(history, start - timedelta(days=1))
    if found:
        return found
    inside = [d for d in history if start.isoformat() <= d <= end.isoformat()]
    return (min(inside), history[min(inside)]) if inside else None


def monthly_rows(history, today, months=12):
    """Growth per calendar month, newest first, as (label, change)."""
    rows = []
    year, month = today.year, today.month
    for _ in range(months):
        start = date(year, month, 1)
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        end = min(nxt - timedelta(days=1), today)
        a = baseline(history, start, end)
        b = on_or_before(history, end)
        if a and b:
            label = f"{start:%B %Y}" + (" (so far)" if end == today else "")
            rows.append((label, growth(used(b[1]) - used(a[1]))))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return rows


def yearly_rows(history, today):
    rows = []
    for year in range(today.year, int(min(history)[:4]) - 1, -1):
        start, end = date(year, 1, 1), min(date(year, 12, 31), today)
        a = baseline(history, start, end)
        b = on_or_before(history, end)
        if a and b:
            label = str(year) + (" (so far)" if end == today else "")
            rows.append((label, growth(used(b[1]) - used(a[1]))))
    return rows


def daily_rows(history, today, days=7):
    rows = []
    for back in range(days):
        day = today - timedelta(days=back)
        cur, prev = history.get(day.isoformat()), on_or_before(
            history, day - timedelta(days=1)
        )
        if cur and prev:
            rows.append((f"{day:%a %d %b}", growth(used(cur) - used(prev[1]))))
    return rows


def days_until_full(history, today):
    """At the pace of the last 30 days (or since the first measurement)."""
    now = history[today.isoformat()]
    found = on_or_before(history, today - timedelta(days=30))
    if found is None:
        first = min(history)
        found = first, history[first]
    days = (today - date.fromisoformat(found[0])).days
    grew = used(now) - used(found[1])
    if days < 3 or grew <= 0:
        return None
    return now["free"] / (grew / days)


def load_counts(path=COUNTS):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("days") else None


def message_table(counts, today):
    """Rows of (label, yesterday, this month, this year, all counted), or
    None when nothing has been counted yet. Groups come by name, private
    chats as one total."""
    if not counts:
        return None
    days = counts.get("days", {})
    names = counts.get("rooms", {})
    yesterday = (today - timedelta(days=1)).isoformat()
    month, year = today.isoformat()[:7], today.isoformat()[:4]
    periods = [
        lambda d: d == yesterday,
        lambda d: d.startswith(month),
        lambda d: d.startswith(year),
        lambda d: True,
    ]

    def total(pick):
        return [sum(pick(day, entry) for day, entry in days.items() if p(day))
                for p in periods]

    groups = sorted({r for e in days.values() for r in e.get("groups", {})})
    rows = []
    for room in groups:
        rows.append((names.get(room) or "Group without a name",
                     *total(lambda d, e, r=room: e.get("groups", {}).get(r, 0))))
    rows.sort(key=lambda row: -row[-1])
    rows.append(("Private chats (all together)", *total(lambda d, e: e.get("private", 0))))
    rows.append(("All messages", *total(
        lambda d, e: e.get("private", 0) + sum(e.get("groups", {}).values()))))
    rows.append(("Reactions", *total(lambda d, e: e.get("reactions", 0))))
    return rows, min(days)


def build_report(history, today, counts=None):
    """Returns (subject, text, html)."""
    now = history[today.isoformat()]
    total = used(now)
    disk_used_pct = 100 * (now["disk"] - now["free"]) / now["disk"] if now["disk"] else 0
    free_pct = 100 - disk_used_pct

    summary = [
        ("Dart Chat uses in total", size(total)),
        ("Photos and files", size(now["media"])),
        ("Messages and accounts (database)", size(now["database"])),
        ("Other add-on data", size(now["other"])),
        ("Free space left on the disk", f"{size(now['free'])} of {size(now['disk'])} ({free_pct:.0f}% free)"),
    ]
    periods = [
        since(history, today, today - timedelta(days=1), "Last day"),
        since(history, today, today - timedelta(days=7), "Last 7 days"),
        since(history, today, today - timedelta(days=30), "Last 30 days"),
        since(history, today, today - timedelta(days=365), "Last 12 months"),
    ]
    sections = [
        ("Day by day", daily_rows(history, today)),
        ("Month by month", monthly_rows(history, today)),
        ("Year by year", yearly_rows(history, today)),
    ]

    notes = []
    left = days_until_full(history, today)
    if left is not None:
        if left > 365 * 20:
            notes.append("At the current pace, the disk will not fill up for more than 20 years.")
        elif left > 365:
            notes.append(f"At the current pace, the disk would be full in about {left / 365:.1f} years.")
        else:
            notes.append(f"At the current pace, the disk would be full in about {left:.0f} days.")
    if free_pct < 10:
        notes.insert(0, "Warning: less than 10% of the disk is free.")

    subject = f"Dart Chat storage: {size(total)} used, {size(now['free'])} free"
    when = f"{today:%A %d %B %Y}"

    text_lines = [f"Dart Chat storage report for {when}", ""]
    text_lines += [f"{k}: {v}" for k, v in summary]
    messages = message_table(counts, today)
    heads = ("Yesterday", f"{today:%B}", str(today.year), "Since counting began")
    counted_from = ""
    if messages:
        counted_from = (
            f"Counting began on {date.fromisoformat(messages[1]):%d %b %Y}."
            " Edits are not counted, and Dart Bot's own posts are left out."
        )
        text_lines += ["", "Messages (" + " / ".join(heads) + ")"]
        text_lines += [f"{r[0]}: " + " / ".join(str(n) for n in r[1:]) for r in messages[0]]
        text_lines += [counted_from]
    text_lines += ["", "Growth"]
    text_lines += [f"{k}: {v}{n}" for k, v, n in periods]
    for title, rows in sections:
        if rows:
            text_lines += ["", title] + [f"{k}: {v}" for k, v in rows]
    if notes:
        text_lines += [""] + notes
    text_lines += ["", "Sent every day by your Dart Chat server."]
    text = "\n".join(text_lines) + "\n"

    def esc(value):
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(rows):
        return (
            '<table style="border-collapse:collapse;margin:4px 0 16px">'
            + "".join(
                '<tr><td style="padding:3px 16px 3px 0;color:#555">'
                f"{esc(k)}</td><td style=\"padding:3px 0;font-weight:bold\">{esc(v)}</td></tr>"
                for k, v in rows
            )
            + "</table>"
        )

    html = (
        '<div style="font-family:sans-serif;font-size:15px;line-height:1.4">'
        f'<p style="font-size:13px;color:#777">{esc(when)}</p>'
        f'<p style="font-size:26px;font-weight:bold;margin:0">{esc(size(total))} used</p>'
        f'<p style="margin:4px 0 16px">{esc(size(now["free"]))} free on the disk ({free_pct:.0f}%)</p>'
        + table(summary[1:])
        + (
            "<h3>Messages</h3>"
            '<table style="border-collapse:collapse;margin:4px 0 4px">'
            "<tr>" + "".join(
                f'<th style="padding:3px 12px 3px 0;text-align:{"left" if i == 0 else "right"};'
                f'font-size:13px;color:#777">{esc(h)}</th>'
                for i, h in enumerate(("",) + heads))
            + "</tr>"
            + "".join(
                "<tr>" + "".join(
                    f'<td style="padding:3px 12px 3px 0;text-align:{"left" if i == 0 else "right"};'
                    f'{"color:#555" if i == 0 else "font-weight:bold"}">{esc(str(v))}</td>'
                    for i, v in enumerate(row))
                + "</tr>"
                for row in messages[0])
            + "</table>"
            f'<p style="font-size:12px;color:#999;margin:0 0 16px">{esc(counted_from)}</p>'
            if messages else ""
        )
        + "<h3>Growth</h3>"
        + table([(k, v + n) for k, v, n in periods])
        + "".join(f"<h3>{esc(t)}</h3>" + table(r) for t, r in sections if r)
        + "".join(f"<p>{esc(n)}</p>" for n in notes)
        + '<p style="font-size:12px;color:#999">Sent every day by your Dart Chat server.</p></div>'
    )
    return subject, text, html


# -- sending -----------------------------------------------------------------


def default_sender():
    domain = SERVER_NAME.split(".", 1)[1] if SERVER_NAME.count(".") >= 2 else SERVER_NAME
    return f"Dart Chat <invites@{domain}>"


def send(subject, text, html):
    body = json.dumps(
        {
            "from": EMAIL_FROM or default_sender(),
            "to": [TO],
            "subject": subject,
            "text": text,
            "html": html,
        }
    ).encode()
    request = urllib.request.Request(f"{RESEND_BASE}/emails", data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "dartchat-storage/1.0")
    request.add_header("Authorization", f"Bearer {RESEND_KEY}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        log(f"Email service refused the storage report: {error.code} {error.read()[:200]!r}")
    except (urllib.error.URLError, OSError) as error:
        log(f"Email service unreachable: {error}")
    return False


# -- main loop ---------------------------------------------------------------


def parse_time(value):
    try:
        hour, minute = (int(part) for part in value.strip().split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    return None


def run_once(now, history, report_at):
    """One check. Returns the (possibly updated) history and whether to save."""
    today = now.date()
    key = today.isoformat()
    due = (now.hour, now.minute) >= report_at
    changed = False
    if key not in history and (due or not history):
        # Measure at report time, and once right away on the very first start.
        history[key] = measure()
        history = thin(history, today)
        changed = True
    return history, changed, due


def main():
    report_at = parse_time(REPORT_TIME)
    if report_at is None:
        log(f"storage_report_time '{REPORT_TIME}' is not a time like 08:00; using 08:00")
        report_at = (8, 0)
    try:
        tz = ZoneInfo(TIMEZONE)
    except Exception:
        log(f"Unknown timezone '{TIMEZONE}', using Europe/Belgrade")
        tz = ZoneInfo("Europe/Belgrade")
    if not TO:
        log("Daily storage email is off (set storage_report_email); still recording sizes")
    elif not RESEND_KEY:
        log("Daily storage email is off (no resend_api_key); still recording sizes")
    else:
        log(f"Daily storage email at {report_at[0]:02d}:{report_at[1]:02d} {tz.key}")

    history = load_history()
    while True:
        try:
            now = datetime.now(tz)
            history, changed, due = run_once(now, history, report_at)
            if changed:
                save_history(history)
            key = now.date().isoformat()
            sent_marker = history.get("_sent", "")
            # The very first report goes out right away, so the owner sees
            # that it works; after that, once a day at report time.
            first = not sent_marker and key in history
            if (due or first) and TO and RESEND_KEY and sent_marker < key and key in history:
                subject, text, html = build_report(
                    {d: e for d, e in history.items() if not d.startswith("_")},
                    now.date(),
                    load_counts(),
                )
                if send(subject, text, html):
                    log("Storage report sent")
                    history["_sent"] = key
                    save_history(history)
                else:
                    time.sleep(600)
        except Exception as error:  # keep going; try again next minute
            log(f"Storage report problem: {error}")
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
