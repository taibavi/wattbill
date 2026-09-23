#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
עדכון אוטומטי של טבלת התעריפים של WattBill.

הסקריפט רץ ב‑GitHub Actions (לא בדפדפן), ולכן אין לו בעיית CORS מול gov.il.
הוא:
  1. מאתר את ספר לוחות התעריפים העדכני של רשות החשמל,
  2. מוריד אותו וממיר ל‑טקסט,
  3. מחלץ את לוח 5.3-1 (תעריף הצריכה והקיבולת לצרכן ביתי)
     ואת לוח 5.4-1 (תשלום קבוע – שירותי חלוקה ואספקה, מונה חד־פאזי ותלת־פאזי),
  4. מוודא שהערכים הגיוניים,
  5. ורק אם הכול עבר – מעדכן את data/tariffs.json.

עקרון מנחה: עדיף להישאר עם ערך ישן ונכון מאשר לפרסם ערך חדש ושגוי.
כל כישלון ולידציה עוצר את העדכון ומחזיר קוד יציאה שונה מאפס, כדי ש‑GitHub
ישלח התראה במקום לדחוף נתון פגום לאפליקציה.

שימוש:
    python3 fetch_tariffs.py --data ../data/tariffs.json
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

NUM = r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?"


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log("FAIL: " + msg)
    sys.exit(2)


# ----------------------------------------------------------------------------
# 1. איתור והורדה
# ----------------------------------------------------------------------------
def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "he,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def candidate_urls(today=None):
    """כתובות אפשריות לספר התעריפים, מהחדש לישן.

    שמות הקבצים של רשות החשמל עקביים למדי, אבל לא זהים בין מהדורות,
    ולכן מנסים כמה תבניות ידועות לכל מהדורה חצי‑שנתית."""
    today = today or dt.date.today()
    editions = []
    y, half = today.year, 7 if today.month >= 7 else 1
    for _ in range(5):                       # המהדורה הנוכחית וארבע אחורה
        editions.append((half, y))
        if half == 7:
            half = 1
        else:
            half, y = 7, y - 1
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
    hits = [u for u in urls if re.search(r"(sefer|tarrif|tariff)", u, re.I)]
    seen, out = set(), []
    for u in hits:
        if u not in seen:
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


def pdf_to_text(blob):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "book.pdf")
        with open(p, "wb") as f:
            f.write(blob)
        try:
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", p, "-"],
                                 capture_output=True, timeout=300)
        except FileNotFoundError:
            fail("pdftotext לא מותקן (חבילת poppler-utils).")
        if out.returncode != 0:
            fail("pdftotext נכשל: %s" % out.stderr.decode("utf-8", "replace")[:300])
        return out.stdout.decode("utf-8", "replace")


