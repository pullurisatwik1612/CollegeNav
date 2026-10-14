# CollegeNav: Intelligent College Bus Navigation
Flask + SQLite + Tailwind + Leaflet (OpenStreetMap). No paid services and no API keys needed.

## Run (2 minutes)
```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                                           # http://localhost:5000
```
Admin: http://localhost:5000/admin (password `admin123`; set `ADMIN_PASSWORD` to change).
Reset all data: stop the app, delete `collegenav.db`, start again (demo data re-seeds).

## How it covers the 9 expected outcomes
| # | Outcome | Where |
|---|---|---|
| 01 | Digital records of buses, routes, stops, schedules | /admin (Routes, Stops, Buses) |
| 02 | Search by start and destination | Home page; typing, swap, or "use my location" |
| 03 | Stops, sequence, schedule | /routes/<id>: map, stop order, departure times |
| 04 | Suggest suitable routes | Ranked by next departure; nearest-stop finder; one-change journeys |
| 05 | Notify about changes, delays | Admin > Notices; students get an in-app pop-up and optional browser notification within ~15 s |
| 06 | Admin updates | /admin: edit stops, order, minutes, times, status, notices |
| 07 | Live tracking and ETAs | /driver on a phone sends real GPS; ETAs per stop; demo simulator when no GPS |
| 08 | Mobile-friendly | Responsive UI with bottom navigation |
| 09 | Mark unavailable or outdated info | "Verified N days ago" badges, Suspended routes, "GPS lost" and "unavailable" messages |

## Replace demo data with the real college routes (do this first, ~45 min)
1. Collect from the transport office: stop names, order, approximate minutes between stops, departure times, days, bus numbers.
2. /admin > Stops: add each stop (tap the map to set the pin).
3. /admin > Routes > create route > add stops in order with "minutes after first stop" > add departure times.
4. Use "Create return route" to generate the evening direction, then set its times.
5. Delete the demo routes and stops (delete routes first, then stops).
6. Set `COLLEGE_NAME` and `DEMO=0` (hides the demo footer).
To start with an empty database: set `AUTO_SEED=0` before the first run.

## Live tracking: how it works
- Real: open `/driver` on the bus driver's phone, pick the bus, enter its PIN (/admin > Buses), tap Start. Position is sent every ~4 s. ETAs are computed by projecting the GPS point onto the route and using the minutes between stops. If no ping arrives for 90 s the app says "GPS lost" and falls back.
- Demo: /admin > Overview > Live tracking mode. "Demo loop" keeps buses moving at any time of day. The UI always labels simulated data as simulated.
- Phones only share GPS over HTTPS (localhost works on the same machine). Deploying gives HTTPS automatically.

## Deploy (backup link for judges)
1. Push to GitHub. 2. render.com > New > Web Service > the repo.
3. Build: `pip install -r requirements.txt`   Start: `gunicorn app:app`
4. Env vars: `SECRET_KEY`, `ADMIN_PASSWORD`, `COLLEGE_NAME`, `DEMO=0`.
Important: free hosting wipes the SQLite file on restart or redeploy, so the demo data re-seeds and admin edits disappear.
Make your admin edits AFTER the final deploy, and open the site once before presenting (free instances sleep). Safest plan: present from your laptop at localhost, keep the public link as backup.

## Files
`app.py` (all logic, one file) | `templates/` (UI) | `.agent/` (Antigravity rules and workflows) | `SPEC.md`, `TASKS.md`, `PITCH.md`, `MASTER_PROMPT_COLLEGENAV.md`

## Troubleshooting
- Map blank: needs internet for the OpenStreetMap tiles and Tailwind CDN. Keep a hotspot ready.
- "Use my location" does nothing: allow location in the browser; needs HTTPS or localhost.
- Port busy: `PORT=5001 python app.py`.
- Times look wrong on a server: set `TZ_NAME=Asia/Kolkata`.
