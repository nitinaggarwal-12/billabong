#!/usr/bin/env python3
"""
seed_urology.py — a small, hand-verified urology starter set so the app works
before you have run the full CMS ingest.

This is NOT a substitute for cms_ingest.py. It contains a few dozen well-
established rules, not the ~2.3 million PTP pairs CMS actually publishes.
Payment fields are deliberately left NULL: RVU values change every quarter and
guessing them would put wrong dollar figures in front of a physician.

    python3 seed_urology.py
"""

import os
import sqlite3
from datetime import date

from cms_ingest import SCHEMA, seed_status

DB = os.environ.get("CODEREV_DB", "cms.db")
SRC = "seed_urology.py (starter set — replace with cms_ingest.py)"
TODAY = date.today().isoformat()

# Column 1 is the comprehensive code; Column 2 is bundled into it.
# Modifier indicator: 0 = no modifier can bypass, 1 = a modifier may bypass,
# 9 = edit not applicable.
PTP = [
    # Diagnostic cystoscopy is included in every therapeutic cystoscopy.
    ("52204", "52000", "1", "Standards of medical / surgical practice"),
    ("52224", "52000", "1", "Standards of medical / surgical practice"),
    ("52234", "52000", "1", "Standards of medical / surgical practice"),
    ("52235", "52000", "1", "Standards of medical / surgical practice"),
    ("52240", "52000", "1", "Standards of medical / surgical practice"),
    ("52281", "52000", "1", "Standards of medical / surgical practice"),
    ("52310", "52000", "1", "Standards of medical / surgical practice"),
    ("52332", "52000", "1", "Standards of medical / surgical practice"),
    ("52351", "52000", "1", "Standards of medical / surgical practice"),
    ("52352", "52000", "1", "Standards of medical / surgical practice"),
    ("52353", "52000", "1", "Standards of medical / surgical practice"),
    ("52356", "52000", "1", "Standards of medical / surgical practice"),
    ("52601", "52000", "1", "Standards of medical / surgical practice"),
    ("52630", "52000", "1", "Standards of medical / surgical practice"),
    ("52648", "52000", "1", "Standards of medical / surgical practice"),
    ("52649", "52000", "1", "Standards of medical / surgical practice"),
    # Ureteroscopy hierarchy — the more extensive code includes the lesser.
    ("52353", "52352", "1", "More extensive procedure"),
    ("52356", "52352", "1", "More extensive procedure"),
    ("52356", "52353", "1", "More extensive procedure"),
    # 52356 already includes stent insertion. Reporting 52332 alongside it
    # is the single most common urology unbundling error.
    ("52356", "52332", "0", "More extensive procedure"),
    ("52353", "52332", "1", "More extensive procedure"),
    ("52351", "52000", "1", "Standards of medical / surgical practice"),
    # Prostate procedures include the diagnostic scope.
    ("52441", "52000", "1", "Standards of medical / surgical practice"),
    ("52442", "52441", "9", "Add-on code — report with primary"),
    ("52597", "52000", "1", "Standards of medical / surgical practice"),
    ("53854", "52000", "1", "Standards of medical / surgical practice"),
    # Botox bladder injection includes the cystoscopy.
    ("52287", "52000", "1", "Standards of medical / surgical practice"),
]

# code -> (max units per date of service, adjudication indicator, rationale)
MUE = [
    ("52000", "1", "3 Date of Service Edit: Clinical", "Nature of service/procedure"),
    ("52204", "1", "3 Date of Service Edit: Clinical", "Clinical: Data"),
    ("52332", "2", "3 Date of Service Edit: Clinical", "Anatomic considerations"),
    ("52352", "2", "3 Date of Service Edit: Clinical", "Anatomic considerations"),
    ("52353", "2", "3 Date of Service Edit: Clinical", "Anatomic considerations"),
    ("52356", "2", "3 Date of Service Edit: Clinical", "Anatomic considerations"),
    ("52441", "1", "3 Date of Service Edit: Clinical", "Nature of service/procedure"),
    ("55250", "1", "2 Date of Service Edit: Policy", "Anatomic considerations"),
]

