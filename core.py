"""Settings, time logic, check-in phrase matching and name suggestions."""
import json, os, re, shutil, sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

IST = timezone(timedelta(hours=5, minutes=30))
_ROOT = os.environ.get("APPDATA", os.path.expanduser("~"))
APP_DIR = os.path.join(_ROOT, "AttendanceAutomation")
OLD_DIR = os.path.join(_ROOT, "AttendanceAgent")       # previous name: settings, WhatsApp link and backups are carried over
if not os.path.exists(os.path.join(APP_DIR, "settings.json")) and os.path.exists(os.path.join(OLD_DIR, "settings.json")):
    try: shutil.copytree(OLD_DIR, APP_DIR, dirs_exist_ok=True)       # retried on next start if a file was locked
    except (OSError, shutil.Error): pass
os.makedirs(APP_DIR, exist_ok=True)


def base_dir():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


DEFAULTS = {
    "tracker_path": "", "group_jid": "", "group_name": "",
    "employees": [],          # {name, ids[], timing, label}
    "cl_time": "19:00", "grace_min": 0, "test_mode": True,
    "cl_last_date": "", "seen_ids": [], "hist_days": 0, "use_stated": True, "chat_aliases": {},
    "sender_aliases": {}, "start_on_login": False,
}


class Store:
    def __init__(self):
        self.path = os.path.join(APP_DIR, "settings.json")
        self.d = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except (OSError, ValueError):
            pass

    def __getitem__(self, k): return self.d[k]
    def __setitem__(self, k, v): self.d[k] = v

    def save(self):
        self.d["seen_ids"] = self.d["seen_ids"][-3000:]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=2)


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def clean_id(j):
    """'919876543210:12@s.whatsapp.net' -> '919876543210'"""
    return str(j or "").split("@")[0].split(":")[0]


def sender_alias(ids, aliases):
    """Return a locally saved display name for any known WhatsApp sender id."""
    for sender_id in ids:
        name = aliases.get(clean_id(sender_id), "")
        if name:
            return name
    return ""


