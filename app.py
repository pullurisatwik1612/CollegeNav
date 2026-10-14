"""
CollegeNav: Intelligent College Bus Navigation (Flask + SQLite + Tailwind + Leaflet).

Covers the 9 expected outcomes:
 01 digital records of buses, routes, stops, schedules   -> admin pages + SQLite
 02 search by start and destination                      -> "/" (direct + one-change routes)
 03 stops, sequence, schedule                            -> "/routes/<id>"
 04 route suggestions from transport data                -> search ranking, nearest stop, transfers
 05 notices for changes, delays, disruptions             -> alerts + live toast/push in browser
 06 admin updates                                        -> "/admin"
 07 optional live tracking and ETAs                      -> driver GPS page + simulator fallback
 08 mobile-friendly                                      -> responsive UI with bottom nav
 09 flag unavailable / outdated info                     -> verified-date badges, suspended, GPS-lost notices

Run:  pip install -r requirements.txt && python app.py   (http://localhost:5000)
Admin: /admin  (password from ADMIN_PASSWORD, default admin123)
"""
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))
except Exception:  # tzdata missing -> fixed IST offset
    TZ = timezone(timedelta(hours=5, minutes=30))

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "collegenav.db"))

# ----------------------------- EDIT HERE ------------------------------------
APP_NAME = os.environ.get("APP_NAME", "CollegeNav")
COLLEGE_NAME = os.environ.get("COLLEGE_NAME", "Your College")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DEMO = os.environ.get("DEMO", "1") == "1"
GPS_FRESH_SEC = 90          # a GPS ping older than this is treated as lost
VERIFY_OK_DAYS = 14         # route info verified within this many days = fresh
VERIFY_WARN_DAYS = 30       # older than this = clearly outdated
WALK_M_PER_MIN = 80
TRANSFER_BUFFER_MIN = 3
# -----------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stops(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, landmark TEXT,
  lat REAL NOT NULL, lng REAL NOT NULL);
CREATE TABLE IF NOT EXISTS routes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#2563eb', status TEXT NOT NULL DEFAULT 'active',
  last_verified TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS route_stops(
  id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL, stop_id INTEGER NOT NULL,
  seq INTEGER NOT NULL, offset_min INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL, depart_time TEXT NOT NULL,
  days TEXT NOT NULL DEFAULT '1,2,3,4,5,6');
CREATE TABLE IF NOT EXISTS buses(
  id INTEGER PRIMARY KEY AUTOINCREMENT, reg_no TEXT NOT NULL, capacity INTEGER DEFAULT 50,
  route_id INTEGER, driver_name TEXT, driver_token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bus_positions(
  bus_id INTEGER PRIMARY KEY, lat REAL, lng REAL, speed REAL, updated_ts REAL, source TEXT);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER, kind TEXT NOT NULL, message TEXT NOT NULL,
  created_ts REAL NOT NULL, expires_ts REAL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
ALERT_KINDS = ["delay", "cancelled", "diversion", "info"]
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


# ------------------------------- time/format --------------------------------
def local_now():
    return datetime.now(TZ)


def parse_hhmm(s):
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def clock(m):
    m = int(round(m)) % 1440
    h, mi = divmod(m, 60)
    return f"{(h % 12) or 12}:{mi:02d} {'AM' if h < 12 else 'PM'}"


def ago(ts):
    s = max(0, int(time.time() - ts))
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def freshness(last_verified):
    """Outcome 09: tell users how trustworthy the route info is."""
    if not last_verified:
        return {"level": "old", "text": "Never verified: may be outdated", "days": None}
    try:
        d = datetime.strptime(last_verified, "%Y-%m-%d").date()
    except ValueError:
        return {"level": "old", "text": "Verification date unknown", "days": None}
    days = (local_now().date() - d).days
    if days <= 1:
        return {"level": "ok", "text": "Verified today" if days <= 0 else "Verified yesterday", "days": days}
    if days <= VERIFY_OK_DAYS:
        return {"level": "ok", "text": f"Verified {days} days ago", "days": days}
    if days <= VERIFY_WARN_DAYS:
        return {"level": "warn", "text": f"Last verified {days} days ago", "days": days}
    return {"level": "old", "text": f"May be outdated: last verified {days} days ago", "days": days}


# ------------------------------- DB helpers ---------------------------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_e):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def get_setting(key, default=None):
    r = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    db().commit()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
    conn.close()
    if n == 0 and os.environ.get("AUTO_SEED", "1") == "1":
        seed()


# ------------------------------- geometry -----------------------------------
def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_route_stops(route_id):
    rows = db().execute(
        "SELECT s.id AS stop_id, s.name, s.lat, s.lng, rs.seq, rs.offset_min FROM route_stops rs "
        "JOIN stops s ON s.id=rs.stop_id WHERE rs.route_id=? ORDER BY rs.seq", (route_id,)).fetchall()
    return [dict(r) for r in rows]


def pos_at_offset(stops, off):
    if off <= stops[0]["offset_min"]:
        return stops[0]["lat"], stops[0]["lng"]
    for a, b in zip(stops, stops[1:]):
        if a["offset_min"] <= off <= b["offset_min"]:
            span = b["offset_min"] - a["offset_min"]
            f = (off - a["offset_min"]) / span if span else 0
            return a["lat"] + (b["lat"] - a["lat"]) * f, a["lng"] + (b["lng"] - a["lng"]) * f
    return stops[-1]["lat"], stops[-1]["lng"]


def offset_from_position(stops, lat, lng):
    """Project a GPS point onto the route and return the matching schedule offset (minutes)."""
    best = (float("inf"), 0.0)
    k = math.cos(math.radians(lat))
    for a, b in zip(stops, stops[1:]):
        ax, ay, bx, by, px, py = a["lng"] * k, a["lat"], b["lng"] * k, b["lat"], lng * k, lat
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        t = 0 if ll == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / ll))
        d = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2
        if d < best[0]:
            best = (d, a["offset_min"] + t * (b["offset_min"] - a["offset_min"]))
    return best[1]