# ----------------------------------------------------------------------------
# 2. חילוץ
# ----------------------------------------------------------------------------
def norm(text):
    """מנרמל רווחים כדי שהחילוץ לא יהיה תלוי באופן שבו ה‑PDF פורק לשורות."""
    text = text.replace("‏", " ").replace("‎", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def nums(s, k):
    return [float(x.replace(",", "")) for x in re.findall(NUM, s)[:k]]


def sector_index(header):
    """בספרי 2023–2024 העמודה הביתית ראשונה; בספר 07/2026 היא אחרונה.
    קובעים לפי מיקום התווית 'ביתי' מול 'מ"ע' (צובר מתח עליון) בכותרת הלוח עצמו.
    לא מחפשים 'צובר מ"ע' כמחרוזת אחת כי בחלק מהמהדורות המילה נשברת בין שורות."""
    b = header.rfind("ביתי")
    c = max(header.rfind('מ"ע'), header.rfind("מ״ע"))
    if b < 0 or c < 0:
        return None
    return 0 if b < c else -1


def extract_53(t):
    """לוח 5.3-1 – תעריפים אחידים: תעריף הצריכה והקיבולת לצרכן ביתי."""
    var = re.compile(r'(?:תשלום|רכיב) משתנה:?\s*אגורות\s*לק(?:ו)?וט"ש\s*((?:' + NUM + r'\s+){4,}' + NUM + ')')
    fix = re.compile(r'(?:תשלום|רכיב) (?:קבוע|קיבולת):?\s*₪\s*ל-?\s*KVA\s*לשנה\s*((?:' + NUM + r'\s+){4,}' + NUM + ')')
    mv = var.search(t)
    if not mv:
        fail("לא נמצאה שורת התשלום המשתנה בלוח 5.3-1.")
    head = t[max(0, mv.start() - 350):mv.start()]
    idx = sector_index(head)
    if idx is None:
        fail("לא זוהה סדר העמודות בלוח 5.3-1 (לא נמצאו התוויות 'ביתי' ו'צובר מ\"ע').")
    ev = nums(mv.group(1), 6)
    if len(ev) < 5:
        fail("לוח 5.3-1: נמצאו רק %d ערכים בשורת התשלום המשתנה." % len(ev))
    energy = ev[idx]
    # ביתי וכללי זהים בכל המהדורות שנבדקו – בדיקת שפיות לסדר העמודות
    neighbour = ev[1] if idx == 0 else ev[-2]
    if abs(neighbour - energy) > 0.011:
        fail("לוח 5.3-1: התעריף הביתי (%.2f) אינו תואם לתעריף הכללי (%.2f) – "
             "ייתכן שסדר העמודות השתנה." % (energy, neighbour))

    mf = fix.search(t, mv.end() - 5)
    if not mf:
        fail("לא נמצאה שורת התשלום הקבוע (KVA) בלוח 5.3-1.")
    cv = nums(mf.group(1), 6)
    if len(cv) < 5:
        fail("לוח 5.3-1: נמצאו רק %d ערכים בשורת התשלום הקבוע." % len(cv))
    return {"energy": round(energy / 100.0, 6), "capacity": cv[idx]}, head


ROW_RE = re.compile(r'חודשיים\s+((?:' + NUM + r'\s+){3,}' + NUM + ')')


def priced_rows(block):
    """כל שורות החיוב הדו‑חודשיות בפרק, כ‑(מיקום, סכום).

    בלוח 5.4-1 כל שורה בנויה משלושה רכיבים ואחריהם עמודות סכום שסדרן משתנה
    בין המהדורות (ב‑07/2024 הסכום אחרון, ב‑07/2023 הוא ראשון). לכן מחשבים את
    סכום שלושת הרכיבים ובוחרים את העמודה שתואמת לו – זה עמיד לשינוי סדר."""
    out = []
    for m in ROW_RE.finditer(block):
        v = nums(m.group(1), 7)
        if len(v) < 4:
            continue
        total = sum(v[:3])
        for cand in v[3:6]:
            if abs(cand - total) <= 0.035:
                out.append((m.start(), cand))
                break
    return out


def extract_54(t):
    """לוח 5.4-1 – תשלום קבוע לשירותי צרכנות, שורות 7 (חד־פאזי) ו‑8 (תלת־פאזי).

    שתי מלכודות שנצפו בפועל:
      • המקף משתנה בין המהדורות: 'חד-פאזי', 'חד- פאזי', 'חד - פאזי'.
      • ב‑07/2026 שורה 8 נחתכת בין עמודים, כך שהמספרים שלה מופיעים *לפני*
        תיאור השורה. לכן בוחרים את שורת המספרים הקרובה ביותר לתיאור,
        לא בהכרח את זו שאחריו."""
    out = {}
    secs = [("A", "צרכנות חלוקה"), ("B", "צרכנות אספקה")]
    for i, (skey, sanchor) in enumerate(secs):
        si = t.find(sanchor)
        if si < 0:
            fail("לא נמצא הפרק «%s» בלוח 5.4-1." % sanchor)
        stop = t.find(secs[1][1], si + 1) if i == 0 else len(t)
        block = t[si: stop if stop > si else len(t)]

        # חיתוך לשורות לפי המספור בעמודה הראשונה. לוקחים כל שורה במלואה,
        # כולל מה שגלש לעמוד הבא, ורק אז מחפשים בתוכה את המספרים והתיאור.
        starts = [m.start() for m in re.finditer(r'(?<!\d)\d{1,2}\s+תעריף אחיד', block)]
        if len(starts) < 2:
            fail("בפרק «%s» לא זוהו שורות התעריף האחיד." % sanchor)
        starts.append(len(block))
        for rkey, phase in (("", "חד"), ("3", "תלת")):
            pat = re.compile(r'מונה\s+' + phase + r'\s*-\s*פאזי')
            seg = None
            for a, b in zip(starts, starts[1:]):
                s = block[a:b]
                if pat.search(s) and "תשלום מראש" not in s:
                    seg = s
                    break
            if seg is None:
                fail("לא נמצאה שורת «מונה %s־פאזי» בפרק «%s»." % (phase, sanchor))
            vals = priced_rows(seg)
            if not vals:
                fail("לא ניתן לאמת את הסכום בשורת «מונה %s־פאזי» בפרק «%s»."
                     % (phase, sanchor))
            out["fixed" + skey + rkey] = vals[0][1]
    # בכל המהדורות שנבדקו התעריף התלת־פאזי שונה מהחד־פאזי. זהות ביניהם
    # פירושה שנבחרה אותה שורה פעמיים – עדיף להיכשל מאשר לפרסם ערך שגוי.
    for skey in ("A", "B"):
        if abs(out["fixed" + skey] - out["fixed" + skey + "3"]) < 1e-9:
            fail("בפרק %s התעריף החד־פאזי והתלת־פאזי יצאו זהים (%s) – "
                 "ככל הנראה זוהתה אותה שורה פעמיים." % (skey, out["fixed" + skey]))
    return out


def extract_all(text):
    t = norm(text)
    v, head = extract_53(t)
    v.update(extract_54(t))
    return v, head


# ----------------------------------------------------------------------------
# 3. ולידציה ומיזוג
# ----------------------------------------------------------------------------
def current_value(data, key):
    rows = data["components"].get(key) or []
    if not rows:
        return None
    return sorted(rows, key=lambda r: r["from"])[-1]


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


def effective_date(head, today=None):
    """תאריך התחילה של המהדורה, מתוך כותרת לוח 5.3-1 עצמה.

    הכותרת מכילה גם את תאריך ההחלטה וגם את 'תאריך עדכון אחרון'; המאוחר
    מביניהם הוא תמיד תאריך התחילה (למשל 21/12/2022 מול 01/01/2023)."""
    today = today or dt.date.today()
    cands = []
    for d, mo, y in re.findall(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b', head):
        try:
            cands.append(dt.date(int(y), int(mo), int(d)))
        except ValueError:
            pass
    if not cands:
        fail("לא נמצא תאריך תחילה בכותרת לוח 5.3-1.")
    eff = max(cands)
    if eff > today + dt.timedelta(days=400):
        fail("תאריך התחילה שחולץ (%s) רחוק מדי בעתיד – ככל הנראה פענוח שגוי." % eff)
    return eff.isoformat()


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/tariffs.json")
    ap.add_argument("--url", help="כתובת ספר תעריפים מפורשת")
    ap.add_argument("--text", help="קובץ טקסט מוכן, במקום הורדה (לבדיקות)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.text:
        src = a.text
        text = open(a.text, encoding="utf-8", errors="replace").read()
    else:
        log("מאתר את ספר התעריפים…")
        src, blob = download_book(a.url)
        text = pdf_to_text(blob)
    log("אורך הטקסט: %d תווים" % len(text))

    found, head = extract_all(text)
    eff = effective_date(head)
    log("בתוקף מ‑%s" % eff)
    for k in sorted(found):
        log("  %-9s %s" % (k, found[k]))

    if a.dry_run and not os.path.exists(a.data):
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
