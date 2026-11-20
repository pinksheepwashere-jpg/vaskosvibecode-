#!/usr/bin/env python3
"""
Roommatch watcher - alerts you when a new room appears in the Leiden area
that is NOT allocated by waiting time (first come first served, hospiteren).

Uses only the Python standard library. Run:
    python3 roommatch_watcher.py          # checks every 10 minutes until you stop it (Ctrl+C)
    python3 roommatch_watcher.py --once   # checks once and exits (for cron / Task Scheduler)
"""

import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

# ------------------------------------------------------------------ settings
CITIES = {"Leiden", "Oegstgeest", "Leiderdorp", "Voorschoten"}
MAX_RENT = 700  # total monthly rent in euro

# Allocation types Roommatch uses (field model.modelCategorie.code):
#   "reactiedatum"             -> first come, first served ("direct te huur")
#   "hospiteren"               -> current residents pick (waiting time irrelevant)
#   "hospitereninschrijfduur"  -> residents pick, waiting time as tiebreaker
#   "inschrijfduur"            -> ranked by waiting time (skipped: you rank 100+)
WANTED_TYPES = {"reactiedatum", "hospiteren", "hospitereninschrijfduur"}

CHECK_EVERY_MINUTES = 10

# Optional push notification to your phone via the free ntfy app (ntfy.sh).
# Install "ntfy" on your phone, subscribe to a topic name, and put the same
# name here. Pick something long and random - anyone who knows it can read it.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # set as a GitHub secret, or type it here
# ---------------------------------------------------------------------------

API = ("https://roommatching-aanbodapi.zig365.nl/api/v1/actueel-aanbod"
       "?limit=100&locale=en_GB&page={page}")
DETAIL_URL = "https://www.roommatch.nl/en/offerings/to-rent/details/{key}"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roommatch_seen.json")
HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roommatch_heartbeat.txt")

TYPE_LABELS = {
    "reactiedatum": "FIRST COME FIRST SERVED",
    "hospiteren": "Hospiteren",
    "hospitereninschrijfduur": "Hospiteren (waiting time tiebreak)",
}


def fetch_all():
    listings, page = [], 0
    while True:
        req = urllib.request.Request(API.format(page=page),
                                     headers={"User-Agent": "Mozilla/5.0 (personal room alert)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        listings += data.get("data", [])
        meta = data.get("_metadata", {})
        page += 1
        if page >= meta.get("page_count", 1):
            return listings


def city_of(x):
    c = x.get("city")
    return c.get("name") if isinstance(c, dict) else c


def type_of(x):
    model = x.get("model") or {}
    return (model.get("modelCategorie") or {}).get("code")


def matches(x):
    rent = x.get("totalRent") or 0
    return city_of(x) in CITIES and type_of(x) in WANTED_TYPES and rent <= MAX_RENT


def describe(x):
    addr = f"{x.get('street', '')} {x.get('houseNumber', '')}{x.get('houseNumberAddition') or ''}".strip()
    avail = (x.get("availableFromDate") or "")[:10]
    close = (x.get("closingDate") or "")[:16].replace("T", " ")
    lines = [
        f"{TYPE_LABELS.get(type_of(x), type_of(x))}: {addr}, {city_of(x)}",
        f"EUR {x.get('totalRent')} / month, {x.get('areaDwelling') or '?'} m2, available {avail}",
    ]
    if x.get("hospiterenDatum"):
        lines.append(f"Hospiteren date: {x['hospiterenDatum'][:16].replace('T', ' ')}")
    if close:
        lines.append(f"Closes: {close} (UTC)")
    lines.append(DETAIL_URL.format(key=x.get("urlKey")))
    return "\n".join(lines)


def notify(title, body, priority="high", tags="house"):
    print(f"\n*** {title} ***\n{body}\n")
    if NTFY_TOPIC:
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                                         headers={"Title": title, "Priority": priority, "Tags": tags})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"(phone notification failed: {e})")
    try:
        if platform.system() == "Darwin":
            safe = body.split("\n")[0].replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}" sound name "Glass"'])
        elif platform.system() == "Windows":
            import winsound
            winsound.MessageBeep()
    except Exception:
        pass


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


def weekly_heartbeat(total):
    """Once per calendar week: send a quiet 'still running' message and write a
    small file. The file change also keeps the GitHub repository active, so
    GitHub does not pause the schedule after 60 days without activity."""
    week = datetime.now().strftime("%G-W%V")
    try:
        with open(HEARTBEAT_FILE) as f:
            if f.read().strip() == week:
                return
    except FileNotFoundError:
        pass
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(week + "\n")
    notify("Roommatch watcher is running",
           f"Weekly check-in: still watching. {total} rooms on Roommatch right now.",
           priority="low", tags="white_check_mark")


def check(strict=False):
    """strict=True (GitHub mode): exit with an error when something is wrong,
    so the run turns red and GitHub emails you."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        listings = fetch_all()
    except Exception as e:
        print(f"[{stamp}] Could not reach Roommatch: {e}")
        if strict:
            sys.exit(1)
        return
    if not listings:
        print(f"[{stamp}] Roommatch returned 0 listings - the site may have changed.")
        if strict:
            sys.exit(1)
        return
    seen = load_seen()
    hits = [x for x in listings if matches(x)]
    new = [x for x in hits if str(x.get("id")) not in seen]
    print(f"[{stamp}] {len(listings)} listings total, {len(hits)} match your filters, {len(new)} new")
    for x in new:
        fcfs = type_of(x) == "reactiedatum"
        notify("FIRST COME FIRST SERVED - reply now!" if fcfs else "New Leiden room on Roommatch",
               describe(x), priority="urgent" if fcfs else "high")
        seen.add(str(x.get("id")))
    save_seen(seen)
    weekly_heartbeat(len(listings))


if __name__ == "__main__":
    if "--once" in sys.argv:
        check(strict=True)
    else:
        print(f"Watching Roommatch every {CHECK_EVERY_MINUTES} min. Press Ctrl+C to stop.")
        while True:
            check()
            time.sleep(CHECK_EVERY_MINUTES * 60)
