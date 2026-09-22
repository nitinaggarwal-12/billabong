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
from http.server import BaseHTTPRequestHandler, HTTPServer

DB = os.environ.get("CODEREV_DB", "cms.db")
PORT = int(os.environ.get("PORT", "8000"))
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
<title>Urology coding audit</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
@import url('https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
:root{--paper:#FAFAF8;--ink:#16202B;--muted:#5C6B7A;--rule:#DEDCD5;
--recover:#0F6E5C;--risk:#9E2B3F;--query:#7A5A16}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font-family:'IBM Plex Sans',system-ui,sans-serif;line-height:1.5;padding:40px 26px 80px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-family:Newsreader,Georgia,serif;font-weight:500;font-size:34px;margin:0 0 6px;letter-spacing:-.015em}
.sub{color:var(--muted);font-size:14.5px;max-width:62ch;margin:0}
.head{border-bottom:2px solid var(--ink);padding-bottom:18px}
.bar{display:flex;gap:20px;flex-wrap:wrap;font-size:12px;color:var(--muted);
margin:14px 0 26px;font-family:'IBM Plex Mono',monospace}
.bar b{color:var(--ink);font-weight:500}
.grid{display:grid;grid-template-columns:1.05fr 1fr;gap:40px;align-items:start}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
label{display:block;font-size:12.5px;font-weight:600;margin-bottom:7px}
label span{font-weight:400;color:var(--muted)}
textarea,input{width:100%;background:#fff;border:1px solid var(--rule);border-radius:2px;
padding:12px 13px;color:var(--ink);font-family:'IBM Plex Mono',monospace;font-size:12.5px;line-height:1.65}
textarea{min-height:300px;resize:vertical}
textarea:focus,input:focus{outline:2px solid var(--ink);outline-offset:1px}
.samples{display:flex;flex-direction:column;gap:6px;margin-bottom:22px}
.samples button{text-align:left;background:#fff;border:1px solid var(--rule);border-radius:2px;
padding:10px 12px;cursor:pointer;font:inherit;font-size:13px;display:flex;
justify-content:space-between;gap:14px}
.samples button:hover{border-color:var(--ink)}
.samples span{color:var(--muted);font-size:12px;font-family:'IBM Plex Mono',monospace}
.btn{background:var(--ink);color:var(--paper);border:1px solid var(--ink);padding:11px 20px;
border-radius:2px;font:inherit;font-size:13.5px;font-weight:500;cursor:pointer;margin-top:18px}
.btn:disabled{opacity:.35;cursor:not-allowed}
.flag{margin:14px 0;padding:13px 15px;border:1px solid var(--risk);background:#fff;font-size:13px}
.flag h4{margin:0 0 6px;font-size:13px;color:var(--risk)}
.flag ul{margin:6px 0 10px;padding-left:18px;color:var(--muted)}
.empty{border:1px dashed var(--rule);padding:40px 26px;text-align:center;color:var(--muted);
font-size:13.5px;background:#fff}
.layer{font-family:'IBM Plex Mono',monospace;font-size:11.5px;color:var(--muted);
border-bottom:1px solid var(--rule);padding-bottom:6px;margin:28px 0 0}
.layer:first-of-type{margin-top:0}
.f{border-top:1px solid var(--rule);padding:20px 0}
.f:first-of-type{border-top:none}
.fh{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.code{font-family:'IBM Plex Mono',monospace;font-size:19px;font-weight:500}
.dir{font-size:12px;font-weight:600;padding-bottom:1px}
.under{color:var(--recover);border-bottom:2px solid var(--recover)}
.over{color:var(--risk);border-bottom:2px solid var(--risk)}
.query{color:var(--query);border-bottom:2px solid var(--query)}
.info{color:var(--muted);border-bottom:2px solid var(--rule)}
.hl{font-family:Newsreader,Georgia,serif;font-size:17px;margin:0 0 10px;max-width:52ch}
.dt{font-size:13.5px;color:var(--muted);margin:0 0 12px;max-width:56ch}
.cite{border-left:2px solid var(--ink);padding-left:13px;margin-bottom:12px}
.cite .a{font-family:'IBM Plex Mono',monospace;font-size:11.5px;font-weight:500}
.cite .s{font-size:11.5px;color:var(--muted);word-break:break-all}
.ev{background:#fff;border:1px solid var(--rule);padding:10px 12px;
font-family:'IBM Plex Mono',monospace;font-size:12px;margin-bottom:12px;max-width:62ch}
.act{font-size:13.5px;max-width:56ch}
.act b{font-weight:600}
.vb{background:none;border:1px solid var(--rule);border-radius:2px;padding:5px 12px;
font:inherit;font-size:12.5px;cursor:pointer;color:var(--muted);margin:12px 8px 0 0}
.vb.on{background:var(--ink);border-color:var(--ink);color:var(--paper)}
</style></head><body><div class=wrap>
<div class=head><h1>Urology coding audit</h1>
<p class=sub>Deterministic checks run against the CMS files loaded on this machine.
Documentation review is a separate, clearly marked layer.</p></div>
<div class=bar id=bar></div>
<div class=grid>
<div>
  <label>Sample charts <span>— synthetic, safe to use</span></label>
  <div class=samples id=samples></div>
  <label for=note>Operative or procedure note</label>
  <textarea id=note placeholder="Paste a de-identified note, or load a sample."></textarea>
  <div id=phi></div>
  <div style="margin-top:18px">
    <label for=codes>Codes submitted <span>— e.g. 52353, 52332 x2</span></label>
    <input id=codes placeholder="52000, 52204">
  </div>
  <button class=btn id=go>Run audit</button>
</div>
<div id=out><div class=empty>Load a sample, or paste a note and the codes that were billed.</div></div>
</div></div>
<script>
const S = __SAMPLES__;
const $ = s => document.querySelector(s);
S.forEach((s,i)=>{const b=document.createElement('button');
b.innerHTML = s.label + '<span>'+s.billed+'</span>';
b.onclick=()=>{$('#note').value=s.note;$('#codes').value=s.billed;check();$('#out').innerHTML='';};
$('#samples').appendChild(b);});

fetch('/status').then(r=>r.json()).then(d=>{
  $('#bar').innerHTML = Object.entries(d.counts).map(([k,v])=>
    `${k} <b>${v.toLocaleString()}</b>`).join('') +
    ` · review <b>${d.review?'on':'off'}</b>` + ` · source <b>${d.source}</b>`;
});

let phiCount=0;
function check(){
  const t=$('#note').value;
  fetch('/scan',{method:'POST',body:JSON.stringify({note:t})})
   .then(r=>r.json()).then(d=>{
    phiCount=Object.values(d.hits).reduce((a,b)=>a+b,0);
    $('#phi').innerHTML = phiCount ? `<div class=flag><h4>Identifiers detected — audit blocked</h4>
      <ul>${Object.entries(d.hits).map(([k,v])=>`<li>${k} — ${v}</li>`).join('')}</ul>
      <button class=vb onclick="redact()">Remove them and continue</button></div>` : '';
    $('#go').disabled = phiCount>0;
  });
}
function redact(){
  fetch('/redact',{method:'POST',body:JSON.stringify({note:$('#note').value})})
   .then(r=>r.json()).then(d=>{$('#note').value=d.note;check();});
}
$('#note').addEventListener('input', ()=>{clearTimeout(window.t);window.t=setTimeout(check,400);});

$('#go').onclick=()=>{
  $('#out').innerHTML='<div class=empty>Checking against CMS edit tables…</div>';
  fetch('/audit',{method:'POST',body:JSON.stringify(
    {note:$('#note').value,codes:$('#codes').value})})
   .then(r=>r.json()).then(render);
};

function render(d){
  if(d.error){$('#out').innerHTML=`<div class=flag><h4>Audit failed</h4>${d.error}</div>`;return;}
  let h='';
  const groups=[['rules','Rules layer — deterministic, cites a CMS file'],
                ['review','Documentation review — model judgment, verify before use']];
  for(const [k,title] of groups){
    const fs=d.findings.filter(f=>f.kind===k);
    if(!fs.length && k==='review' && d.note_review){h+=`<div class=layer>${title}</div>
      <div class=empty style="margin-top:14px">${d.note_review}</div>`;continue;}
    if(!fs.length) continue;
    h+=`<div class=layer>${title}</div>`;
    fs.forEach((f,i)=>{
      h+=`<div class=f><div class=fh><span class=code>${f.code||'—'}</span>
      <span class="dir ${f.direction}">${({under:'Under-coded',over:'Over-coded',
        query:'Needs query',info:'Context'})[f.direction]||f.direction}</span>
      ${f.confidence?`<span style="margin-left:auto;font-size:12px;color:var(--muted)">${f.confidence} confidence</span>`:''}</div>
      <p class=hl>${f.headline}</p><p class=dt>${f.detail||''}</p>
      <div class=cite><div class=a>${f.authority||''}</div><div class=s>${f.source||''}</div></div>
      ${f.evidence?`<div class=ev>${f.evidence}</div>`:''}
      <p class=act><b>Do this:</b> ${f.action||''}</p>
      ${['Agree','Disagree','Unsure'].map(v=>
        `<button class=vb onclick="this.parentNode.querySelectorAll('.vb').forEach(b=>b.classList.remove('on'));this.classList.add('on')">${v}</button>`).join('')}
      </div>`;
    });
  }
  if(!h) h='<div class=empty>No findings. Either the coding is clean or the note is too thin to check.</div>';
  $('#out').innerHTML=h;
}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json"):
        body = (json.dumps(obj) if ctype == "application/json" else obj).encode()
        self.send_response(200)
        self.send_header("content-type", ctype + "; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE.replace("__SAMPLES__", json.dumps(SAMPLES)), "text/html")
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
        payload = json.loads(self.rfile.read(n) or "{}")

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
    HTTPServer(("", PORT), H).serve_forever()