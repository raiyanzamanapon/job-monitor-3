#!/usr/bin/env python3
# Job Monitor: double-click this file to open the app (needs Python 3.9+ installed).
import os
import subprocess
import sys

# a windowed (no-console) build has no stdout/stderr; give libraries somewhere to write
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


def _ensure_deps():
    """Install the few packages the app needs the first time it opens."""
    if getattr(sys, "frozen", False):
        return  # packaged .exe already contains everything
    need = []
    for mod, pkg in (("requests", "requests"), ("bs4", "beautifulsoup4")):
        try:
            __import__(mod)
        except ImportError:
            need.append(pkg)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Dhaka")
    except Exception:
        if sys.platform.startswith("win"):
            need.append("tzdata")
    if not need:
        return
    flags = 0x08000000 if sys.platform.startswith("win") else 0  # no console window
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "--quiet", *need],
                              creationflags=flags)
    except Exception as e:
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showerror("Job Monitor", "Could not install required packages: " + ", ".join(need) +
                                 "\n\nPlease open Command Prompt and run:\n  pip install " + " ".join(need) +
                                 "\n\n" + str(e))
        except Exception:
            pass
        sys.exit(1)
    import importlib
    importlib.invalidate_caches()


_ensure_deps()

#!/usr/bin/env python3
"""Job monitor: reads Bangladesh job-circular sites, finds the jobs you care about
(architect posts, architecture lecturer posts at universities, bank architect posts,
NGO/UN architect posts), remembers what it has seen and what changed since the last
check, and writes a Markdown report.

This file is the checker. JobMonitorApp.pyw wraps it in a window.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# When packaged as an .exe, keep settings and results next to the .exe
# (not in the temporary folder the exe unpacks itself into).
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

NOTICE_PROFILE = "Notice to check"
NOTICE_SHORT = "Needs a look"
CONF_RANK = {"high": 0, "low": 1, "check": 2}

# ---------------------------------------------------------------- text ----

def norm(s):
    """Unicode-normalise and drop zero-width joiners so Bengali spelling variants match."""
    s = unicodedata.normalize("NFC", s or "")
    return s.replace("‌", "").replace("‍", "")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


_RX = {}


def rx(pattern):
    r = _RX.get(pattern)
    if r is None:
        r = _RX[pattern] = re.compile(norm(pattern), re.I)
    return r


_SEP = r"[\s,;:()\[\]{}/\\|\-–—.।]"


def phrase_pattern(phrase):
    """'প্রভাষক, আর্কিটেকচার বিভাগ' -> a pattern that tolerates commas, brackets and spacing."""
    toks = [t for t in re.split(_SEP + "+", norm(phrase)) if t]
    return (_SEP + "{0,8}").join(re.escape(t) for t in toks)


# ---------------------------------------------------------------- dates ----

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

MONTHS = {
    "january": 1, "jan": 1, "জানুয়ারি": 1, "জানুয়ারী": 1,
    "february": 2, "feb": 2, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারী": 2,
    "march": 3, "mar": 3, "মার্চ": 3,
    "april": 4, "apr": 4, "এপ্রিল": 4,
    "may": 5, "মে": 5,
    "june": 6, "jun": 6, "জুন": 6,
    "july": 7, "jul": 7, "জুলাই": 7,
    "august": 8, "aug": 8, "আগস্ট": 8,
    "september": 9, "sept": 9, "sep": 9, "সেপ্টেম্বর": 9,
    "october": 10, "oct": 10, "অক্টোবর": 10,
    "november": 11, "nov": 11, "নভেম্বর": 11,
    "december": 12, "dec": 12, "ডিসেম্বর": 12,
}
_MONTH_ALT = "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True))
DATE_TEXT_RE = re.compile(
    r"((?:\d{1,2}\s*,\s*)*\d{1,2})(?:st|nd|rd|th)?[\s\-/]*(" + _MONTH_ALT + r")\.?,?\s*(\d{4})",
    re.I,
)
DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
DEADLINE_KW = re.compile(
    r"(last\s*date|deadline|closing|apply\s*(?:by|before|until)|until|before|due\s*date|"
    r"শেষ\s*তারিখ|আবেদনের\s*শেষ|শেষ\s*দিন|সময়সীমা|পর্যন্ত)", re.I)


def parse_deadlines(text):
    """Every date found in text (Bengali or English; 'dd Month yyyy', 'dd, dd Month yyyy', dd/mm/yyyy)."""
    text = norm(text or "").translate(BN_DIGITS)
    found = []
    for m in DATE_TEXT_RE.finditer(text):
        month = MONTHS[m.group(2).lower()]
        year = int(m.group(3))
        for d in re.split(r"\s*,\s*", m.group(1)):
            try:
                found.append(date(year, month, int(d)))
            except ValueError:
                pass
    for m in DATE_NUM_RE.finditer(text):
        try:
            found.append(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return sorted(set(found))


def extract_deadlines(text):
    """Only dates written next to a deadline word, so publication dates are not mistaken for deadlines."""
    text = norm(text or "").translate(BN_DIGITS)
    out = []
    for m in DEADLINE_KW.finditer(text):
        # "Last date: 25 Nov" puts the date after the word; "25 Nov পর্যন্ত" puts it before.
        lo = max(0, m.start() - 30) if m.group(0) == "পর্যন্ত" else m.start()
        out += parse_deadlines(text[lo: m.end() + 70])
    return sorted(set(out))


def today_in(tz_name):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


# ----------------------------------------------------------------- http ----

def make_session(cfg):
    s = requests.Session()
    s.headers.update({
        "User-Agent": cfg.get("user_agent", "Mozilla/5.0"),
        "Accept-Language": "bn,en;q=0.8",
    })
    return s


def friendly(e):
    if isinstance(e, requests.exceptions.SSLError):
        return "security certificate problem"
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timed out"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "could not connect"
    if isinstance(e, requests.exceptions.HTTPError):
        return f"HTTP {e.response.status_code}" if e.response is not None else "HTTP error"
    return str(e)[:120]


def http_get(session, url, timeout=25, retries=2):
    """Return (status_code, html). 404 returns (404, ''). Other errors raise RuntimeError."""
    last, verify = None, True
    for i in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout, verify=verify)
            if r.status_code == 404:
                return 404, ""
            r.raise_for_status()
            if "charset" not in r.headers.get("content-type", "").lower():
                r.encoding = "utf-8"
            return r.status_code, r.text
        except requests.exceptions.SSLError as e:
            last = e
            if verify:  # many university sites have broken certificates; these are public pages
                verify = False
                try:
                    requests.packages.urllib3.disable_warnings()
                except Exception:
                    pass
                continue
            time.sleep(1)
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{urlparse(url).netloc}: {friendly(last)}")


# -------------------------------------------------------- organisations ----

def _auto_aliases(name):
    out = [name]
    plain = re.sub(r"\s*\(.*?\)", "", name).strip()
    out.append(plain)
    for m in re.findall(r"\(([^)]*)\)", name):
        m = m.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9&.\-]{2,7}", m):
            out.append(m)
    base = re.sub(r"\s+(PLC|Limited|Ltd\.?)$", "", plain, flags=re.I).strip()
    out.append(base)
    for v in list(out):
        if "&" in v:
            out.append(v.replace("&", "and"))
        if " and " in v:
            out.append(v.replace(" and ", " & "))
    return out


def compile_orgs(entries):
    compiled = []
    for e in entries:
        pats, seen = [], set()
        for a in _auto_aliases(e["name"]) + list(e.get("aliases", [])):
            sensitive = a.startswith("!")
            a = norm(a.lstrip("!")).strip()
            if not a or a in seen:
                continue
            seen.add(a)
            if re.fullmatch(r"[A-Z][A-Z0-9&.\-]{2,7}", a):
                pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])"))
            else:
                body = r"\s+".join(re.escape(w) for w in a.split())
                pats.append(re.compile(body, 0 if sensitive else re.I))
        compiled.append({"name": e["name"], "short": e.get("short", e["name"]), "pats": pats})
    return compiled


def detect_org(orgs, title, body=""):
    """The organisation named in the title (else the body); the longest match wins, so
    'Khulna University of Engineering & Technology' beats 'Khulna University'."""
    for text in (norm(title), norm(body)):
        best = None
        for o in orgs:
            for p in o["pats"]:
                m = p.search(text)
                if m:
                    key = (-(m.end() - m.start()), m.start())
                    if best is None or key < best[0]:
                        best = (key, o)
        if best:
            return best[1]
    return None


# ---------------------------------------------------------- classifying ----

def compile_cfg(cfg):
    profiles = []
    for p in cfg.get("profiles", []):
        profiles.append({
            "name": p["name"], "short": p.get("short", p["name"]),
            "applies_to": p.get("applies_to", []),
            "groups": [[rx(x) for x in g] for g in p.get("all_of", [])],
            "near": p.get("near"),
            "phrases": [re.compile(phrase_pattern(s), re.I) for s in p.get("phrases", [])],
            "exclude": [rx(x) for x in p.get("exclude", [])],
            "org_list": p.get("org_list"), "org_required": p.get("org_required", False),
            "org_from": p.get("org_from"), "confidence": p.get("confidence"),
            "skip_if": p.get("skip_if", []),
        })
    return {"profiles": profiles,
            "orgs": {"universities": compile_orgs(cfg.get("universities", [])),
                     "banks": compile_orgs(cfg.get("banks", []))}}


def snippet_at(text, pos, before=100, after=170):
    s, e = max(0, pos - before), min(len(text), pos + after)
    return ("..." if s > 0 else "") + clean(text[s:e]) + ("..." if e < len(text) else "")


def match_profile(prof, text):
    """None, or (confidence, position). With 'near', the groups must sit close together to be
    'high'; if they appear only far apart the match is 'low' (possible)."""
    for ph in prof["phrases"]:
        m = ph.search(text)
        if m:
            return "high", m.start()
    if not prof["groups"]:
        return None
    positions = []
    for g in prof["groups"]:
        pos = sorted(m.start() for r in g for m in r.finditer(text))
        if not pos:
            return None
        positions.append(pos)
    near = prof["near"]
    if not near:
        return "high", positions[-1][0]
    first = positions[0]
    best = None
    for other in positions[1:]:
        pair = min(((abs(a - b), min(a, b)) for a in first for b in other), default=None)
        if pair is None or pair[0] > near:
            return "low", positions[-1][0]
        best = pair if best is None or pair[0] < best[0] else best
    if best is None:
        return "high", first[0]
    return "high", best[1]


def classify(title, body, group, comp, assumed_org=False, default_org=""):
    """Return (matches, organisation label) for one circular."""
    text = norm(f"{title}\n{body or ''}")
    ntitle = norm(title)
    hits, names, org_label = [], set(), ""
    for prof in comp["profiles"]:
        if group not in prof["applies_to"] or any(s in names for s in prof["skip_if"]):
            continue
        t = text
        for ex in prof["exclude"]:
            t = ex.sub(" ", t)
        res = match_profile(prof, t)
        if not res:
            continue
        conf, pos = res
        org = None
        if prof["org_list"]:
            org = detect_org(comp["orgs"].get(prof["org_list"], []), ntitle, text)
            if prof["org_required"] and not org and not assumed_org:
                continue
        if prof["confidence"]:
            conf = prof["confidence"]
        hits.append({"profile": prof["name"], "short": prof["short"], "confidence": conf,
                     "snippet": snippet_at(t, pos)})
        names.add(prof["name"])
        if org and not org_label:
            org_label = org["short"]
        if prof["org_from"] is not None and not org_label:
            for r in prof["groups"][prof["org_from"]]:
                m = r.search(text)
                if m:
                    org_label = clean(m.group(0))
                    if re.fullmatch(r"[A-Za-z]{2,5}", org_label):
                        org_label = org_label.upper()
                    break
    return hits, (org_label or default_org)


def merge_matches(old, new):
    by = {m["profile"]: m for m in old}
    for m in new:
        cur = by.get(m["profile"])
        if cur is None or CONF_RANK[m["confidence"]] < CONF_RANK[cur["confidence"]]:
            by[m["profile"]] = m
    return list(by.values())


def best_conf(matches):
    return min((m["confidence"] for m in matches), key=lambda c: CONF_RANK[c], default="high")


def cfg_signature(cfg):
    keys = {k: cfg.get(k) for k in ("profiles", "universities", "banks")}
    return hashlib.sha1(json.dumps(keys, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# -------------------------------------------------------------- parsing ----

def parse_listing(html, base_url, must_contain=None):
    """Circular cards on a BD Govt Job style listing page: a heading (h2/h3) holding a link,
    with the card's text (deadline etc.) around it."""
    soup = BeautifulSoup(html, "html.parser")
    base = urlparse(base_url)
    items, seen = [], set()
    for h in soup.find_all(["h2", "h3"]):
        a = h.find("a", href=True)
        if not a:
            continue
        url = urljoin(base_url, a["href"]).split("#")[0]
        p = urlparse(url)
        segs = [x for x in p.path.split("/") if x]
        if p.netloc != base.netloc or len(segs) != 1 or url in seen:
            continue
        card = h
        for _ in range(5):
            parent = card.parent
            if parent is None or len(parent.find_all(["h2", "h3"])) > 1:
                break
            card = parent
        card_text = clean(card.get_text(" "))
        if must_contain and must_contain not in card_text:
            continue
        seen.add(url)
        items.append({"title": clean(a.get_text(" ")), "url": url, "card_text": card_text})
    return items


def card_deadline(card_text):
    m = re.search(r"শেষ তারিখ(.{0,90})", card_text)
    if not m:
        return ""
    return re.split(r"প্রকাশ", m.group(1))[0].strip()


def card_published(card_text):
    m = re.search(r"প্রকাশ[:：]?\s*(.{0,40})", card_text)
    d = parse_deadlines(m.group(1)) if m else []
    return d[0] if d else None


def page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        t.decompose()
    node = soup.find("article") or soup.select_one(".entry-content") or soup.find("main") or soup.body or soup
    return clean(node.get_text(" "))


SKIP_HOSTS = ("facebook.com", "youtube.com", "twitter.com", "linkedin.com", "instagram.com", "wa.me",
              "whatsapp.com", "t.me")
DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".rar",
           ".ppt", ".pptx")
RECRUIT_RX = re.compile(norm(
    r"recruit|career|vacanc|job|circular|appointment|opening|position|faculty|teacher|lecturer|"
    r"নিয়োগ|বিজ্ঞপ্তি|ক্যারিয়ার|চাকরি|শিক্ষক|প্রভাষক|পদ"), re.I)
RECRUIT_STRICT_RX = re.compile(norm(
    r"recruit|vacanc|career|appointment|faculty|teacher|lecturer|architect|"
    r"নিয়োগ|ক্যারিয়ার|শিক্ষক|প্রভাষক|স্থপতি|স্থাপত্য|আর্কিটেক"), re.I)


