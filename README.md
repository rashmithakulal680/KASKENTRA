# KASKENTRA

**When One Failure Becomes Many: Coordinating the Response When Critical City Services Fail**

KASKENTRA is a decision-support prototype for disaster-management authorities and city control rooms.
It shows how one infrastructure failure (for example a flooded substation) spreads to water pumps,
hospitals, shelters and mobile towers, explains every step, and compares response options.

> All data in this prototype is **simulated and illustrative**. It is not validated for real
> emergency operations. The planner always makes the final decision.

## What works today

- **Cascade simulation** of a 20-asset demo city, hour by hour, with backups, repairs and capacity checks.
- **Hazard types with stated assumptions** (flood, storm, heatwave, fire, equipment fault). Each one
  changes crew access, generator delivery, repair speed, spare capacity or backup duration
  (see `HAZARDS` in `app/simulator.py`).
- **Evidence ledger**: every state change is recorded with its cause and rule.
- **Readable impact**: peak people without service per service, which critical facilities fail and when.
- **Intervention Lab**: extra repair crew, temporary backup and alternate supply are re-simulated from
  the same starting point and ranked against "no action" with transparent weights
  (60% service-hours lost, 30% critical-facility outage, 10% cost).
- **Response planning under real limits**: enter the extra crews, generators and budget available;
  KASKENTRA tries every combination of feasible actions (up to 4) that fits, re-simulates each one and
  recommends the best plan, with its timing, resource use and cost (`app/planner.py`).
- **Field updates and re-planning**: add confirmed reports (repair delayed, asset found failed,
  backup hours left). The scenario is updated, the previous plan is re-checked under the new
  conditions (and flagged if it no longer works), and a new recommendation is shown.
- **Reported vs predicted**: the ledger marks each event as `reported` (hazard, field report),
  `planned` (action) or `predicted` (simulated consequence).
- **Demo button and Play button** for a one-click walkthrough.

## Demo flow

1. Click **Load demo: flood at North Substation** and watch the cascade play.
2. In **Response Planning**, keep 2 crews, 2 generators and &#8377;60,000, then click **Plan response**.
3. In **Field Update**, choose *Asset found failed*, *S3 - East Substation*, hour 3, click **Add report**,
   then **Update scenario & re-plan**. The previous plan relied on East Substation, so it is flagged
   as no longer possible and a new plan is recommended.

## Run locally

```powershell
cd KASKENTRA
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and click **Load demo: flood at North Substation**.

## Code map

- `app/data.py` - demo city: 20 synthetic assets, dependencies, inactive alternate links
- `app/simulator.py` - hazard profiles, hourly rules, interventions, ranking, explanation
- `app/planner.py` - response planning under crews, generators and budget; re-check of the previous plan
- `app/main.py` - API: `/api/network`, `/api/hazards`, `/api/simulate`, `/api/compare`, `/api/plan`
- `static/index.html` - dashboard (plain HTML + JavaScript, SVG map)
- `tests/` - pytest suite

Deployment (Vercel / Render / local demo): see [DEPLOY.md](DEPLOY.md).

## Limitations

Simulated data, simplified rules, template explanation (no LLM), in-memory only.
