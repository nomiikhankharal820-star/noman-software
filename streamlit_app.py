"""AI Lead-to-Sale MVP: intake -> classify -> score -> extract -> proposal -> approval -> follow-ups.
Run: python app.py   (public form: /   dashboard: /dashboard, login admin / admin123 unless env set)"""
import os, re, json, math, sqlite3, smtplib, datetime as dt
from email.message import EmailMessage
from pathlib import Path
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, send_file, abort
import scoring

BASE = Path(__file__).parent
DB = os.getenv("DB_PATH", str(BASE / "leads.db"))
PDF_DIR = BASE / "proposals"; PDF_DIR.mkdir(exist_ok=True)
RATE = float(os.getenv("HOURLY_RATE_USD", 25))
app = Flask(__name__); app.secret_key = os.getenv("SECRET_KEY", "change-me")
FX = {"usd": 1, "$": 1, "pkr": .0036, "rs": .0036, "inr": .012, "eur": 1.08, "gbp": 1.27, "aed": .27}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, name, email, phone, company, service_interest, description,
 budget_text, timeline_text, source, raw TEXT, status, service_type, urgency, budget_band, class_conf REAL, needs_review INT,
 score INT, tier, top_factors TEXT, model_version, requirements TEXT, outcome, outcome_reason, created_at);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, lead_id, from_state, to_state, actor, reason, ts);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, lead_id, version, content TEXT, total REAL, status, approved_by, comment, created_at);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, lead_id, to_addr, subject, body, kind, status, idem UNIQUE, ts);
CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY, lead_id, step, due_at, draft, status);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, who, action, detail, ts);
"""
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
def q(sql, a=(), one=False, commit=False):
    c = db(); cur = c.execute(sql, a); r = cur.fetchone() if one else cur.fetchall()
    if commit: c.commit()
    lid = cur.lastrowid; c.close(); return lid if commit else r
def now(): return dt.datetime.now().isoformat(timespec="seconds")
q_init = lambda: db().executescript(SCHEMA)
q_init()

# ---------- state machine ----------
ALLOWED = {"NEW": {"ACKNOWLEDGED"}, "ACKNOWLEDGED": {"CLASSIFIED"}, "CLASSIFIED": {"SCORED"}, "SCORED": {"EXTRACTED"},
 "EXTRACTED": {"PROPOSAL_DRAFTED"}, "PROPOSAL_DRAFTED": {"PENDING_APPROVAL"},
 "PENDING_APPROVAL": {"APPROVED", "REJECTED", "REVISION_REQUESTED"}, "REVISION_REQUESTED": {"PROPOSAL_DRAFTED"},
 "APPROVED": {"PROPOSAL_SENT"}, "PROPOSAL_SENT": {"FOLLOW_UP_ACTIVE"}, "FOLLOW_UP_ACTIVE": {"WON", "LOST", "ARCHIVED"},
 "REJECTED": {"ARCHIVED", "LOST"}, "FAILED": {"NEW"}}
def transition(lid, to, actor="system", reason=""):
    cur = q("SELECT status FROM leads WHERE id=?", (lid,), one=True)["status"]
    if to != "FAILED" and to not in ALLOWED.get(cur, set()):
        raise ValueError(f"Invalid transition {cur} -> {to}")
    q("UPDATE leads SET status=? WHERE id=?", (to, lid), commit=True)
    q("INSERT INTO history(lead_id,from_state,to_state,actor,reason,ts) VALUES(?,?,?,?,?,?)", (lid, cur, to, actor, reason, now()), commit=True)
def audit(who, action, detail=""): q("INSERT INTO audit(who,action,detail,ts) VALUES(?,?,?,?)", (who, action, detail, now()), commit=True)

# ---------- email (simulated unless SMTP_HOST set); idempotent via key ----------
def send_email(lid, to, subject, body, kind, idem):
    if q("SELECT 1 FROM outbox WHERE idem=?", (idem,), one=True): return
    status = "simulated"
    if os.getenv("SMTP_HOST"):
        m = EmailMessage(); m["From"] = os.getenv("SMTP_FROM", "sales@example.com"); m["To"] = to; m["Subject"] = subject; m.set_content(body)
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")); s.send_message(m)
        status = "sent"
    q("INSERT INTO outbox(lead_id,to_addr,subject,body,kind,status,idem,ts) VALUES(?,?,?,?,?,?,?,?)", (lid, to, subject, body, kind, status, idem, now()), commit=True)

# ---------- classification (rules; swap in an LLM behind the same function) ----------
KEYWORDS = {"web development": ["website", "web app", "web", "ecommerce", "e-commerce", "portal", "store"],
 "mobile app": ["mobile", "android", "ios", "flutter", "react native", "app store"],
 "custom software": ["erp", "crm", "inventory", "system", "software", "management", "pos"],
 "AI/automation": ["ai", "chatbot", "automation", "automate", "machine learning", "llm", "gpt"],
 "UI/UX design": ["ui", "ux", "figma", "wireframe", "redesign", "prototype"],
 "maintenance/support": ["maintenance", "support", "bug", "fix", "hosting"], "consulting": ["consult", "advice", "audit", "architecture"]}
FEATURES = {"payment": "Payment gateway integration", "login": "User authentication", "admin": "Admin panel", "dashboard": "Analytics dashboard",
 "report": "Reporting module", "chat": "Chat / messaging", "notification": "Notifications", "api": "Third-party API integration",
 "inventory": "Inventory module", "booking": "Booking / scheduling", "search": "Search & filtering", "multi-language": "Multi-language support"}
FEATURE_HOURS = {"Payment gateway integration": 24, "User authentication": 16, "Admin panel": 40, "Analytics dashboard": 32, "Reporting module": 24,
 "Chat / messaging": 40, "Notifications": 12, "Third-party API integration": 24, "Inventory module": 40, "Booking / scheduling": 32,
 "Search & filtering": 12, "Multi-language support": 16}
BASE_HOURS = {"web development": [("Discovery & UI design", 24), ("Frontend development", 60), ("Backend development", 60), ("QA & deployment", 24)],
 "mobile app": [("Discovery & UI design", 32), ("App development (cross-platform)", 120), ("Backend & API", 60), ("QA & store release", 32)],
 "custom software": [("Analysis & design", 40), ("Core development", 140), ("Testing & deployment", 40)],
 "AI/automation": [("Use-case analysis & data review", 24), ("Model/LLM workflow development", 80), ("Integration & testing", 40)],
 "UI/UX design": [("User research", 20), ("Wireframes & prototype", 40), ("Visual design & handoff", 30)],
 "maintenance/support": [("Monthly maintenance & support (retainer)", 40)], "consulting": [("Consulting sessions & written report", 20)],
 "other": [("Scoping & estimation", 16), ("Development", 80), ("QA & deployment", 24)]}

def parse_budget(t):
    t = (t or "").lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|m)?", t)
    if not m: return None
    v = float(m.group(1)) * {"k": 1e3, "m": 1e6}.get(m.group(2), 1)
    cur = next((k for k in FX if k in t), "usd"); return round(v * FX[cur])
def parse_days(t):
    m = re.search(r"(\d+)\s*(day|week|month)", (t or "").lower())
    return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)] if m else 0
def classify(lead):
    text = f"{lead['description']} {lead['service_interest'] or ''}".lower()
    sc = {s: sum(len(re.findall(r"\b" + re.escape(k) + r"\b", text)) for k in ks) for s, ks in KEYWORDS.items()}
    tot = sum(sc.values()); best = max(sc, key=sc.get)
    if lead["service_interest"] in scoring.SERVICES: svc, conf = lead["service_interest"], .9
    elif tot == 0: svc, conf = "other", .3
    else: svc, conf = best, round(sc[best] / tot * min(1, .5 + .15 * sc[best]), 2)
    days = parse_days(lead["timeline_text"]); urgent = re.search(r"urgent|asap|immediately|right away", text + (lead["timeline_text"] or "").lower())
    urg = "Critical" if urgent or (0 < days <= 14) else "High" if 0 < days <= 30 else "Medium" if 0 < days <= 90 else "Low"
    usd = parse_budget(lead["budget_text"])
    band = "Not stated" if not usd else "< $1k" if usd < 1000 else "$1k-$5k" if usd < 5000 else "$5k-$20k" if usd < 20000 else "$20k+"
    return svc, conf, urg, band, usd or 0, days

def extract(lead, svc, usd, days):
    text = lead["description"].lower()
    feats = [v for k, v in FEATURES.items() if k in text]
    plats = [p for p, ks in {"Web": ["web", "website"], "iOS": ["ios", "iphone"], "Android": ["android"], "API": ["api"]}.items() if any(k in text for k in ks)]
    missing = []
    if not usd: missing.append(("budget", "Do you have a budget range in mind for this project?"))
    if not days: missing.append(("timeline", "What is your desired launch date or timeline?"))
    if not feats: missing.append(("features", "Could you list the 3-5 most important features you need?"))
    if svc in ("mobile app",) and not plats: missing.append(("platforms", "Do you need iOS, Android, or both?"))
    flags = [f for f, c in {"Very short description (vague scope)": len(text) < 60, "Unrealistic budget for scope": 0 < usd < 300,
             "Possible spam": bool(re.search(r"http[s]?://\S+.*http[s]?://", text))}.items() if c]
    return {"customer_name": lead["name"], "company": lead["company"], "contact_email": lead["email"], "service_type": svc,
            "project_summary": lead["description"][:500], "required_features": feats, "platforms": plats,
            "timeline_days": days or None, "budget_usd": usd or None, "budget_stated": bool(usd),
            "missing_information": [{"field": f, "question": qn} for f, qn in missing], "red_flags": flags,
            "confidence": round(1 - .15 * len(missing), 2)}

# ---------- proposal (catalogue-grounded: no invented prices) ----------
def build_proposal(lead, req, discount=0.0, note=""):
    items = [(d, h, "catalogue") for d, h in BASE_HOURS[req["service_type"]]]
    items += [(f, FEATURE_HOURS[f], "catalogue (detected feature)") for f in req["required_features"]]
    lines = [{"description": d, "hours": h, "rate": RATE, "amount": round(h * RATE, 2), "basis": b,
              "low_confidence": not req["budget_stated"] and b != "catalogue"} for d, h, b in items]
    sub = sum(l["amount"] for l in lines); disc = round(sub * discount / 100, 2); total = round(sub - disc, 2)
    weeks = max(2, math.ceil(sum(l["hours"] for l in lines) / 60))
    return {"title": f"Proposal: {req['service_type'].title()} for {lead['company'] or lead['name']}",
        "understanding": f"{lead['name']} needs: {req['project_summary']}",
        "solution": f"We will deliver a {req['service_type']} solution covering: " + (", ".join(req["required_features"]) or "the scope agreed after discovery") + ".",
        "assumptions": ["Pricing is an estimate from our rate card and may change after discovery.", "Content and third-party licences supplied by the client.",
                        "Two feedback rounds per milestone."] + [f"Open item: {m['field']} not provided." for m in req["missing_information"]],
        "exclusions": ["Hosting and third-party fees", "Post-launch support beyond 30 days (available as a retainer)"],
        "timeline": f"Estimated {weeks} weeks from kickoff, in milestone-based delivery.", "lines": lines, "subtotal": sub,
        "discount_pct": discount, "discount": disc, "total": total, "currency": "USD",
        "payment_terms": "40% advance, 40% on milestone completion, 20% on delivery.", "validity": "Valid for 30 days.",
        "next_steps": "Reply to confirm, or book a call to refine the scope.", "revision_note": note}

def make_pdf(pid, c, lead):
    from reportlab.lib.pagesizes import A4; from reportlab.lib import colors; from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    st = getSampleStyleSheet(); path = PDF_DIR / f"proposal_{pid}.pdf"; el = [Paragraph(c["title"], st["Title"])]
    P = lambda h, t: el.extend([Paragraph(h, st["Heading3"]), Paragraph(t, st["BodyText"])])
    P("Understanding of your requirement", c["understanding"]); P("Proposed solution", c["solution"]); P("Timeline", c["timeline"])
    rows = [["Item", "Hours", "Rate", "Amount"]] + [[l["description"], l["hours"], f"${l['rate']:.0f}", f"${l['amount']:,.2f}"] for l in c["lines"]]
    rows += [["", "", "Subtotal", f"${c['subtotal']:,.2f}"]] + ([["", "", f"Discount {c['discount_pct']}%", f"-${c['discount']:,.2f}"]] if c["discount"] else []) + [["", "", "TOTAL", f"${c['total']:,.2f}"]]
    t = Table(rows, colWidths=[260, 50, 70, 80]); t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), .3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9), ("ALIGN", (1, 0), (-1, -1), "RIGHT")]))
    el += [Paragraph("Quotation", st["Heading3"]), t]
    P("Assumptions", "<br/>".join("- " + a for a in c["assumptions"])); P("Exclusions", "<br/>".join("- " + a for a in c["exclusions"]))
    P("Payment terms", c["payment_terms"] + " " + c["validity"]); P("Next steps", c["next_steps"])
    SimpleDocTemplate(str(path), pagesize=A4).build(el); return str(path)

def draft_proposal(lid, discount=0.0, note=""):
    lead = q("SELECT * FROM leads WHERE id=?", (lid,), one=True); req = json.loads(lead["requirements"])
    ver = (q("SELECT MAX(version) v FROM proposals WHERE lead_id=?", (lid,), one=True)["v"] or 0) + 1
    c = build_proposal(lead, req, discount, note)
    pid = q("INSERT INTO proposals(lead_id,version,content,total,status,created_at) VALUES(?,?,?,?,?,?)", (lid, ver, json.dumps(c), c["total"], "PENDING_APPROVAL", now()), commit=True)
    make_pdf(pid, c, lead); return pid

# ---------- pipeline ----------
FOLLOWUP_DAYS = {"Hot": [2, 5, 9], "Warm": [3, 7, 14], "Cold": [7, 21]}
def run_pipeline(lid):
    try:
        lead = q("SELECT * FROM leads WHERE id=?", (lid,), one=True)
        send_email(lid, lead["email"], "We received your inquiry", f"Hi {lead['name']},\n\nThank you for contacting us. We received your inquiry and will send a tailored proposal shortly.\n\nRegards,\nSales Team", "ack", f"ack-{lid}")
        transition(lid, "ACKNOWLEDGED", reason="acknowledgement email sent")
        svc, conf, urg, band, usd, days = classify(lead)
        q("UPDATE leads SET service_type=?,class_conf=?,urgency=?,budget_band=?,needs_review=? WHERE id=?", (svc, conf, urg, band, int(conf < .6), lid), commit=True)
        transition(lid, "CLASSIFIED", reason=f"confidence {conf}" + (" - flagged for review" if conf < .6 else ""))
        free = lead["email"].split("@")[-1].lower() in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com")
        req = extract(lead, svc, usd, days)
        feats = {"service_type": svc, "source": lead["source"], "budget_usd": usd, "urgency_num": ["Low", "Medium", "High", "Critical"].index(urg),
                 "desc_len": len(lead["description"]), "n_features": len(req["required_features"]), "has_phone": int(bool(lead["phone"])),
                 "corporate_email": int(not free), "timeline_days": days, "has_company": int(bool(lead["company"]))}
        s = scoring.score(feats)
        q("UPDATE leads SET score=?,tier=?,top_factors=?,model_version=? WHERE id=?", (s["score"], s["tier"], json.dumps(s["top_factors"]), s["model_version"], lid), commit=True)
        transition(lid, "SCORED", reason=f"{s['score']} {s['tier']} ({s['model_version']})")
        q("UPDATE leads SET requirements=? WHERE id=?", (json.dumps(req), lid), commit=True)
        transition(lid, "EXTRACTED", reason=f"{len(req['missing_information'])} missing fields")
        if req["missing_information"]:
            body = f"Hi {lead['name']},\n\nTo prepare an accurate proposal, could you help with:\n" + "\n".join(f"{i+1}. {m['question']}" for i, m in enumerate(req["missing_information"])) + "\n\nThank you!"
            q("INSERT OR IGNORE INTO outbox(lead_id,to_addr,subject,body,kind,status,idem,ts) VALUES(?,?,?,?,?,?,?,?)", (lid, lead["email"], "A few questions about your project", body, "clarification", "DRAFT - needs approval", f"clar-{lid}", now()), commit=True)
        draft_proposal(lid); transition(lid, "PROPOSAL_DRAFTED"); transition(lid, "PENDING_APPROVAL", reason="awaiting manager approval")
    except Exception as e:
        q("UPDATE leads SET status='FAILED' WHERE id=?", (lid,), commit=True)
        q("INSERT INTO history(lead_id,from_state,to_state,actor,reason,ts) VALUES(?,?,?,?,?,?)", (lid, "?", "FAILED", "system", repr(e), now()), commit=True)

def create_lead(d, source):
    if not d.get("name") or not re.match(r"[^@\s]+@[^@\s]+\.\w+", d.get("email", "")) or not d.get("description"): raise ValueError("name, valid email and description are required")
    dup = q("SELECT id FROM leads WHERE email=? AND description=?", (d["email"], d["description"]), one=True)
    if dup: return dup["id"]
    lid = q("INSERT INTO leads(name,email,phone,company,service_interest,description,budget_text,timeline_text,source,raw,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["name"], d["email"], d.get("phone", ""), d.get("company", ""), d.get("service_interest", ""), d["description"], d.get("budget", ""), d.get("timeline", ""), source, json.dumps(d), "NEW", now()), commit=True)
    q("INSERT INTO history(lead_id,from_state,to_state,actor,reason,ts) VALUES(?,?,?,?,?,?)", (lid, None, "NEW", "customer", source, now()), commit=True)
    run_pipeline(lid); return lid

def decide(lid, decision, user, comment="", discount=0.0):
    p = q("SELECT * FROM proposals WHERE lead_id=? ORDER BY version DESC", (lid,), one=True); lead = q("SELECT * FROM leads WHERE id=?", (lid,), one=True)
    audit(user, f"proposal_{decision}", f"lead={lid} proposal_v={p['version']} {comment}")
    q("UPDATE proposals SET status=?,approved_by=?,comment=? WHERE id=?", (decision.upper(), user, comment, p["id"]), commit=True)
    if decision == "approved":
        transition(lid, "APPROVED", user); c = json.loads(p["content"])
        send_email(lid, lead["email"], c["title"], f"Hi {lead['name']},\n\nPlease find our proposal attached (proposals/proposal_{p['id']}.pdf). Total: ${c['total']:,.2f}.\n\nRegards,\nSales Team", "proposal", f"prop-{p['id']}")
        transition(lid, "PROPOSAL_SENT", "system"); transition(lid, "FOLLOW_UP_ACTIVE", "system", f"{lead['tier']} sequence")
        for i, d in enumerate(FOLLOWUP_DAYS.get(lead["tier"], [7]), 1):
            q("INSERT INTO followups(lead_id,step,due_at,draft,status) VALUES(?,?,?,?,?)", (lid, i, (dt.date.today() + dt.timedelta(days=d)).isoformat(),
              f"Hi {lead['name']}, following up on the proposal we sent for your {lead['service_type']} project. Happy to answer questions or schedule a call.", "scheduled"), commit=True)
    elif decision == "rejected": transition(lid, "REJECTED", user, comment)
    else:
        transition(lid, "REVISION_REQUESTED", user, comment); draft_proposal(lid, discount, comment)
        transition(lid, "PROPOSAL_DRAFTED", "system"); transition(lid, "PENDING_APPROVAL", "system", "revised version")

# ---------- API ----------
@app.get("/health")
def health():
    try: scoring._load(); ml = "ok"
    except Exception as e: ml = f"error: {e}"
    return jsonify(status="ok", db="ok", ml=ml)
@app.post("/api/v1/leads")
def api_leads():
    try: return jsonify(id=create_lead(request.get_json(force=True), "api")), 201
    except ValueError as e: return jsonify(error=str(e)), 400
@app.post("/api/v1/score")
def api_score(): return jsonify(scoring.score(request.get_json(force=True)))
@app.get("/api/v1/leads")
def api_list():
    if "user" not in session: abort(401)
    return jsonify([dict(r) for r in q("SELECT id,name,company,status,service_type,urgency,score,tier FROM leads ORDER BY id DESC")])

# ---------- UI ----------
CSS = "<style>body{font:15px system-ui;margin:0;background:#f4f6f9}nav{background:#1f3a5f;padding:12px 24px}nav a{color:#fff;margin-right:18px;text-decoration:none}main{max-width:1000px;margin:24px auto;padding:0 16px}.card{background:#fff;border-radius:8px;padding:18px;margin-bottom:16px;box-shadow:0 1px 3px #0002}table{width:100%;border-collapse:collapse}td,th{padding:7px;border-bottom:1px solid #eee;text-align:left;font-size:14px}input,textarea,select{width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box}button{padding:8px 16px;border:0;border-radius:5px;background:#1f3a5f;color:#fff;cursor:pointer}.Hot{color:#c0392b;font-weight:600}.Warm{color:#d68910;font-weight:600}.Cold{color:#2471a3;font-weight:600}.tag{background:#e8eef6;padding:2px 8px;border-radius:10px;font-size:12px}pre{white-space:pre-wrap;background:#f4f6f9;padding:10px;border-radius:6px}</style>"
LAY = CSS + """<nav><a href="/">Inquiry form</a>{% if session.user %}<a href="/dashboard">Dashboard</a><a href="/outbox">Outbox</a><a href="/logout">Logout ({{session.user}})</a>{% else %}<a href="/login">Staff login</a>{% endif %}</nav><main>"""
def page(body, **kw): return render_template_string(LAY + body + "</main>", **kw)
def login_required(f):
    from functools import wraps
    @wraps(f)
    def w(*a, **k): return f(*a, **k) if "user" in session else redirect(url_for("login"))
    return w

@app.route("/", methods=["GET", "POST"])
def form():
    if request.method == "POST":
        if request.form.get("website"): return "OK"  # honeypot
        try: create_lead({k: v.strip() for k, v in request.form.items()}, "web_form")
        except ValueError as e: return page("<div class=card>Error: {{e}}</div>", e=e), 400
        return page("<div class=card><h2>Thank you!</h2>We received your inquiry. A confirmation email is on its way and we will send a proposal soon.</div>")
    return page("""<div class=card><h2>Tell us about your project</h2><form method=post>
 Name*<input name=name required> Email*<input name=email type=email required> Phone<input name=phone> Company<input name=company>
 Service<select name=service_interest><option value="">Not sure</option>{% for s in services %}<option>{{s}}</option>{% endfor %}</select>
 Project description*<textarea name=description rows=6 required></textarea> Budget (e.g. $5000, 300k PKR)<input name=budget> Timeline (e.g. 2 months)<input name=timeline>
 <input name=website style="display:none" tabindex=-1 autocomplete=off><button>Submit inquiry</button></form></div>""", services=scoring.SERVICES)

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        u, p = request.form.get("u", "").strip(), request.form.get("p", "").strip()
        if u == os.getenv("DASH_USER", "admin") and p == os.getenv("DASH_PASS", "admin123"):
            session["user"] = u
            return redirect("/dashboard")
        err = "Wrong username or password"
    return page("<div class=card><h2>Staff login</h2><p style='color:#c0392b'>{{err}}</p><form method=post>User<input name=u>Password<input name=p type=password><button>Login</button></form></div>", err=err)
@app.get("/logout")
def logout(): session.clear(); return redirect("/")

@app.get("/dashboard")
@login_required
def dashboard():
    tier, st = request.args.get("tier", ""), request.args.get("status", "")
    rows = q("SELECT * FROM leads WHERE (?='' OR tier=?) AND (?='' OR status=?) ORDER BY id DESC", (tier, tier, st, st))
    stats = q("SELECT status, COUNT(*) n FROM leads GROUP BY status")
    return page("""<div class=card>{% for s in stats %}<span class=tag>{{s.status}}: {{s.n}}</span> {% endfor %}