# Structural indicators only. RVU columns stay NULL until the real file loads.
# bilateral_ind 1 = 150% payment applies when done bilaterally, so modifier 50
# is worth real money and is the classic urology miss.
RVU = [
    # code,  global, bilateral, mult_proc, description
    ("52000", "000", "2", "2", "Cystourethroscopy, separate procedure"),
    ("52204", "000", "2", "2", "Cystourethroscopy with biopsy"),
    ("52332", "000", "1", "2", "Cystourethroscopy with indwelling ureteral stent"),
    ("52352", "000", "1", "2", "Cystourethroscopy with ureteroscopy, removal of calculus"),
    ("52353", "000", "1", "2", "Cystourethroscopy with ureteroscopy, lithotripsy"),
    ("52356", "000", "1", "2", "Cystourethroscopy, ureteroscopy, lithotripsy with stent"),
    ("52441", "000", "9", "2", "Cystourethroscopy with permanent adjustable implant"),
    ("52442", "ZZZ", "9", "0", "Each additional permanent adjustable implant (add-on)"),
    ("52597", "090", "9", "2", "Cystourethroscopy with waterjet ablation of prostate"),
    ("55707", "000", "2", "2", "Biopsy, prostate, transrectal, ultrasound-guided"),
    ("50590", "090", "1", "2", "Lithotripsy, extracorporeal shock wave"),
    ("51798", "XXX", "9", "9", "Measurement of post-voiding residual by ultrasound"),
    ("51728", "XXX", "9", "2", "Complex cystometrogram with voiding pressure studies"),
    ("51741", "XXX", "9", "2", "Complex uroflowmetry"),
    ("51797", "ZZZ", "9", "0", "Intra-abdominal voiding pressure (add-on)"),
    ("52287", "000", "9", "2", "Cystourethroscopy with chemodenervation of bladder"),
]

HCPCS = [
    ("J9030", "BCG live intraves 1mg", "BCG live intravesical instillation, 1 mg"),
    ("J0585", "Onabotulinumtoxina 1 unit", "Injection, onabotulinumtoxinA, 1 unit"),
    ("J9217", "Leuprolide acetate suspnsn", "Leuprolide acetate suspension, 7.5 mg"),
    ("J9155", "Degarelix injection", "Injection, degarelix, 1 mg"),
]


def main():
    cx = sqlite3.connect(DB)
    cx.executescript(SCHEMA)
    for t in ("ptp_edit", "mue", "rvu", "hcpcs"):
        cx.execute(f"DELETE FROM {t} WHERE src = ?", (SRC,))

    cx.executemany(
        "INSERT INTO ptp_edit VALUES (?,?,?,?,?,?,?,?)",
        [(c1, c2, "", "*", mi, r, SRC, TODAY) for c1, c2, mi, r in PTP])
    cx.executemany(
        "INSERT INTO mue VALUES (?,?,?,?,?,?)",
        [(c, v, i, r, SRC, TODAY) for c, v, i, r in MUE])
    cx.executemany(
        "INSERT INTO rvu VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c, "", d, "A", None, None, None, None, g, b, m, "", "", SRC, TODAY)
         for c, g, b, m, d in RVU])
    cx.executemany(
        "INSERT INTO hcpcs VALUES (?,?,?,?,?)",
        [(c, s, l, SRC, TODAY) for c, s, l in HCPCS])

    seed_status(cx)
    cx.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?)",
               ("seed", SRC, len(PTP) + len(MUE) + len(RVU), TODAY,
                "starter set — not a substitute for the real CMS load"))
    cx.commit()

    print(f"seeded {DB}")
    print(f"  ptp_edit  {len(PTP)}")
    print(f"  mue       {len(MUE)}")
    print(f"  rvu       {len(RVU)}  (payment columns NULL by design)")
    print(f"  hcpcs     {len(HCPCS)}")
    print("\nRun cms_ingest.py to replace this with the full CMS dataset.")
    cx.close()


if __name__ == "__main__":
    main()