def parse_timing(s):
    s = str(s).strip().upper()
    for f in ("%I:%M %p", "%I %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(s, f).time()
        except ValueError:
            pass
    raise ValueError(f"Bad time '{s}' (use e.g. 9:00 AM)")


def fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def classify(dt, timing, grace_min=0):
    limit = datetime.combine(dt.date(), parse_timing(timing)) + timedelta(minutes=int(grace_min))
    return "On-time" if dt.replace(tzinfo=None) <= limit else "Late"


IN_RE = re.compile(r"\b(reached( the)? office|office reached|reached|logged in|log ?in|checked in|in office|present)\b", re.I)
OUT_RE = re.compile(r"\b(home|leaving|left|logout|logged out|log out|leave|absent|yesterday|yday|y'day)\b", re.I)
LOGOUT_RE = re.compile(
    r"\b(logged\s*out|log\s*out|logout|signed\s*out|left\s+(?:the\s+)?office|"
    r"leaving\s+(?:the\s+)?office|leaving\s+for\s+the\s+day|going\s+home)\b",
    re.I,
)


def attendance_kind(text):
    """Classify an attendance message for the Messages view; logout never writes to the tracker."""
    text = text or ""
    if LOGOUT_RE.search(text):
        return "Logged out"
    if not is_checkin(text):
        return ""
    if re.search(r"\b(reached(?:\s+the)?\s+office|office\s+reached)\b", text, re.I):
        return "Reached office"
    return "Logged in"


def is_checkin(text):
    t = text or ""
    return bool(IN_RE.search(t)) and not OUT_RE.search(t)


_NUM = re.compile(r"(?<![\d:./])(\d{1,2})(?:[:.](\d{2}))?(?![\d/])(?:\s*([ap])\.?m\b)?", re.I)
_PRE = re.compile(r"(?:\bat|@|\baround|\babout|\bby|\bsince|\bfrom)\s*$", re.I)
_UNIT = re.compile(r"\s*(?:mins?\b|minutes?\b|hours?\b|hrs?\b)", re.I)


def stated_time(text, sent):
    """Time the person wrote in the message ('reached office at 9:09 AM', 'logged in at 12 pm', 'at 9.30', 'at 9').
    Returned as a datetime on the day it was sent; None if there is none, or it would be later than the send time.
    Without am/pm the earliest sensible hour (6:00 or later) that is not in the future is used."""
    text = text or ""
    for m in _NUM.finditer(text):
        h, mi, ap = int(m.group(1)), m.group(2), m.group(3)
        if not (_PRE.search(text[:m.start()]) or ap or mi is not None): continue     # a bare number is not a time
        if _UNIT.match(text[m.end():]): continue                                      # "10 mins", "2 hours"
        minute = int(mi or 0)
        if minute > 59: continue
        if ap:
            if not 1 <= h <= 12: continue
            hours = [h % 12 + (12 if ap.lower() == "p" else 0)]
        elif h >= 13: hours = [h] if h <= 23 else []
        else: hours = [x for x in ((h % 12), (h % 12) + 12) if x >= 6]
        for hh in hours:
            t = sent.replace(hour=hh, minute=minute, second=0, microsecond=0)
            if t <= sent: return t
    return None


def score(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    if ta <= tb or tb <= ta:
        r = max(r, 0.9)
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        r = max(r, 0.85)
    return r


def suggest(saved, sheet_names):
    """-> (best name, score, ambiguous). Suggestion only; the user confirms."""
    ranked = sorted(((score(saved, n), n) for n in sheet_names), reverse=True)
    if not ranked:
        return "", 0.0, False
    best = ranked[0]
    amb = len(ranked) > 1 and ranked[1][0] >= best[0] - 0.05 and ranked[1][0] > 0.5
    return best[1], best[0], amb


# ---------- WhatsApp "Export chat" (.txt) import ----------
CHAT_RE = re.compile(r"^\[?(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?\]?\s*(?:-\s*)?([^:]{1,60}?):\s(.*)$")


HEAD_RE = re.compile(r"^#{1,3}\s+([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})\s*$")                       # ## October 21, 2026
MD_RE = re.compile(r"^\[(\d{1,2}):(\d{2})\s*([AaPp][Mm])\]\s+\*\*(.+?):\*\*\s*(.*)$")        # [9:13 AM] **Name:** text


def parse_chat(path):
    """Parse a WhatsApp exported chat: Android/iPhone .txt (day-first) or the '## date' + '[time] **Name:** text' layout.
    -> [{name, dt, text}]"""
    out, cur = [], None
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.replace("\u202f", " ").replace("\u200e", "").replace("\u00a0", " ").rstrip("\n")
            h = HEAD_RE.match(line)
            if h:
                for fmt in ("%B %d %Y", "%b %d %Y"):
                    try: cur = datetime.strptime(f"{h.group(1)} {h.group(2)} {h.group(3)}", fmt).date(); break
                    except ValueError: pass
                continue
            md = MD_RE.match(line)
            if md and cur:
                hh, mi, ap, name, text = md.groups()
                hh = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
                out.append({"name": name.strip(), "dt": datetime(cur.year, cur.month, cur.day, hh, int(mi), tzinfo=IST), "text": text})
                continue
            m = CHAT_RE.match(line)
            if not m:
                if out and line.strip() and line.strip() != "---": out[-1]["text"] += " " + line.strip()   # multi-line message
                continue
            dd, mo, yy, hh, mi, ap, name, text = m.groups()
            hh = int(hh)
            if ap:
                ap = ap.lower().replace(".", "")
                hh = hh % 12 + (12 if ap == "pm" else 0)
            try:
                dt = datetime(int(yy) + (2000 if len(yy) == 2 else 0), int(mo), int(dd), hh, int(mi), tzinfo=IST)
            except ValueError:
                continue
            out.append({"name": name.strip(), "dt": dt, "text": text})
    return out


def build_import_ops(msgs, mapping, timings, d_from, d_to, grace=0, use_stated=False):
    """First check-in per person per weekday. mapping: export name -> sheet name; timings: sheet name -> '9:00 AM'."""
    best = {}
    for m in sorted(msgs, key=lambda m: m["dt"]):
        sheet, d = mapping.get(m["name"]), m["dt"].date()
        if not sheet or not (d_from <= d <= d_to) or d.weekday() >= 5: continue
        best.setdefault((d, sheet), m)
    ops = []
    for (d, s), m in sorted(best.items()):
        t = (stated_time(m["text"], m["dt"]) if use_stated else None) or m["dt"]
        ops.append({"kind": "in", "date": d, "name": s, "reported": fmt_time(t), "status": classify(t, timings[s], grace),
                    "note": f"time written in message; sent {fmt_time(m['dt'])}" if t is not m["dt"] else ""})
    return ops


# ---------- saved check-ins (linked employees only, check-in messages only) ----------
CHECKINS_FILE = os.path.join(APP_DIR, "checkins.json")
ATTENDANCE_MESSAGES_FILE = os.path.join(APP_DIR, "attendance_messages.json")


def load_checkins():
    try:
        with open(CHECKINS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_checkins(rows):
    with open(CHECKINS_FILE, "w", encoding="utf-8") as f:
        json.dump(rows[-4000:], f)


def load_attendance_messages():
    try:
        with open(ATTENDANCE_MESSAGES_FILE, encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        return []
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str)
        or not isinstance(row.get("ts"), (int, float))
        or not isinstance(row.get("ids"), list)
        or any(not isinstance(value, str) for value in row.get("ids", []))
        or not isinstance(row.get("name"), str)
        or not isinstance(row.get("text"), str)
        or not isinstance(row.get("kind"), str)
        or not isinstance(row.get("result", ""), str)
        or not isinstance(row.get("group", ""), str)
        for row in rows
    ):
        raise ValueError("Saved WhatsApp attendance messages have an invalid format.")
    return rows


def save_attendance_messages(rows):
    with open(ATTENDANCE_MESSAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(rows[-4000:], f, ensure_ascii=False)


def expand_ids(ids, groups):
    """WhatsApp may show the same person as a phone id or a hidden (LID) id. `groups` are id-sets known to be
    the same person (group members, people seen posting). Returns ids plus every id sharing a group with them."""
    out = set(ids)
    changed = True
    while changed:
        changed = False
        for g in groups:
            if g & out and not g <= out:
                out |= g; changed = True
    return out


# ---------- filling empty days ----------
def plan_fill(empties, events, timings, grace=0, use_stated=True, mark_cl=True, today=None, now=None, cl_time="19:00"):
    """empties: [{date, name, status('' or 'cl')}]  events: [{name, dt, text}]  timings: {name: '9:00 AM'}
    -> (ops, rows) where rows = [(date, name, result_text)] for the preview.
    First check-in per person per weekday wins. People with no check-in become CL only for days that are over
    (before today, or today after the CL time) and only when their cell is still blank."""
    today = today or datetime.now(IST).date()
    now = now or datetime.now(IST)
    first = {}
    for e in sorted(events, key=lambda e: e["dt"]):
        d = e["dt"].date()
        if d.weekday() < 5 and is_checkin(e["text"]):
            first.setdefault((d, e["name"]), e)
    ops, rows = [], []
    for em in sorted(empties, key=lambda x: (x["date"], x["name"])):
        d, n = em["date"], em["name"]
        hit = first.get((d, n))
        if hit:
            t = (stated_time(hit["text"], hit["dt"]) if use_stated else None) or hit["dt"]
            st = classify(t, timings.get(n, "9:00 AM"), grace)
            ops.append({"kind": "in", "date": d, "name": n, "reported": fmt_time(t), "status": st,
                        "note": f"time written in message; sent {fmt_time(hit['dt'])}" if t is not hit["dt"] else ""})
            rows.append((d, n, f"{fmt_time(t)}  {st}"))
        elif mark_cl and em["status"] == "" and (d < today or (d == today and now.time() >= parse_timing(cl_time))):
            ops.append({"kind": "cl", "date": d, "name": n, "reported": "", "status": "CL", "note": ""})
            rows.append((d, n, "no check-in found -> CL"))
        else:
            rows.append((d, n, "left empty" + (" (day not over)" if d >= today else " (no check-in found)")))
    return ops, rows