# ------------------------------- live tracking ------------------------------
def sim_offset(route_id, stops, mode):
    total = stops[-1]["offset_min"]
    if total <= 0:
        return None
    n = local_now()
    nowm = n.hour * 60 + n.minute + n.second / 60
    if mode == "loop":  # demo: bus continuously loops so judges always see movement
        return (nowm + route_id * 7) % total
    for r in db().execute("SELECT depart_time, days FROM schedules WHERE route_id=?", (route_id,)):
        if str(n.isoweekday()) in r["days"].split(","):
            start = parse_hhmm(r["depart_time"])
            if start <= nowm <= start + total:
                return nowm - start
    return None


def build_live(stops, off, source, reg, lat, lng, age):
    etas = []
    for s in stops:
        e = s["offset_min"] - off
        etas.append({"stop_id": s["stop_id"], "name": s["name"], "eta_min": None if e < -0.5 else max(0, round(e))})
    return {"available": True, "source": source,
            "label": "Live GPS" if source == "gps" else "Simulated (demo data)",
            "bus": reg, "lat": lat, "lng": lng, "offset": round(off, 1), "age_sec": age, "etas": etas}


def live_for_route(route_id):
    route = db().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    stops = get_route_stops(route_id)
    if not route or len(stops) < 2:
        return {"available": False, "message": "Route has no stops yet."}
    if route["status"] != "active":
        return {"available": False, "message": "Route suspended: no live data."}
    buses = db().execute(
        "SELECT b.*, p.lat, p.lng, p.updated_ts, p.source FROM buses b "
        "LEFT JOIN bus_positions p ON p.bus_id=b.id WHERE b.route_id=?", (route_id,)).fetchall()
    stale_msg = None
    for b in buses:
        if b["updated_ts"] and b["source"] == "gps":
            age = time.time() - b["updated_ts"]
            if age <= GPS_FRESH_SEC:
                return build_live(stops, offset_from_position(stops, b["lat"], b["lng"]), "gps",
                                  b["reg_no"], b["lat"], b["lng"], int(age))
            stale_msg = f"GPS from {b['reg_no']} was lost {int(age // 60)} min ago."
    mode = get_setting("sim_mode", "loop")
    if mode != "off":
        off = sim_offset(route_id, stops, mode)
        if off is not None:
            lat, lng = pos_at_offset(stops, off)
            return build_live(stops, off, "sim", buses[0]["reg_no"] if buses else "Demo bus", lat, lng, 0)
    return {"available": False, "message": (stale_msg + " " if stale_msg else "") +
            "Live location unavailable. Showing scheduled times only."}


# ------------------------------- alerts -------------------------------------
def active_alerts(route_id=None):
    sql = ("SELECT a.*, r.code AS route_code FROM alerts a LEFT JOIN routes r ON r.id=a.route_id "
           "WHERE a.active=1 AND (a.expires_ts IS NULL OR a.expires_ts>?)")
    args = [time.time()]
    if route_id is not None:
        sql += " AND (a.route_id IS NULL OR a.route_id=?)"
        args.append(route_id)
    out = []
    for r in db().execute(sql + " ORDER BY a.id DESC", args):
        d = dict(r)
        d["age"] = ago(r["created_ts"])
        out.append(d)
    return out