<form style="display:flex;gap:8px;margin-top:12px"><select name=tier><option value="">All tiers</option>{% for t in ['Hot','Warm','Cold'] %}<option>{{t}}</option>{% endfor %}</select>
<select name=status><option value="">All statuses</option>{% for s in ['PENDING_APPROVAL','FOLLOW_UP_ACTIVE','REJECTED','FAILED','WON','LOST'] %}<option>{{s}}</option>{% endfor %}</select><button>Filter</button></form></div>
<div class=card><table><tr><th>#<th>Customer<th>Service<th>Urgency<th>Score<th>Status</tr>{% for r in rows %}
<tr><td><a href="/lead/{{r.id}}">{{r.id}}</a><td>{{r.name}}{% if r.company %} ({{r.company}}){% endif %}<td>{{r.service_type}}{% if r.needs_review %} ⚑{% endif %}<td>{{r.urgency}}
<td class="{{r.tier}}">{{r.score}} {{r.tier}}<td><span class=tag>{{r.status}}</span></tr>{% endfor %}</table></div>""", rows=rows, stats=stats)

@app.get("/lead/<int:lid>")
@login_required
def lead(lid):
    L = q("SELECT * FROM leads WHERE id=?", (lid,), one=True) or abort(404)
    P = q("SELECT * FROM proposals WHERE lead_id=? ORDER BY version DESC", (lid,)); cur = json.loads(P[0]["content"]) if P else None
    return page("""<div class=card><h2>{{L.name}} <span class=tag>{{L.status}}</span></h2>{{L.email}} | {{L.company}} | source: {{L.source}}
