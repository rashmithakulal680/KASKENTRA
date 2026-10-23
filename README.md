# KASKENTRA

**When One Failure Becomes Many: Coordinating the Response When Critical City Services Fail**

KASKENTRA is a decision-support prototype for disaster-management authorities and city control rooms.
The hackathon scenario is **electricity**: a power substation fails, and KASKENTRA shows how the
failure spreads to water pumps, hospitals, shelters and mobile towers, recommends what to do first
with the crews, generators and budget available, re-plans when field reports arrive, and drafts
citizen warnings for human approval.

> All data is **simulated and illustrative**. Map locations are example points in Mangaluru, not real
> utility assets. Not validated for real emergency operations. A person always makes the decision.

## Workflow (6 steps on one screen)

1. **Infrastructure Map** - Leaflet map of 20 assets and their supply lines; click an asset for details.
2. **Simulate a Failure** - pick the substation at risk and the cause (flood, storm, heatwave, fire,
   equipment fault). Each cause has a stated assumption (`HAZARDS` in `app/simulator.py`).
3. **Impact Analysis** - affected services, backup time, when each fails and recovers, people affected,
   and an evidence ledger marking every event as reported, planned or predicted.
4. **Response Planning** - each single option scored and checked for feasibility, then the best
   combination within your crews, generators and budget (`app/planner.py`).
5. **Field Update** - reports (repair delayed, asset found failed, backup left) are checked, saved in
   the database, and trigger re-planning; the old plan is re-checked and flagged if it no longer works.
6. **Citizen-warning Drafts** - one draft per affected zone with the hours and advice; each must be
   approved or rejected by a named person, and the decision is saved. Nothing is sent automatically.

## Data and storage

- **Dataset**: `data/assets.csv` (id, type, capacity, people served, backup hours, repair hours,
  latitude, longitude, zone) and `data/dependencies.csv` (supplier, dependent, load, active).
  Edit these files to model another area; they are checked when the app starts.
- **Database**: SQLite (built into Python, nothing to install) stores field reports and warning
  drafts with their approval decisions. File: `data/kaskentra.db` (set `KASKENTRA_DB` to change it).
- **Map**: Leaflet 1.9.4 is bundled in `static/vendor/leaflet` (BSD-2-Clause licence included), so the
  map works without a CDN. The street background comes from OpenStreetMap and needs internet;
  without it, the assets and lines still show on a plain background.

## Run locally (Windows PowerShell)

```powershell
cd KASKENTRA
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Demo script

1. **Load demo: flood at North Substation** - watch the failure spread on the map.
2. Step 3 shows City Hospital on backup until hour 9 and North Telecom Tower until hour 5.
3. Step 4: **Recommend plan** (2 crews, 2 generators, &#8377;60,000).
4. Step 5: *Asset found failed*, *S3 - East Substation*, hour 3 -> **Add report** ->
   **Update scenario & re-plan**. The previous plan relied on East Substation, so it is flagged and a
   new plan is recommended.
5. Step 6: enter your name, **Generate warning drafts**, then approve or reject each draft.

## Code map

- `app/data.py` - loads and checks the CSV dataset
- `app/simulator.py` - hazard profiles, hourly rules, field reports, interventions, impact
- `app/planner.py` - response planning under limits and re-check of the previous plan
- `app/alerts.py` - citizen-warning drafts per zone
- `app/store.py` - SQLite storage for field reports and warning approvals
- `app/main.py` - API: `/api/network`, `/api/hazards`, `/api/simulate`, `/api/compare`, `/api/plan`,
  `/api/reports`, `/api/alerts/draft`, `/api/alerts`, `/api/alerts/{id}/decision`
- `static/index.html` - dashboard (HTML + JavaScript + Leaflet)
- `tests/` - pytest suite

Deployment: see [DEPLOY.md](DEPLOY.md). On Vercel the database is kept in `/tmp`, which is temporary.

## Limitations

Simulated data, simplified rules, template explanations (no LLM), one city network.