# ------------------------------- search -------------------------------------
def nearest_stops(lat, lng, n=5):
    rows = db().execute("SELECT * FROM stops").fetchall()
    out = []
    for s in rows:
        d = haversine_m(lat, lng, s["lat"], s["lng"])
        routes = db().execute(
            "SELECT DISTINCT r.id, r.code, r.color FROM route_stops rs JOIN routes r ON r.id=rs.route_id "
            "WHERE rs.stop_id=? AND r.status='active' ORDER BY r.code", (s["id"],)).fetchall()
        out.append({"id": s["id"], "name": s["name"], "landmark": s["landmark"], "lat": s["lat"], "lng": s["lng"],
                    "distance_m": int(d), "walk_min": max(1, round(d / WALK_M_PER_MIN)),
                    "routes": [dict(r) for r in routes]})
    out.sort(key=lambda x: x["distance_m"])
    return out[:n]


def resolve_stop(text):
    t = (text or "").strip()
    if not t:
        return None
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$", t)
    if m:
        ns = nearest_stops(float(m.group(1)), float(m.group(2)), 1)
        return db().execute("SELECT * FROM stops WHERE id=?", (ns[0]["id"],)).fetchone() if ns else None
    r = db().execute("SELECT * FROM stops WHERE lower(name)=lower(?)", (t,)).fetchone()
    if r:
        return r
    return db().execute("SELECT * FROM stops WHERE name LIKE ? OR landmark LIKE ? ORDER BY length(name) LIMIT 1",
                        (f"%{t}%", f"%{t}%")).fetchone()


def departures_at(route_id, stop_offset, from_abs_min, today, n=3):
    """Upcoming bus times at a stop. Times are absolute minutes counted from today 00:00."""
    scheds = db().execute("SELECT depart_time, days FROM schedules WHERE route_id=?", (route_id,)).fetchall()
    found = []
    for day in range(0, 8):
        wd = str((today + timedelta(days=day)).isoweekday())
        for s in scheds:
            if wd in s["days"].split(","):
                a = day * 1440 + parse_hhmm(s["depart_time"]) + stop_offset
                if a >= from_abs_min:
                    found.append(a)
    found.sort()
    def day_label(a):
        d = a // 1440
        return "today" if d == 0 else ("tomorrow" if d == 1 else (today + timedelta(days=d)).strftime("%a"))
    return [{"abs": a, "clock": clock(a), "wait": int(a - from_abs_min), "day": day_label(a)} for a in found[:n]]


def find_routes(from_stop, to_stop, query_min, today):
    routes = db().execute("SELECT * FROM routes ORDER BY code").fetchall()
    data = {}
    for r in routes:
        data[r["id"]] = (r, get_route_stops(r["id"]))
    direct, transfers = [], []
    for rid, (r, st) in data.items():
        ids = [s["stop_id"] for s in st]
        if from_stop["id"] not in ids:
            continue
        i = ids.index(from_stop["id"])
        js = [k for k, x in enumerate(ids) if x == to_stop["id"] and k > i]
        if not js:
            continue
        j = js[0]
        active = r["status"] == "active"
        deps = departures_at(rid, st[i]["offset_min"], query_min, today) if active else []
        live = live_for_route(rid) if active else {"available": False, "message": "Route suspended."}
        eta_from = None
        if live.get("available"):
            for e in live["etas"]:
                if e["stop_id"] == from_stop["id"]:
                    eta_from = e["eta_min"]
        direct.append({
            "route_id": rid, "code": r["code"], "name": r["name"], "color": r["color"], "status": r["status"],
            "fresh": freshness(r["last_verified"]), "from": st[i]["name"], "to": st[j]["name"],
            "stops_between": j - i, "duration": st[j]["offset_min"] - st[i]["offset_min"], "deps": deps,
            "alerts": active_alerts(rid),
            "live": {"available": live.get("available", False), "source": live.get("source"),
                     "label": live.get("label"), "message": live.get("message"), "eta_from": eta_from, "bus": live.get("bus")},
            "unavailable": not active,
            "reason": "This route is currently suspended." if not active else ("No buses scheduled in the coming week." if not deps else "")})
    direct.sort(key=lambda x: (x["unavailable"], x["deps"][0]["abs"] if x["deps"] else 10 ** 9))
    # one-change journeys
    for aid, (a, sa) in data.items():
        if a["status"] != "active":
            continue
        ida = [s["stop_id"] for s in sa]
        if from_stop["id"] not in ida:
            continue
        i = ida.index(from_stop["id"])
        if any(x == to_stop["id"] for x in ida[i + 1:]):
            continue
        depa = departures_at(aid, sa[i]["offset_min"], query_min, today, 1)
        if not depa:
            continue
        for bid, (b, sb) in data.items():
            if bid == aid or b["status"] != "active":
                continue
            idb = [s["stop_id"] for s in sb]
            if to_stop["id"] not in idb:
                continue
            k = idb.index(to_stop["id"])
            best = None
            for xa in range(i + 1, len(sa)):
                sid = sa[xa]["stop_id"]
                if sid not in idb[:k]:
                    continue
                xb = idb.index(sid)
                arr_x = depa[0]["abs"] + sa[xa]["offset_min"] - sa[i]["offset_min"]
                depb = departures_at(bid, sb[xb]["offset_min"], arr_x + TRANSFER_BUFFER_MIN, today, 1)
                if not depb:
                    continue
                arrive = depb[0]["abs"] + sb[k]["offset_min"] - sb[xb]["offset_min"]
                if best is None or arrive < best["arrive_abs"]:
                    best = {"a_code": a["code"], "a_color": a["color"], "a_name": a["name"], "a_id": aid,
                            "b_code": b["code"], "b_color": b["color"], "b_name": b["name"], "b_id": bid,
                            "change_at": sa[xa]["name"], "dep": clock(depa[0]["abs"]), "arrive_change": clock(arr_x),
                            "dep_b": clock(depb[0]["abs"]), "arrive": clock(arrive), "arrive_abs": arrive,
                            "total_min": int(arrive - depa[0]["abs"]), "wait_change": int(depb[0]["abs"] - arr_x),
                            "fresh_warn": "old" in (freshness(a["last_verified"])["level"], freshness(b["last_verified"])["level"])}
            if best:
                transfers.append(best)
    transfers.sort(key=lambda x: x["arrive_abs"])
    return direct, transfers[:3]


