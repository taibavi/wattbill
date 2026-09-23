#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
עדכון אוטומטי של טבלת התעריפים של WattBill.

הסקריפט רץ ב‑GitHub Actions (לא בדפדפן), ולכן אין לו בעיית CORS מול gov.il.
הוא:
  1. מאתר את ספר לוחות התעריפים העדכני של רשות החשמל,
  2. מוריד אותו וממיר לטקסט (גם עם -layout וגם בלי),
  3. מחלץ את לוח 5.3-1 (תעריף הצריכה והקיבולת לצרכן ביתי)
     ואת לוח 5.4-1 (תשלום קבוע – שירותי חלוקה ואספקה, מונה חד־פאזי ותלת־פאזי),
  4. מוודא שהערכים הגיוניים,
  5. ורק אם הכול עבר – מעדכן את data/tariffs.json.

עקרון מנחה: עדיף להישאר עם ערך ישן ונכון מאשר לפרסם ערך חדש ושגוי.
כל כישלון ולידציה עוצר את העדכון ומחזיר קוד יציאה שונה מאפס, כדי ש‑GitHub
ישלח התראה במקום לדחוף נתון פגום לאפליקציה.

הערה על חילוץ הטקסט: כלי חילוץ שונים מסדרים טבלה עברית אחרת לגמרי – לפעמים
התווית לפני המספרים, לפעמים אחריהם, ולפעמים שורה נחתכת בין עמודים. לכן הפענוח
כאן אינו מסתמך על סדר, אלא על שתי עובדות אריתמטיות שנכונות בכל המהדורות:
  • בלוח 5.3-1 התעריף הביתי והכללי זהים, והם שתי העמודות בקצה אחד של השורה.
  • בלוח 5.4-1 סכום שלושת רכיבי השורה שווה לעמודת הסה״כ של אותה שורה.

שימוש:
    python3 fetch_tariffs.py --data data/tariffs.json
    python3 fetch_tariffs.py --text book.txt --dry-run     # בדיקה מקומית
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

UA = "Mozilla/5.0 (compatible; wattbill-tariff-bot/1.0; +https://github.com/taibavi/wattbill)"
LANDING = "https://www.gov.il/he/pages/tariffbook"

# ----------------------------------------------------------------------------
# טווחי שפיות. ערך מחוץ לטווח = פענוח שגוי, לא תעריף חדש.
# ----------------------------------------------------------------------------
RANGES = {
    "energy":   (0.30, 1.20),   # ₪ לקוט״ש, לפני מע״מ
    "capacity": (1.00, 25.00),  # ₪ לכל KVA לשנה
    "fixedA":   (3.00, 40.00),  # ₪ לחודש
    "fixedB":   (3.00, 40.00),
    "fixedA3":  (3.00, 40.00),
    "fixedB3":  (3.00, 40.00),
}
MAX_JUMP = 0.30   # שינוי של יותר מ‑30% מול הערך הקיים דורש בדיקה אנושית

NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?"
BIDI = re.compile(r"[‎‏‪-‮⁦-⁩﻿]")
DEBUG = []


class Bad(Exception):
    """פענוח שלא ניתן לסמוך עליו."""


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log("FAIL: " + msg)
    dump_debug()
    sys.exit(2)


def dump_debug():
    """בכישלון – מדפיסים חלונות רצופים מהטקסט סביב העוגנים.

    בגרסה קודמת הודפסו רק שורות שהכילו מילת עוגן, ודווקא שורות המספרים –
    שאין בהן אף מילה – לא הופיעו. לכן כאן מדפיסים רצף שורות שלם."""
    if not DEBUG:
        return
    for name, text in DEBUG:
        lines = [l.rstrip() for l in strip_bidi(text).split("\n")]
        log("")
        log("─── אבחון: %s ───" % name)
        spots = []
        for i, l in enumerate(lines):
            if re.search(r'(?:תשלום|רכיב)\s*משתנה', l):
                spots.append(("לוח 5.3-1", i))
                if len(spots) >= 3:
                    break
        for i, l in enumerate(lines):
            if re.search(r'מונה\s*חד\s*-?\s*פאזי', l):
                spots.append(("לוח 5.4-1", i))
                break
        if not spots:
            log("  (לא נמצא אף עוגן מוכר – ככל הנראה חילוץ הטקסט נכשל)")
            continue
        for label, i in spots[:4]:
            log("  ·· %s, סביב שורה %d ··" % (label, i))
            for j in range(max(0, i - 4), min(len(lines), i + 16)):
                if lines[j].strip():
                    log("%6d| %s" % (j, lines[j].strip()[:160]))