<pre>{{L.description}}</pre>Service: <b>{{L.service_type}}</b> (confidence {{L.class_conf}}){% if L.needs_review %} ⚑ low confidence - review{% endif %} | Urgency: <b>{{L.urgency}}</b> | Budget: <b>{{L.budget_band}}</b></div>
<div class=card><h3>Score: <span class="{{L.tier}}">{{L.score}} - {{L.tier}}</span> <small>model {{L.model_version}}</small></h3>
{% for f in factors %}<div>{{f.feature}} = {{f.value}} &rarr; {{'%+.1f'|format(f.impact_points)}} pts</div>{% endfor %}</div>
<div class=card><h3>Extracted requirements</h3><pre>{{req}}</pre></div>
{% if cur %}<div class=card><h3>Proposal v{{P[0].version}} - ${{'{:,.2f}'.format(cur.total)}} <small>({{P[0].status}})</small> <a href="/proposal/{{P[0].id}}.pdf">PDF</a></h3>
<table>{% for l in cur.lines %}<tr><td>{{l.description}}<td>{{l.hours}}h<td>${{'{:,.0f}'.format(l.amount)}}</tr>{% endfor %}</table>
{% if L.status=='PENDING_APPROVAL' %}<form method=post action="/lead/{{L.id}}/decision" style="margin-top:12px">Comment<input name=comment>Discount % (for revision)<input name=discount value=0>
<button name=d value=approved>Approve &amp; send</button> <button name=d value=revision style="background:#d68910">Request revision</button> <button name=d value=rejected style="background:#c0392b">Reject</button></form>{% endif %}</div>{% endif %}
{% if L.status=='FOLLOW_UP_ACTIVE' %}<div class=card><h3>Follow-ups</h3>{% for f in fu %}<div>Step {{f.step}} due {{f.due_at}} ({{f.status}})</div>{% endfor %}
<form method=post action="/lead/{{L.id}}/outcome">Reason<input name=reason><button name=o value=WON>Mark Won</button> <button name=o value=LOST style="background:#c0392b">Mark Lost</button></form></div>{% endif %}
<div class=card><h3>Timeline</h3><table>{% for h in hist %}<tr><td>{{h.ts}}<td>{{h.from_state}} &rarr; {{h.to_state}}<td>{{h.actor}}<td>{{h.reason}}</tr>{% endfor %}</table></div>""",
        L=L, P=P, cur=cur, factors=json.loads(L["top_factors"] or "[]"), req=json.dumps(json.loads(L["requirements"] or "{}"), indent=2),
        fu=q("SELECT * FROM followups WHERE lead_id=?", (lid,)), hist=q("SELECT * FROM history WHERE lead_id=? ORDER BY id", (lid,)))

@app.post("/lead/<int:lid>/decision")
@login_required
def lead_decision(lid):
    try: decide(lid, request.form["d"], session["user"], request.form.get("comment", ""), float(request.form.get("discount") or 0))
    except ValueError as e: return str(e), 400
    return redirect(f"/lead/{lid}")
@app.post("/lead/<int:lid>/outcome")
@login_required
def lead_outcome(lid):
    o = request.form["o"]; transition(lid, o, session["user"], request.form.get("reason", ""))
    q("UPDATE leads SET outcome=?,outcome_reason=? WHERE id=?", (o, request.form.get("reason", ""), lid), commit=True)
    q("UPDATE followups SET status='stopped' WHERE lead_id=?", (lid,), commit=True); audit(session["user"], f"outcome_{o}", f"lead={lid}")
    return redirect(f"/lead/{lid}")
@app.get("/proposal/<int:pid>.pdf")
@login_required
def pdf(pid): return send_file(PDF_DIR / f"proposal_{pid}.pdf")
@app.get("/outbox")
@login_required
def outbox():
    return page("""<div class=card><h2>Outbox (emails)</h2><table><tr><th>To<th>Subject<th>Kind<th>Status</tr>{% for m in rows %}<tr><td>{{m.to_addr}}<td>{{m.subject}}<td>{{m.kind}}<td>{{m.status}}</tr>{% endfor %}</table></div>""", rows=q("SELECT * FROM outbox ORDER BY id DESC"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