# ------------------------------- auth ---------------------------------------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.context_processor
def inject():
    return dict(app_name=APP_NAME, college=COLLEGE_NAME, demo=DEMO, is_admin=bool(session.get("admin")),
                alert_count=len(active_alerts()), clock=clock,
                all_stops=db().execute("SELECT name FROM stops ORDER BY name").fetchall(),
                kinds=ALERT_KINDS, day_names=DAY_NAMES)


# ------------------------------- public pages -------------------------------
@app.route("/")
def home():
    now = local_now()
    f, t = request.args.get("from", "").strip(), request.args.get("to", "").strip()
    time_str = request.args.get("time") or now.strftime("%H:%M")
    ctx = dict(f=f, t=t, time_str=time_str, searched=False, direct=[], transfers=[], error=None,
               notices=active_alerts()[:4], now_clock=clock(now.hour * 60 + now.minute))
    if f and t:
        ctx["searched"] = True
        fs, ts = resolve_stop(f), resolve_stop(t)
        if not fs or not ts:
            ctx["error"] = ("We could not find the stop \"%s\". Pick one from the suggestions." % (f if not fs else t))
        elif fs["id"] == ts["id"]:
            ctx["error"] = "Start and destination are the same stop."
        else:
            try:
                qmin = parse_hhmm(time_str)
            except Exception:
                qmin = now.hour * 60 + now.minute
            ctx["direct"], ctx["transfers"] = find_routes(fs, ts, qmin, now.date())
            ctx.update(f=fs["name"], t=ts["name"])
    return render_template("home.html", **ctx)


@app.route("/routes")
def routes_list():
    out = []
    for r in db().execute("SELECT * FROM routes ORDER BY code"):
        st = get_route_stops(r["id"])
        times = [x["depart_time"] for x in db().execute(
            "SELECT depart_time FROM schedules WHERE route_id=? ORDER BY depart_time", (r["id"],))]
        out.append({"r": r, "n": len(st), "first": st[0]["name"] if st else "-", "last": st[-1]["name"] if st else "-",
                    "dur": st[-1]["offset_min"] if st else 0, "trips": len(times),
                    "first_bus": clock(parse_hhmm(times[0])) if times else "-",
                    "last_bus": clock(parse_hhmm(times[-1])) if times else "-",
                    "fresh": freshness(r["last_verified"])})
    return render_template("routes.html", rows=out)


@app.route("/routes/<int:route_id>")
def route_detail(route_id):
    r = db().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    if not r:
        flash("Route not found.", "warn")
        return redirect(url_for("routes_list"))
    st = get_route_stops(route_id)
    scheds = db().execute("SELECT * FROM schedules WHERE route_id=? ORDER BY depart_time", (route_id,)).fetchall()
    first_off = st[0]["offset_min"] if st else 0
    times = [{"t": clock(parse_hhmm(s["depart_time"]) + first_off), "days": s["days"]} for s in scheds]
    days_union = sorted({int(d) for s in scheds for d in s["days"].split(",") if d})
    return render_template("route.html", r=r, stops=st, times=times, fresh=freshness(r["last_verified"]),
                           alerts=active_alerts(route_id), days=days_union, highlight=request.args.get("stop", ""))


