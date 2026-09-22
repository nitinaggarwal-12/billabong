#!/usr/bin/env python3
"""
app.py — urology coding audit, running against the real CMS tables in cms.db.

    python3 seed_urology.py      # or: python3 cms_ingest.py
    python3 app.py               # http://localhost:8000

Two layers, deliberately separate:

  RULES   Deterministic lookups against CMS data. Bundling, unit limits,
          deleted codes, bilateral eligibility. These are facts. They cite a
          row in a CMS file and they are either right or the file is wrong.

  REVIEW  Optional LLM pass over the note for things no table can answer —
          is the documentation there, is medical necessity stated, was a
          service performed but never coded. Set ANTHROPIC_API_KEY to enable.
          Marked separately in the UI because it is judgment, not fact.

Never mix them. The whole point is that a physician can see which findings
rest on a published rule and which rest on an opinion.
"""

import json
import os
import re
import sqlite3
import sys
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("CODEREV_DB", "cms.db")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 2_000_000
API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Verify against the CY2026 Physician Fee Schedule final rule before you show
# a dollar figure to anyone. Two conversion factors apply in 2026 — one for
# APM qualifying participants and one for everyone else. Leave None to display
# RVUs instead of dollars.
CONVERSION_FACTOR = None

IDENTIFIERS = [
    ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("MRN", r"\b(?:MRN|MR#|Medical Record(?:\s*(?:No|Number|#))?)\s*[:#]?\s*[A-Z0-9-]{4,}\b"),
    ("PHONE", r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    ("EMAIL", r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    ("DATE", r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)?\d{2}\b"),
    ("DOB", r"\b(?:DOB|Date of Birth|Born)\s*[:#]?\s*\S+"),
    ("NAME", r"\b(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"),
    ("NAME", r"\b(?:Patient(?:\s*Name)?|Surgeon|Provider|Attending)\s*[:#]\s*[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)*"),
    ("ADDRESS", r"\b\d{1,5}\s+[A-Z][A-Za-z]+\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Ln|Lane|Dr|Drive)\b\.?"),
    ("ZIP", r"\b\d{5}(?:-\d{4})?\b"),
]


def scan(text):
    hits = {}
    for label, pat in IDENTIFIERS:
        n = len(re.findall(pat, text))
        if n:
            hits[label] = hits.get(label, 0) + n
    return hits


def db():
    cx = sqlite3.connect(DB)
    cx.row_factory = sqlite3.Row
    return cx


def parse_codes(s):
    """Accept '52000, 52204 x2' or '52000 52204'. Returns [(code, units)]."""
    out = []
    for chunk in re.split(r"[,\n;]+", s or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"([0-9A-Z]{5})(?:\s*[-:]?\s*(?:59|25|50|51|58|78|79|XU|XS|XE|XP))?\s*(?:x\s*(\d+))?",
                     chunk, re.I)
        if m:
            out.append((m.group(1).upper(), int(m.group(2) or 1), chunk))
    return out


def money(total_rvu):
    if total_rvu and CONVERSION_FACTOR:
        return f"${total_rvu * CONVERSION_FACTOR:,.2f}"
    return None


# ------------------------------------------------------------------ rules

def run_rules(codes, note, cx):
    findings = []
    submitted = [c for c, _, _ in codes]
    note_l = (note or "").lower()

    # 1. Deleted or replaced codes ------------------------------------
    for code, units, raw in codes:
        r = cx.execute("SELECT * FROM code_status WHERE code=?", (code,)).fetchone()
        if r and r["status"] == "deleted":
            findings.append({
                "kind": "rules", "direction": "over", "code": code,
                "headline": f"{code} was deleted effective {r['effective_year']}",
                "detail": r["note"],
                "action": f"Recode to {r['replaced_by']}. Claims using {code} for "
                          f"dates of service in {r['effective_year']} or later will deny.",
                "authority": r["authority"], "source": "code_status table",
            })

    # 2. PTP bundling -------------------------------------------------
    for a in submitted:
        for b in submitted:
            if a == b:
                continue
            r = cx.execute(
                "SELECT * FROM ptp_edit WHERE col1=? AND col2=? "
                "AND (deletion IS NULL OR deletion IN ('','*'))", (a, b)).fetchone()
            if not r:
                continue
            mi = (r["modifier_indicator"] or "").strip()
            if mi == "0":
                verdict = ("No modifier can bypass this edit. Reporting both codes "
                           "on the same date of service is not payable.")
            elif mi == "1":
                verdict = ("A modifier may bypass this edit, but only when the "
                           "documentation supports a separate and distinct service. "
                           "If the note does not, remove the column 2 code.")
            else:
                verdict = "Edit present; modifier indicator not applicable."
            findings.append({
                "kind": "rules", "direction": "over", "code": b,
                "headline": f"{b} is bundled into {a}",
                "detail": f"{r['rationale'] or 'NCCI procedure-to-procedure edit'}. "
                          f"Modifier indicator {mi or 'unspecified'}. {verdict}",
                "action": f"Remove {b}, or document why it was separate and distinct.",
                "authority": "NCCI PTP edit, Practitioner Services",
                "source": r["src"],
            })

    # 3. MUE ----------------------------------------------------------
    for code, units, raw in codes:
        r = cx.execute("SELECT * FROM mue WHERE code=?", (code,)).fetchone()
        if r and r["mue_value"] and str(r["mue_value"]).isdigit():
            cap = int(r["mue_value"])
            if units > cap:
                findings.append({
                    "kind": "rules", "direction": "over", "code": code,
                    "headline": f"{code} reported at {units} units, MUE cap is {cap}",
                    "detail": f"{r['rationale'] or ''} Adjudication indicator: "
                              f"{r['adjudication_indicator'] or 'n/a'}.",
                    "action": f"Reduce to {cap} units, or split across dates of "
                              f"service if clinically accurate.",
                    "authority": "NCCI Medically Unlikely Edit",
                    "source": r["src"],
                })

    # 4. Bilateral opportunity ----------------------------------------
    bilateral_words = ("bilateral", "both sides", "right side", "left side",
                       "right ureter", "left ureter", "contralateral")
    looks_bilateral = sum(w in note_l for w in bilateral_words) >= 2 or "bilateral" in note_l
    if looks_bilateral:
        for code, units, raw in codes:
            r = cx.execute("SELECT * FROM rvu WHERE code=? LIMIT 1", (code,)).fetchone()
            if not r or (r["bilateral_ind"] or "").strip() != "1":
                continue
            already = bool(re.search(r"\b(50|RT|LT)\b", raw, re.I)) or units > 1
            if already:
                continue
            findings.append({
                "kind": "rules", "direction": "under", "code": code,
                "headline": f"{code} may qualify for bilateral modifier 50",
                "detail": "The note describes a bilateral procedure and this code "
                          "carries bilateral surgery indicator 1, meaning payment is "
                          "adjusted to 150% when performed bilaterally. It was "
                          "submitted without modifier 50 and at one unit.",
                "action": "If both sides were treated, append modifier 50. Confirm "
                          "the note documents each side separately.",
                "authority": "PFS Relative Value File, bilateral surgery indicator",
                "source": r["src"],
            })

    # 5. Global period context ----------------------------------------
    for code, units, raw in codes:
        r = cx.execute("SELECT * FROM rvu WHERE code=? LIMIT 1", (code,)).fetchone()
        if r and (r["global_days"] or "").strip() in ("010", "090"):
            findings.append({
                "kind": "rules", "direction": "info", "code": code,
                "headline": f"{code} carries a {r['global_days'].lstrip('0')}-day global period",
                "detail": f"{r['description'] or ''} Related visits and staged "
                          f"procedures in the global window need modifier 24, 58, "
                          f"78, or 79 to be paid separately.",
                "action": "Check whether any follow-up visits in the window were "
                          "written off that should have been billed.",
                "authority": "PFS Relative Value File, global days",
                "source": r["src"],
            })

    return findings


# ------------------------------------------------------------------ review

REVIEW_PROMPT = """You are a certified urology coding auditor. CPT 2026 applies.

A deterministic rules engine has ALREADY checked this claim against the CMS
NCCI PTP edit file, the MUE file, and the PFS relative value file. Do not
repeat bundling, unit-limit, or deleted-code findings — those are handled.

Your job is only what a lookup table cannot answer:
  - services described in the note that were never coded at all
  - codes whose documentation requirements are not met by this note
  - missing elements that would need a physician query before coding
  - medical necessity that is asserted but not supported

Rules:
1. Assert only what the note documents. If something is missing, raise it as a
   query, not as a recommended code.
2. Quote the note verbatim as evidence. No quote, no finding.
3. You have no interest in increasing revenue. Report over-coding as readily
   as under-coding.
4. Low confidence is an acceptable answer. Manufactured certainty is not.

Return ONLY raw JSON, no fences:
{"findings":[{"direction":"under|over|query","code":"CPT or null",
"headline":"one line","detail":"2-3 sentences","evidence":"verbatim quote",
"action":"what to do","confidence":"high|medium|low"}]}

At most 4 findings."""


def run_review(codes, note):
    if not API_KEY:
        return [], "ANTHROPIC_API_KEY not set — documentation review skipped."
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1400,
        "system": REVIEW_PROMPT,
        "messages": [{"role": "user", "content":
                      f"SUBMITTED CODES: {', '.join(c for c, _, _ in codes) or 'none'}\n\nNOTE:\n{note}"}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "x-api-key": API_KEY,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        parsed = json.loads(re.sub(r"```json|```", "", text).strip())
        out = parsed.get("findings", [])
        for f in out:
            f["kind"] = "review"
            f["authority"] = "Model judgment — requires coder verification"
            f["source"] = "claude-sonnet-4-6"
        return out, None
    except Exception as e:
        return [], f"Documentation review failed: {e}"


# ------------------------------------------------------------------ server

SAMPLES = [
    {"label": "Cysto with bladder biopsy", "billed": "52000, 52204",
     "note": """OPERATIVE NOTE
PROCEDURE: Cystourethroscopy with bladder biopsy.
INDICATIONS: 68-year-old male with painless gross hematuria and a 1.2 cm filling
defect at the right lateral bladder wall on CT urogram.
DESCRIPTION: The 22 French rigid cystoscope was introduced per urethra. The
bladder was systematically surveyed in all quadrants. A 1.2 cm sessile papillary
lesion was identified on the right lateral wall. Cold cup biopsy forceps were
introduced and three representative specimens obtained and sent to pathology.
The base was fulgurated with the Bugbee electrode. Bilateral ureteral orifices
were visualized and effluxed clear urine. The bladder was drained."""},
    {"label": "Bilateral ureteral stents", "billed": "52332",
     "note": """OPERATIVE NOTE
PROCEDURE: Cystoscopy with bilateral retrograde pyelography and bilateral
indwelling ureteral stent placement.
INDICATIONS: 71-year-old male with bilateral hydronephrosis and rising creatinine.
RIGHT SIDE: The right ureteral orifice was cannulated. Retrograde pyelogram
showed a smooth extrinsic narrowing of the mid right ureter. A 6 French by 26 cm
double-J stent was positioned with the proximal curl in the right renal pelvis.
LEFT SIDE: The left ureteral orifice was then cannulated. Retrograde pyelogram
demonstrated a comparable mid ureteral narrowing. A second 6 French by 26 cm
double-J stent was placed in identical fashion with good curls confirmed."""},
    {"label": "Transrectal prostate biopsy", "billed": "55700, 76942",
     "note": """PROCEDURE NOTE
PROCEDURE: Transrectal ultrasound-guided prostate biopsy.
INDICATION: PSA 7.4 ng/mL with abnormal digital rectal exam. Multiparametric MRI
showed a PI-RADS 4 lesion in the left posterolateral peripheral zone.
DESCRIPTION: Periprostatic nerve block administered bilaterally. The biplanar
transrectal ultrasound probe was introduced. Prostate volume measured 44 cc.
Under continuous real-time transrectal ultrasound guidance, twelve systematic
sextant cores were obtained. Two additional targeted cores were taken from the
hypoechoic region in the left mid gland corresponding to the MRI lesion using
cognitive registration. All cores labeled by site and sent to pathology."""},
    {"label": "Ureteroscopy with stent", "billed": "52353, 52332, 52000",
     "note": """OPERATIVE NOTE
PROCEDURE: Cystourethroscopy, left ureteroscopy with holmium laser lithotripsy
and placement of left ureteral stent.
INDICATIONS: 54-year-old with an obstructing 9 mm left mid ureteral calculus.
DESCRIPTION: Cystoscopy performed. The left ureteral orifice was identified and
a guidewire advanced. The semirigid ureteroscope was advanced to the level of
the stone. Holmium laser lithotripsy was performed until the calculus was
fragmented to dust. Fragments were extracted with a basket. A 6 French by 26 cm
double-J stent was then placed over the wire with good proximal curl in the
left renal pelvis. The right side was not instrumented."""},
]

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Billabong | Clinical Coding Intelligence</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Mono:wght@400;500&display=swap');
:root{--nav:#102a43;--blue:#1665d8;--bg:#f5f7fa;--card:#fff;--text:#172b4d;--muted:#66788a;--line:#dfe3e8;--green:#147d64;--red:#c93756;--amber:#a15c00;--violet:#6b4eff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif;font-size:14px}
button,input,textarea{font:inherit}.shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}.side{background:var(--nav);color:#d9e2ec;padding:22px 16px;position:sticky;top:0;height:100vh}.brand{font-size:20px;font-weight:700;color:white;padding:0 10px 24px}.brand small{display:block;font-size:10px;letter-spacing:.12em;color:#9fb3c8;margin-top:3px}.nav{display:grid;gap:5px}.nav div{padding:10px 12px;border-radius:7px}.nav .on{background:#243b53;color:white;font-weight:600}.nav .muted{margin-top:18px;color:#829ab1;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.sidefoot{position:absolute;bottom:20px;left:18px;right:18px;font-size:11px;color:#829ab1;line-height:1.5}
.main{min-width:0}.top{height:64px;background:white;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 28px;position:sticky;top:0;z-index:3}.top h1{font-size:17px;margin:0}.pill{padding:5px 9px;border-radius:99px;background:#e9f5f2;color:var(--green);font-size:11px;font-weight:600}.content{padding:24px 28px;max-width:1500px;margin:auto}
.hero{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:18px}.hero h2{font-size:24px;margin:0 0 5px}.hero p{margin:0;color:var(--muted)}.status{display:flex;gap:14px;color:var(--muted);font:11px Roboto Mono,monospace}
.notice{background:#eef5ff;border:1px solid #c9dcfb;border-radius:8px;padding:10px 13px;margin-bottom:18px;color:#315b8a;font-size:12px}
.workspace{display:grid;grid-template-columns:minmax(360px,.9fr) minmax(430px,1.1fr);gap:18px}.card{background:white;border:1px solid var(--line);border-radius:10px;box-shadow:0 1px 2px rgba(16,42,67,.04)}.hd{padding:15px 17px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}.hd b{font-size:13px}.bd{padding:17px}
.samples{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px}.sample{border:1px solid var(--line);background:white;border-radius:6px;padding:7px 9px;font-size:11px;color:var(--text);cursor:pointer}.sample:hover{border-color:var(--blue);color:var(--blue)}
label{display:block;font-size:11px;font-weight:600;color:#52667a;margin:14px 0 6px;text-transform:uppercase;letter-spacing:.04em}textarea,input{width:100%;border:1px solid #cbd5e1;border-radius:7px;padding:11px 12px;background:#fbfcfe;color:var(--text)}textarea{height:330px;resize:vertical;font:12px/1.6 Roboto Mono,monospace}textarea:focus,input:focus{outline:2px solid #b8d4fb;border-color:var(--blue)}
.actions{display:flex;align-items:center;justify-content:space-between;margin-top:15px}.primary{border:0;background:var(--blue);color:white;border-radius:7px;padding:10px 16px;font-weight:600;cursor:pointer}.primary:disabled{opacity:.4}.safe{font-size:11px;color:var(--green)}
.empty{padding:55px 24px;text-align:center;color:var(--muted)}.empty strong{display:block;color:var(--text);font-size:15px;margin-bottom:6px}.layer{padding:12px 16px;background:#f8fafc;border-bottom:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;display:flex;justify-content:space-between}.layer.rules{color:#315b8a}.layer.review{color:var(--violet)}
.finding{padding:17px;border-bottom:1px solid var(--line)}.finding:last-child{border-bottom:0}.row{display:flex;gap:8px;align-items:center}.code{font:600 14px Roboto Mono,monospace}.tag{font-size:10px;font-weight:700;padding:3px 6px;border-radius:4px}.over{background:#fff0f3;color:var(--red)}.under{background:#e9f5f2;color:var(--green)}.query{background:#fff7e6;color:var(--amber)}.info{background:#edf2f7;color:#52667a}.confidence{margin-left:auto;font-size:10px;color:var(--muted)}.finding h3{font-size:14px;margin:10px 0 7px}.finding p{font-size:12px;color:var(--muted);line-height:1.55;margin:6px 0}.evidence{border-left:3px solid var(--violet);background:#faf9ff;padding:9px 11px;margin:10px 0;font:11px/1.5 Roboto Mono,monospace}.authority{background:#f8fafc;border-radius:6px;padding:9px 10px;margin:10px 0;font-size:10.5px;color:#52667a;word-break:break-word}.authority b{display:block;color:var(--text);margin-bottom:2px}.decision{display:flex;gap:6px;margin-top:12px}.decision button{background:white;border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:11px;cursor:pointer}.decision button.on{background:var(--nav);color:white;border-color:var(--nav)}
.flag{background:#fff5f5;border:1px solid #ffd2da;border-radius:7px;padding:11px;margin-top:10px;color:var(--red);font-size:12px}.flag button{margin-top:8px}.legend{display:flex;gap:12px;font-size:10px;color:var(--muted)}.dot:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--blue);margin-right:5px}.dot.ai:before{background:var(--violet)}
@media(max-width:980px){.shell{grid-template-columns:1fr}.side{display:none}.workspace{grid-template-columns:1fr}.top{position:static}.content{padding:18px}}
</style></head><body><div class=shell>
<aside class=side><div class=brand>Billabong<small>CLINICAL CODING INTELLIGENCE</small></div><div class=nav><div class=on>Audit workspace</div><div>Case queue</div><div>Rule integrity</div><div>Evaluation lab</div><div class=muted>Governance</div><div>CMS data health</div><div>Audit trail</div></div><div class=sidefoot>Decision support only<br>Human verification required</div></aside>
<main class=main><header class=top><h1>Urology coding audit</h1><span class=pill>Demo environment</span></header><div class=content>
<div class=hero><div><h2>Review a procedure note</h2><p>CMS rule checks and documentation judgment stay visibly separate.</p></div><div class=status id=bar>Loading data status…</div></div>
<div class=notice><b>Privacy gate:</b> identifiers are screened before an audit can run. Use de-identified or synthetic notes for this demo.</div>
<div class=workspace><section class=card><div class=hd><b>Clinical documentation</b><span class=safe>● Privacy screening active</span></div><div class=bd>
<div class=samples id=samples></div><label for=note>Operative / procedure note</label><textarea id=note placeholder="Paste a de-identified note, or choose a synthetic sample above."></textarea><div id=phi></div>
<label for=codes>Submitted CPT / HCPCS codes</label><input id=codes placeholder="e.g. 52353, 52332 x2"><div class=actions><span style="font-size:11px;color:var(--muted)">Rules first · Review second · Human decides</span><button class=primary id=go>Run coding audit</button></div>
</div></section><section class=card><div class=hd><b>Audit findings</b><div class=legend><span class=dot>Published rule</span><span class="dot ai">AI-assisted review</span></div></div><div id=out><div class=empty><strong>Ready for review</strong>Select a synthetic case or enter a de-identified note and submitted codes.</div></div></section></div>
</div></main></div><script>
const S=__SAMPLES__,$=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
S.forEach(s=>{let b=document.createElement('button');b.className='sample';b.textContent=s.label+' · '+s.billed;b.onclick=()=>{$('#note').value=s.note;$('#codes').value=s.billed;check();};$('#samples').appendChild(b)});
fetch('/status').then(r=>r.json()).then(d=>{$('#bar').innerHTML='<span>CMS source <b>'+esc(d.source)+'</b></span><span>PTP <b>'+Number(d.counts.ptp||0).toLocaleString()+'</b></span><span>MUE <b>'+Number(d.counts.mue||0).toLocaleString()+'</b></span><span>Review <b>'+(d.review?'enabled':'demo-off')+'</b></span>'}).catch(()=>{$('#bar').textContent='Status unavailable'});
let phiCount=0;function check(){fetch('/scan',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({note:$('#note').value})}).then(r=>r.json()).then(d=>{phiCount=Object.values(d.hits||{}).reduce((a,b)=>a+b,0);$('#go').disabled=phiCount>0;$('#phi').innerHTML=phiCount?'<div class=flag><b>Identifiers detected — audit blocked</b><br>'+Object.entries(d.hits).map(([k,v])=>esc(k)+' '+v).join(' · ')+'<br><button class=sample onclick="redact()">Redact detected identifiers</button></div>':''})}
function redact(){fetch('/redact',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({note:$('#note').value})}).then(r=>r.json()).then(d=>{$('#note').value=d.note;check()})}
$('#note').addEventListener('input',()=>{clearTimeout(window.t);window.t=setTimeout(check,350)});
$('#go').onclick=()=>{if(!$('#codes').value.trim()||!$('#note').value.trim()){return} $('#out').innerHTML='<div class=empty><strong>Running audit…</strong>Checking deterministic CMS rules before documentation review.</div>';fetch('/audit',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({note:$('#note').value,codes:$('#codes').value})}).then(r=>r.json()).then(render).catch(()=>{$('#out').innerHTML='<div class=empty><strong>Audit unavailable</strong>Please retry.</div>'})};
function render(d){if(d.error){$('#out').innerHTML='<div class=empty><strong>Audit blocked</strong>'+esc(d.error)+'</div>';return}let h='';for(const [k,title,sub] of [['rules','CMS rules layer','Deterministic · published source'],['review','Documentation review','AI-assisted · coder verification']]){let fs=(d.findings||[]).filter(f=>f.kind===k);if(!fs.length&&k==='review'&&d.note_review){h+='<div class="layer review"><span>'+title+'</span><span>'+sub+'</span></div><div class=empty>'+esc(d.note_review)+'</div>';continue}if(!fs.length)continue;h+='<div class="layer '+k+'"><span>'+title+'</span><span>'+sub+'</span></div>';fs.forEach(f=>{let label={under:'Under-coded',over:'Over-coded',query:'Needs query',info:'Context'}[f.direction]||f.direction;h+='<article class=finding><div class=row><span class=code>'+esc(f.code||'—')+'</span><span class="tag '+esc(f.direction)+'">'+esc(label)+'</span>'+(f.confidence?'<span class=confidence>'+esc(f.confidence)+' confidence</span>':'')+'</div><h3>'+esc(f.headline)+'</h3><p>'+esc(f.detail||'')+'</p>'+(f.evidence?'<div class=evidence>“'+esc(f.evidence)+'”</div>':'')+'<div class=authority><b>'+esc(f.authority||'')+'</b>'+esc(f.source||'')+'</div><p><b>Recommended next step:</b> '+esc(f.action||'')+'</p><div class=decision><button onclick="pick(this)">Agree</button><button onclick="pick(this)">Disagree</button><button onclick="pick(this)">Unsure</button></div></article>'})}if(!h)h='<div class=empty><strong>No findings returned</strong>No issue was identified by the loaded rules and enabled review layers.</div>';$('#out').innerHTML=h}
function pick(b){b.parentNode.querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on')}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json", status=200):
        body = (json.dumps(obj) if ctype == "application/json" else obj).encode()
        self.send_response(status)
        self.send_header("content-type", ctype + "; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("x-frame-options", "DENY")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header("content-security-policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE.replace("__SAMPLES__", json.dumps(SAMPLES)), "text/html")
        elif self.path == "/health":
            self._send({"ok": True, "service": "billabong"})
        elif self.path == "/status":
            cx = db()
            counts = {}
            for t in ("ptp_edit", "mue", "rvu"):
                try:
                    counts[t.replace("_edit", "")] = cx.execute(
                        f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.OperationalError:
                    counts[t] = 0
            row = cx.execute("SELECT src FROM ptp_edit LIMIT 1").fetchone()
            src = "seed" if row and "seed_urology" in (row["src"] or "") else "CMS"
            cx.close()
            self._send({"counts": counts, "review": bool(API_KEY), "source": src})
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        if n > MAX_BODY:
            self._send({"error": "Request too large."}, status=413)
            return
        try:
            payload = json.loads(self.rfile.read(n) or "{}")
        except json.JSONDecodeError:
            self._send({"error": "Invalid JSON."}, status=400)
            return

        if self.path == "/scan":
            self._send({"hits": scan(payload.get("note", ""))})
            return

        if self.path == "/redact":
            t = payload.get("note", "")
            for label, pat in IDENTIFIERS:
                t = re.sub(pat, f"[{label} REMOVED]", t)
            self._send({"note": t})
            return

        if self.path == "/audit":
            note = payload.get("note", "")
            if scan(note):
                self._send({"error": "Identifiers still present. Audit blocked."})
                return
            codes = parse_codes(payload.get("codes", ""))
            cx = db()
            try:
                findings = run_rules(codes, note, cx)
            finally:
                cx.close()
            review, msg = run_review(codes, note)
            self._send({"findings": findings + review, "note_review": msg})
            return

        self.send_error(404)


if __name__ == "__main__":
    if not os.path.exists(DB):
        sys.exit(f"No {DB}. Run: python3 seed_urology.py  (or cms_ingest.py)")
    print(f"  db      {DB}")
    print(f"  review  {'enabled' if API_KEY else 'off (set ANTHROPIC_API_KEY)'}")
    print(f"  serving http://localhost:{PORT}\n")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    ThreadingHTTPServer(("", PORT), H).serve_forever()