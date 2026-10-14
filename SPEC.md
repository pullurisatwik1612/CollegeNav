# SPEC: CollegeNav (DejaVu Hackathon, CampusSolve track, problem 05)

## Problem statement
Students struggle to identify suitable college bus routes, find nearby stops, understand route coverage, and get timely information about route changes or disruptions. Without one transport information system they rely on outdated schedules. Build a college transport navigation platform that helps students discover routes and reliable information about buses and stops, lets transport administrators manage route details and communicate service changes, and where reliable location data exists, offers live tracking or ETAs.

Expected outcomes: (1) digital records of buses, routes, stops, schedules; (2) search by start and destination; (3) stops, route sequences, schedules; (4) suggest suitable routes; (5) notify about changes, delays, disruptions; (6) admin updates; (7) optional live tracking and arrival estimates; (8) mobile-friendly; (9) clearly identify unavailable or outdated information.

## User and pain
Students (and staff) who take college buses. They do not know which route serves their area, where to board, when the next bus comes, or whether a route changed today. Admins have no single place to publish changes.

## One-line pitch
CollegeNav tells every student which bus to take, from which stop, when it arrives, and whether the information can be trusted, in one mobile page, while transport staff update everything in seconds.

## Killer features
1. Trust layer: every route shows when it was last verified; suspended, outdated and lost-GPS states are shown honestly (outcome 09).
2. Zero-hardware live tracking: the driver's phone becomes the GPS tracker; ETAs per stop from the route timetable.
3. Instant notices: an admin publishes a delay and students get a pop-up within seconds.

## MVP (built)
Search from/to with nearest-stop and one-change suggestions | route pages with map, stops and times | live map | driver GPS page | notices with push-style alerts | admin for stops, routes, schedules, buses, notices | verified-date badges | mobile UI.

## Next (nice to have, pick by remaining time)
QR code at each stop linking to that stop's page | install as an app (PWA) | Telugu/Hindi toggle | "bus is full" or "bus arrived" student reports | route-change history | CSV import of timetable.

## Cut list (will NOT build)
Payments and passes | traffic-aware ML ETAs | native apps | user accounts for students.

## Stack
Flask + SQLite + Jinja + Tailwind (CDN) + Leaflet/OpenStreetMap. Single-file backend (`app.py`). Deploy: Render, or run on a laptop.

## Data model
stops(name, landmark, lat, lng) | routes(code, name, color, status, last_verified, notes) | route_stops(route, stop, seq, offset_min) | schedules(route, depart_time, days) | buses(reg_no, route, driver_name, driver_token) | bus_positions(bus, lat, lng, updated_ts, source) | alerts(route or all, kind, message, expires) | settings(sim_mode)