@app.route("/map")
def live_map():
    rs = []
    for r in db().execute("SELECT * FROM routes WHERE status='active' ORDER BY code"):
        rs.append({"id": r["id"], "code": r["code"], "name": r["name"], "color": r["color"],
                   "stops": get_route_stops(r["id"])})
    return render_template("map.html", routes=rs)


@app.route("/alerts")
def alerts_page():
    return render_template("alerts.html", alerts=active_alerts())


@app.route("/driver")
def driver():
    buses = db().execute("SELECT b.id, b.reg_no, r.code FROM buses b LEFT JOIN routes r ON r.id=b.route_id ORDER BY b.reg_no").fetchall()
    return render_template("driver.html", buses=buses)


@app.route("/health")
def health():
    return jsonify(ok=True)


# ------------------------------- public API ---------------------------------
@app.route("/api/nearest")
def api_nearest():
    try:
        lat, lng = float(request.args["lat"]), float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify(error="lat and lng required"), 400
    return jsonify(stops=nearest_stops(lat, lng, int(request.args.get("n", 5))))


@app.route("/api/live/<int:route_id>")
def api_live(route_id):
    return jsonify(live_for_route(route_id))


@app.route("/api/live")
def api_live_all():
    out = {}
    for r in db().execute("SELECT id FROM routes WHERE status='active'"):
        out[r["id"]] = live_for_route(r["id"])
    return jsonify(out)


@app.route("/api/alerts")
def api_alerts():
    return jsonify(alerts=[{"id": a["id"], "route_id": a["route_id"], "route_code": a["route_code"],
                            "kind": a["kind"], "message": a["message"], "age": a["age"]} for a in active_alerts()])


@app.route("/api/search")
def api_search():
    fs, ts = resolve_stop(request.args.get("from")), resolve_stop(request.args.get("to"))
    if not fs or not ts:
        return jsonify(error="unknown stop"), 404
    now = local_now()
    try:
        qmin = parse_hhmm(request.args.get("time", ""))
    except Exception:
        qmin = now.hour * 60 + now.minute
    d, t = find_routes(fs, ts, qmin, now.date())
    return jsonify(direct=d, transfers=t)


