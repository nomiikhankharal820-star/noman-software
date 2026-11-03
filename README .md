# AI Lead-to-Sale MVP

Intake -> classification -> ML scoring -> requirement extraction -> proposal + PDF -> manager approval -> follow-ups -> outcome.

## Run
    pip install -r requirements.txt
    python scoring.py train      # trains on SYNTHETIC data (already included in models/)
    python app.py                # http://localhost:5000

- Public inquiry form: `/`  | Staff dashboard: `/dashboard` (login `admin` / `admin123`; set `DASH_USER`, `DASH_PASS`, `SECRET_KEY`)
- API: `POST /api/v1/leads`, `POST /api/v1/score`, `GET /api/v1/leads`, `GET /health`
- Real data: `python scoring.py train leads.csv` (columns: service_type, source, budget_usd, urgency_num, desc_len, n_features, has_phone, corporate_email, timeline_days, has_company, won)
- Emails are simulated (see Outbox page). Set `SMTP_HOST/PORT/USER/PASS/FROM` to send for real.
- `HOURLY_RATE_USD` sets the rate; pricing comes only from the catalogue in `app.py` (no invented prices).

## Covered from the PRD
FR-A1/A3/A4/A5/A7 intake, honeypot, duplicates, ack email, raw storage | FR-B classification + confidence flag |
FR-C scoring, tiers, top factors, model versioning | FR-D extraction + missing-info + clarification draft |
FR-E proposal, quotation, PDF, versions | FR-F approve/reject/revise, audit log | FR-G1 tier-based follow-ups |
FR-I dashboard, filters, outcome capture | FR-W1 state machine with history, idempotent emails, FAILED state.

## Not in MVP yet (next phases)
Email inbox listener, LLM provider (rules are used now; plug an LLM into `classify`/`extract`), CRM adapter,
n8n workflows, retries/dead-letter queue, reply detection, roles/SSO, analytics, Docker/Postgres.