# ----------------------------------------------------------------------------
# 1. איתור והורדה
# ----------------------------------------------------------------------------
def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "he,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def candidate_urls(today=None):
    """כתובות אפשריות לספר התעריפים, מהחדש לישן."""
    today = today or dt.date.today()
    editions = []
    y, half = today.year, 7 if today.month >= 7 else 1
    for _ in range(5):                       # המהדורה הנוכחית וארבע אחורה
        editions.append((half, y))
        half, y = (1, y) if half == 7 else (7, y - 1)
    pats = [
        "https://www.gov.il/BlobFolder/generalpage/tarriffbook/he/sefer_tariff_{m:02d}_{y}.pdf",
        "https://www.gov.il/BlobFolder/generalpage/tarriffbook/he/Files_netunei_hasmal_sefer_tariff_{m:02d}_{y}.pdf",
        "https://www.gov.il/BlobFolder/generalpage/tarriffbook/he/sefer_tarrif_{m:02d}_{y}.pdf",
        "https://www.gov.il/BlobFolder/generalpage/maagar_tarrifr_histoty/he/Files_tarrif_sefer_{m:02d}_{y}.pdf",
    ]
    return [p.format(m=m, y=y) for (m, y) in editions for p in pats]


def discover_from_landing():
    """מנסה לשלוף את הקישור לספר ישירות מדף רשות החשמל."""
    try:
        html = http_get(LANDING).decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        log("  דף הנחיתה לא נגיש: %s" % e)
        return []
    urls = re.findall(r'https?://[^\s"\'<>]+?\.pdf', html)
    urls += ["https://www.gov.il" + u for u in
             re.findall(r'"(/BlobFolder/[^"]+?\.pdf)"', html)]
    seen, out = set(), []
    for u in urls:
        if re.search(r"(sefer|tarrif|tariff)", u, re.I) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_book(explicit=None):
    urls = [explicit] if explicit else (discover_from_landing() + candidate_urls())
    for url in urls:
        try:
            blob = http_get(url)
        except Exception as e:                                # noqa: BLE001
            log("  לא זמין: %s (%s)" % (url, type(e).__name__))
            continue
        if not blob.startswith(b"%PDF"):
            log("  לא PDF: %s" % url)
            continue
        log("  נמצא ספר תעריפים: %s (%d KB)" % (url, len(blob) // 1024))
        return url, blob
    fail("לא נמצא אף ספר תעריפים להורדה.")


def pdf_to_texts(blob):
    """מחזיר את הטקסט בשתי צורות. -layout שומר על מבנה הטבלה, והמצב הרגיל
    מפרק אותה – לכל אחד מהם יש מקרים שבהם הוא מצליח והשני לא."""
    out = []
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.pdf")
        with open(p, "wb") as f:
            f.write(blob)
        for name, args in (("layout", ["-layout"]), ("raw", [])):
            try:
                r = subprocess.run(["pdftotext", "-enc", "UTF-8"] + args + [p, "-"],
                                   capture_output=True, timeout=300)
            except FileNotFoundError:
                fail("pdftotext לא מותקן (חבילת poppler-utils).")
            if r.returncode == 0 and r.stdout:
                out.append((name, r.stdout.decode("utf-8", "replace")))
            else:
                log("  pdftotext %s נכשל" % name)
    if not out:
        fail("המרת ה‑PDF לטקסט נכשלה.")
    return out


# ----------------------------------------------------------------------------
# 2. חילוץ
# ----------------------------------------------------------------------------
def strip_bidi(t):
    """pdftotext עוטף כל שורה עברית בתווי כיווניות בלתי נראים, והם שוברים
    כל ביטוי רגולרי שמצפה לרצף טקסט. מסירים אותם לפני כל דבר אחר."""
    return BIDI.sub("", t.replace("\xa0", " "))


def nums(s, k=99):
    return [float(x.replace(",", "")) for x in re.findall(NUM, s)[:k]]


def home_index(vals):
    """איזו עמודה היא הצרכן הביתי.

    בלוח 5.3-1 יש שש עמודות מגזר, והעמודה הביתית והכללית תמיד זהות זו לזו
    ויושבות בקצה אחד. בספרי 2023–2024 זהו הקצה הראשון, ובספר 07/2026 הקצה
    האחרון – ולכן מזהים לפי הזוג השווה ולא לפי מיקום קבוע."""
    first = abs(vals[0] - vals[1]) < 0.011
    last = abs(vals[-1] - vals[-2]) < 0.011
    if first and not last:
        return 0
    if last and not first:
        return -1
    raise Bad("לא ניתן לזהות את העמודה הביתית בלוח 5.3-1 (הערכים: %s)" %
              " ".join("%g" % v for v in vals))


def row_total(vals):
    """הסכום של שורה בלוח 5.4-1: שלושה רכיבים עוקבים שסכומם שווה למספר אחר
    באותה שורה. עמיד לכך שעמודת הסה״כ מופיעה לפני או אחרי הרכיבים."""
    hits = set()
    for i in range(len(vals) - 2):
        s = sum(vals[i:i + 3])
        for j, v in enumerate(vals):
            if (j < i or j >= i + 3) and abs(v - s) <= 0.035:
                hits.add(round(v, 2))
    if len(hits) == 1:
        return hits.pop()
    return None


TOU = re.compile(r'שפל|פסגה|חורף|קיץ|מעבר')


def extract_53(lines):
    """לוח 5.3-1 – תעריף הצריכה והקיבולת לצרכן ביתי.

    התווית עשויה לשבת באותה שורה עם המספרים (לפניהם או אחריהם) או בשורה שמעל,
    תלוי בכלי החילוץ. שורות של לוחות תעו"ז נפסלות לפי מילות העומס שבהן."""
    var = []
    for i in range(len(lines)):
        if len(nums(lines[i])) < 5 or TOU.search(lines[i]):
            continue
        ctx = " ".join(lines[max(0, i - 4):i + 1])
        if "משתנה" in ctx and re.search(r'אגורות|לקווט|לקוט', ctx):
            var.append(i)
    if not var:
        raise Bad("לא נמצאה שורת התשלום המשתנה בלוח 5.3-1.")
    i = var[0]
    ev = nums(lines[i], 6)
    idx = home_index(ev)

    fix = [j for j in range(i + 1, min(i + 6, len(lines)))
           if "KVA" in " ".join(lines[max(0, j - 1):j + 1]) and len(nums(lines[j])) >= 5]
    if not fix:
        raise Bad("לא נמצאה שורת התשלום הקבוע (KVA) מתחת לשורת התשלום המשתנה.")
    cv = nums(lines[fix[0]], 6)
    if len(cv) != len(ev):
        raise Bad("שורת הקיבולת (%d ערכים) אינה תואמת לשורת הצריכה (%d)." % (len(cv), len(ev)))
    return {"energy": round(ev[idx] / 100.0, 6), "capacity": cv[idx]}, " ".join(lines[max(0, i - 10):i])


def regions_54(lines):
    """כל המועמדים לגבולות לוח 5.4-1, לפי סדר הופעתם.

    ההופעה הראשונה של הכותרת היא כמעט תמיד בתוכן העניינים ולא בלוח עצמו,
    ולכן מחזירים את כל המועמדים והקורא בוחר את זה שיש בו שורות חיוב אמיתיות."""
    out = []
    for i, l in enumerate(lines):
        if not (("5.4" in l and "תשלום קבוע" in l and "5.4.1" not in l)
                or ("צרכנות" in l and "חלוקה" in l)):
            continue
        end = len(lines)
        for j in range(i + 3, len(lines)):
            if "5.4.1" in lines[j] or re.search(r'2\s*-\s*5\.4|5\.4\s*[–-]\s*:?\s*2', lines[j]):
                end = j
                break
        out.append((i, end))
    if not out:
        raise Bad("לא נמצאה תחילת לוח 5.4-1.")
    return out


def phase_hits(lines, lo, hi):
    """מיקומי התיאורים של מונה חד־פאזי ותלת־פאזי, בלי שורות תשלום מראש."""
    out = {"": [], "3": []}
    for suffix, phase in (("", "חד"), ("3", "תלת")):
        pat = re.compile(r'מונה\s*' + phase + r'\s*-?\s*פאזי')
        for i in range(lo, hi):
            ctx = " ".join(lines[i:i + 2])       # התיאור עלול להישבר בין שורות
            if not pat.search(ctx):
                continue
            around = " ".join(lines[max(lo, i - 2):i + 2])
            if "תשלום מראש" in around or "זיכוי" in around:
                continue
            if out[suffix] and i - out[suffix][-1] <= 1:
                continue                          # אותה הופעה שנפרסה לשתי שורות
            out[suffix].append(i)
    return out


def extract_54(lines):
    """לוח 5.4-1 – תשלום קבוע לשירותי צרכנות, מונה חד־פאזי ותלת־פאזי.

    התיאור של שורה יכול לשבת כמה שורות מעל או מתחת למספרים שלה (ובספר 07/2026
    אפילו בעמוד הבא), ולכן משייכים כל תיאור לשורת המספרים הקרובה אליו."""
    last = None
    for lo, hi in regions_54(lines):
        rows = []
        for i in range(lo, hi):
            v = nums(lines[i], 8)
            if len(v) >= 4:
                t = row_total(v)
                if t is not None:
                    rows.append((i, t))
        if len(rows) < 4:
            last = Bad("בלוח 5.4-1 נמצאו רק %d שורות חיוב תקינות." % len(rows))
            continue                        # ככל הנראה תוכן העניינים, לא הלוח
        try:
            return rows_to_values(lines, lo, hi, rows)
        except Bad as e:
            last = e
    raise last


def rows_to_values(lines, lo, hi, rows):
    hits = phase_hits(lines, lo, hi)
    out = {}
    for suffix in ("", "3"):
        if len(hits[suffix]) < 2:
            raise Bad("נמצאו רק %d שורות «מונה %s־פאזי» בלוח 5.4-1 (דרושות שתיים: חלוקה ואספקה)."
                      % (len(hits[suffix]), "חד" if suffix == "" else "תלת"))
        for key, pos in (("A", hits[suffix][0]), ("B", hits[suffix][1])):
            out["fixed" + key + suffix] = min(rows, key=lambda r: abs(r[0] - pos))[1]

    for key in ("A", "B"):
        if abs(out["fixed" + key] - out["fixed" + key + "3"]) < 1e-9:
            raise Bad("בפרק %s התעריף החד־פאזי והתלת־פאזי יצאו זהים (%s) – "
                      "ככל הנראה זוהתה אותה שורה פעמיים." % (key, out["fixed" + key]))
    if abs(out["fixedA"] - out["fixedB"]) < 1e-9:
        raise Bad("תעריפי החלוקה והאספקה יצאו זהים – ככל הנראה נקראה אותה טבלה פעמיים.")
    return out


# --- פענוח חלופי: כשהטקסט מפורק כך שכל תא בשורה נפרדת, אין שורות טבלה
#     לעבוד איתן, ולכן מאחדים הכול לרצף אחד וחותכים לפי מספור השורות בלוח.
ROW_RE = re.compile(r'חודשיים\s+((?:' + NUM + r'\s+){3,}' + NUM + ')')


def extract_53_blob(t):
    m = re.search(r'(?:תשלום|רכיב) משתנה[:\s]*אגורות\s*לק[\u05d5\u05d8\u05e9"\u05f4]{2,5}\s*((?:'
                  + NUM + r'\s+){4,}' + NUM + ')', t)
    if not m:
        raise Bad("לא נמצאה שורת התשלום המשתנה בלוח 5.3-1.")
    ev = nums(m.group(1), 6)
    idx = home_index(ev)
    mf = re.search(r'KVA\s*לשנה\s*((?:' + NUM + r'\s+){4,}' + NUM + ')', t[m.start():])
    if not mf:
        raise Bad("לא נמצאה שורת התשלום הקבוע (KVA) בלוח 5.3-1.")
    cv = nums(mf.group(1), 6)
    if len(cv) != len(ev):
        raise Bad("שורת הקיבולת אינה תואמת לשורת הצריכה.")
    return {"energy": round(ev[idx] / 100.0, 6), "capacity": cv[idx]}, t[max(0, m.start() - 350):m.start()]


def extract_54_blob(t):
    out = {}
    secs = [("A", "צרכנות חלוקה"), ("B", "צרכנות אספקה")]
    for i, (key, anchor) in enumerate(secs):
        si = t.find(anchor)
        if si < 0:
            raise Bad("לא נמצא הפרק «%s» בלוח 5.4-1." % anchor)
        stop = t.find(secs[1][1], si + 1) if i == 0 else len(t)
        block = t[si: stop if stop > si else len(t)]
        starts = [m.start() for m in re.finditer(r'(?<!\d)\d{1,2}\s+תעריף אחיד', block)]
        if len(starts) < 2:
            raise Bad("בפרק «%s» לא זוהו שורות התעריף האחיד." % anchor)
        starts.append(len(block))
        for suffix, phase in (("", "חד"), ("3", "תלת")):
            pat = re.compile(r'מונה\s*' + phase + r'\s*-?\s*פאזי')
            seg = next((block[a:b] for a, b in zip(starts, starts[1:])
                        if pat.search(block[a:b]) and "תשלום מראש" not in block[a:b]), None)
            if seg is None:
                raise Bad("לא נמצאה שורת «מונה %s־פאזי» בפרק «%s»." % (phase, anchor))
            m = ROW_RE.search(seg)
            val = row_total(nums(m.group(1), 6)) if m else None
            if val is None:
                raise Bad("לא ניתן לאמת את הסכום בשורת «מונה %s־פאזי» בפרק «%s»." % (phase, anchor))
            out["fixed" + key + suffix] = val
        if abs(out["fixed" + key] - out["fixed" + key + "3"]) < 1e-9:
            raise Bad("בפרק %s החד־פאזי והתלת־פאזי יצאו זהים." % key)
    return out


def parse_lines(text):
    lines = [l.strip() for l in strip_bidi(text).split("\n")]
    v, head = extract_53(lines)
    v.update(extract_54(lines))
    return v, head


def parse_blob(text):
    t = re.sub(r"\s+", " ", strip_bidi(text))
    v, head = extract_53_blob(t)
    v.update(extract_54_blob(t))
    return v, head



# ----------------------------------------------------------------------------
# פענוח "חופשי": בלי שום הנחה על מבנה שורות.
#
# כל כלי חילוץ מפרק את הטבלאות אחרת – לפעמים תא בכל שורה, לפעמים שורה שלמה,
# ולפעמים התווית נפרדת מהמספרים בשתי שורות. השיטה הזו מתעלמת לגמרי משורות
# ומחפשת את המספרים לפי שתי זהויות אריתמטיות בלבד, ולפי הקרבה לתווית בטקסט.
# ----------------------------------------------------------------------------
NUM_RE = re.compile(NUM)
GAP = 16          # כמה תווים שאינם ספרה מותר שיפרידו בין שני מספרים באותה שורה
SPAN_53 = 260     # אורך מרבי של שורת ערכים בלוח 5.3-1
SPAN_54 = 150     # אורך מרבי של שורת ערכים בלוח 5.4-1


def numbers_with_pos(t):
    return [(m.start(), m.end(), float(m.group().replace(",", ""))) for m in NUM_RE.finditer(t)]


def runs(items, minlen, maxgap, maxspan):
    """רצפים של מספרים שקרובים זה לזה בטקסט – כלומר שורת טבלה אחת."""
    out, cur = [], []
    for it in items:
        if cur and (it[0] - cur[-1][1]) > maxgap:
            if len(cur) >= minlen:
                out.append(cur)
            cur = []
        cur.append(it)
        if len(cur) >= 2 and (cur[-1][1] - cur[0][0]) > maxspan:
            if len(cur) >= minlen:
                out.append(cur)
            cur = cur[-1:]
    if len(cur) >= minlen:
        out.append(cur)
    return out


def parse_free(text):
    t = strip_bidi(text)
    allnums = numbers_with_pos(t)

    # ---- לוח 5.3-1 ----
    found = None
    for m in re.finditer(r'(?:תשלום|רכיב)\s*משתנה', t):
        if TOU.search(t[m.start():m.start() + 200]):
            continue                       # לוח תעו"ז, לא תעריף אחיד
        head = t[max(0, m.start() - 900):m.start() + 120]
        # המספרים עשויים לשבת לפני התווית או אחריה, תלוי בכלי החילוץ
        seg = [n for n in allnums if m.start() - SPAN_53 <= n[0] < m.start() + 700]
        for run in runs(seg, 5, GAP, SPAN_53):
            vals = [v for _, _, v in run][:6]
            try:
                idx = home_index(vals)
            except Bad:
                continue
            energy = vals[idx]
            if not (20 <= energy <= 130):  # אגורות לקווט"ש
                continue
            # שורת הקיבולת תמיד באה מיד אחרי שורת הצריכה. חיפוש גם אחורה היה
            # תופס את שורת הקיבולת של לוח התעו"ז שמעליה.
            near = [n for n in allnums if run[-1][1] < n[0] <= run[-1][1] + 500]
            kva = None
            for r2 in runs(near, 5, GAP, SPAN_53):
                lo2, hi2 = min(r2[0][0], run[0][0]), max(r2[-1][1], run[-1][1])
                if "KVA" not in t[lo2:hi2 + 60]:
                    continue
                cv = [v for _, _, v in r2][:6]
                if len(cv) == len(vals) and 0.5 <= cv[idx] <= 40:
                    kva = cv[idx]
                    break
            if kva is None:
                continue
            found = ({"energy": round(energy / 100.0, 6), "capacity": kva}, head)
            break
        if found:
            break
    if not found:
        raise Bad("לא נמצאה שורת התשלום המשתנה בלוח 5.3-1.")
    out, head = found

    # ---- לוח 5.4-1 ----
    # בלי לאתר את גבולות הלוח: מאתרים ישירות את תיאורי השורות, ולכל תיאור
    # מחפשים שורת מספרים תקינה בקרבתו. כך תוכן העניינים וכותרות עמוד לא מפריעים.
    rows = []                              # (מיקום, סכום השורה)
    for i in range(len(allnums) - 2):
        a, b, c = allnums[i], allnums[i + 1], allnums[i + 2]
        if c[1] - a[0] > SPAN_54 or (b[0] - a[1]) > GAP or (c[0] - b[1]) > GAP:
            continue
        total = a[2] + b[2] + c[2]
        if not (3.0 <= total <= 40.0):
            continue
        for j in range(max(0, i - 3), min(len(allnums), i + 7)):
            if i <= j <= i + 2 or abs(allnums[j][0] - a[0]) > SPAN_54:
                continue
            if abs(allnums[j][2] - total) <= 0.035:
                rows.append((a[0], round(allnums[j][2], 2)))
                break

    # כל תיאורי השורות, כולל "תשלום מראש". שורות התשלום מראש חוזרות על אותם
    # ערכים, ובספר 07/2026 שורה נחתכת בין עמודים כך שהמספרים שלה רחוקים
    # מהתיאור – ואז השורה הקרובה ביותר עלולה להיות דווקא של תשלום מראש.
    # לכן מסמנים אותן, וכל שורת מספרים ש"שייכת" לתיאור תשלום מראש נפסלת.
    descs = []                             # (מיקום, פאזה, האם תשלום מראש)
    for suffix, phase in (("", "חד"), ("3", "תלת")):
        for m in re.finditer(r'מונה\s*(?:תשלום\s*מראש\s*)?' + phase + r'\s*-?\s*פאזי', t):
            pos = m.start()
            if "זיכוי" in t[max(0, pos - 80):pos]:
                continue
            pre = "תשלום" in m.group() or "תשלום מראש" in t[max(0, pos - 80):pos + 10]
            descs.append((pos, suffix, pre))
    descs.sort()
    if not descs:
        raise Bad("לא נמצא אף תיאור של שורת מונה בלוח 5.4-1.")

    def owner(rowpos):
        return min(descs, key=lambda d: abs(d[0] - rowpos))

    usable = [r for r in rows if not owner(r[0])[2]]
    if len(usable) < 4:
        raise Bad("בלוח 5.4-1 נמצאו רק %d שורות חיוב שאינן תשלום מראש." % len(usable))

    hits = {"": [], "3": []}
    for pos, suffix, pre in descs:
        if pre:
            continue
        if hits[suffix] and pos - hits[suffix][-1] < 40:
            continue
        if not [r for r in usable if abs(r[0] - pos) <= 700]:
            continue
        hits[suffix].append(pos)
    for suffix, phase in (("", "חד"), ("3", "תלת")):
        if len(hits[suffix]) < 2:
            raise Bad("נמצאו רק %d שורות «מונה %s־פאזי» עם ערכים תקינים בלוח 5.4-1 (דרושות שתיים)."
                      % (len(hits[suffix]), phase))

    # סדר השורות בספר קבוע: חלוקה חד, חלוקה תלת, אספקה חד, אספקה תלת
    order = [hits[""][0], hits["3"][0], hits[""][1], hits["3"][1]]
    if order != sorted(order):
        raise Bad("סדר שורות לוח 5.4-1 אינו כמצופה (חלוקה לפני אספקה, חד־פאזי לפני תלת־פאזי).")

    for suffix in ("", "3"):
        for key, pos in (("A", hits[suffix][0]), ("B", hits[suffix][1])):
            out["fixed" + key + suffix] = min(usable, key=lambda r: abs(r[0] - pos))[1]

    for key in ("A", "B"):
        if abs(out["fixed" + key] - out["fixed" + key + "3"]) < 1e-9:
            raise Bad("בפרק %s החד־פאזי והתלת־פאזי יצאו זהים (%s)." % (key, out["fixed" + key]))
    if abs(out["fixedA"] - out["fixedB"]) < 1e-9:
        raise Bad("תעריפי החלוקה והאספקה יצאו זהים – ככל הנראה נקראה אותה טבלה פעמיים.")
    return out, head


PARSERS = [("שורות", parse_lines), ("רצף", parse_blob), ("חופשי", parse_free)]


def extract_all(texts):
    """מריצים כל שיטת פענוח על כל צורת טקסט. מספיק שאחת מצליחה, אבל אם שתיים
    מצליחות והן לא מסכימות – עוצרים, כי אז אי אפשר לדעת מי צודקת."""
    results, errors = [], []
    for tname, text in texts:
        DEBUG.append((tname, text))
        for pname, fn in PARSERS:
            try:
                v, head = fn(text)
                results.append(("%s/%s" % (tname, pname), v, head))
                log("  ✓ פוענח מ‑%s בשיטת %s" % (tname, pname))
            except Bad as e:
                errors.append("%s/%s: %s" % (tname, pname, e))
            except Exception as e:                            # noqa: BLE001
                errors.append("%s/%s: %s" % (tname, pname, e))
    if not results:
        fail("הפענוח נכשל בכל השיטות. " + " | ".join(errors[:6]))
    base = results[0][1]
    for name, v, _ in results[1:]:
        for k in base:
            if k in v and abs(v[k] - base[k]) > 1e-9:
                fail("שתי שיטות פענוח לא הסכימו על «%s» (%s מול %s, לפי %s)."
                     % (k, base[k], v[k], name))
    return base, results[0][2]


def effective_date(head, today=None):
    """תאריך התחילה של המהדורה, מתוך כותרת לוח 5.3-1.

    הכותרת מכילה גם את תאריך ההחלטה וגם את תאריך העדכון האחרון; המאוחר
    מביניהם הוא תאריך התחילה (למשל 21/12/2022 מול 01/01/2023)."""
    today = today or dt.date.today()
    cands = []
    for d, mo, y in re.findall(r'\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b', head):
        try:
            cands.append(dt.date(int(y), int(mo), int(d)))
        except ValueError:
            pass
    if not cands:
        # ניחוש של תאריך התחילה מסוכן יותר מכישלון: הוא עלול להדביק ערכים
        # ישנים לתאריך חדש. עדיף לעצור ולהתריע.
        fail("לא נמצא תאריך תחילה בכותרת לוח 5.3-1 – לא ניתן לדעת ממתי התעריף בתוקף.")
    eff = max(cands)
    if eff > today + dt.timedelta(days=400):
        fail("תאריך התחילה שחולץ (%s) רחוק מדי בעתיד – ככל הנראה פענוח שגוי." % eff)
    return eff.isoformat()


# ----------------------------------------------------------------------------
# 3. ולידציה ומיזוג
# ----------------------------------------------------------------------------
def current_value(data, key):
    rows = data["components"].get(key) or []
    return sorted(rows, key=lambda r: r["from"])[-1] if rows else None


def validate(found, data):
    for key, val in sorted(found.items()):
        lo, hi = RANGES[key]
        if not (lo <= val <= hi):
            fail("«%s» = %s אינו בטווח הסביר (%s–%s)." % (key, val, lo, hi))
        cur = current_value(data, key)
        if cur and cur["value"]:
            jump = abs(val - cur["value"]) / cur["value"]
            if jump > MAX_JUMP:
                fail("«%s» השתנה ב‑%.0f%% (%s → %s). שינוי חריג – נדרשת בדיקה ידנית."
                     % (key, jump * 100, cur["value"], val))


def merge(data, found, src, eff):
    """מוסיף שורה חדשה לכל רכיב שהשתנה, וסוגר את השורה הפתוחה יום לפניה."""
    changed = []
    day = dt.timedelta(days=1)
    for key, val in sorted(found.items()):
        cur = current_value(data, key)
        if cur and abs(cur["value"] - val) < 1e-9:
            continue
        if cur and cur["from"] >= eff:
            log("  «%s»: כבר קיימת שורה מ‑%s, מדלג." % (key, cur["from"]))
            continue
        if cur:
            cur["to"] = (dt.date.fromisoformat(eff) - day).isoformat()
        data["components"].setdefault(key, []).append({
            "id": "%s_auto_%s" % (key, eff.replace("-", "")),
            "from": eff, "to": None, "value": val,
            "verified": True, "estimated": False, "auto": True,
            "note": "עודכן אוטומטית מספר לוחות התעריפים של רשות החשמל, בתוקף מ‑%s." % eff,
            "src": src,
        })
        changed.append("%s: %s → %s" % (key, cur["value"] if cur else "—", val))
    return changed


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/tariffs.json")
    ap.add_argument("--url", help="כתובת ספר תעריפים מפורשת")
    ap.add_argument("--text", help="קובץ טקסט מוכן, במקום הורדה (לבדיקות)")
    ap.add_argument("--pdf", help="קובץ PDF מקומי, במקום הורדה (לבדיקות)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.text:
        src = a.text
        texts = [("file", open(a.text, encoding="utf-8", errors="replace").read())]
    elif a.pdf:
        src = a.pdf
        texts = pdf_to_texts(open(a.pdf, "rb").read())
    else:
        log("מאתר את ספר התעריפים…")
        src, blob = download_book(a.url)
        texts = pdf_to_texts(blob)
    for n, t in texts:
        log("  טקסט %s: %d תווים" % (n, len(t)))

    found, head = extract_all(texts)
    eff = effective_date(head)
    log("בתוקף מ‑%s" % eff)
    for k in sorted(found):
        log("  %-9s %s" % (k, found[k]))

    if not os.path.exists(a.data):
        log("(אין קובץ נתונים להשוות אליו – עוצר כאן)")
        return 0

    with open(a.data, encoding="utf-8") as f:
        data = json.load(f)
    validate(found, data)

    changed = merge(data, found, src, eff)
    data["lastCheck"] = {"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "status": "changed" if changed else "unchanged", "source": src}
    if changed:
        data["updated"] = dt.date.today().isoformat()
        log("שינויים: " + "; ".join(changed))
    else:
        log("אין שינוי בתעריפים.")

    if a.dry_run:
        log("(dry-run – לא נכתב כלום)")
        return 0
    with open(a.data, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("changed=%s\n" % ("true" if changed else "false"))
            f.write("summary=%s\n" % ("; ".join(changed) or "no change"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