@app.route("/api/driver/location", methods=["POST"])
def api_driver_location():
    j = request.get_json(silent=True) or {}
    try:
        bus_id, lat, lng = int(j["bus_id"]), float(j["lat"]), float(j["lng"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="bus_id, lat, lng required"), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(error="bad coordinates"), 400
    b = db().execute("SELECT * FROM buses WHERE id=?", (bus_id,)).fetchone()
    if not b or str(j.get("token", "")) != b["driver_token"]:
        return jsonify(error="invalid bus or PIN"), 403
    db().execute("INSERT INTO bus_positions(bus_id,lat,lng,speed,updated_ts,source) VALUES(?,?,?,?,?,'gps') "
                 "ON CONFLICT(bus_id) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, speed=excluded.speed, "
                 "updated_ts=excluded.updated_ts, source='gps'", (bus_id, lat, lng, j.get("speed"), time.time()))
    db().commit()
    return jsonify(ok=True)


# ------------------------------- admin --------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("admin_home"))
        flash("Wrong password.", "warn")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_home():
    c = db()
    counts = {k: c.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ("stops", "routes", "buses", "schedules")}
    stale = [r for r in c.execute("SELECT * FROM routes ORDER BY code") if freshness(r["last_verified"])["level"] != "ok"]
    gps = c.execute("SELECT COUNT(*) FROM bus_positions WHERE source='gps' AND updated_ts>?",
                    (time.time() - GPS_FRESH_SEC,)).fetchone()[0]
    return render_template("admin_home.html", counts=counts, stale=stale, gps=gps,
                           sim_mode=get_setting("sim_mode", "loop"), fresh=freshness,
                           routes=c.execute("SELECT * FROM routes ORDER BY code").fetchall())


@app.route("/admin/sim", methods=["POST"])
@admin_required
def admin_sim():
    mode = request.form.get("mode", "loop")
    set_setting("sim_mode", mode if mode in ("off", "trips", "loop") else "loop")
    flash("Simulation mode updated.", "ok")
    return redirect(url_for("admin_home"))


@app.route("/admin/stops", methods=["GET", "POST"])
@admin_required
def admin_stops():
    if request.method == "POST":
        act = request.form.get("act")
        try:
            if act == "add":
                db().execute("INSERT INTO stops(name,landmark,lat,lng) VALUES(?,?,?,?)",
                             (request.form["name"].strip(), request.form.get("landmark", "").strip(),
                              float(request.form["lat"]), float(request.form["lng"])))
            elif act == "save":
                db().execute("UPDATE stops SET name=?, landmark=?, lat=?, lng=? WHERE id=?",
                             (request.form["name"].strip(), request.form.get("landmark", "").strip(),
                              float(request.form["lat"]), float(request.form["lng"]), int(request.form["id"])))
            elif act == "delete":
                used = db().execute("SELECT COUNT(*) FROM route_stops WHERE stop_id=?", (request.form["id"],)).fetchone()[0]
                if used:
                    flash("That stop is used by a route. Remove it from the route first.", "warn")
                    return redirect(url_for("admin_stops"))
                db().execute("DELETE FROM stops WHERE id=?", (request.form["id"],))
            db().commit()
            flash("Saved.", "ok")
        except (ValueError, KeyError):
            flash("Enter a name and valid latitude and longitude.", "warn")
        except sqlite3.IntegrityError:
            flash("A stop with that name already exists.", "warn")
        return redirect(url_for("admin_stops"))
    return render_template("admin_stops.html", stops=db().execute("SELECT * FROM stops ORDER BY name").fetchall())


@app.route("/admin/routes", methods=["GET", "POST"])
@admin_required
def admin_routes():
    if request.method == "POST":
        code, name = request.form.get("code", "").strip(), request.form.get("name", "").strip()
        if not code or not name:
            flash("Code and name are required.", "warn")
        else:
            cur = db().execute("INSERT INTO routes(code,name,color,status,last_verified,notes) VALUES(?,?,?,?,?,?)",
                               (code, name, request.form.get("color", "#2563eb"), "active",
                                local_now().strftime("%Y-%m-%d"), ""))
            db().commit()
            return redirect(url_for("admin_route", route_id=cur.lastrowid))
    rows = [{"r": r, "fresh": freshness(r["last_verified"])} for r in db().execute("SELECT * FROM routes ORDER BY code")]
    return render_template("admin_routes.html", rows=rows)


@app.route("/admin/routes/<int:route_id>", methods=["GET", "POST"])
@admin_required
def admin_route(route_id):
    c = db()
    r = c.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    if not r:
        return redirect(url_for("admin_routes"))
    if request.method == "POST":
        act = request.form.get("act")
        if act == "info":
            c.execute("UPDATE routes SET code=?, name=?, color=?, status=?, last_verified=?, notes=? WHERE id=?",
                      (request.form["code"].strip(), request.form["name"].strip(), request.form.get("color", "#2563eb"),
                       request.form.get("status", "active"), request.form.get("last_verified") or None,
                       request.form.get("notes", ""), route_id))
            flash("Route details saved.", "ok")
        elif act == "verify":
            c.execute("UPDATE routes SET last_verified=? WHERE id=?", (local_now().strftime("%Y-%m-%d"), route_id))
            flash("Marked as verified today.", "ok")
        elif act == "stops":
            ids, offs = request.form.getlist("stop_id"), request.form.getlist("offset")
            try:
                rows = [(int(a), int(b)) for a, b in zip(ids, offs) if a]
            except ValueError:
                flash("Offsets must be whole numbers of minutes.", "warn")
                return redirect(url_for("admin_route", route_id=route_id))
            if any(rows[i][1] > rows[i + 1][1] for i in range(len(rows) - 1)):
                flash("Offsets must increase down the list (minutes after the first stop).", "warn")
                return redirect(url_for("admin_route", route_id=route_id))
            c.execute("DELETE FROM route_stops WHERE route_id=?", (route_id,))
            for seq, (sid, off) in enumerate(rows):
                c.execute("INSERT INTO route_stops(route_id,stop_id,seq,offset_min) VALUES(?,?,?,?)", (route_id, sid, seq, off))
            flash("Stops saved.", "ok")
        elif act == "schedule":
            times = [t for t in re.split(r"[\s,;]+", request.form.get("times", "")) if t]
            bad = [t for t in times if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t)]
            if bad:
                flash("Invalid time(s): %s. Use 24-hour HH:MM." % ", ".join(bad), "warn")
                return redirect(url_for("admin_route", route_id=route_id))
            days = ",".join(sorted(request.form.getlist("days"))) or "1,2,3,4,5,6"
            c.execute("DELETE FROM schedules WHERE route_id=?", (route_id,))
            for t in sorted({f"{int(x.split(':')[0]):02d}:{x.split(':')[1]}" for x in times}):
                c.execute("INSERT INTO schedules(route_id,depart_time,days) VALUES(?,?,?)", (route_id, t, days))
            flash("Schedule saved.", "ok")
        elif act == "reverse":
            st = get_route_stops(route_id)
            if len(st) < 2:
                flash("Add stops first.", "warn")
            else:
                cur = c.execute("INSERT INTO routes(code,name,color,status,last_verified,notes) VALUES(?,?,?,?,?,?)",
                                (r["code"] + "-R", "Return: " + r["name"], r["color"], r["status"], r["last_verified"], r["notes"]))
                total = st[-1]["offset_min"]
                for seq, s in enumerate(reversed(st)):
                    c.execute("INSERT INTO route_stops(route_id,stop_id,seq,offset_min) VALUES(?,?,?,?)",
                              (cur.lastrowid, s["stop_id"], seq, total - s["offset_min"]))
                c.commit()
                flash("Return route created. Add its schedule.", "ok")
                return redirect(url_for("admin_route", route_id=cur.lastrowid))
        elif act == "delete":
            for t in ("route_stops", "schedules"):
                c.execute(f"DELETE FROM {t} WHERE route_id=?", (route_id,))
            c.execute("UPDATE buses SET route_id=NULL WHERE route_id=?", (route_id,))
            c.execute("DELETE FROM alerts WHERE route_id=?", (route_id,))
            c.execute("DELETE FROM routes WHERE id=?", (route_id,))
            c.commit()
            flash("Route deleted.", "ok")
            return redirect(url_for("admin_routes"))
        c.commit()
        return redirect(url_for("admin_route", route_id=route_id))
    sched = c.execute("SELECT * FROM schedules WHERE route_id=? ORDER BY depart_time", (route_id,)).fetchall()
    return render_template("admin_route.html", r=r, stops=get_route_stops(route_id),
                           stop_options=c.execute("SELECT id, name FROM stops ORDER BY name").fetchall(),
                           times="\n".join(s["depart_time"] for s in sched),
                           days=set(sched[0]["days"].split(",")) if sched else {"1", "2", "3", "4", "5", "6"},
                           fresh=freshness(r["last_verified"]))