def parse_notice_links(html, base_url, gate=True):
    """Links on a notice board / career page whose text or surroundings look like recruitment."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        text = clean(a.get_text(" ")) or clean(a.get("title", ""))
        if len(text) < 6 or url in seen:
            continue
        ctx, parent = text, a.parent
        for _ in range(3):
            if parent is None:
                break
            if len(parent.find_all("a", href=True)) > 1:
                break  # the parent holds other notices too; do not mix their text into this one
            t = clean(parent.get_text(" "))
            if len(t) > 400:
                break
            ctx = t
            parent = parent.parent
        if gate and not RECRUIT_RX.search(norm(text + " " + ctx)):
            continue
        seen.add(url)
        out.append({"title": text, "url": url, "ctx": ctx})
    return out


# ---------------------------------------------------------------- state ----

def load_state(path):
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("items", {})
                data.setdefault("pages", {})
                return data
        except json.JSONDecodeError:
            pass
    return {"items": {}, "pages": {}}


def save_state(path, state):
    # write to a temp file first so closing the app mid-save cannot corrupt the history
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_user_sources(base_dir):
    try:
        data = json.loads((base_dir / "user_sources.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for s in data if isinstance(data, list) else []:
        if isinstance(s, dict) and s.get("url") and s.get("name"):
            s = dict(s)
            s.setdefault("type", "html_notices")
            s.setdefault("group", "uni")
            s.setdefault("id", "user-" + hashlib.sha1(s["url"].encode()).hexdigest()[:8])
            s["user"] = True
            out.append(s)
    return out


def university_sources(cfg):
    """One notice-page source per university (its own pages, else its home page)."""
    out = []
    if not cfg.get("university_pages", True):
        return out
    for u in cfg.get("universities", []):
        pages = u.get("pages") or ([f"https://{u['domain']}/"] if u.get("domain") else [])
        for i, url in enumerate(pages):
            out.append({"id": f"uni-{u.get('short', u['name']).lower()}-{i}", "name": f"{u.get('short', u['name'])} notices",
                        "type": "html_notices", "url": url, "group": "uni", "org": u.get("short", u["name"]),
                        "max_links": 25, "university": True})
    return out


def expand_sources(cfg, base_dir=None):
    return list(cfg.get("sources", [])) + university_sources(cfg) + load_user_sources(base_dir or BASE_DIR)


def get_open_matches(items, today, ttl=45):
    """[(url, item, last_deadline_or_None)] for matching, still-open circulars, earliest deadline first.
    A circular with no deadline found is dropped ttl days after it was first seen."""
    out = []
    for url, it in items.items():
        if not it.get("matches") or it.get("expired"):
            continue
        dls = [date.fromisoformat(d) for d in it.get("deadlines", [])]
        last = max(dls) if dls else None
        if last and last < today:
            continue
        if not last:
            try:
                if (today - date.fromisoformat(it["first_seen"])).days > ttl:
                    continue
            except (KeyError, ValueError):
                pass
        out.append((url, it, last))
    out.sort(key=lambda x: (x[2] is None, x[2] or date.max))
    return out


# ------------------------------------------------------------------ run ----

def run(cfg, base_dir=BASE_DIR, today=None, max_pages=None, dry_run=False, log=None):
    log = log or (lambda m: None)
    today = today or today_in(cfg.get("timezone", "Asia/Dhaka"))
    now = datetime.now()
    run_id = now.isoformat(timespec="microseconds")
    delay = cfg.get("delay_seconds", 1.0)
    budget = {"n": cfg.get("max_detail_fetches", 250)}
    ttl = cfg.get("undated_ttl_days", 45)
    comp = compile_cfg(cfg)
    state_path = base_dir / "state.json"
    state = load_state(state_path)
    items = state["items"]
    prev_run_ts = state.get("last_run")
    sig = cfg_signature(cfg)
    if state.get("cfg_sig") != sig:
        if items:
            log("Matching rules changed: circulars will be read again")
        for it in items.values():
            for k in ("matches", "sig", "groups"):
                it.pop(k, None)
        state["cfg_sig"] = sig
    session = make_session(cfg)
    problems, manual, page_hits, status = [], [], [], {}
    processed, bodies = set(), {}

    def get_body(url, title):
        """Open a page once per run; text, or None if it could not be read."""
        if url in bodies:
            return bodies[url]
        if budget["n"] <= 0:
            msg = "Detail-fetch limit reached; some circulars were not opened. Run again to continue."
            if msg not in problems:
                problems.append(msg)
            return None
        log(f"Opening: {title[:80]}")
        try:
            _, dhtml = http_get(session, url)
            time.sleep(delay)
        except RuntimeError as e:
            bodies[url] = None
            problems.append(str(e))
            return None
        budget["n"] -= 1
        bodies[url] = page_text(dhtml)
        return bodies[url]

    def stamp(src, ok, detail, seen=0, matched=0, warn=False):
        status[src["id"]] = {"ok": ok, "warn": warn, "detail": detail, "items": seen, "matches": matched,
                             "checked": now.isoformat(timespec="minutes")}

    for src in expand_sources(cfg, base_dir):
        stype = src["type"]
        if stype == "manual":
            manual.append(src)
            continue
        seen_n = match_n = 0
        try:
            if stype == "bdgovtjob_list":
                pages = max_pages or src.get("pages", 2)
                group = src["group"]
                for n in range(1, pages + 1):
                    url = src["url"] if n == 1 else src["url"].rstrip("/") + f"/page/{n}/"
                    log(f"Reading {src['name']} (page {n})")
                    code, html = http_get(session, url)
                    time.sleep(delay)
                    if code == 404:
                        if n == 1:
                            raise RuntimeError("page not found (404)")
                        break
                    cards = parse_listing(html, src["url"], src.get("card_must_contain", "শেষ তারিখ"))
                    if not cards:
                        if n == 1:
                            raise RuntimeError("no circulars found; the site layout may have changed")
                        break
                    for c in cards:
                        url = c["url"]
                        if url in processed:
                            it = items.get(url)
                            if it and not it.get("expired") and group not in it.get("groups", []):
                                body = get_body(url, c["title"])
                                if body is not None:
                                    hits, org = classify(c["title"], body, group, comp, src.get("org_assumed", False))
                                    it["matches"] = merge_matches(it.get("matches", []), hits)
                                    it["groups"] = it.get("groups", []) + [group]
                                    if org and not it.get("org"):
                                        it["org"] = org
                            continue
                        processed.add(url)
                        seen_n += 1
                        dl_text = card_deadline(c["card_text"])
                        deadlines = parse_deadlines(dl_text)
                        expired = bool(deadlines and max(deadlines) < today) or "মেয়াদ শেষ" in c["card_text"]
                        csig = hashlib.sha1((c["title"] + dl_text).encode("utf-8")).hexdigest()
                        prev = items.get(url)
                        entry = prev or {"first_seen": today.isoformat(), "first_run": run_id}
                        pub = card_published(c["card_text"])
                        entry.update({"title": c["title"], "source": src["name"], "source_id": src["id"],
                                      "group": group, "deadlines": [d.isoformat() for d in deadlines],
                                      "expired": expired, "published": pub.isoformat() if pub else entry.get("published", "")})
                        items[url] = entry
                        if expired:
                            continue
                        if prev and prev.get("sig") == csig and "matches" in prev and group in prev.get("groups", []):
                            match_n += bool(prev["matches"])
                            continue
                        body = get_body(url, c["title"])
                        if body is None:
                            continue
                        hits, org = classify(c["title"], body, group, comp, src.get("org_assumed", False))
                        entry["matches"], entry["groups"], entry["sig"] = hits, [group], csig
                        if org:
                            entry["org"] = org
                        if hits and not deadlines:
                            entry["deadlines"] = [d.isoformat() for d in extract_deadlines(body)[-3:]]
                        match_n += bool(hits)
                stamp(src, True, f"{seen_n} circulars read", seen_n, match_n)

            elif stype == "html_notices":
                log(f"Reading {src['name']}")
                code, html = http_get(session, src["url"])
                time.sleep(delay * 0.6)
                if code == 404:
                    raise RuntimeError("page not found (404)")
                group = src.get("group", "uni")
                links = parse_notice_links(html, src["url"], gate=src.get("gate", True))[: src.get("max_links", 30)]
                for l in links:
                    url, title, ctx = l["url"], l["title"], l["ctx"]
                    if any(h in urlparse(url).netloc.lower() for h in SKIP_HOSTS):
                        continue
                    seen_n += 1
                    csig = hashlib.sha1((title + ctx).encode("utf-8")).hexdigest()
                    prev = items.get(url)
                    if prev and prev.get("sig") == csig and "matches" in prev:
                        match_n += bool(prev["matches"])
                        continue
                    entry = prev or {"first_seen": today.isoformat(), "first_run": run_id}
                    org_default = src.get("org", "")
                    hits, org = classify(title, ctx, group, comp, False, org_default)
                    is_doc = urlparse(url).path.lower().endswith(DOC_EXT)
                    body = None
                    strict = bool(RECRUIT_STRICT_RX.search(norm(title + " " + ctx)))
                    if not is_doc and strict:
                        body = get_body(url, title)
                    if body is not None:
                        bh, borg = classify(title, body, group, comp, False, org_default)
                        hits, org = merge_matches(hits, bh), (borg or org)
                    dls = extract_deadlines(body if body else ctx)
                    if (not hits and strict and (is_doc or body is None) and src.get("report_unverified", True)
                            and (len(title) >= 15 or re.search(r"\d", title))):
                        hits = [{"profile": NOTICE_PROFILE, "short": NOTICE_SHORT, "confidence": "check",
                                 "snippet": ctx[:260]}]
                    entry.update({"title": title, "source": src["name"], "source_id": src["id"], "group": group,
                                  "org": org, "deadlines": [d.isoformat() for d in dls], "matches": hits,
                                  "groups": [group], "sig": csig, "published": entry.get("published", ""),
                                  "expired": bool(dls and max(dls) < today)})
                    items[url] = entry
                    match_n += bool(hits)
                if not links:
                    stamp(src, True, "read, but no recruitment links found (the page may load its list with scripts)",
                          0, 0, warn=True)
                else:
                    stamp(src, True, f"{len(links)} notices read", seen_n, match_n)

            elif stype == "page_watch":
                log(f"Checking page: {src['name']}")
                _, html = http_get(session, src["url"])
                time.sleep(delay)
                text = page_text(html)
                hit = [k for k in src["keywords"] if rx(k).search(norm(text))]
                h = hashlib.sha1(text.encode("utf-8")).hexdigest()
                old = state["pages"].get(src["url"], {})
                changed = old.get("hash") != h
                state["pages"][src["url"]] = {
                    "hash": h,
                    "last_changed": today.isoformat() if changed else old.get("last_changed", today.isoformat()),
                }
                if hit:
                    m = rx(src["keywords"][0]).search(norm(text))
                    page_hits.append({
                        "name": src["name"], "url": src["url"], "changed": changed,
                        "last_changed": state["pages"][src["url"]]["last_changed"],
                        "snippet": snippet_at(norm(text), m.start()) if m else "",
                        "deadlines": [d.isoformat() for d in parse_deadlines(text) if d >= today][:3],
                    })
                stamp(src, True, "page read", 1, 1 if hit else 0)
        except RuntimeError as e:
            stamp(src, False, str(e))
            problems.append(f"{src['name']}: {e}")

    # ---- what changed since the previous check
    open_now = get_open_matches(items, today, ttl)
    cur = {u: ",".join(it.get("deadlines", [])) for u, it, _ in open_now}
    prev_matches = state.get("last_matches")
    first = prev_matches is None
    prev_matches = prev_matches or {}
    new = [u for u in cur if u not in prev_matches]
    changed = [u for u in cur if u in prev_matches and prev_matches[u] != cur[u]]
    closed = [u for u in prev_matches if u not in cur]
    n_ok = sum(1 for s in status.values() if s["ok"])
    n_fail = sum(1 for s in status.values() if not s["ok"])
    offline = n_ok == 0 and n_fail > 0
    latest = {
        "ts": now.isoformat(timespec="minutes"), "prev_ts": prev_run_ts, "first": first,
        "same": (not first) and not (new or changed or closed) and not offline,
        "offline": offline, "count": len(cur), "new": new, "changed": changed,
        "closed": [{"url": u, "title": items.get(u, {}).get("title", u)} for u in closed],
        "sources_ok": n_ok, "sources_failed": n_fail,
    }
    state["latest"] = latest
    runs = state.get("runs", [])
    runs.insert(0, {"ts": latest["ts"], "count": latest["count"], "new": len(new), "changed": len(changed),
                    "closed": len(closed), "same": latest["same"], "offline": offline})
    state["runs"] = runs[:30]
    if not offline:
        state["last_matches"] = cur
    state["sources_status"] = status
    state["last_run"] = latest["ts"]
    state["page_hits"] = page_hits
    state["problems"] = problems

    report = build_report(cfg, items, latest, today, page_hits, manual, problems, status, ttl)
    if not dry_run:
        save_state(state_path, state)
        out_dir = base_dir / "reports"
        out_dir.mkdir(exist_ok=True)
        (out_dir / f"report_{now.strftime('%Y-%m-%d_%H%M')}.md").write_text(report, encoding="utf-8")
        (base_dir / "latest_report.md").write_text(report, encoding="utf-8")
    return report


# --------------------------------------------------------------- report ----

def build_report(cfg, items, latest, today, page_hits, manual, problems, status, ttl=45):
    soon = cfg.get("closing_soon_days", 7)
    open_matches = get_open_matches(items, today, ttl)
    by_url = {u: (u, it, last) for u, it, last in open_matches}
    new = [by_url[u] for u in latest["new"] if u in by_url]
    changed = [by_url[u] for u in latest["changed"] if u in by_url]
    closing = [m for m in open_matches if m[2] and (m[2] - today).days <= soon]

    def fmt(url, it, last):
        left = ""
        if last:
            d = (last - today).days
            left = f" ({'today' if d == 0 else 'tomorrow' if d == 1 else f'{d} days left'})"
        dl = last.strftime("%d %b %Y") + left if last else "not found: check the circular"
        profs = "; ".join(sorted({m["short"] + ("" if m["confidence"] == "high" else " (" + ("possible" if m["confidence"] == "low" else "to check") + ")") for m in it["matches"]}))
        lines = [f"- **{it['title']}**", f"  - Link: {url}"]
        if it.get("org"):
            lines.append(f"  - Organisation: {it['org']}")
        lines += [f"  - Type: {profs}", f"  - Deadline: {dl}", f"  - Source: {it['source']} | first seen {it['first_seen']}"]
        snip = next((m["snippet"] for m in it["matches"] if m.get("snippet")), "")
        if snip:
            lines.append(f"  - Context: {snip}")
        return "\n".join(lines)

    if latest.get("offline"):
        headline = "No source could be read. Check your internet connection."
    elif latest["first"]:
        headline = f"First check: {latest['count']} matching jobs found."
    elif latest["same"]:
        headline = f"Same results as the last check: nothing new, nothing closed. {latest['count']} jobs are open."
    else:
        headline = (f"{len(latest['new'])} new, {len(latest['changed'])} with a changed deadline, "
                    f"{len(latest['closed'])} closed since the last check. {latest['count']} jobs are open.")
    out = [f"# Job report: {today.strftime('%A, %d %B %Y')}", "", headline, "",
           f"Sources read: {latest['sources_ok']} | could not be read: {latest['sources_failed']}", ""]

    def section(title, rows, empty):
        out.append(f"## {title}")
        out.append("")
        out.extend([fmt(*r) + "\n" for r in rows] if rows else [empty, ""])

    section("New since the last check", new, "Nothing new.")
    section("Deadline changed", changed, "No deadline changed.")
    if latest["closed"]:
        out += ["## Closed since the last check", ""] + [f"- {c['title']}" for c in latest["closed"]] + [""]
    section(f"Closing within {soon} days", closing, "Nothing closing soon.")
    section("All open matches (earliest deadline first)", open_matches, "No open matches right now.")

    out += ["## Pages being watched", ""]
    if page_hits:
        for p in page_hits:
            dl = ", ".join(p["deadlines"]) or "none detected"
            out += [f"- **{p['name']}**: {p['url']}",
                    f"  - Page {'changed since last run' if p['changed'] else 'unchanged'} | dates found: {dl} (verify)",
                    f"  - Context: {p['snippet']}", ""]
    else:
        out += ["No watched page mentions your keywords right now.", ""]

    bad = [s for s in status.values() if not s["ok"]]
    out += ["## Sources", "", f"{len(status) - len(bad)} read, {len(bad)} could not be read."]
    out += [""] + [f"- {s['detail']}" for s in bad[:20]] + [""] if bad else [""]
    out += ["## Check these yourself (a script cannot read them)", ""]
    out += [f"- [{m['name']}]({m['url']}): {m.get('note', '')}" for m in manual] or ["None."]
    out.append("")
    if problems:
        out += ["## Problems during this run", ""] + [f"- {p}" for p in problems[:40]] + [""]
    out.append("Always confirm details and the deadline on the official circular before applying.")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Bangladesh job monitor")
    ap.add_argument("--config", default=str(BASE_DIR / "config.json"))
    ap.add_argument("--max-pages", type=int, help="override pages per source")
    ap.add_argument("--dry-run", action="store_true", help="print the report, do not save anything")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    report = run(cfg, max_pages=args.max_pages, dry_run=args.dry_run)
    if args.dry_run:
        sys.stdout.buffer.write(report.encode("utf-8"))
    else:
        print("Report saved: latest_report.md (copy in reports/)")




DEFAULT_CONFIG = json.loads(r'''{
 "config_version": 2,
 "timezone": "Asia/Dhaka",
 "delay_seconds": 1.0,
 "max_detail_fetches": 300,
 "closing_soon_days": 7,
 "undated_ttl_days": 45,
 "university_pages": true,
 "check_on_start": true,
 "theme": "frost",
 "user_agent": "Mozilla/5.0 (compatible; PersonalJobMonitor/2.0)",
 "profiles": [
  {
   "name": "Government Architect",
   "short": "Government",
   "applies_to": [
    "gov",
    "uni"
   ],
   "all_of": [
    [
     "\\b(assistant\\s+|junior\\s+|senior\\s+|chief\\s+|deputy\\s+|executive\\s+)?architects?\\b",
     "স্থপতি",
     "আর্কিটেক্ট"
    ]
   ],
   "exclude": [
    "\\b(solution|software|enterprise|data|cloud|network|it|system|security|technical|application|integration|information)\\s+architects?\\b"
   ]
  },
  {
   "name": "University Lecturer (Architecture)",
   "short": "University",
   "applies_to": [
    "uni"
   ],
   "all_of": [
    [
     "\\blecturers?\\b",
     "প্রভাষক",
     "লেকচারার",
     "\\bfaculty\\s+members?\\b"
    ],
    [
     "\\barchitecture\\b",
     "\\barchitectural\\b",
     "\\bB\\.?\\s?Arch\\b",
     "স্থাপত্য",
     "আর্কিটেকচার",
     "আর্কিটেক্চার"
    ]
   ],
   "near": 220,
   "phrases": [
    "প্রভাষক, আর্কিটেকচার বিভাগ",
    "লেকচারার, স্থাপত্য বিভাগ",
    "প্রভাষক, স্থাপত্য বিভাগ",
    "লেকচারার, আর্কিটেকচার বিভাগ",
    "প্রভাষক (আর্কিটেকচার)",
    "প্রভাষক (স্থাপত্য)",
    "লেকচারার (আর্কিটেকচার)",
    "লেকচারার (স্থাপত্য)",
    "আর্কিটেকচার বিভাগে প্রভাষক",
    "স্থাপত্য বিভাগে প্রভাষক",
    "আর্কিটেকচার বিভাগে লেকচারার",
    "স্থাপত্য বিভাগে লেকচারার",
    "প্রভাষক আর্কিটেকচার",
    "প্রভাষক স্থাপত্য",
    "Lecturer, Department of Architecture",
    "Lecturer in Architecture",
    "Lecturer (Architecture)",
    "Department of Architecture, Lecturer",
    "Assistant Lecturer, Architecture",
    "Lecturer, Architecture"
   ],
   "org_list": "universities",
   "org_required": false
  },
  {
   "name": "Lecturer post at a watched university (department not shown)",
   "short": "Needs a look",
   "applies_to": [
    "uni"
   ],
   "all_of": [
    [
     "\\blecturers?\\b",
     "প্রভাষক",
     "লেকচারার"
    ]
   ],
   "org_list": "universities",
   "org_required": true,
   "confidence": "check",
   "skip_if": [
    "University Lecturer (Architecture)"
   ]
  },
  {
   "name": "Bank Architect / Interior Designer",
   "short": "Bank",
   "applies_to": [
    "bank",
    "gov"
   ],
   "all_of": [
    [
     "\\barchitects?\\b",
     "\\barchitecture\\b",
     "\\barchitectural\\b",
     "\\binterior\\s+(designers?|design|architects?)\\b",
     "স্থপতি",
     "স্থাপত্য",
     "আর্কিটেক্ট",
     "আর্কিটেকচার",
     "ইন্টেরিয়র"
    ]
   ],
   "exclude": [
    "\\b(solution|software|enterprise|data|cloud|network|it|system|security|technical|application|integration|information)\\s+architects?\\b"
   ],
   "org_list": "banks",
   "org_required": true
  },
  {
   "name": "BRAC / UN / USAID-type organisation (architecture-related)",
   "short": "NGO / UN",
   "applies_to": [
    "ngo"
   ],
   "all_of": [
    [
     "\\bBRAC\\b",
     "ব্র্যাক",
     "\\bUNDP\\b",
     "\\bUNICEF\\b",
     "\\bUNOPS\\b",
     "\\bUN[- ]Habitat\\b",
     "\\bUSAID\\b",
     "\\bWFP\\b",
     "\\bUNHCR\\b",
     "\\bIOM\\b",
     "\\bFAO\\b",
     "\\bADB\\b",
     "\\bWorld Bank\\b",
     "\\bUnited Nations\\b",
     "জাতিসংঘ"
    ],
    [
     "\\barchitect\\w*",
     "\\burban (design|planning)\\b",
     "\\blandscape\\b",
     "\\binterior design\\b",
     "স্থপতি",
     "স্থাপত্য"
    ]
   ],
   "exclude": [
    "\\b(solution|software|enterprise|data|cloud|network|it|system|security|technical|application|integration|information)\\s+architects?\\b"
   ],
   "org_from": 0
  }
 ],
 "universities": [
  {
   "name": "Bangladesh University of Engineering and Technology (BUET)",
   "short": "BUET",
   "domain": "buet.ac.bd",
   "aliases": [
    "বুয়েট",
    "বাংলাদেশ প্রকৌশল ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Military Institute of Science and Technology (MIST)",
   "short": "MIST",
   "domain": "mist.ac.bd",
   "aliases": [
    "মিলিটারি ইনস্টিটিউট অব সায়েন্স অ্যান্ড টেকনোলজি",
    "মিলিটারি ইনস্টিটিউট অফ সায়েন্স অ্যান্ড টেকনোলজি"
   ]
  },
  {
   "name": "Dhaka University of Engineering & Technology (DUET)",
   "short": "DUET",
   "domain": "duet.ac.bd",
   "aliases": [
    "ডুয়েট",
    "ঢাকা প্রকৌশল ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Bangabandhu Sheikh Mujibur Rahman Science and Technology University (BSMRSTU)",
   "short": "BSMRSTU",
   "domain": "bsmrstu.edu.bd",
   "aliases": [
    "GSTU",
    "Gopalganj Science and Technology University",
    "বঙ্গবন্ধু শেখ মুজিবুর রহমান বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়",
    "গোপালগঞ্জ বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Chittagong University of Engineering & Technology (CUET)",
   "short": "CUET",
   "domain": "cuet.ac.bd",
   "aliases": [
    "চুয়েট",
    "চট্টগ্রাম প্রকৌশল ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Khulna University (KU)",
   "short": "Khulna University",
   "domain": "ku.ac.bd",
   "aliases": [
    "খুলনা বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Khulna University of Engineering & Technology (KUET)",
   "short": "KUET",
   "domain": "kuet.ac.bd",
   "aliases": [
    "কুয়েট",
    "খুলনা প্রকৌশল ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ],
   "pages": [
    "https://careers.kuet.ac.bd/",
    "https://www.kuet.ac.bd/"
   ]
  },
  {
   "name": "Rajshahi University of Engineering & Technology (RUET)",
   "short": "RUET",
   "domain": "ruet.ac.bd",
   "aliases": [
    "রুয়েট",
    "রাজশাহী প্রকৌশল ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Pabna University of Science & Technology (PUST)",
   "short": "PUST",
   "domain": "pust.ac.bd",
   "aliases": [
    "পাবনা বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Hajee Mohammad Danesh Science & Technology University (HSTU)",
   "short": "HSTU",
   "domain": "hstu.ac.bd",
   "aliases": [
    "হাজী মোহাম্মদ দানেশ বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়",
    "হাজি মোহাম্মদ দানেশ বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Shahjalal University of Science and Technology (SUST)",
   "short": "SUST",
   "domain": "sust.edu",
   "aliases": [
    "শাহজালাল বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Ahsanullah University of Science and Technology (AUST)",
   "short": "AUST",
   "domain": "aust.edu",
   "aliases": [
    "আহছানউল্লা বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়",
    "আহসানউল্লাহ বিজ্ঞান ও প্রযুক্তি বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "American International University-Bangladesh (AIUB)",
   "short": "AIUB",
   "domain": "aiub.edu",
   "aliases": [
    "American International University Bangladesh",
    "আমেরিকান ইন্টারন্যাশনাল ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Bangladesh University (BU)",
   "short": "Bangladesh University",
   "domain": "bu.edu.bd",
   "aliases": [
    "বাংলাদেশ ইউনিভার্সিটি"
   ]
  },
  {
   "name": "BRAC University (BRACU)",
   "short": "BRAC University",
   "domain": "bracu.ac.bd",
   "aliases": [
    "BRACU",
    "ব্র্যাক ইউনিভার্সিটি",
    "ব্র্যাক বিশ্ববিদ্যালয়"
   ]
  },
  {
   "name": "Daffodil International University (DIU)",
   "short": "DIU",
   "domain": "daffodilvarsity.edu.bd",
   "aliases": [
    "Daffodil University",
    "ড্যাফোডিল ইন্টারন্যাশনাল ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Fareast International University (FIU)",
   "short": "FIU",
   "domain": "",
   "aliases": [
    "Far East International University",
    "ফার ইস্ট ইন্টারন্যাশনাল ইউনিভার্সিটি"
   ]
  },
  {
   "name": "North South University (NSU)",
   "short": "NSU",
   "domain": "northsouth.edu",
   "aliases": [
    "নর্থ সাউথ ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Primeasia University",
   "short": "Primeasia",
   "domain": "primeasia.edu.bd",
   "aliases": [
    "Prime Asia University",
    "প্রাইমএশিয়া ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Shanto-Mariam University of Creative Technology (SMUCT)",
   "short": "SMUCT",
   "domain": "smuct.ac.bd",
   "aliases": [
    "Shanto Mariam University of Creative Technology",
    "শান্ত-মারিয়াম ইউনিভার্সিটি অব ক্রিয়েটিভ টেকনোলজি"
   ]
  },
  {
   "name": "Sonargaon University (SU)",
   "short": "Sonargaon University",
   "domain": "su.edu.bd",
   "aliases": [
    "সোনারগাঁও ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Southeast University (SEU)",
   "short": "SEU",
   "domain": "seu.edu.bd",
   "aliases": [
    "South East University",
    "সাউথইস্ট ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Stamford University Bangladesh",
   "short": "Stamford",
   "domain": "stamforduniversity.edu.bd",
   "aliases": [
    "স্ট্যামফোর্ড ইউনিভার্সিটি"
   ]
  },
  {
   "name": "State University of Bangladesh (SUB)",
   "short": "State University",
   "domain": "sub.edu.bd",
   "aliases": [
    "স্টেট ইউনিভার্সিটি অব বাংলাদেশ",
    "স্টেট ইউনিভার্সিটি অফ বাংলাদেশ"
   ]
  },
  {
   "name": "University of Asia Pacific (UAP)",
   "short": "UAP",
   "domain": "uap-bd.edu",
   "aliases": [
    "ইউনিভার্সিটি অব এশিয়া প্যাসিফিক",
    "ইউনিভার্সিটি অফ এশিয়া প্যাসিফিক"
   ]
  },
  {
   "name": "World University of Bangladesh (WUB)",
   "short": "WUB",
   "domain": "wub.edu.bd",
   "aliases": [
    "ওয়ার্ল্ড ইউনিভার্সিটি অব বাংলাদেশ"
   ]
  },
  {
   "name": "Premier University (PUC)",
   "short": "Premier University",
   "domain": "",
   "aliases": [
    "প্রিমিয়ার ইউনিভার্সিটি"
   ]
  },
  {
   "name": "Leading University (LU)",
   "short": "Leading University",
   "domain": "lus.ac.bd",
   "aliases": [
    "লিডিং ইউনিভার্সিটি"
   ]
  }
 ],
 "banks": [
  {
   "name": "Bangladesh Bank (Central Bank)",
   "short": "Bangladesh Bank",
   "aliases": [
    "বাংলাদেশ ব্যাংক"
   ]
  },
  {
   "name": "Sonali Bank PLC",
   "short": "Sonali Bank",
   "aliases": [
    "সোনালী ব্যাংক"
   ]
  },
  {
   "name": "Janata Bank PLC",
   "short": "Janata Bank",
   "aliases": [
    "জনতা ব্যাংক"
   ]
  },
  {
   "name": "Agrani Bank PLC",
   "short": "Agrani Bank",
   "aliases": [
    "অগ্রণী ব্যাংক"
   ]
  },
  {
   "name": "Rupali Bank PLC",
   "short": "Rupali Bank",
   "aliases": [
    "রূপালী ব্যাংক"
   ]
  },
  {
   "name": "BASIC Bank PLC",
   "short": "BASIC Bank",
   "aliases": [
    "বেসিক ব্যাংক"
   ]
  },
  {
   "name": "Bangladesh Development Bank PLC (BDBL)",
   "short": "BDBL",
   "aliases": [
    "বাংলাদেশ ডেভেলপমেন্ট ব্যাংক",
    "বিডিবিএল"
   ]
  },
  {
   "name": "Bangladesh Krishi Bank",
   "short": "Krishi Bank",
   "aliases": [
    "বাংলাদেশ কৃষি ব্যাংক",
    "কৃষি ব্যাংক"
   ]
  },
  {
   "name": "Rajshahi Krishi Unnayan Bank (RAKUB)",
   "short": "RAKUB",
   "aliases": [
    "রাজশাহী কৃষি উন্নয়ন ব্যাংক",
    "রাকাব"
   ]
  },
  {
   "name": "Probashi Kollyan Bank",
   "short": "Probashi Kollyan Bank",
   "aliases": [
    "Probashi Kallyan Bank",
    "প্রবাসী কল্যাণ ব্যাংক"
   ]
  },
  {
   "name": "AB Bank PLC",
   "short": "AB Bank",
   "aliases": [
    "!AB Bank",
    "এবি ব্যাংক"
   ]
  },
  {
   "name": "Bangladesh Commerce Bank Limited",
   "short": "Commerce Bank",
   "aliases": [
    "বাংলাদেশ কমার্স ব্যাংক"
   ]
  },
  {
   "name": "Bank Asia PLC",
   "short": "Bank Asia",
   "aliases": [
    "ব্যাংক এশিয়া"
   ]
  },
  {
   "name": "BRAC Bank PLC",
   "short": "BRAC Bank",
   "aliases": [
    "ব্র্যাক ব্যাংক"
   ]
  },
  {
   "name": "City Bank PLC",
   "short": "City Bank",
   "aliases": [
    "The City Bank",
    "সিটি ব্যাংক"
   ]
  },
  {
   "name": "Dhaka Bank PLC",
   "short": "Dhaka Bank",
   "aliases": [
    "ঢাকা ব্যাংক"
   ]
  },
  {
   "name": "Dutch-Bangla Bank PLC",
   "short": "Dutch-Bangla Bank",
   "aliases": [
    "Dutch Bangla Bank",
    "DBBL",
    "ডাচ-বাংলা ব্যাংক"
   ]
  },
  {
   "name": "Eastern Bank PLC (EBL)",
   "short": "EBL",
   "aliases": [
    "ইস্টার্ন ব্যাংক"
   ]
  },
  {
   "name": "IFIC Bank PLC",
   "short": "IFIC Bank",
   "aliases": [
    "IFIC",
    "আইএফআইসি ব্যাংক"
   ]
  },
  {
   "name": "Jamuna Bank PLC",
   "short": "Jamuna Bank",
   "aliases": [
    "যমুনা ব্যাংক"
   ]
  },
  {
   "name": "Meghna Bank PLC",
   "short": "Meghna Bank",
   "aliases": [
    "মেঘনা ব্যাংক"
   ]
  },
  {
   "name": "Mercantile Bank PLC",
   "short": "Mercantile Bank",
   "aliases": [
    "মার্কেন্টাইল ব্যাংক"
   ]
  },
  {
   "name": "Midland Bank PLC",
   "short": "Midland Bank",
   "aliases": [
    "মিডল্যান্ড ব্যাংক"
   ]
  },
  {
   "name": "Modhumoti Bank PLC",
   "short": "Modhumoti Bank",
   "aliases": [
    "Madhumoti Bank",
    "মধুমতি ব্যাংক"
   ]
  },
  {
   "name": "Mutual Trust Bank PLC (MTB)",
   "short": "MTB",
   "aliases": [
    "Mutual Trust Bank",
    "মিউচুয়াল ট্রাস্ট ব্যাংক"
   ]
  },
  {
   "name": "National Bank PLC",
   "short": "National Bank",
   "aliases": [
    "National Bank Limited",
    "ন্যাশনাল ব্যাংক"
   ]
  },
  {
   "name": "National Credit & Commerce Bank PLC (NCC Bank)",
   "short": "NCC Bank",
   "aliases": [
    "NCC",
    "এনসিসি ব্যাংক",
    "ন্যাশনাল ক্রেডিট অ্যান্ড কমার্স ব্যাংক"
   ]
  },
  {
   "name": "NRB Bank PLC",
   "short": "NRB Bank",
   "aliases": [
    "এনআরবি ব্যাংক"
   ]
  },
  {
   "name": "NRBC Bank PLC",
   "short": "NRBC Bank",
   "aliases": [
    "NRBC",
    "এনআরবিসি ব্যাংক"
   ]
  },
  {
   "name": "ONE Bank PLC",
   "short": "ONE Bank",
   "aliases": [
    "!ONE Bank",
    "ওয়ান ব্যাংক"
   ]
  },
  {
   "name": "Padma Bank PLC",
   "short": "Padma Bank",
   "aliases": [
    "পদ্মা ব্যাংক"
   ]
  },
  {
   "name": "Prime Bank PLC",
   "short": "Prime Bank",
   "aliases": [
    "প্রাইম ব্যাংক"
   ]
  },
  {
   "name": "Pubali Bank PLC",
   "short": "Pubali Bank",
   "aliases": [
    "পূবালী ব্যাংক"
   ]
  },
  {
   "name": "South Bangla Agriculture & Commerce Bank PLC (SBAC)",
   "short": "SBAC Bank",
   "aliases": [
    "SBAC",
    "সাউথ বাংলা এগ্রিকালচার অ্যান্ড কমার্স ব্যাংক"
   ]
  },
  {
   "name": "Southeast Bank PLC",
   "short": "Southeast Bank",
   "aliases": [
    "South East Bank",
    "সাউথইস্ট ব্যাংক"
   ]
  },
  {
   "name": "Shimanto Bank PLC",
   "short": "Shimanto Bank",
   "aliases": [
    "Simanto Bank",
    "শিমান্ত ব্যাংক",
    "সীমান্ত ব্যাংক"
   ]
  },
  {
   "name": "The Premier Bank PLC",
   "short": "Premier Bank",
   "aliases": [
    "Premier Bank",
    "দ্য প্রিমিয়ার ব্যাংক",
    "প্রিমিয়ার ব্যাংক"
   ]
  },
  {
   "name": "Trust Bank PLC",
   "short": "Trust Bank",
   "aliases": [
    "ট্রাস্ট ব্যাংক"
   ]
  },
  {
   "name": "United Commercial Bank PLC (UCB)",
   "short": "UCB",
   "aliases": [
    "ইউনাইটেড কমার্শিয়াল ব্যাংক",
    "ইউসিবি"
   ]
  },
  {
   "name": "Uttara Bank PLC",
   "short": "Uttara Bank",
   "aliases": [
    "উত্তরা ব্যাংক"
   ]
  },
  {
   "name": "Bengal Commercial Bank PLC",
   "short": "Bengal Commercial Bank",
   "aliases": [
    "বেঙ্গল কমার্শিয়াল ব্যাংক"
   ]
  },
  {
   "name": "Citizens Bank PLC",
   "short": "Citizens Bank",
   "aliases": [
    "সিটিজেনস ব্যাংক"
   ]
  },
  {
   "name": "Community Bank Bangladesh PLC",
   "short": "Community Bank",
   "aliases": [
    "Community Bank",
    "কমিউনিটি ব্যাংক"
   ]
  },
  {
   "name": "Al-Arafah Islami Bank PLC",
   "short": "Al-Arafah Islami Bank",
   "aliases": [
    "Al Arafah Islami Bank",
    "আল-আরাফাহ ইসলামী ব্যাংক",
    "আল আরাফাহ ইসলামী ব্যাংক"
   ]
  },
  {
   "name": "EXIM Bank PLC",
   "short": "EXIM Bank",
   "aliases": [
    "এক্সিম ব্যাংক"
   ]
  },
  {
   "name": "First Security Islami Bank PLC",
   "short": "First Security Islami Bank",
   "aliases": [
    "FSIBL",
    "ফার্স্ট সিকিউরিটি ইসলামী ব্যাংক"
   ]
  },
  {
   "name": "Global Islami Bank PLC",
   "short": "Global Islami Bank",
   "aliases": [
    "গ্লোবাল ইসলামী ব্যাংক"
   ]
  },
  {
   "name": "ICB Islamic Bank PLC",
   "short": "ICB Islamic Bank",
   "aliases": [
    "আইসিবি ইসলামিক ব্যাংক"
   ]
  },
  {
   "name": "Islami Bank Bangladesh PLC",
   "short": "Islami Bank",
   "aliases": [
    "IBBL",
    "ইসলামী ব্যাংক বাংলাদেশ"
   ]
  },
  {
   "name": "Shahjalal Islami Bank PLC",
   "short": "Shahjalal Islami Bank",
   "aliases": [
    "শাহজালাল ইসলামী ব্যাংক"
   ]
  },
  {
   "name": "Social Islami Bank PLC",
   "short": "Social Islami Bank",
   "aliases": [
    "SIBL",
    "সোশ্যাল ইসলামী ব্যাংক"
   ]
  },
  {
   "name": "Standard Bank PLC",
   "short": "Standard Bank",
   "aliases": [
    "স্ট্যান্ডার্ড ব্যাংক"
   ]
  },
  {
   "name": "Union Bank PLC",
   "short": "Union Bank",
   "aliases": [
    "ইউনিয়ন ব্যাংক"
   ]
  },
  {
   "name": "Bank Alfalah Limited",
   "short": "Bank Alfalah",
   "aliases": [
    "ব্যাংক আলফালাহ"
   ]
  },
  {
   "name": "Citibank N.A.",
   "short": "Citibank",
   "aliases": [
    "Citibank",
    "সিটিব্যাংক"
   ]
  },
  {
   "name": "Commercial Bank of Ceylon PLC",
   "short": "Commercial Bank of Ceylon",
   "aliases": [
    "কমার্শিয়াল ব্যাংক অব সিলন"
   ]
  },
  {
   "name": "Habib Bank Limited",
   "short": "Habib Bank",
   "aliases": [
    "হাবিব ব্যাংক"
   ]
  },
  {
   "name": "HSBC (The Hongkong and Shanghai Banking Corporation)",
   "short": "HSBC",
   "aliases": [
    "Hongkong and Shanghai Banking",
    "এইচএসবিসি"
   ]
  },
  {
   "name": "National Bank of Pakistan",
   "short": "National Bank of Pakistan",
   "aliases": [
    "ন্যাশনাল ব্যাংক অব পাকিস্তান"
   ]
  },
  {
   "name": "Standard Chartered Bank",
   "short": "Standard Chartered",
   "aliases": [
    "স্ট্যান্ডার্ড চার্টার্ড ব্যাংক"
   ]
  },
  {
   "name": "State Bank of India",
   "short": "State Bank of India",
   "aliases": [
    "স্টেট ব্যাংক অব ইন্ডিয়া"
   ]
  },
  {
   "name": "Woori Bank",
   "short": "Woori Bank",
   "aliases": [
    "উরি ব্যাংক"
   ]
  },
  {
   "name": "Ansar VDP Unnayan Bank",
   "short": "Ansar VDP Bank",
   "aliases": [
    "Ansar-VDP Unnayan Bank",
    "আনসার ভিডিপি উন্নয়ন ব্যাংক"
   ]
  },
  {
   "name": "Karmasangsthan Bank",
   "short": "Karmasangsthan Bank",
   "aliases": [
    "কর্মসংস্থান ব্যাংক"
   ]
  },
  {
   "name": "Grameen Bank",
   "short": "Grameen Bank",
   "aliases": [
    "গ্রামীণ ব্যাংক"
   ]
  },
  {
   "name": "Palli Sanchay Bank",
   "short": "Palli Sanchay Bank",
   "aliases": [
    "পল্লী সঞ্চয় ব্যাংক"
   ]
  },
  {
   "name": "Jubilee Bank",
   "short": "Jubilee Bank",
   "aliases": [
    "জুবিলী ব্যাংক",
    "জুবিলি ব্যাংক"
   ]
  }
 ],
 "sources": [
  {
   "id": "bdgov_gov",
   "name": "BD Govt Job: government circulars",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/government-jobs-circular/",
   "pages": 3,
   "group": "gov"
  },
  {
   "id": "bdgov_teletalk",
   "name": "BD Govt Job: online-apply (Teletalk) circulars",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/teletalk-application/",
   "pages": 3,
   "group": "gov"
  },
  {
   "id": "bdgov_uni",
   "name": "BD Govt Job: university circulars",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/university-job-circular/",
   "pages": 4,
   "group": "uni"
  },
  {
   "id": "bdgov_edu",
   "name": "BD Govt Job: educational institution circulars",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/educational-institution-jobs/",
   "pages": 2,
   "group": "uni"
  },
  {
   "id": "bdgov_bank",
   "name": "BD Govt Job: bank circulars",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/bank-jobs/",
   "pages": 4,
   "group": "bank",
   "org_assumed": true
  },
  {
   "id": "bdgov_private",
   "name": "BD Govt Job: private-sector circulars (banks)",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/private-jobs/",
   "pages": 3,
   "group": "bank"
  },
  {
   "id": "bdgov_ngo",
   "name": "BD Govt Job: NGO circulars (BRAC, UN etc.)",
   "type": "bdgovtjob_list",
   "url": "https://bdgovtjob.net/category/ngo-job-circular/",
   "pages": 3,
   "group": "ngo"
  },
  {
   "id": "bracu_lecturer",
   "name": "BRAC University: Lecturer page",
   "type": "page_watch",
   "url": "https://www.bracu.ac.bd/lecturer",
   "keywords": [
    "architecture",
    "স্থাপত্য"
   ],
   "group": "uni"
  },
  {
   "id": "m_teletalk",
   "name": "Alljobs by Teletalk",
   "type": "manual",
   "url": "https://alljobs.teletalk.com.bd/",
   "note": "Listings load with JavaScript, so a script cannot read them. Search 'Architect'."
  },
  {
   "id": "m_erecruit",
   "name": "Bangladesh Bank e-recruitment (state-owned banks)",
   "type": "manual",
   "url": "https://erecruitment.bb.org.bd/",
   "note": "Could not be read automatically. State-bank circulars are also picked up through BD Govt Job."
  },
  {
   "id": "m_bpsc",
   "name": "Bangladesh Public Service Commission (BPSC)",
   "type": "manual",
   "url": "https://bpsc.gov.bd/",
   "note": "Official source for non-cadre and cadre circulars."
  },
  {
   "id": "m_brac",
   "name": "BRAC careers",
   "type": "manual",
   "url": "https://careers.brac.net/",
   "note": "Official BRAC portal. Search 'Architect', 'Engineer - Design', 'Infrastructure'."
  },
  {
   "id": "m_undp",
   "name": "UNDP jobs",
   "type": "manual",
   "url": "https://jobs.undp.org/",
   "note": "Filter country: Bangladesh."
  },
  {
   "id": "m_un",
   "name": "UN Careers",
   "type": "manual",
   "url": "https://careers.un.org/",
   "note": "Filter location: Bangladesh."
  },
  {
   "id": "m_reliefweb",
   "name": "ReliefWeb jobs",
   "type": "manual",
   "url": "https://reliefweb.int/jobs",
   "note": "Filter country: Bangladesh. Covers UN agencies and international NGOs."
  },
  {
   "id": "m_bdjobs",
   "name": "Bdjobs",
   "type": "manual",
   "url": "https://jobs.bdjobs.com/",
   "note": "Search 'Architect' and 'Lecturer'."
  }
 ]
}''')


UI_HTML = '<!doctype html>\n<html lang="en" data-theme="frost">\n<head>\n<meta charset="utf-8">\n<title>Job Monitor</title>\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<style>\n:root{\n  --font:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;\n  --ga:.42;            /* glass opacity, changed by the slider */\n  --r:14px;\n}\nhtml[data-theme=frost]{\n  --bg:radial-gradient(1100px 700px at 12% 8%,#ffffff 0%,transparent 60%),radial-gradient(900px 700px at 90% 95%,#b9bec6 0%,transparent 62%),linear-gradient(135deg,#e9ebee,#cfd3d9);\n  --g:255 255 255; --g2:255 255 255; --ink:#15171a; --mute:#5d636b; --soft:rgb(20 23 26/.06); --soft2:rgb(20 23 26/.11);\n  --btn:#15171a; --btnink:#f4f5f6; --edge:rgb(255 255 255/.55); --shadow:0 10px 40px rgb(30 35 42/.12);\n  --ok:96 130 108; --warn:176 120 70; --scheme:light;\n}\nhtml[data-theme=smoke]{\n  --bg:radial-gradient(1000px 700px at 15% 0%,#8b9097 0%,transparent 60%),radial-gradient(900px 700px at 95% 100%,#2b2e33 0%,transparent 65%),linear-gradient(135deg,#6e737a,#3d4046);\n  --g:92 96 102; --g2:30 32 36; --ink:#f4f5f6; --mute:#cfd3d8; --soft:rgb(255 255 255/.08); --soft2:rgb(255 255 255/.15);\n  --btn:#f1f2f4; --btnink:#1a1c1f; --edge:rgb(255 255 255/.18); --shadow:0 10px 40px rgb(0 0 0/.25);\n  --ok:150 190 160; --warn:222 170 118; --scheme:dark;\n}\nhtml[data-theme=graphite]{\n  --bg:radial-gradient(1000px 700px at 10% 0%,#3a3d42 0%,transparent 60%),radial-gradient(900px 700px at 100% 100%,#0b0c0e 0%,transparent 65%),linear-gradient(135deg,#25272b,#0f1012);\n  --g:58 61 66; --g2:10 11 13; --ink:#eef0f2; --mute:#aab0b8; --soft:rgb(255 255 255/.06); --soft2:rgb(255 255 255/.12);\n  --btn:#ecedef; --btnink:#121315; --edge:rgb(255 255 255/.10); --shadow:0 10px 40px rgb(0 0 0/.45);\n  --ok:140 186 154; --warn:218 166 116; --scheme:dark;\n}\n*{box-sizing:border-box;margin:0}\nhtml,body{height:100%}\nbody{font:14px/1.45 var(--font);color:var(--ink);background:var(--bg) fixed;color-scheme:var(--scheme);overflow:hidden}\nbutton,input,select,textarea{font:inherit;color:inherit}\nbutton{cursor:pointer;background:none;border:0}\na{color:inherit}\n.app{display:grid;grid-template-columns:200px 1fr;height:100%;gap:14px;padding:14px}\n.glass{background:rgb(var(--g) / var(--ga));backdrop-filter:blur(26px) saturate(1.2);-webkit-backdrop-filter:blur(26px) saturate(1.2);\n  border-radius:var(--r);box-shadow:var(--shadow),inset 0 1px 0 var(--edge)}\n.glass.dark{background:rgb(var(--g2) / calc(var(--ga) + .1))}\n\n/* side */\n.side{display:flex;flex-direction:column;padding:20px 12px 14px;min-height:0}\n.brand{padding:0 10px 18px;font-size:15px;font-weight:600;letter-spacing:.01em}\n.brand small{display:block;font-weight:400;color:var(--mute);font-size:12px;margin-top:2px}\n.nav{display:flex;flex-direction:column;gap:2px}\n.nav button{text-align:left;padding:8px 10px;border-radius:9px;color:var(--mute);display:flex;justify-content:space-between;gap:8px}\n.nav button:hover{background:var(--soft);color:var(--ink)}\n.nav button.on{background:var(--soft2);color:var(--ink);font-weight:600}\n.nav .n{font-variant-numeric:tabular-nums;font-weight:400;color:var(--mute)}\n.spacer{flex:1}\n.set{padding:10px;color:var(--mute);font-size:12.5px;display:flex;flex-direction:column;gap:9px}\n.set .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}\n.set button{color:var(--mute)} .set button.on{color:var(--ink);font-weight:600}\n.set button:hover{color:var(--ink)}\n.set input[type=range]{width:100%;accent-color:var(--btn)}\n.set label{display:flex;gap:7px;align-items:center;cursor:pointer}\n\n/* main */\n.main{display:flex;flex-direction:column;min-height:0;padding:0}\n.top{display:flex;align-items:center;gap:16px;padding:18px 24px 6px}\n.top h1{font-size:20px;font-weight:600;letter-spacing:-.01em}\n.top .sub{color:var(--mute);font-size:12.5px;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.btn{padding:8px 16px;border-radius:10px;background:var(--btn);color:var(--btnink);font-weight:600}\n.btn:disabled{opacity:.5;cursor:default}\n.btn.quiet{background:var(--soft);color:var(--ink);font-weight:500}\n.btn.quiet:hover{background:var(--soft2)}\n.btn.sm{padding:5px 11px;font-size:13px}\n.view{flex:1;overflow:auto;padding:10px 24px 24px;min-height:0}\n.view::-webkit-scrollbar{width:10px}.view::-webkit-scrollbar-thumb{background:var(--soft2);border-radius:8px;border:3px solid transparent;background-clip:content-box}\n\n/* controls */\n.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:4px 0 14px}\n.bar input[type=search],.bar select,.field input,.field select,.field textarea,.add input,.add select{\n  background:var(--soft);border:0;border-radius:9px;padding:7px 11px;outline:none;min-width:0}\n.bar input[type=search]{flex:1;min-width:180px}\n.bar select option,.field select option{background:#444;color:#fff}\n.bar input:focus,.bar select:focus,.field input:focus,.field textarea:focus,.field select:focus,.add input:focus{background:var(--soft2)}\n.count{color:var(--mute);font-size:12.5px;margin-left:auto}\n\n/* rows */\n.h{font-size:12px;color:var(--mute);letter-spacing:.06em;text-transform:uppercase;margin:20px 4px 6px}\n.h:first-child{margin-top:4px}\n.item{display:grid;grid-template-columns:1fr auto;gap:2px 16px;padding:11px 14px;border-radius:11px;cursor:pointer;margin-bottom:2px}\n.item:hover{background:var(--soft)}\n.item .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}\n.item .m{color:var(--mute);font-size:12.5px;grid-column:1}\n.item .r{grid-row:1/3;grid-column:2;text-align:right;font-size:12.5px;color:var(--mute);align-self:center;white-space:nowrap}\n.item .r b{display:block;font-weight:600;color:var(--ink);font-size:13px}\n.item.applied{background:rgb(var(--ok)/.16)}\n.item.applied:hover{background:rgb(var(--ok)/.24)}\n.item.hid{opacity:.5}\n.tag-ok{color:rgb(var(--ok));font-weight:600}\nhtml[data-theme=frost] .tag-ok{color:rgb(70 105 82)}\n.urgent{color:rgb(var(--warn))!important}\nhtml[data-theme=frost] .urgent{color:rgb(150 95 45)!important}\n.dim{color:var(--mute)}\n\n/* latest */\n.lead{font-size:22px;font-weight:600;letter-spacing:-.015em;margin:6px 4px 4px;max-width:760px}\n.lead2{color:var(--mute);margin:0 4px 16px}\n.stats{display:flex;gap:30px;margin:0 4px 8px;flex-wrap:wrap}\n.stats div{font-size:12.5px;color:var(--mute)}\n.stats b{display:block;font-size:24px;font-weight:600;color:var(--ink);letter-spacing:-.02em;font-variant-numeric:tabular-nums}\n.notice{margin:0 4px 14px;color:var(--mute)}\n.notice a{cursor:pointer;text-decoration:underline;text-underline-offset:3px}\n.runs{margin:0 4px;font-variant-numeric:tabular-nums}\n.runs div{display:grid;grid-template-columns:150px 1fr;padding:4px 0;color:var(--mute);font-size:13px}\n\n/* applied */\n.card{padding:16px 18px;margin-bottom:12px;border-radius:12px;background:var(--soft)}\n.card .head{display:flex;gap:12px;align-items:baseline;margin-bottom:10px}\n.card .head input.t{flex:1;font-weight:600;font-size:15px;background:none;border:0;outline:0;padding:0;border-radius:0;min-width:0}\n.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px 14px}\n.field{display:flex;flex-direction:column;gap:4px}\n.field span{font-size:12px;color:var(--mute)}\n.field.wide{grid-column:1/-1}\n.field textarea{min-height:76px;resize:vertical}\n.field .pw{display:flex;gap:4px;align-items:center}\n.field .pw input{flex:1}\n.field input,.field select,.field textarea{background:rgb(var(--g2)/.0);background:var(--soft)}\n.tools{display:flex;gap:14px;margin-top:10px;align-items:center}\n.tools .sp{flex:1}\n.link{color:var(--mute);cursor:pointer;font-size:13px}.link:hover{color:var(--ink)}\n.saved-msg{font-size:12px;color:var(--mute);min-width:50px}\n\n/* tables / lists */\n.tbl .row{display:grid;grid-template-columns:minmax(160px,1.1fr) 90px minmax(160px,2fr) auto;gap:14px;padding:8px 14px;border-radius:9px;align-items:baseline}\n.tbl .row:hover{background:var(--soft)}\n.tbl .row .d{color:var(--mute);font-size:13px;overflow:hidden;text-overflow:ellipsis}\n.tbl.tools-l .row{grid-template-columns:minmax(200px,1.4fr) auto auto auto}\n.add{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 4px}\n.add input{flex:1;min-width:160px}\npre.rep{white-space:pre-wrap;font:13px/1.55 ui-monospace,Consolas,monospace;padding:4px 4px;color:var(--ink)}\n.log div{font:12.5px/1.6 ui-monospace,Consolas,monospace;color:var(--mute)}\n.empty{color:var(--mute);padding:30px 6px;max-width:560px}\n.running{margin:0 24px;padding:8px 14px;border-radius:10px;background:var(--soft);color:var(--mute);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n\n/* drawer */\n.scrim{position:fixed;inset:0;background:rgb(0 0 0/.18);opacity:0;pointer-events:none;transition:opacity .18s}\n.scrim.on{opacity:1;pointer-events:auto}\n.drawer{position:fixed;top:14px;right:14px;bottom:14px;width:min(460px,92vw);padding:24px 24px 18px;transform:translateX(calc(100% + 30px));transition:transform .22s ease;overflow:auto;display:flex;flex-direction:column;gap:12px}\n.drawer.on{transform:none}\n.drawer h2{font-size:17px;line-height:1.3;font-weight:600}\n.drawer dl{display:grid;grid-template-columns:96px 1fr;gap:6px 12px;font-size:13.5px}\n.drawer dt{color:var(--mute)}\n.drawer .snip{color:var(--mute);font-size:13px;line-height:1.55;background:var(--soft);padding:10px 12px;border-radius:10px;word-break:break-word}\n.drawer .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:8px}\n.toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,20px);opacity:0;transition:.2s;padding:9px 16px;border-radius:10px;pointer-events:none}\n.toast.on{opacity:1;transform:translate(-50%,0)}\n@media(max-width:860px){.app{grid-template-columns:1fr}.side{display:none}}\n</style>\n</head>\n<body>\n<div class="app">\n  <aside class="side glass">\n    <div class="brand">Job Monitor<small>Bangladesh · architecture</small></div>\n    <nav class="nav" id="nav"></nav>\n    <div class="spacer"></div>\n    <div class="set">\n      <div class="row" id="themes"><button data-theme="frost">Frost</button><button data-theme="smoke">Smoke</button><button data-theme="graphite">Graphite</button></div>\n      <div>Glass<input type="range" id="glass" min="12" max="85" value="42"></div>\n      <label><input type="checkbox" id="onstart"> Check when opened</label>\n      <div class="row"><button id="openFolder">Data folder</button></div>\n    </div>\n  </aside>\n  <section class="main glass">\n    <div class="top"><h1 id="title">Latest</h1><div class="sub" id="sub"></div><button class="btn" id="check">Check now</button></div>\n    <div class="running" id="running" hidden></div>\n    <div class="view" id="view"></div>\n  </section>\n</div>\n<div class="scrim" id="scrim"></div>\n<aside class="drawer glass" id="drawer"></aside>\n<div class="toast glass" id="toast"></div>\n\n<script>\nconst TOKEN="__TOKEN__";\nconst $=s=>document.querySelector(s);\nconst esc=s=>String(s==null?"":s).replace(/[&<>"\']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\nconst store={get(k,d){try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};\nasync function api(path,body){\n  const o={headers:{"X-Token":TOKEN}};\n  if(body!==undefined){o.method="POST";o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body)}\n  const r=await fetch(path,o);let j={};try{j=await r.json()}catch(e){}\n  if(!r.ok)throw new Error(j.error||("Error "+r.status));return j;\n}\nfunction toast(m){const t=$("#toast");t.textContent=m;t.classList.add("on");clearTimeout(toast.h);toast.h=setTimeout(()=>t.classList.remove("on"),2600)}\nconst fmtD=iso=>{if(!iso)return"";const d=new Date(iso.slice(0,10)+"T00:00");return isNaN(d)?iso:d.toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"numeric"})};\nconst fmtT=iso=>{if(!iso)return"";const d=new Date(iso);return isNaN(d)?iso:d.toLocaleString("en-GB",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"})};\nfunction left(r){const n=r.days_left;if(n==null)return"No date found";if(n<0)return"Closed";if(n===0)return"Today";if(n===1)return"Tomorrow";return n+" days left"}\nconst TYPES={gov:"Government",uni:"University",bank:"Bank",ngo:"NGO / UN",check:"Needs a look",watch:"Watched page"};\nconst VIEWS=[["latest","Latest"],["jobs","Jobs"],["applied","Applied"],["saved","Saved"],["sources","Sources"],["tools","Search tools"],["report","Report"],["activity","Activity"]];\n\nlet S=null, view=store.get("view","latest"), TOOLS=null, APPS=[], appsLoaded=false, lastSig="";\nlet F=Object.assign({q:"",sort:"deadline",type:"all",org:"all",dl:"any",status:"active"},store.get("filters",{}));\nlet AF={q:"",stage:"all"};\n\n/* ---------- appearance ---------- */\nfunction setTheme(t,save){document.documentElement.dataset.theme=t;store.set("theme",t);document.querySelectorAll("#themes button").forEach(b=>b.classList.toggle("on",b.dataset.theme===t));if(save)api("/api/setting",{theme:t}).catch(()=>{})}\nfunction setGlass(v){document.documentElement.style.setProperty("--ga",(v/100).toFixed(2));store.set("glass",v)}\n\n/* ---------- shell ---------- */\nfunction renderNav(){\n  const cnt={jobs:S?S.rows.filter(r=>!r.mark||r.mark!=="hidden").length:"",applied:S?S.applied_count:"",saved:S?S.rows.filter(r=>r.mark==="saved").length:""};\n  $("#nav").innerHTML=VIEWS.map(([k,l])=>`<button data-view="${k}" class="${k===view?"on":""}">${l}${cnt[k]!==undefined&&cnt[k]!==""?`<span class="n">${cnt[k]}</span>`:""}</button>`).join("");\n}\nfunction go(v){\n  flushSaves();view=v;store.set("view",v);renderNav();$("#title").textContent=VIEWS.find(x=>x[0]===v)[1];\n  if(v==="applied")loadApps().then(()=>renderView(true));else renderView(true);\n}\nfunction chrome(){\n  if(!S)return;\n  const b=$("#check");b.disabled=S.running;b.textContent=S.running?"Checking…":"Check now";\n  const run=$("#running");run.hidden=!S.running;run.textContent=S.status||"";\n  $("#sub").textContent=S.last_run?("Last check "+fmtT(S.last_run)):"No check yet";\n  $("#onstart").checked=!!S.check_on_start;\n}\n\n/* ---------- jobs ---------- */\nfunction visible(rows){\n  const q=F.q.trim().toLowerCase();\n  return rows.filter(r=>{\n    if(F.status==="active"&&r.mark==="hidden")return false;\n    if(F.status==="new"&&!r.new)return false;\n    if(F.status==="open"&&(r.applied||r.mark==="hidden"))return false;\n    if(F.status==="applied"&&!r.applied)return false;\n    if(F.status==="saved"&&r.mark!=="saved")return false;\n    if(F.status==="hidden"&&r.mark!=="hidden")return false;\n    if(F.type!=="all"&&r.type!==F.type&&!(F.type==="check"&&r.conf==="check"))return false;\n    if(F.org!=="all"&&r.org!==F.org)return false;\n    const n=r.days_left;\n    if(F.dl==="week"&&!(n!=null&&n<=7))return false;\n    if(F.dl==="two"&&!(n!=null&&n<=14))return false;\n    if(F.dl==="month"&&!(n!=null&&n<=30))return false;\n    if(F.dl==="none"&&n!=null)return false;\n    if(q&&!(r.title+" "+r.org+" "+r.types.join(" ")+" "+r.snippet).toLowerCase().includes(q))return false;\n    return true;\n  });\n}\nfunction sorted(rows){\n  const dl=r=>r.deadline||"9999",cmp={\n    deadline:(a,b)=>dl(a).localeCompare(dl(b)),\n    added:(a,b)=>(b.added||"").localeCompare(a.added||""),\n    posted:(a,b)=>(b.published||b.added||"").localeCompare(a.published||a.added||""),\n    org:(a,b)=>(a.org||"~").localeCompare(b.org||"~")||dl(a).localeCompare(dl(b)),\n    type:(a,b)=>Object.keys(TYPES).indexOf(a.type)-Object.keys(TYPES).indexOf(b.type)||dl(a).localeCompare(dl(b)),\n  }[F.sort]||(()=>0);\n  return rows.slice().sort(cmp);\n}\nfunction itemHTML(r){\n  const types=r.types.map((t,i)=>t+(i===0&&r.conf==="low"?" (possible)":"")).join(" · ");\n  const meta=[r.org,types,r.published?("Posted "+fmtD(r.published)):("Found "+fmtD(r.added))].filter(Boolean).join("  ·  ");\n  const urgent=r.days_left!=null&&r.days_left<=(S?S.soon_days:7)&&r.days_left>=0;\n  const right=`<b class="${urgent?"urgent":""}">${r.deadline?fmtD(r.deadline):"No date"}</b>${r.applied?\'<span class="tag-ok">Applied</span>\':r.new?"New":r.mark==="saved"?"Saved":left(r)}`;\n  return `<div class="item ${r.applied?"applied":""} ${r.mark==="hidden"?"hid":""}" data-open="${esc(r.url)}"><div class="t">${esc(r.title)}</div><div class="m">${esc(meta)}</div><div class="r">${right}</div></div>`;\n}\nfunction renderJobs(full){\n  const rows=S.rows;\n  if(full){\n    const orgs=[...new Set(rows.map(r=>r.org).filter(Boolean))].sort();\n    const tcount=t=>rows.filter(r=>r.type===t).length;\n    $("#view").innerHTML=`<div class="bar">\n      <input type="search" id="fq" placeholder="Search title, organisation, text" value="${esc(F.q)}">\n      <select id="fsort" title="Sort by"><option value="deadline">Sort: Deadline</option><option value="added">Sort: Newest found</option><option value="posted">Sort: Newest posted</option><option value="org">Sort: Organisation</option><option value="type">Sort: Type</option></select>\n      <select id="ftype"><option value="all">All types</option>${Object.entries(TYPES).filter(([k])=>tcount(k)||k===F.type).map(([k,l])=>`<option value="${k}">${l} (${tcount(k)})</option>`).join("")}</select>\n      <select id="forg"><option value="all">All organisations</option>${orgs.map(o=>`<option>${esc(o)}</option>`).join("")}</select>\n      <select id="fdl"><option value="any">Any deadline</option><option value="week">Within 7 days</option><option value="two">Within 14 days</option><option value="month">Within 30 days</option><option value="none">No date found</option></select>\n      <select id="fstatus"><option value="active">Not hidden</option><option value="new">New only</option><option value="open">Not applied yet</option><option value="applied">Applied</option><option value="saved">Saved</option><option value="hidden">Hidden</option></select>\n      <span class="count" id="fcount"></span></div><div id="list"></div>`;\n    $("#fsort").value=F.sort;$("#ftype").value=F.type;$("#forg").value=F.org;$("#fdl").value=F.dl;$("#fstatus").value=F.status;\n    if(F.org!=="all"&&!orgs.includes(F.org)){F.org="all";$("#forg").value="all"}\n  }\n  const list=sorted(visible(rows));\n  $("#fcount").textContent=list.length+" of "+rows.length;\n  let html="",last=null;\n  for(const r of list){\n    const g=F.sort==="type"?(TYPES[r.type]||""):F.sort==="org"?(r.org||"Organisation not known"):null;\n    if(g!==null&&g!==last){html+=`<div class="h">${esc(g)}</div>`;last=g}\n    html+=itemHTML(r);\n  }\n  $("#list").innerHTML=html||`<div class="empty">${rows.length?"Nothing matches these filters.":S.last_run?"No matching jobs right now. The app keeps looking each time you check.":"Press Check now to look for jobs."}</div>`;\n}\n\n/* ---------- latest ---------- */\nfunction section(title,rows){\n  return rows.length?`<div class="h">${title}</div>`+rows.map(x=>x.title&&!x.kind?`<div class="item" data-open="${esc(x.url)}"><div class="t">${esc(x.title)}</div></div>`:itemHTML(x)).join(""):"";\n}\nfunction renderLatest(){\n  const L=S.latest;\n  if(!L){$("#view").innerHTML=`<div class="lead">No check yet.</div><div class="lead2">Press Check now. The first check reads many pages and takes a few minutes.</div>`;return}\n  const by=Object.fromEntries(S.rows.map(r=>[r.url,r]));\n  const nw=L.new.map(x=>by[x.url]||{title:x.title,url:x.url}),ch=L.changed.map(x=>by[x.url]||{title:x.title,url:x.url});\n  const soon=S.rows.filter(r=>r.days_left!=null&&r.days_left>=0&&r.days_left<=S.soon_days&&r.mark!=="hidden"&&!r.applied);\n  let lead,sub="";\n  if(L.offline){lead="No source could be read.";sub="Check your internet connection and try again."}\n  else if(L.first){lead=`First check done: ${L.count} matching ${L.count===1?"job":"jobs"} found.`;sub="From now on this page shows what changed since the previous check."}\n  else if(L.same){lead="Same results as the last check.";sub=`Nothing new and nothing closed. ${L.count} jobs are still open. Last check ${fmtT(L.ts)}, the one before ${fmtT(L.prev_ts)}.`}\n  else{lead=`${L.new.length} new, ${L.changed.length} with a changed deadline, ${L.closed.length} closed.`;sub=`Compared with the check on ${fmtT(L.prev_ts)}. ${L.count} jobs are open now.`}\n  const bad=L.sources_failed;\n  $("#view").innerHTML=`<div class="lead">${esc(lead)}</div><div class="lead2">${esc(sub)}</div>\n   <div class="stats"><div><b>${L.count}</b>open</div><div><b>${L.new.length}</b>new</div><div><b>${soon.length}</b>closing in ${S.soon_days} days</div><div><b>${S.applied_count}</b>applied</div></div>\n   ${bad?`<div class="notice">${bad} of ${L.sources_ok+bad} sources could not be read. <a data-go="sources">See Sources</a></div>`:""}\n   ${section("New since the last check",nw)}${section("Deadline changed",ch)}\n   ${L.closed.length?`<div class="h">Closed since the last check</div>`+L.closed.map(c=>`<div class="item"><div class="t dim">${esc(c.title)}</div></div>`).join(""):""}\n   ${section("Closing soon",soon)}\n   ${S.runs.length>1?`<div class="h">Earlier checks</div><div class="runs">${S.runs.slice(0,8).map(r=>`<div><span>${fmtT(r.ts)}</span><span>${r.offline?"Could not connect":r.same?"Same results":`${r.new} new, ${r.changed} changed, ${r.closed} closed`} · ${r.count} open</span></div>`).join("")}</div>`:""}`;\n}\n\n/* ---------- saved ---------- */\nfunction renderSaved(){\n  const rows=sorted(S.rows.filter(r=>r.mark==="saved"));\n  $("#view").innerHTML=rows.length?rows.map(itemHTML).join(""):`<div class="empty">Nothing saved. Open a job and choose Save to keep it here.</div>`;\n}\n\n/* ---------- applied ---------- */\nasync function loadApps(){const j=await api("/api/apps");APPS=j.apps;appsLoaded=true;S&&(S.stages=j.stages)}\nconst pending={};\nfunction flushSaves(){for(const id in pending){clearTimeout(pending[id].t);pending[id].run()}}\nfunction appCard(a){\n  const f=(k,l,t="text",cls="")=>`<label class="field ${cls}"><span>${l}</span><input data-k="${k}" type="${t}" value="${esc(a[k])}" autocomplete="off"></label>`;\n  return `<div class="card" data-id="${a.id}">\n   <div class="head"><input class="t" data-k="title" value="${esc(a.title)}" placeholder="Post or circular title"><span class="saved-msg"></span></div>\n   <div class="grid">\n    ${f("org","Organisation")}\n    <label class="field"><span>Stage</span><select data-k="stage">${S.stages.map(s=>`<option${s===a.stage?" selected":""}>${s}</option>`).join("")}</select></label>\n    ${f("applied_on","Applied on","date")}${f("next_date","Next date (exam, viva…)","date")}${f("deadline","Application deadline","date")}\n    ${f("app_id","Application ID")}${f("login","User name / login")}\n    <label class="field"><span>Password</span><span class="pw"><input data-k="password" type="password" value="${esc(a.password)}" autocomplete="new-password"><button class="link" data-act="showpw">Show</button><button class="link" data-act="copypw">Copy</button></span></label>\n    ${f("txn_id","Transaction ID")}\n    <label class="field wide"><span>Notes</span><textarea data-k="notes" placeholder="Anything worth remembering">${esc(a.notes)}</textarea></label>\n   </div>\n   <div class="tools">${a.url?`<a class="link" data-act="openurl" data-url="${esc(a.url)}">Open circular</a>`:""}<span class="sp"></span><button class="link" data-act="del">Delete</button></div></div>`;\n}\nfunction renderApplied(full){\n  if(full){\n    $("#view").innerHTML=`<div class="bar"><input type="search" id="aq" placeholder="Search applications" value="${esc(AF.q)}">\n      <select id="astage"><option value="all">All stages</option>${S.stages.map(s=>`<option>${s}</option>`).join("")}</select>\n      <button class="btn sm" id="addApp">Add application</button><span class="count" id="acount"></span></div>\n      <div class="notice">IDs and passwords are stored as plain text in applications.json in the app folder. Use this for portal logins only, never for your email or bank passwords.</div><div id="alist"></div>`;\n    $("#astage").value=AF.stage;\n  }\n  const q=AF.q.trim().toLowerCase();\n  const list=APPS.filter(a=>(AF.stage==="all"||a.stage===AF.stage)&&(!q||Object.values(a).join(" ").toLowerCase().includes(q)));\n  $("#acount").textContent=list.length+" of "+APPS.length;\n  $("#alist").innerHTML=list.length?list.map(appCard).join(""):`<div class="empty">${APPS.length?"No application matches.":"Nothing here yet. Open a job and choose Mark as applied, or add one yourself. Keep your application ID, password and transaction ID with it."}</div>`;\n}\nfunction scheduleSave(card,input){\n  const id=card.dataset.id;\n  const run=async()=>{\n    delete pending[id];const fields={};card.querySelectorAll("[data-k]").forEach(e=>fields[e.dataset.k]=e.value);\n    const m=card.querySelector(".saved-msg");\n    try{await api("/api/app/save",{id,fields});Object.assign(APPS.find(a=>a.id===id)||{},fields);m.textContent="Saved"}catch(e){m.textContent="Not saved"}\n    setTimeout(()=>{m.textContent=""},1600);\n  };\n  if(pending[id])clearTimeout(pending[id].t);\n  pending[id]={t:setTimeout(run,600),run};\n}\n\n/* ---------- sources ---------- */\nfunction renderSources(full){\n  if(full){\n    $("#view").innerHTML=`<div class="notice">The app reads these pages every check. A university page can be added below, for example its notice or career page.</div>\n     <div class="add"><input id="sn" placeholder="Name, e.g. KUET Career"><input id="su" placeholder="https://…"><select id="sg"><option value="uni">University</option><option value="gov">Government</option><option value="bank">Bank</option><option value="ngo">NGO / UN</option></select><button class="btn sm" id="sadd">Add page</button></div><div id="slist"></div>`;\n  }\n  const src=S.sources.filter(s=>s.type!=="manual");\n  const rank=s=>s.ok===false?0:s.warn?1:s.ok==null?3:2;\n  const bad=src.filter(s=>s.ok===false).length;\n  const row=s=>`<div class="row"><div>${esc(s.name)}</div><div class="${s.ok===false?"urgent":"dim"}">${s.ok===false?"Not read":s.ok==null?"Not checked":s.warn?"Read, empty":"Read"}</div><div class="d">${esc(s.detail||s.url)}${s.ok?` · ${s.matches} match${s.matches===1?"":"es"}`:""}</div><div>${s.user?`<button class="link" data-act="rmsrc" data-id="${esc(s.id)}">Remove</button>`:""}<a class="link" data-act="openurl" data-url="${esc(s.url)}">Open</a></div></div>`;\n  $("#slist").innerHTML=`<div class="h">${src.length} pages read automatically${bad?` · ${bad} could not be read`:""}</div><div class="tbl">${src.slice().sort((a,b)=>rank(a)-rank(b)).map(row).join("")}</div>\n   <div class="h">Open these yourself</div><div class="tbl">${S.manual.map(s=>`<div class="row"><div>${esc(s.name)}</div><div class="dim">Manual</div><div class="d">${esc(s.note||"")}</div><div><a class="link" data-act="openurl" data-url="${esc(s.url)}">Open</a></div></div>`).join("")}</div>`;\n}\n\n/* ---------- search tools ---------- */\nconst G=q=>"https://www.google.com/search?q="+encodeURIComponent(q);\nconst LI=(k)=>"https://www.linkedin.com/jobs/search/?location=Bangladesh&keywords="+encodeURIComponent(k);\nconst UNIKW=\'(lecturer OR "assistant professor" OR "প্রভাষক" OR "সহকারী অধ্যাপক" OR "লেকচারার") (architecture OR "স্থাপত্য" OR "আর্কিটেকচার")\';\nasync function renderTools(full){\n  if(!TOOLS)TOOLS=await api("/api/tools");\n  if(full){\n    $("#view").innerHTML=`<div class="notice">LinkedIn and some career pages cannot be read by the app. These buttons open ready-made searches in your browser.</div>\n    <div class="bar"><input type="search" id="tq" placeholder="Filter universities and banks"></div><div id="tl"></div>`;\n  }\n  const q=($("#tq")?.value||"").toLowerCase();\n  const a=(u,l)=>`<a class="link" data-act="openurl" data-url="${esc(u)}">${l}</a>`;\n  const li=[["Architect, Bangladesh","Architect"],["Lecturer, architecture","Lecturer Architecture"],["Bank architect or interior","Architect bank"],["BRAC, UN, USAID architect","Architect BRAC OR UNDP OR USAID"],["Architectural engineer","Architectural engineer"]];\n  const un=TOOLS.universities.filter(u=>!q||(u.name+u.short).toLowerCase().includes(q));\n  const bk=TOOLS.banks.filter(b=>!q||(b.name+b.short).toLowerCase().includes(q));\n  $("#tl").innerHTML=`<div class="h">LinkedIn</div><div class="tbl tools-l">${li.map(([l,k])=>`<div class="row"><div>${l}</div><div></div><div></div><div>${a(LI(k),"Open")}</div></div>`).join("")}</div>\n   <div class="h">Universities (${un.length})</div><div class="tbl tools-l">${un.map(u=>`<div class="row"><div>${esc(u.name)}</div><div>${a(G((u.domain?`site:${u.domain} `:esc(u.short)+" ")+UNIKW),"Lecturer posts")}</div><div>${a(G(u.name+" career circular recruitment notice"),"Career")}</div><div>${u.domain?a("https://"+u.domain+"/","Website"):""}</div></div>`).join("")}</div>\n   <div class="h">Banks (${bk.length})</div><div class="tbl tools-l">${bk.map(b=>`<div class="row"><div>${esc(b.name)}</div><div>${a(G(\'"\'+b.short+\'" career circular (architect OR "civil engineer" OR "interior" OR "স্থপতি")\'),"Search")}</div><div>${a(LI(b.short+" architect"),"LinkedIn")}</div><div>${a(G(b.name+" career notice"),"Career")}</div></div>`).join("")}</div>\n   <div class="h">Portals</div><div class="tbl tools-l">${S.manual.map(s=>`<div class="row"><div>${esc(s.name)}</div><div class="d" style="grid-column:2/4">${esc(s.note||"")}</div><div>${a(s.url,"Open")}</div></div>`).join("")}</div>`;\n}\n\n/* ---------- report / activity ---------- */\nasync function renderReport(){\n  const j=await api("/api/report");\n  $("#view").innerHTML=`<div class="bar"><button class="btn quiet sm" data-act="openrep">Open in Notepad</button><button class="btn quiet sm" data-act="openfolder">Open folder</button></div>`+(j.text?`<pre class="rep">${esc(j.text)}</pre>`:`<div class="empty">The report appears after the first check.</div>`);\n}\nfunction renderActivity(){\n  const pr=S.problems||[];\n  $("#view").innerHTML=(S.cfg_error?`<div class="notice">${esc(S.cfg_error)}</div>`:"")+(S.error?`<div class="notice urgent">${esc(S.error)}</div>`:"")+\n   (pr.length?`<div class="h">Problems in the last check</div>${pr.map(p=>`<div class="notice">${esc(p)}</div>`).join("")}`:"")+\n   `<div class="h">Log</div><div class="log">${S.log.map(l=>`<div>${esc(l)}</div>`).join("")||\'<div>Nothing yet.</div>\'}</div><div class="bar" style="margin-top:20px"><button class="btn quiet sm" data-act="forget">Clear results</button></div>`;\n}\n\n/* ---------- drawer ---------- */\nfunction openDrawer(url){\n  const r=S.rows.find(x=>x.url===url);if(!r)return api("/api/openurl",{url}).catch(()=>{});\n  const d=$("#drawer");\n  d.innerHTML=`<h2>${esc(r.title)}</h2><dl>\n   <dt>Organisation</dt><dd>${esc(r.org||"Not detected")}</dd><dt>Type</dt><dd>${esc(r.types.join(", "))}${r.conf!=="high"?` <span class="dim">· ${r.conf==="low"?"possible match":"please check the circular"}</span>`:""}</dd>\n   <dt>Deadline</dt><dd class="${r.days_left!=null&&r.days_left<=S.soon_days?"urgent":""}">${r.deadline?fmtD(r.deadline)+" · "+left(r):"Not found, check the circular"}</dd>\n   <dt>Found</dt><dd>${fmtD(r.added)}</dd>${r.published?`<dt>Posted</dt><dd>${fmtD(r.published)}</dd>`:""}<dt>Source</dt><dd>${esc(r.source)}</dd></dl>\n   ${r.snippet?`<div class="snip">${esc(r.snippet)}</div>`:""}\n   <div class="acts"><button class="btn sm" data-act="openurl" data-url="${esc(r.url)}">Open circular</button>\n   ${r.applied?`<button class="btn quiet sm" data-act="toapplied">See in Applied</button>`:`<button class="btn quiet sm" data-act="apply" data-url="${esc(r.url)}">Mark as applied</button>`}\n   <button class="btn quiet sm" data-act="mark" data-url="${esc(r.url)}" data-m="${r.mark==="saved"?"":"saved"}">${r.mark==="saved"?"Unsave":"Save"}</button>\n   <button class="btn quiet sm" data-act="mark" data-url="${esc(r.url)}" data-m="${r.mark==="hidden"?"":"hidden"}">${r.mark==="hidden"?"Unhide":"Hide"}</button></div>`;\n  d.classList.add("on");$("#scrim").classList.add("on");\n}\nfunction closeDrawer(){$("#drawer").classList.remove("on");$("#scrim").classList.remove("on")}\n\n/* ---------- render dispatch ---------- */\nasync function renderView(full){\n  if(!S)return;\n  const keepScroll=full?0:$("#view").scrollTop;\n  switch(view){\n    case"latest":renderLatest();break;\n    case"jobs":renderJobs(full);break;\n    case"saved":renderSaved();break;\n    case"applied":if(full)renderApplied(true);break;   // never rebuilt by polling: you may be typing\n    case"sources":renderSources(full);break;\n    case"tools":if(full)await renderTools(true);break;\n    case"report":if(full)await renderReport();break;\n    case"activity":renderActivity();break;\n  }\n  if(!full)$("#view").scrollTop=keepScroll;\n}\nasync function refresh(force){\n  try{\n    S=await api("/api/state");\n  }catch(e){return}\n  const sig=JSON.stringify([S.running,S.status,S.last_run,S.rows.length,S.rows.map(r=>r.mark+r.applied+r.new).join(""),S.applied_count,S.sources.length,S.log.length,S.theme]);\n  chrome();renderNav();\n  if(force||sig!==lastSig){\n    lastSig=sig;\n    if(view==="jobs"&&document.activeElement&&document.activeElement.id==="fq"){renderJobs(false)}\n    else if(!(view==="sources"&&document.activeElement&&/^s[ngu]$/.test(document.activeElement.id)))renderView(!!force);\n  }\n}\n\n/* ---------- events ---------- */\ndocument.addEventListener("click",async e=>{\n  const t=e.target.closest("[data-view],[data-open],[data-act],[data-go],#scrim,#check,#addApp,#sadd,#openFolder,#themes button");\n  if(!t)return;\n  try{\n  if(t.dataset.view)return go(t.dataset.view);\n  if(t.dataset.go)return go(t.dataset.go);\n  if(t.id==="scrim")return closeDrawer();\n  if(t.id==="check"){await api("/api/check",{});return refresh(true)}\n  if(t.id==="openFolder")return api("/api/open",{what:"folder"});\n  if(t.parentElement&&t.parentElement.id==="themes")return setTheme(t.dataset.theme,true);\n  if(t.id==="addApp"){const j=await api("/api/app/add",{fields:{}});APPS.unshift(j.app);S.applied_count++;AF={q:"",stage:"all"};renderApplied(true);renderNav();document.querySelector(".card [data-k=title]")?.focus();return}\n  if(t.id==="sadd"){await api("/api/source/add",{name:$("#sn").value,url:$("#su").value,group:$("#sg").value});toast("Page added. It is read at the next check.");await refresh(true);return}\n  if(t.dataset.open)return openDrawer(t.dataset.open);\n  const a=t.dataset.act;\n  if(a==="openurl")return api("/api/openurl",{url:t.dataset.url});\n  if(a==="apply"){await api("/api/app/add",{from_url:t.dataset.url});toast("Added to Applied");closeDrawer();appsLoaded=false;return refresh(true)}\n  if(a==="toapplied"){closeDrawer();return go("applied")}\n  if(a==="mark"){await api("/api/mark",{url:t.dataset.url,mark:t.dataset.m});await refresh(true);return openDrawer(t.dataset.url)}\n  if(a==="rmsrc"){await api("/api/source/remove",{id:t.dataset.id});return refresh(true)}\n  if(a==="openrep")return api("/api/open",{what:"report"});\n  if(a==="openfolder")return api("/api/open",{what:"folder"});\n  if(a==="forget"){if(!t.dataset.sure){t.dataset.sure=1;t.textContent="Click again to confirm";return}await api("/api/forget",{});toast("Results cleared");return refresh(true)}\n  const card=t.closest(".card");\n  if(a==="showpw"){const i=card.querySelector("[data-k=password]");i.type=i.type==="password"?"text":"password";t.textContent=i.type==="password"?"Show":"Hide";return}\n  if(a==="copypw"){const v=card.querySelector("[data-k=password]").value;try{await navigator.clipboard.writeText(v);toast("Copied")}catch(_){toast("Could not copy")}return}\n  if(a==="del"){if(!t.dataset.sure){t.dataset.sure=1;t.textContent="Click again to delete";return}\n    delete pending[card.dataset.id];await api("/api/app/delete",{id:card.dataset.id});APPS=APPS.filter(x=>x.id!==card.dataset.id);S.applied_count=APPS.length;renderApplied(false);renderNav();return}\n  }catch(err){toast(err.message)}\n});\ndocument.addEventListener("input",e=>{\n  const t=e.target;\n  if(t.id==="fq"){F.q=t.value;store.set("filters",F);return renderJobs(false)}\n  if(t.id==="aq"){AF.q=t.value;return renderApplied(false)}\n  if(t.id==="tq")return renderTools(false);\n  if(t.id==="glass")return setGlass(t.value);\n  const card=t.closest(".card");if(card&&t.dataset.k)scheduleSave(card,t);\n});\ndocument.addEventListener("change",e=>{\n  const t=e.target,m={fsort:"sort",ftype:"type",forg:"org",fdl:"dl",fstatus:"status"};\n  if(m[t.id]){F[m[t.id]]=t.value;store.set("filters",F);return renderJobs(false)}\n  if(t.id==="astage"){AF.stage=t.value;return renderApplied(false)}\n  if(t.id==="onstart")return api("/api/setting",{check_on_start:t.checked}).catch(()=>{});\n});\ndocument.addEventListener("keydown",e=>{if(e.key==="Escape")closeDrawer()});\nwindow.addEventListener("beforeunload",flushSaves);\n\n/* ---------- start ---------- */\n(async function(){\n  const had=store.get("theme",null);setTheme(had||"frost",false);const gv=store.get("glass",42);$("#glass").value=gv;setGlass(gv);\n  await refresh(true);\n  if(!had&&S&&S.theme)setTheme(S.theme,false);\n  go(view);\n  (function tick(){setTimeout(async()=>{await refresh(false);tick()},S&&S.running?1500:5000)})();\n})();\n</script>\n</body>\n</html>\n'


# =================================================================== app ====
# Everything above this line is the checker. Below: a tiny local server (only
# reachable from this computer) and the code that opens it in its own window.

import secrets
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_TITLE = "Job Monitor"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
MARKS_PATH = BASE_DIR / "marks.json"
APPS_PATH = BASE_DIR / "applications.json"
USER_SOURCES_PATH = BASE_DIR / "user_sources.json"
REPORT_PATH = BASE_DIR / "latest_report.md"
WATCHED = "Watched pages"
VALID_MARKS = ("saved", "hidden", "")
THEMES = ("frost", "smoke", "graphite")
STAGES = ["Applied", "Admit card", "Written test", "Viva / interview", "Result", "Not selected"]
APP_FIELDS = ("title", "org", "url", "deadline", "applied_on", "stage", "next_date",
              "app_id", "login", "password", "txn_id", "notes")
TYPE_KEYS = {"Government": "gov", "University": "uni", "Needs a look": "check", "Bank": "bank", "NGO / UN": "ngo"}


def read_json(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_cfg():
    """Return (config, message_or_None). Creates config.json on first run and upgrades an old one."""
    fresh = json.loads(json.dumps(DEFAULT_CONFIG))
    if not CONFIG_PATH.exists():
        write_json(CONFIG_PATH, fresh)
        return fresh, None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return fresh, f"config.json could not be read ({e}). Using built-in settings."
    if not isinstance(cfg, dict):
        return fresh, "config.json is not valid. Using built-in settings."
    if cfg.get("config_version", 1) < fresh["config_version"]:
        try:
            shutil.copy(CONFIG_PATH, CONFIG_PATH.with_name("config.old.json"))
        except OSError:
            pass
        for k in ("check_on_start", "theme", "delay_seconds", "timezone"):
            if k in cfg:
                fresh[k] = cfg[k]
        write_json(CONFIG_PATH, fresh)
        return fresh, ("Settings were upgraded with the university and bank lists and Bengali keywords. "
                       "Your old file was kept as config.old.json.")
    return cfg, None


def save_cfg_key(key, value):
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg[key] = value
        write_json(CONFIG_PATH, cfg)
    except (OSError, json.JSONDecodeError):
        pass


def load_marks():
    return read_json(MARKS_PATH, {})


def save_marks(marks):
    write_json(MARKS_PATH, marks)


def load_apps():
    return [a for a in read_json(APPS_PATH, []) if isinstance(a, dict) and a.get("id")]


def save_apps(apps):
    write_json(APPS_PATH, apps)


def clean_app_fields(body):
    out = {}
    for k in APP_FIELDS:
        if k in body:
            v = body[k]
            if not isinstance(v, str):
                v = "" if v is None else str(v)
            out[k] = v[:5000]
    if "stage" in out and out["stage"] not in STAGES:
        out["stage"] = STAGES[0]
    if "url" in out and out["url"] and not out["url"].lower().startswith(("http://", "https://")):
        out["url"] = ""
    return out


def migrate_marks():
    """Older versions kept 'applied' as a mark; it now lives in applications.json."""
    marks = load_marks()
    old = [u for u, m in marks.items() if m == "applied"]
    if not old:
        return
    state = load_state(STATE_PATH)
    apps = load_apps()
    have = {a.get("url") for a in apps}
    for u in old:
        if u not in have:
            it = state["items"].get(u, {})
            apps.append(new_app({"title": it.get("title", u), "url": u, "org": it.get("org", ""),
                                 "deadline": (it.get("deadlines") or [""])[-1]}))
        del marks[u]
    save_apps(apps)
    save_marks(marks)


def new_app(fields):
    app = {k: "" for k in APP_FIELDS}
    app.update(clean_app_fields(fields))
    app.setdefault("stage", STAGES[0])
    if not app["stage"]:
        app["stage"] = STAGES[0]
    app["id"] = secrets.token_hex(5)
    app["created"] = datetime.now().isoformat(timespec="minutes")
    if not app["applied_on"]:
        app["applied_on"] = date.today().isoformat()
    return app


def open_path(path):
    """Open a folder, or a text file in Notepad, with the system's default tools."""
    path = str(path)
    if sys.platform.startswith("win"):
        if os.path.isdir(path):
            os.startfile(path)  # noqa
        else:
            subprocess.Popen(["notepad", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Core:
    """Holds what the window needs: the running check, its log, and the settings."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log = []
        self.status = ""
        self.error = ""
        self.cfg, self.cfg_error = load_cfg()
        self.last_ping = time.time()
        try:
            migrate_marks()
        except Exception:
            pass

    def add_log(self, msg):
        with self.lock:
            self.log.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")
            del self.log[:-300]
            self.status = msg

    def start_check(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.error = ""
            self.status = "Starting..."
        threading.Thread(target=self._work, daemon=True).start()
        return True

    def _work(self):
        try:
            cfg, err = load_cfg()
            self.cfg = cfg
            self.add_log("Check started")
            run(cfg, base_dir=BASE_DIR, log=self.add_log)
            self.add_log("Check finished")
        except Exception as e:  # keep the app alive whatever happens
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"
            self.add_log("ERROR: " + self.error)
        finally:
            with self.lock:
                self.running = False

    def snapshot(self):
        cfg = self.cfg
        state = load_state(STATE_PATH)
        today = today_in(cfg.get("timezone", "Asia/Dhaka"))
        marks = load_marks()
        apps = load_apps()
        applied_urls = {a.get("url") for a in apps if a.get("url")}
        order = [p["short"] for p in cfg.get("profiles", [])]
        ttl = cfg.get("undated_ttl_days", 45)
        latest = state.get("latest") or {}
        new_set, changed_set = set(latest.get("new", [])), set(latest.get("changed", []))

        def rank(n):
            return order.index(n) if n in order else len(order)

        rows = []
        for url, it, last in get_open_matches(state["items"], today, ttl):
            ms = it["matches"]
            shorts = sorted({m["short"] for m in ms}, key=rank)
            best = min((m.get("confidence", "high") for m in ms), key=lambda c: CONF_RANK.get(c, 9))
            rows.append({
                "kind": "job", "url": url, "title": it["title"], "org": it.get("org", ""),
                "deadline": last.isoformat() if last else None,
                "days_left": (last - today).days if last else None,
                "added": it.get("first_seen", ""), "published": it.get("published", ""),
                "new": url in new_set, "changed": url in changed_set,
                "types": shorts, "type": TYPE_KEYS.get(shorts[0], "gov") if shorts else "gov",
                "conf": best, "source": it.get("source", ""),
                "snippet": next((m["snippet"] for m in ms if m.get("snippet")), ""),
                "mark": marks.get(url, ""), "applied": url in applied_urls,
            })
        for p in state.get("page_hits", []):
            dls = sorted(p.get("deadlines", []))
            rows.append({
                "kind": "page", "url": p["url"], "title": p["name"] + " (watched page)", "org": "",
                "deadline": dls[0] if dls else None,
                "days_left": (date.fromisoformat(dls[0]) - today).days if dls else None,
                "added": p.get("last_changed", ""), "published": "", "new": bool(p.get("changed")),
                "changed": False, "types": [WATCHED], "type": "watch", "conf": "high",
                "source": "Watched page", "snippet": p.get("snippet", ""),
                "mark": marks.get(p["url"], ""), "applied": p["url"] in applied_urls,
            })
        rows.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or ""))
        titles = {u: it.get("title", u) for u, it in state["items"].items()}
        lt = None
        if latest:
            lt = dict(latest)
            lt["new"] = [{"url": u, "title": titles.get(u, u)} for u in latest.get("new", [])]
            lt["changed"] = [{"url": u, "title": titles.get(u, u)} for u in latest.get("changed", [])]
        status = state.get("sources_status", {})
        sources = []
        for s in expand_sources(cfg, BASE_DIR):
            st = status.get(s["id"], {})
            sources.append({"id": s["id"], "name": s["name"], "url": s["url"], "type": s["type"],
                            "group": s.get("group") or "", "note": s.get("note", ""), "user": bool(s.get("user")),
                            "ok": st.get("ok"), "warn": st.get("warn", False), "detail": st.get("detail", ""),
                            "items": st.get("items", 0), "matches": st.get("matches", 0),
                            "checked": st.get("checked", "")})
        with self.lock:
            running, st_msg, error, log = self.running, self.status, self.error, list(self.log[-120:])
        return {
            "today": today.isoformat(), "soon_days": cfg.get("closing_soon_days", 7),
            "rows": rows, "last_run": state.get("last_run"), "latest": lt, "runs": state.get("runs", []),
            "problems": state.get("problems", []), "running": running, "status": st_msg,
            "error": error, "log": log, "cfg_error": self.cfg_error,
            "check_on_start": cfg.get("check_on_start", True), "theme": cfg.get("theme", "frost"),
            "sources": sources, "stages": STAGES, "applied_count": len(apps),
            "manual": [s for s in sources if s["type"] == "manual"],
        }

    def tools(self):
        cfg = self.cfg
        return {"universities": [{"name": u["name"], "short": u.get("short", u["name"]), "domain": u.get("domain", "")}
                                 for u in cfg.get("universities", [])],
                "banks": [{"name": b["name"], "short": b.get("short", b["name"])} for b in cfg.get("banks", [])]}


class Handler(BaseHTTPRequestHandler):
    server_version = "JobMonitor"

    def log_message(self, *args):  # no console output
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _host_ok(self):
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

    def _authed(self):
        return secrets.compare_digest(self.headers.get("X-Token") or "", self.server.token)

    def do_GET(self):
        try:
            if not self._host_ok():
                return self._send(403, "forbidden", "text/plain")
            path = self.path.split("?")[0]
            if path == "/":
                return self._send(200, UI_HTML.replace("__TOKEN__", self.server.token), "text/html; charset=utf-8")
            if not path.startswith("/api/") or not self._authed():
                return self._send(403, "forbidden", "text/plain")
            core = self.server.core
            core.last_ping = time.time()
            if path == "/api/state":
                return self._json(core.snapshot())
            if path == "/api/report":
                try:
                    text = REPORT_PATH.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                return self._json({"text": text})
            if path == "/api/apps":
                return self._json({"apps": load_apps(), "stages": STAGES})
            if path == "/api/tools":
                return self._json(core.tools())
            if path == "/api/ping":
                return self._json({"ok": True})
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            if not self._host_ok() or not self._authed():
                return self._send(403, "forbidden", "text/plain")
            core = self.server.core
            core.last_ping = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            path = self.path.split("?")[0]
            if path == "/api/check":
                return self._json({"started": core.start_check()})
            if path == "/api/mark":
                url, mark = body.get("url"), body.get("mark", "")
                if not isinstance(url, str) or mark not in VALID_MARKS:
                    return self._json({"error": "bad request"}, 400)
                marks = load_marks()
                if mark:
                    marks[url] = mark
                else:
                    marks.pop(url, None)
                save_marks(marks)
                return self._json({"ok": True})
            if path == "/api/forget":
                if core.running:
                    return self._json({"error": "a check is running"}, 409)
                STATE_PATH.unlink(missing_ok=True)
                return self._json({"ok": True})
            if path == "/api/setting":
                if "check_on_start" in body:
                    save_cfg_key("check_on_start", bool(body["check_on_start"]))
                    core.cfg["check_on_start"] = bool(body["check_on_start"])
                if "theme" in body:
                    if body["theme"] not in THEMES:
                        return self._json({"error": "bad request"}, 400)
                    save_cfg_key("theme", body["theme"])
                    core.cfg["theme"] = body["theme"]
                return self._json({"ok": True})
            if path == "/api/app/add":
                fields = dict(body.get("fields") or {})
                src = body.get("from_url")
                if isinstance(src, str) and src:
                    state = load_state(STATE_PATH)
                    it = state["items"].get(src)
                    if it:
                        dls = it.get("deadlines") or []
                        fields = {"title": it.get("title", ""), "org": it.get("org", ""), "url": src,
                                  "deadline": dls[-1] if dls else "", **fields}
                    else:
                        fields.setdefault("url", src)
                    if any(a.get("url") == src for a in load_apps()):
                        return self._json({"error": "already in Applied"}, 409)
                if not fields.get("title") and not fields.get("url"):
                    fields["title"] = "Untitled application"
                app = new_app(fields)
                apps = load_apps()
                apps.insert(0, app)
                save_apps(apps)
                return self._json({"ok": True, "app": app})
            if path == "/api/app/save":
                aid = body.get("id")
                apps = load_apps()
                for a in apps:
                    if a["id"] == aid:
                        a.update(clean_app_fields(body.get("fields") or {}))
                        save_apps(apps)
                        return self._json({"ok": True})
                return self._json({"error": "not found"}, 404)
            if path == "/api/app/delete":
                apps = load_apps()
                keep = [a for a in apps if a["id"] != body.get("id")]
                if len(keep) == len(apps):
                    return self._json({"error": "not found"}, 404)
                save_apps(keep)
                return self._json({"ok": True})
            if path == "/api/source/add":
                name, url = str(body.get("name") or "").strip()[:80], str(body.get("url") or "").strip()
                group = body.get("group") if body.get("group") in ("uni", "gov", "bank", "ngo") else "uni"
                if not name or not url.lower().startswith(("http://", "https://")):
                    return self._json({"error": "Enter a name and a web address starting with http:// or https://"}, 400)
                existing = read_json(USER_SOURCES_PATH, [])
                if any(isinstance(e, dict) and e.get("url") == url for e in existing):
                    return self._json({"error": "That page is already in the list"}, 409)
                existing.append({"name": name, "url": url, "group": group, "type": "html_notices"})
                write_json(USER_SOURCES_PATH, existing)
                return self._json({"ok": True})
            if path == "/api/source/remove":
                sid = body.get("id")
                users = load_user_sources(BASE_DIR)
                keep = [u for u in users if u["id"] != sid]
                if len(keep) == len(users):
                    return self._json({"error": "not found"}, 404)
                write_json(USER_SOURCES_PATH, [{k: v for k, v in u.items() if k not in ("user", "id")} for u in keep])
                return self._json({"ok": True})
            if path == "/api/open":
                what = body.get("what")
                if what == "folder":
                    open_path(BASE_DIR)
                elif what == "config":
                    open_path(CONFIG_PATH)
                elif what == "report":
                    open_path(REPORT_PATH)
                else:
                    return self._json({"error": "bad request"}, 400)
                return self._json({"ok": True})
            if path == "/api/openurl":
                url = body.get("url")
                if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
                    return self._json({"error": "bad request"}, 400)
                webbrowser.open(url)  # opens in your normal browser, with your sign-ins
                return self._json({"ok": True})
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)}, 500)


def start_server(core):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.core = core
    srv.token = secrets.token_urlsafe(18)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def find_app_browser():
    """Edge or Chrome, which can show a page as a chromeless app window."""
    cands = []
    if sys.platform.startswith("win"):
        for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(key)
            if root:
                cands += [os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        cands = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        cands = [shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def open_window(url):
    """Open the app in its own window. Returns the process, or None when we had to
    fall back to a normal browser tab."""
    exe = find_app_browser()
    if exe:
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
        profile = Path(root) / "JobMonitor" / "window"
        try:
            profile.mkdir(parents=True, exist_ok=True)
            return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1320,800",
                                     "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])
        except OSError:
            pass
    webbrowser.open(url)
    return None


def main_app():
    core = Core()
    srv, url = start_server(core)
    if core.cfg.get("check_on_start", True):
        core.start_check()
    proc = open_window(url)
    t0 = time.time()
    handoff = False
    while True:
        time.sleep(1)
        idle = time.time() - core.last_ping
        if proc is None or handoff:
            if idle > 900:  # normal browser tab: stop 15 minutes after the tab was closed
                break
            continue
        if proc.poll() is not None:
            if time.time() - t0 < 6:  # the browser handed the window to another process
                handoff = True
                continue
            break  # the window was closed
    for _ in range(90):  # let a running check finish saving
        if not core.running:
            break
        time.sleep(1)
    srv.shutdown()


if __name__ == "__main__":
    main_app()