@app.route("/admin/alerts", methods=["GET", "POST"])
@admin_required
def admin_alerts():
    if request.method == "POST":
        act = request.form.get("act")
        if act == "add":
            msg = request.form.get("message", "").strip()
            if not msg:
                flash("Write a message.", "warn")
            else:
                rid = request.form.get("route_id")
                hours = request.form.get("hours")
                exp = time.time() + float(hours) * 3600 if hours else None
                kind = request.form.get("kind") if request.form.get("kind") in ALERT_KINDS else "info"
                db().execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                             (int(rid) if rid else None, kind, msg, time.time(), exp))
                flash("Notice published. Students see it instantly.", "ok")
        elif act == "off":
            db().execute("UPDATE alerts SET active=0 WHERE id=?", (request.form["id"],))
        elif act == "delete":
            db().execute("DELETE FROM alerts WHERE id=?", (request.form["id"],))
        db().commit()
        return redirect(url_for("admin_alerts"))
    rows = db().execute("SELECT a.*, r.code AS route_code FROM alerts a LEFT JOIN routes r ON r.id=a.route_id "
                        "ORDER BY a.id DESC LIMIT 50").fetchall()
    return render_template("admin_alerts.html", rows=rows, routes=db().execute("SELECT id, code, name FROM routes ORDER BY code").fetchall(),
                           ago=ago, now=time.time())


@app.route("/admin/buses", methods=["GET", "POST"])
@admin_required
def admin_buses():
    if request.method == "POST":
        act = request.form.get("act")
        if act == "add":
            db().execute("INSERT INTO buses(reg_no,capacity,route_id,driver_name,driver_token) VALUES(?,?,?,?,?)",
                         (request.form.get("reg_no", "").strip() or "NEW BUS", int(request.form.get("capacity") or 50),
                          int(request.form["route_id"]) if request.form.get("route_id") else None,
                          request.form.get("driver_name", "").strip(), request.form.get("token") or "1234"))
        elif act == "save":
            db().execute("UPDATE buses SET reg_no=?, capacity=?, route_id=?, driver_name=?, driver_token=? WHERE id=?",
                         (request.form.get("reg_no", "").strip(), int(request.form.get("capacity") or 50),
                          int(request.form["route_id"]) if request.form.get("route_id") else None,
                          request.form.get("driver_name", "").strip(), request.form.get("token") or "1234",
                          int(request.form["id"])))
        elif act == "delete":
            db().execute("DELETE FROM bus_positions WHERE bus_id=?", (request.form["id"],))
            db().execute("DELETE FROM buses WHERE id=?", (request.form["id"],))
        db().commit()
        flash("Saved.", "ok")
        return redirect(url_for("admin_buses"))
    return render_template("admin_buses.html", routes=db().execute("SELECT id, code, name FROM routes ORDER BY code").fetchall(),
                           buses=db().execute("SELECT * FROM buses ORDER BY reg_no").fetchall())


# ------------------------------- demo data ----------------------------------
def seed():
    """Realistic DEMO data (approximate coordinates). Replace via /admin with your real routes."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    stops = [
        ("Dilsukhnagar", "Near bus depot", 17.3688, 78.5247), ("LB Nagar", "Metro station", 17.3457, 78.5522),
        ("Vanasthalipuram", "Main road", 17.3320, 78.5700), ("Hayathnagar", "Bus stand", 17.3293, 78.6020),
        ("Abdullapurmet", "Highway stop", 17.3095, 78.6660), ("Uppal", "Metro station", 17.4058, 78.5591),
        ("Nagole", "Metro station", 17.3921, 78.5606), ("Pedda Amberpet", "ORR junction", 17.3315, 78.6270),
        ("Mehdipatnam", "Bus stop", 17.3950, 78.4400), ("Attapur", "Pillar 150", 17.3680, 78.4290),
        ("College Main Gate", "Campus entrance", 17.2200, 78.7000)]
    ids = {}
    for n, lm, la, lo in stops:
        ids[n] = conn.execute("INSERT INTO stops(name,landmark,lat,lng) VALUES(?,?,?,?)", (n, lm, la, lo)).lastrowid
    today = local_now().date()

    def day(n):
        return (today - timedelta(days=n)).strftime("%Y-%m-%d")

    defs = [
        ("R1", "Dilsukhnagar to College", "#2563eb", "active", 2, "Via LB Nagar and Hayathnagar",
         [("Dilsukhnagar", 0), ("LB Nagar", 10), ("Vanasthalipuram", 18), ("Hayathnagar", 30), ("Abdullapurmet", 42), ("College Main Gate", 55)]),
        ("R2", "Uppal to College", "#16a34a", "active", 1, "Via Nagole and LB Nagar",
         [("Uppal", 0), ("Nagole", 7), ("LB Nagar", 17), ("Vanasthalipuram", 25), ("Hayathnagar", 37), ("College Main Gate", 60)]),
        ("R3", "Pedda Amberpet to College", "#ea580c", "active", 40, "Timings not re-checked this term",
         [("Pedda Amberpet", 0), ("Abdullapurmet", 12), ("College Main Gate", 25)]),
        ("R4", "Mehdipatnam to College", "#7c3aed", "suspended", 6, "Suspended for road works",
         [("Mehdipatnam", 0), ("Attapur", 10), ("College Main Gate", 60)])]
    inbound = [f"{h:02d}:{m:02d}" for h in range(6, 10) for m in (0, 30)]
    outbound = ["12:30", "15:30", "16:00", "16:30", "17:00", "17:30", "18:00", "18:30"]
    route_ids = {}
    n_bus = 0

    def add_route(code, name, color, status, verified, notes, seq, times):
        rid = conn.execute("INSERT INTO routes(code,name,color,status,last_verified,notes) VALUES(?,?,?,?,?,?)",
                           (code, name, color, status, verified, notes)).lastrowid
        for i, (s, off) in enumerate(seq):
            conn.execute("INSERT INTO route_stops(route_id,stop_id,seq,offset_min) VALUES(?,?,?,?)", (rid, ids[s], i, off))
        for t in times:
            conn.execute("INSERT INTO schedules(route_id,depart_time,days) VALUES(?,?,?)", (rid, t, "1,2,3,4,5,6"))
        return rid

    for code, name, color, status, vd, notes, seq in defs:
        rid = add_route(code, name, color, status, day(vd), notes, seq, inbound)
        route_ids[code] = rid
        total = seq[-1][1]
        rev = [(s, total - o) for s, o in reversed(seq)]
        rid2 = add_route(code + "-R", "College to " + name.split(" to ")[0], color, status, day(vd), notes, rev, outbound)
        route_ids[code + "-R"] = rid2
    regs = ["TS 08 UA 1021", "TS 08 UA 1022", "TS 08 UB 2045", "TS 08 UB 2046", "TS 08 UC 3310", "TS 08 UC 3311", "TS 08 UD 4402", "TS 08 UD 4403"]
    for code, rid in route_ids.items():
        conn.execute("INSERT INTO buses(reg_no,capacity,route_id,driver_name,driver_token) VALUES(?,?,?,?,?)",
                     (regs[n_bus % len(regs)], 50, rid, "Driver " + str(n_bus + 1), str(n_bus + 1) * 4))
        n_bus += 1
    now = time.time()
    conn.execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (route_ids["R1"], "delay", "R1 is running about 15 minutes late because of road work near LB Nagar.", now - 1500, now + 6 * 3600))
    conn.execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (route_ids["R4"], "cancelled", "R4 is suspended until further notice. Please use R1 from Dilsukhnagar.", now - 7200, None))
    conn.execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (None, "info", "No bus service on Sundays and public holidays.", now - 86400, None))
    conn.execute("INSERT INTO settings(key,value) VALUES('sim_mode','loop')")
    conn.commit()
    conn.close()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
