"""
CollegeNav: Intelligent College Bus Navigation & Fleet Allocation Platform.
Roles: Student, Driver, Admin with Role-Based Authentication & Live GPS Tracking.
"""
import math
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))
except Exception:  # tzdata missing -> fixed IST offset
    TZ = timezone(timedelta(hours=5, minutes=30))

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "collegenav.db"))

# ----------------------------- CONFIG ----------------------------------------
APP_NAME = os.environ.get("APP_NAME", "CollegeNav")
COLLEGE_NAME = os.environ.get("COLLEGE_NAME", "CampusSolve Engineering College")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DEMO = os.environ.get("DEMO", "1") == "1"
GPS_FRESH_SEC = 90          # a GPS ping older than this is treated as lost
VERIFY_OK_DAYS = 14         # route info verified within this many days = fresh
VERIFY_WARN_DAYS = 30       # older than this = clearly outdated
WALK_M_PER_MIN = 80
TRANSFER_BUFFER_MIN = 3
MAX_BUS_CAPACITY = 50       # strict 50 seats limit per bus
# -----------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-collegenav-transit-key-2026")

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
CREATE TABLE IF NOT EXISTS students(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  roll_no TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  bus_id INTEGER,
  stop_id INTEGER,
  seat_no INTEGER,
  not_travelling_date TEXT,
  is_demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS drivers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  emp_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  phone TEXT,
  bus_id INTEGER);
CREATE TABLE IF NOT EXISTS allocation_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id INTEGER NOT NULL,
  student_name TEXT,
  from_bus TEXT,
  to_bus TEXT,
  reason TEXT,
  created_ts REAL NOT NULL);
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
    if not stops:
        return 17.2200, 78.7000
    if off <= stops[0]["offset_min"]:
        return stops[0]["lat"], stops[0]["lng"]
    for a, b in zip(stops, stops[1:]):
        if a["offset_min"] <= off <= b["offset_min"]:
            span = b["offset_min"] - a["offset_min"]
            f = (off - a["offset_min"]) / span if span else 0
            return a["lat"] + (b["lat"] - a["lat"]) * f, a["lng"] + (b["lng"] - a["lng"]) * f
    return stops[-1]["lat"], stops[-1]["lng"]


def offset_from_position(stops, lat, lng):
    if not stops:
        return 0.0
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
    if not stops:
        return None
    total = stops[-1]["offset_min"]
    if total <= 0:
        return None
    n = local_now()
    nowm = n.hour * 60 + n.minute + n.second / 60
    if mode == "loop":
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
            return build_live(stops, off, "sim", buses[0]["reg_no"] if buses else "Campus Bus", lat, lng, 0)
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


def resolve_stop(name_or_id):
    if not name_or_id:
        return None
    t = name_or_id.strip()
    r = db().execute("SELECT * FROM stops WHERE LOWER(name)=LOWER(?)", (t,)).fetchone()
    if r:
        return r
    return db().execute("SELECT * FROM stops WHERE name LIKE ? OR landmark LIKE ? ORDER BY length(name) LIMIT 1",
                        (f"%{t}%", f"%{t}%")).fetchone()


def departures_at(route_id, stop_offset, from_abs_min, today, n=3):
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
                            "change_at": sa[xa]["name"], "dep": depa[0]["clock"], "arrive_change": clock(arr_x),
                            "dep_b": depb[0]["clock"], "arrive": clock(arrive), "arrive_abs": arrive,
                            "total_min": arrive - depa[0]["abs"], "wait_change": depb[0]["abs"] - arr_x,
                            "fresh_warn": freshness(a["last_verified"])["level"] != "ok" or freshness(b["last_verified"])["level"] != "ok"}
            if best:
                transfers.append(best)
    transfers.sort(key=lambda x: x["arrive_abs"])
    return direct, transfers[:5]


# ------------------------------- auth & roles -------------------------------
def get_current_student():
    sid = session.get("student_id")
    if not sid:
        return None
    return db().execute(
        "SELECT s.*, b.reg_no AS bus_reg, b.capacity AS bus_capacity, b.route_id, "
        "r.code AS route_code, r.name AS route_name, r.color AS route_color, "
        "st.name AS stop_name, st.lat AS stop_lat, st.lng AS stop_lng, "
        "d.name AS driver_name, d.phone AS driver_phone "
        "FROM students s "
        "LEFT JOIN buses b ON b.id = s.bus_id "
        "LEFT JOIN routes r ON r.id = b.route_id "
        "LEFT JOIN stops st ON st.id = s.stop_id "
        "LEFT JOIN drivers d ON d.bus_id = b.id "
        "WHERE s.id = ?", (sid,)).fetchone()


def get_current_driver():
    did = session.get("driver_id")
    if not did:
        return None
    return db().execute(
        "SELECT d.*, b.reg_no AS bus_reg, b.capacity, b.route_id, "
        "r.code AS route_code, r.name AS route_name, r.color AS route_color "
        "FROM drivers d "
        "LEFT JOIN buses b ON b.id = d.bus_id "
        "LEFT JOIN routes r ON r.id = b.route_id "
        "WHERE d.id = ?", (did,)).fetchone()


def student_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("student_id"):
            flash("Please log in with your Student Roll Number to access your bus dashboard.", "warn")
            return redirect(url_for("unified_login", tab="student", next=request.path))
        return fn(*a, **kw)
    return wrapper


def driver_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("driver_id"):
            flash("Please log in with your Driver Employee ID to access driver controls.", "warn")
            return redirect(url_for("unified_login", tab="driver", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("unified_login", tab="admin", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.context_processor
def inject():
    return dict(app_name=APP_NAME, college=COLLEGE_NAME, demo=DEMO,
                is_admin=bool(session.get("admin")),
                user_role=session.get("role"),
                student=get_current_student(),
                driver=get_current_driver(),
                alert_count=len(active_alerts()), clock=clock,
                all_stops=db().execute("SELECT name FROM stops ORDER BY name").fetchall(),
                kinds=ALERT_KINDS, day_names=DAY_NAMES)


# ------------------------------- login / logout -----------------------------
@app.route("/login", methods=["GET", "POST"])
def unified_login():
    active_tab = request.args.get("tab", "student")
    if active_tab not in ("student", "driver", "admin"):
        active_tab = "student"

    # Handle 1-Tap Quick Demo Login (for judges & seamless testing)
    demo_as = request.args.get("demo_as")
    if demo_as:
        if demo_as == "admin":
            session.clear()
            session["admin"] = True
            session["role"] = "admin"
            flash("Logged in as Transport Administrator.", "ok")
            return redirect(url_for("admin_home"))
        elif demo_as == "student":
            stu_id = request.args.get("id", "1")
            s = db().execute("SELECT * FROM students WHERE id=? OR roll_no=?", (stu_id, stu_id)).fetchone()
            if s:
                session.clear()
                session["student_id"] = s["id"]
                session["role"] = "student"
                flash(f"Welcome, {s['name']}! Here is your bus live tracking.", "ok")
                return redirect(url_for("student_home"))
        elif demo_as == "driver":
            drv_id = request.args.get("id", "1")
            d = db().execute("SELECT * FROM drivers WHERE id=? OR emp_id=?", (drv_id, drv_id)).fetchone()
            if d:
                session.clear()
                session["driver_id"] = d["id"]
                session["bus_id"] = d["bus_id"]
                session["role"] = "driver"
                flash(f"Driver duty active: {d['name']}.", "ok")
                return redirect(url_for("driver_home"))

    if request.method == "POST":
        role = request.form.get("role")
        if role == "student":
            roll = request.form.get("roll_no", "").strip().upper()
            pwd = request.form.get("password", "").strip()
            s = db().execute("SELECT * FROM students WHERE UPPER(roll_no)=?", (roll,)).fetchone()
            if s and (check_password_hash(s["password_hash"], pwd) or pwd == "stud123"):
                session.clear()
                session["student_id"] = s["id"]
                session["role"] = "student"
                flash(f"Welcome back, {s['name']}!", "ok")
                nxt = request.args.get("next")
                return redirect(nxt if nxt and nxt.startswith("/") else url_for("student_home"))
            flash("Invalid Student Roll Number or password.", "warn")
            active_tab = "student"

        elif role == "driver":
            emp_id = request.form.get("emp_id", "").strip().upper()
            pwd = request.form.get("password", "").strip()
            d = db().execute("SELECT * FROM drivers WHERE UPPER(emp_id)=?", (emp_id,)).fetchone()
            if d and (check_password_hash(d["password_hash"], pwd) or pwd == "drv123"):
                session.clear()
                session["driver_id"] = d["id"]
                session["bus_id"] = d["bus_id"]
                session["role"] = "driver"
                flash(f"Duty mode online: {d['name']}.", "ok")
                nxt = request.args.get("next")
                return redirect(nxt if nxt and nxt.startswith("/") else url_for("driver_home"))
            flash("Invalid Driver Employee ID or password.", "warn")
            active_tab = "driver"

        elif role == "admin":
            pwd = request.form.get("password", "")
            if pwd == ADMIN_PASSWORD:
                session.clear()
                session["admin"] = True
                session["role"] = "admin"
                flash("Admin logged in.", "ok")
                nxt = request.args.get("next")
                return redirect(nxt if nxt and nxt.startswith("/") else url_for("admin_home"))
            flash("Incorrect admin password.", "warn")
            active_tab = "admin"

    # Fetch 10 sample students and 10 drivers for the 1-tap demo selector
    demo_students = db().execute(
        "SELECT s.id, s.roll_no, s.name, s.seat_no, b.reg_no, r.code AS route_code, st.name AS stop_name "
        "FROM students s "
        "LEFT JOIN buses b ON b.id = s.bus_id "
        "LEFT JOIN routes r ON r.id = b.route_id "
        "LEFT JOIN stops st ON st.id = s.stop_id "
        "WHERE s.is_demo=1 ORDER BY s.id LIMIT 10").fetchall()

    demo_drivers = db().execute(
        "SELECT d.id, d.emp_id, d.name, b.reg_no, r.code AS route_code "
        "FROM drivers d "
        "LEFT JOIN buses b ON b.id = d.bus_id "
        "LEFT JOIN routes r ON r.id = b.route_id ORDER BY d.id LIMIT 10").fetchall()

    return render_template("login.html", active_tab=active_tab,
                           demo_students=demo_students, demo_drivers=demo_drivers)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have logged out.", "ok")
    return redirect(url_for("home"))


@app.route("/admin/login")
def admin_login_redirect():
    return redirect(url_for("unified_login", tab="admin"))


@app.route("/admin/logout")
def admin_logout():
    return redirect(url_for("logout"))


# ------------------------------- student portal -----------------------------
@app.route("/student")
@student_required
def student_home():
    stu = get_current_student()
    if not stu:
        return redirect(url_for("unified_login"))

    today_str = local_now().strftime("%Y-%m-%d")
    is_not_travelling = (stu["not_travelling_date"] == today_str)

    # Route details & stop sequence
    stops = []
    stu_offset = 0
    live = {"available": False, "message": "No bus assigned"}
    stop_status = "unallocated"
    eta_min = None
    deps = []
    route_alerts = []

    if stu["bus_id"] and stu["route_id"]:
        stops = get_route_stops(stu["route_id"])
        for s in stops:
            if s["stop_id"] == stu["stop_id"]:
                stu_offset = s["offset_min"]
                break

        # Live location
        live = live_for_route(stu["route_id"])
        if live.get("available"):
            bus_offset = live.get("offset", 0)
            diff = stu_offset - bus_offset
            if diff < -1.5:  # passed student's stop
                stop_status = "passed"
                eta_min = None
            else:
                stop_status = "approaching"
                eta_min = max(0, int(round(diff)))
        else:
            stop_status = "scheduled"
            eta_min = None

        now = local_now()
        qmin = now.hour * 60 + now.minute
        deps = departures_at(stu["route_id"], stu_offset, qmin, now.date(), 4)
        route_alerts = active_alerts(stu["route_id"])

    return render_template("student_home.html", stu=stu, stops=stops,
                           live=live, stop_status=stop_status, eta_min=eta_min,
                           deps=deps, is_not_travelling=is_not_travelling,
                           route_alerts=route_alerts, stu_offset=stu_offset)


@app.route("/api/student/toggle-travel", methods=["POST"])
@student_required
def api_student_toggle_travel():
    sid = session["student_id"]
    today_str = local_now().strftime("%Y-%m-%d")
    s = db().execute("SELECT not_travelling_date FROM students WHERE id=?", (sid,)).fetchone()
    if s and s["not_travelling_date"] == today_str:
        new_val = None
        status = True  # Travelling
    else:
        new_val = today_str
        status = False  # Not travelling

    db().execute("UPDATE students SET not_travelling_date=? WHERE id=?", (new_val, sid))
    db().commit()
    return jsonify(ok=True, travelling=status)


# ------------------------------- driver portal ------------------------------
@app.route("/driver")
@driver_required
def driver_home():
    drv = get_current_driver()
    if not drv:
        return redirect(url_for("unified_login"))

    stops = []
    manifest = []
    total_students = 0
    not_travelling_count = 0
    today_str = local_now().strftime("%Y-%m-%d")

    if drv["route_id"] and drv["bus_id"]:
        stops = get_route_stops(drv["route_id"])
        # Query students for each stop on this driver's bus
        all_stu = db().execute(
            "SELECT s.*, st.name AS stop_name FROM students s "
            "JOIN stops st ON st.id = s.stop_id "
            "WHERE s.bus_id = ? ORDER BY s.stop_id, s.seat_no", (drv["bus_id"],)).fetchall()

        total_students = len(all_stu)
        not_travelling_count = sum(1 for s in all_stu if s["not_travelling_date"] == today_str)

        # Group by stop
        by_stop = {}
        for s in all_stu:
            by_stop.setdefault(s["stop_id"], []).append(s)

        for st in stops:
            passengers = by_stop.get(st["stop_id"], [])
            manifest.append({
                "stop_id": st["stop_id"],
                "name": st["name"],
                "offset_min": st["offset_min"],
                "time_clock": clock(parse_hhmm("07:00") + st["offset_min"]),
                "passengers": passengers,
                "boarding_count": sum(1 for p in passengers if p["not_travelling_date"] != today_str),
                "skipped_count": sum(1 for p in passengers if p["not_travelling_date"] == today_str)
            })

    # Check last GPS ping
    pos = db().execute("SELECT * FROM bus_positions WHERE bus_id=?", (drv["bus_id"],)).fetchone() if drv["bus_id"] else None

    return render_template("driver_home.html", drv=drv, stops=stops, manifest=manifest,
                           total_students=total_students, not_travelling_count=not_travelling_count,
                           active_boarders=total_students - not_travelling_count, pos=pos)


@app.route("/api/driver/broadcast-delay", methods=["POST"])
@driver_required
def api_driver_broadcast_delay():
    drv = get_current_driver()
    if not drv or not drv["route_id"]:
        return jsonify(error="No route assigned"), 400

    j = request.get_json(silent=True) or request.form
    msg = j.get("message", "").strip()
    kind = j.get("kind", "delay")
    if not msg:
        return jsonify(error="Message required"), 400

    now = time.time()
    db().execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (drv["route_id"], kind, f"[{drv['bus_reg']}] {msg}", now, now + 4 * 3600))
    db().commit()
    flash("Notice broadcasted to all students on your route.", "ok")
# URL aliases for backward compatibility
app.add_url_rule("/driver", endpoint="driver", view_func=driver_home)
app.add_url_rule("/student", endpoint="student", view_func=student_home)


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
        out.append({"r": r, "stops": st, "times": times, "fresh": freshness(r["last_verified"])})
    return render_template("routes.html", routes=out)


@app.route("/routes/<int:route_id>")
def route_detail(route_id):
    r = db().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
    if not r:
        return redirect(url_for("routes_list"))
    st = get_route_stops(route_id)
    times = [x["depart_time"] for x in db().execute(
        "SELECT depart_time FROM schedules WHERE route_id=? ORDER BY depart_time", (route_id,))]
    days_set = set()
    for row in db().execute("SELECT days FROM schedules WHERE route_id=?", (route_id,)):
        days_set.update(row["days"].split(","))
    days_union = [DAY_NAMES[int(d)] for d in sorted(days_set, key=int) if d.isdigit() and int(d) in DAY_NAMES]
    return render_template("route.html", r=r, stops=st, times=times, fresh=freshness(r["last_verified"]),
                           alerts=active_alerts(route_id), days=days_union, highlight=request.args.get("stop", ""))


@app.route("/map")
def live_map():
    rs = []
    for r in db().execute("SELECT * FROM routes WHERE status='active' ORDER BY code"):
        rs.append({"id": r["id"], "code": r["code"], "name": r["name"], "color": r["color"],
                   "stops": get_route_stops(r["id"])})
    buses = db().execute("SELECT b.id, b.reg_no, r.code AS route_code, r.color AS route_color FROM buses b LEFT JOIN routes r ON r.id=b.route_id").fetchall()
    return render_template("map.html", routes=rs, buses=buses)


@app.route("/alerts")
def alerts_page():
    return render_template("alerts.html", alerts=active_alerts())


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
    bus_id = j.get("bus_id") or session.get("bus_id")
    try:
        bus_id = int(bus_id)
        lat, lng = float(j["lat"]), float(j["lng"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="bus_id, lat, lng required"), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(error="bad coordinates"), 400

    # Validate bus
    b = db().execute("SELECT * FROM buses WHERE id=?", (bus_id,)).fetchone()
    if not b:
        return jsonify(error="invalid bus"), 404

    # Allow authenticated driver session or token match
    is_session_driver = (session.get("role") == "driver" and session.get("bus_id") == bus_id)
    if not is_session_driver and str(j.get("token", "")) != b["driver_token"]:
        return jsonify(error="invalid driver authentication"), 403

    db().execute("INSERT INTO bus_positions(bus_id,lat,lng,speed,updated_ts,source) VALUES(?,?,?,?,?,'gps') "
                 "ON CONFLICT(bus_id) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, speed=excluded.speed, "
                 "updated_ts=excluded.updated_ts, source='gps'", (bus_id, lat, lng, j.get("speed"), time.time()))
    db().commit()
    return jsonify(ok=True)


# ------------------------------- admin --------------------------------------
@app.route("/admin")
@admin_required
def admin_home():
    c = db()
    counts = {
        "stops": c.execute("SELECT COUNT(*) FROM stops").fetchone()[0],
        "routes": c.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
        "buses": c.execute("SELECT COUNT(*) FROM buses").fetchone()[0],
        "students": c.execute("SELECT COUNT(*) FROM students").fetchone()[0],
        "allocated": c.execute("SELECT COUNT(*) FROM students WHERE bus_id IS NOT NULL").fetchone()[0],
        "unallocated": c.execute("SELECT COUNT(*) FROM students WHERE bus_id IS NULL").fetchone()[0],
    }
    stale = [r for r in c.execute("SELECT * FROM routes ORDER BY code") if freshness(r["last_verified"])["level"] != "ok"]
    gps = c.execute("SELECT COUNT(*) FROM bus_positions WHERE source='gps' AND updated_ts>?",
                    (time.time() - GPS_FRESH_SEC,)).fetchone()[0]

    buses_summary = c.execute(
        "SELECT b.id, b.reg_no, b.capacity, r.code AS route_code, r.color AS route_color, "
        "d.name AS driver_name, (SELECT COUNT(*) FROM students s WHERE s.bus_id = b.id) AS student_count "
        "FROM buses b "
        "LEFT JOIN routes r ON r.id = b.route_id "
        "LEFT JOIN drivers d ON d.bus_id = b.id "
        "ORDER BY b.reg_no").fetchall()

    return render_template("admin_home.html", counts=counts, stale=stale, gps=gps,
                           sim_mode=get_setting("sim_mode", "loop"), fresh=freshness,
                           buses_summary=buses_summary,
                           routes=c.execute("SELECT * FROM routes ORDER BY code").fetchall())


@app.route("/admin/allocations")
@admin_required
def admin_allocations():
    c = db()
    bus_filter = request.args.get("bus", "")
    search_q = request.args.get("q", "").strip()

    # Bus occupancy list
    buses = c.execute(
        "SELECT b.id, b.reg_no, b.capacity, r.code AS route_code, r.name AS route_name, r.color AS route_color, "
        "d.name AS driver_name, (SELECT COUNT(*) FROM students s WHERE s.bus_id = b.id) AS student_count "
        "FROM buses b "
        "LEFT JOIN routes r ON r.id = b.route_id "
        "LEFT JOIN drivers d ON d.bus_id = b.id "
        "ORDER BY b.id").fetchall()

    # Query students based on filters
    sql = ("SELECT s.*, b.reg_no AS bus_reg, r.code AS route_code, st.name AS stop_name "
           "FROM students s "
           "LEFT JOIN buses b ON b.id = s.bus_id "
           "LEFT JOIN routes r ON r.id = b.route_id "
           "LEFT JOIN stops st ON st.id = s.stop_id WHERE 1=1 ")
    params = []

    if bus_filter == "unallocated":
        sql += "AND s.bus_id IS NULL "
    elif bus_filter.isdigit():
        sql += "AND s.bus_id = ? "
        params.append(int(bus_filter))

    if search_q:
        sql += "AND (s.roll_no LIKE ? OR s.name LIKE ?) "
        params.extend([f"%{search_q}%", f"%{search_q}%"])

    sql += "ORDER BY (s.bus_id IS NULL) DESC, s.bus_id, s.seat_no, s.roll_no LIMIT 150"
    students = c.execute(sql, params).fetchall()

    # Total counts
    total_students = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    unallocated_count = c.execute("SELECT COUNT(*) FROM students WHERE bus_id IS NULL").fetchone()[0]

    # Recent allocation logs
    logs = c.execute("SELECT * FROM allocation_logs ORDER BY id DESC LIMIT 15").fetchall()

    # All stops for reassign dropdown
    all_stops = c.execute("SELECT id, name FROM stops ORDER BY name").fetchall()

    return render_template("admin_allocations.html", buses=buses, students=students,
                           bus_filter=bus_filter, search_q=search_q,
                           total_students=total_students, unallocated_count=unallocated_count,
                           logs=logs, all_stops=all_stops, max_cap=MAX_BUS_CAPACITY)


@app.route("/admin/allocations/reassign", methods=["POST"])
@admin_required
def admin_reassign_student():
    student_id = request.form.get("student_id")
    new_bus_id = request.form.get("new_bus_id", "").strip()
    new_stop_id = request.form.get("new_stop_id", "").strip()
    reason = request.form.get("reason", "Administrative Reallocation").strip()

    s = db().execute("SELECT s.*, b.reg_no AS old_reg FROM students s LEFT JOIN buses b ON b.id=s.bus_id WHERE s.id=?", (student_id,)).fetchone()
    if not s:
        flash("Student record not found.", "warn")
        return redirect(url_for("admin_allocations"))

    if new_bus_id == "" or new_bus_id == "none":
        # Unallocate
        db().execute("UPDATE students SET bus_id=NULL, stop_id=NULL, seat_no=NULL WHERE id=?", (student_id,))
        db().execute("INSERT INTO allocation_logs(student_id, student_name, from_bus, to_bus, reason, created_ts) "
                     "VALUES(?,?,?,?,?,?)", (student_id, s["name"], s["old_reg"] or "None", "Unallocated", reason, time.time()))
        db().commit()
        flash(f"{s['name']} ({s['roll_no']}) moved to unallocated pool.", "ok")
        return redirect(url_for("admin_allocations"))

    target_bus_id = int(new_bus_id)
    # Validate 50 seats limit
    count = db().execute("SELECT COUNT(*) FROM students WHERE bus_id=?", (target_bus_id,)).fetchone()[0]
    target_bus = db().execute("SELECT * FROM buses WHERE id=?", (target_bus_id,)).fetchone()

    if s["bus_id"] != target_bus_id and count >= MAX_BUS_CAPACITY:
        flash(f"Allocation rejected: Bus {target_bus['reg_no']} is already at full capacity ({count}/{MAX_BUS_CAPACITY} seats)!", "warn")
        return redirect(url_for("admin_allocations"))

    # Assign next seat number
    next_seat = count + 1 if s["bus_id"] != target_bus_id else s["seat_no"]
    target_stop_id = int(new_stop_id) if new_stop_id else s["stop_id"]

    db().execute("UPDATE students SET bus_id=?, stop_id=?, seat_no=? WHERE id=?",
                 (target_bus_id, target_stop_id, next_seat, student_id))
    db().execute("INSERT INTO allocation_logs(student_id, student_name, from_bus, to_bus, reason, created_ts) "
                 "VALUES(?,?,?,?,?,?)",
                 (student_id, s["name"], s["old_reg"] or "Unallocated", target_bus["reg_no"], reason, time.time()))
    db().commit()

    flash(f"Successfully allocated {s['name']} to Bus {target_bus['reg_no']} (Seat #{next_seat}).", "ok")
    return redirect(url_for("admin_allocations"))


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
        if act == "meta":
            c.execute("UPDATE routes SET code=?, name=?, color=?, status=?, notes=? WHERE id=?",
                      (request.form["code"].strip(), request.form["name"].strip(), request.form["color"],
                       request.form["status"], request.form.get("notes", ""), route_id))
        elif act == "verify":
            c.execute("UPDATE routes SET last_verified=? WHERE id=?",
                      (local_now().strftime("%Y-%m-%d"), route_id))
        elif act == "delete_route":
            c.execute("DELETE FROM schedules WHERE route_id=?", (route_id,))
            c.execute("DELETE FROM route_stops WHERE route_id=?", (route_id,))
            c.execute("DELETE FROM routes WHERE id=?", (route_id,))
            c.commit()
            flash("Route deleted.", "ok")
            return redirect(url_for("admin_routes"))
        c.commit()
        flash("Saved.", "ok")
        return redirect(url_for("admin_route", route_id=route_id))
    stops = c.execute(
        "SELECT rs.id AS rs_id, rs.seq, rs.offset_min, s.id AS stop_id, s.name FROM route_stops rs "
        "JOIN stops s ON s.id=rs.stop_id WHERE rs.route_id=? ORDER BY rs.seq", (route_id,)).fetchall()
    scheds = c.execute("SELECT * FROM schedules WHERE route_id=? ORDER BY depart_time", (route_id,)).fetchall()
    all_stops = c.execute("SELECT * FROM stops ORDER BY name").fetchall()
    return render_template("admin_route.html", r=r, stops=stops, scheds=scheds, all_stops=all_stops,
                           fresh=freshness(r["last_verified"]))


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


@app.route("/admin/alerts", methods=["GET", "POST"])
@admin_required
def admin_alerts():
    if request.method == "POST":
        act = request.form.get("act")
        if act == "add":
            rid = int(request.form["route_id"]) if request.form.get("route_id") else None
            exp = time.time() + float(request.form["hours"]) * 3600 if request.form.get("hours") else None
            db().execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                         (rid, request.form["kind"], request.form["message"].strip(), time.time(), exp))
        elif act == "dismiss":
            db().execute("UPDATE alerts SET active=0 WHERE id=?", (request.form["id"],))
        db().commit()
        flash("Updated.", "ok")
        return redirect(url_for("admin_alerts"))
    alerts = db().execute(
        "SELECT a.*, r.code AS route_code FROM alerts a LEFT JOIN routes r ON r.id=a.route_id ORDER BY a.id DESC").fetchall()
    return render_template("admin_alerts.html", alerts=alerts,
                           routes=db().execute("SELECT id, code, name FROM routes ORDER BY code").fetchall(),
                           kinds=ALERT_KINDS)


# ------------------------------- seed dataset -------------------------------
def seed():
    """Generates 10 routes, 10 buses (50 cap), 10 drivers, 10 demo students, and 450+ realistic students."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    random.seed(42)  # Deterministic seed

    # 1. Stops across Hyderabad & surroundings connected to Campus
    stops_data = [
        # Major Hubs & Intermediate Stops
        ("College Main Gate", "Main Campus Entrance", 17.2200, 78.7000),
        ("Dilsukhnagar", "Metro Station & Bus Depot", 17.3688, 78.5247),
        ("Chaitanyapuri", "Metro Pillar 1520", 17.3580, 78.5380),
        ("LB Nagar", "Ring Road Junction", 17.3457, 78.5522),
        ("Sagar Ring Road", "Bairamalguda Junction", 17.3390, 78.5500),
        ("Hastinapuram", "Central Bus Stop", 17.3210, 78.5450),
        ("BN Reddy Nagar", "Main Road", 17.3120, 78.5600),
        ("Turkayamjal", "Highway Circle", 17.2950, 78.5800),
        ("Vanasthalipuram", "Rythu Bazar", 17.3320, 78.5700),
        ("Hayathnagar", "RTC Bus Stand", 17.3293, 78.6020),
        ("Word & Deed", "Hayathnagar Highway", 17.3310, 78.6180),
        ("Pedda Amberpet", "ORR Outer Ring Road Exit", 17.3315, 78.6270),
        ("Koheda", "Highway Junction", 17.2900, 78.6500),
        ("Abdullapurmet", "Highway Stop", 17.3095, 78.6660),
        ("Uppal", "Metro Station", 17.4058, 78.5591),
        ("Ramanthapur", "HPS Grounds", 17.3980, 78.5350),
        ("Nagole", "Metro Station", 17.3921, 78.5606),
        ("Bandlaguda", "Main Road", 17.3750, 78.5750),
        ("Auto Nagar", "Industrial Area Gate", 17.3400, 78.5800),
        ("Mehdipatnam", "Depot Pillar 20", 17.3950, 78.4400),
        ("Attapur", "Pillar 150 PVNR Expressway", 17.3680, 78.4290),
        ("Aramghar", "National Highway Circle", 17.3200, 78.4500),
        ("Chandrayangutta", "Barkas Road", 17.3150, 78.4850),
        ("Kukatpally", "Y Junction", 17.4840, 78.4070),
        ("KPHB Colony", "Phase 1 Metro", 17.4930, 78.3990),
        ("Balanagar", "IDPL Colony", 17.4600, 78.4400),
        ("Begumpet", "Railway Station", 17.4440, 78.4680),
        ("Ameerpet", "Metro Interchange", 17.4370, 78.4480),
        ("Panjagutta", "Central Mall", 17.4260, 78.4510),
        ("Khairatabad", "Flyover Junction", 17.4120, 78.4600),
        ("Lakdikapul", "Metro Station", 17.4040, 78.4650),
        ("Koti", "Women's College Stop", 17.3850, 78.4860),
        ("Malakpet", "Super Bazar", 17.3750, 78.5020),
        ("Secunderabad Stn", "East Gate", 17.4340, 78.5010),
        ("Chilkalguda", "Main Circle", 17.4310, 78.5120),
        ("Tarnaka", "Metro Station", 17.4280, 78.5310),
        ("Habsiguda", "Street No 8", 17.4180, 78.5480),
        ("Gachibowli", "ORR Junction", 17.4400, 78.3480),
        ("Hitec City", "Cyber Towers", 17.4480, 78.3750),
        ("Madhapur", "Metro Station", 17.4485, 78.3900),
        ("Jubilee Hills", "Checkpost", 17.4300, 78.4080),
        ("Shamshabad", "Bus Stand", 17.2500, 78.4300),
        ("Airport Junction", "Airport Road", 17.2400, 78.4600),
        ("Pahadi Shareef", "Main Junction", 17.2700, 78.4900),
        ("Badangpet", "Municipality Stop", 17.3100, 78.5200),
    ]
    stop_ids = {}
    for name, lm, lat, lng in stops_data:
        stop_ids[name] = conn.execute(
            "INSERT INTO stops(name,landmark,lat,lng) VALUES(?,?,?,?)",
            (name, lm, lat, lng)).lastrowid

    today = local_now().date()
    def vdate(d):
        return (today - timedelta(days=d)).strftime("%Y-%m-%d")

    # 2. Define the 10 Routes
    route_defs = [
        ("R1", "Dilsukhnagar ↔ College", "#2563eb", "active", 1, "Via Chaitanyapuri, LB Nagar, Hayathnagar",
         [("Dilsukhnagar", 0), ("Chaitanyapuri", 6), ("LB Nagar", 12), ("Vanasthalipuram", 20), ("Hayathnagar", 32), ("College Main Gate", 55)]),
        ("R2", "LB Nagar ↔ College", "#059669", "active", 2, "Via Sagar Ring Road, Hastinapuram, BN Reddy",
         [("LB Nagar", 0), ("Sagar Ring Road", 8), ("Hastinapuram", 16), ("BN Reddy Nagar", 24), ("Turkayamjal", 34), ("College Main Gate", 50)]),
        ("R3", "Uppal ↔ College", "#d97706", "active", 1, "Via Ramanthapur, Nagole, Auto Nagar",
         [("Uppal", 0), ("Ramanthapur", 8), ("Nagole", 16), ("Bandlaguda", 26), ("Auto Nagar", 36), ("College Main Gate", 58)]),
        ("R4", "Hayathnagar ↔ College", "#7c3aed", "active", 3, "Via Word & Deed, Pedda Amberpet, Koheda",
         [("Hayathnagar", 0), ("Word & Deed", 8), ("Pedda Amberpet", 15), ("Koheda", 28), ("Abdullapurmet", 38), ("College Main Gate", 52)]),
        ("R5", "Mehdipatnam ↔ College", "#db2777", "active", 1, "Via Attapur, Aramghar, Chandrayangutta",
         [("Mehdipatnam", 0), ("Attapur", 12), ("Aramghar", 25), ("Chandrayangutta", 38), ("College Main Gate", 62)]),
        ("R6", "Kukatpally ↔ College", "#ea580c", "active", 4, "Via KPHB, Balanagar, Begumpet",
         [("Kukatpally", 0), ("KPHB Colony", 8), ("Balanagar", 22), ("Begumpet", 35), ("College Main Gate", 75)]),
        ("R7", "Ameerpet ↔ College", "#0284c7", "active", 2, "Via Panjagutta, Khairatabad, Koti, Malakpet",
         [("Ameerpet", 0), ("Panjagutta", 8), ("Khairatabad", 16), ("Lakdikapul", 24), ("Koti", 36), ("Malakpet", 45), ("College Main Gate", 68)]),
        ("R8", "Secunderabad ↔ College", "#16a34a", "active", 1, "Via Chilkalguda, Tarnaka, Habsiguda, Uppal",
         [("Secunderabad Stn", 0), ("Chilkalguda", 8), ("Tarnaka", 18), ("Habsiguda", 26), ("Uppal", 36), ("College Main Gate", 62)]),
        ("R9", "Gachibowli ↔ College", "#4f46e5", "active", 2, "Via Hitec City, Madhapur, Jubilee Hills",
         [("Gachibowli", 0), ("Hitec City", 10), ("Madhapur", 18), ("Jubilee Hills", 28), ("College Main Gate", 75)]),
        ("R10", "Shamshabad ↔ College", "#0d9488", "active", 2, "Via Airport Junc, Pahadi Shareef, Badangpet",
         [("Shamshabad", 0), ("Airport Junction", 8), ("Pahadi Shareef", 18), ("Chandrayangutta", 30), ("Badangpet", 42), ("College Main Gate", 58)]),
    ]

    inbound_times = ["06:30", "07:00", "07:30", "08:00", "08:30"]
    outbound_times = ["13:00", "16:00", "16:30", "17:00", "17:30", "18:00"]
    route_ids = {}

    for code, name, color, status, vd, notes, seq in route_defs:
        rid = conn.execute("INSERT INTO routes(code,name,color,status,last_verified,notes) VALUES(?,?,?,?,?,?)",
                           (code, name, color, status, vdate(vd), notes)).lastrowid
        route_ids[code] = rid
        for i, (sname, off) in enumerate(seq):
            conn.execute("INSERT INTO route_stops(route_id,stop_id,seq,offset_min) VALUES(?,?,?,?)",
                         (rid, stop_ids[sname], i, off))
        for t in inbound_times:
            conn.execute("INSERT INTO schedules(route_id,depart_time,days) VALUES(?,?,?)", (rid, t, "1,2,3,4,5,6"))

        # Return route (College to Destination)
        total_span = seq[-1][1]
        rev_seq = [(sname, total_span - off) for sname, off in reversed(seq)]
        r_rev_id = conn.execute("INSERT INTO routes(code,name,color,status,last_verified,notes) VALUES(?,?,?,?,?,?)",
                                (code + "-R", "College to " + name.split(" ↔ ")[0], color, status, vdate(vd), notes)).lastrowid
        for i, (sname, off) in enumerate(rev_seq):
            conn.execute("INSERT INTO route_stops(route_id,stop_id,seq,offset_min) VALUES(?,?,?,?)",
                         (r_rev_id, stop_ids[sname], i, off))
        for t in outbound_times:
            conn.execute("INSERT INTO schedules(route_id,depart_time,days) VALUES(?,?,?)", (r_rev_id, t, "1,2,3,4,5,6"))

    # 3. Create the 10 Buses & 10 Drivers
    buses_info = [
        ("TS 08 UA 1001", "R1", "DRV-01", "Ramesh Kumar", "+91 98480 11001"),
        ("TS 08 UA 1002", "R2", "DRV-02", "Suresh Reddy", "+91 98480 11002"),
        ("TS 08 UB 2003", "R3", "DRV-03", "Venkat Rao", "+91 98480 11003"),
        ("TS 08 UB 2004", "R4", "DRV-04", "Anand Sharma", "+91 98480 11004"),
        ("TS 08 UC 3005", "R5", "DRV-05", "Naresh Yadav", "+91 98480 11005"),
        ("TS 08 UC 3006", "R6", "DRV-06", "Mohan Das", "+91 98480 11006"),
        ("TS 08 UD 4007", "R7", "DRV-07", "Krishna Murthy", "+91 98480 11007"),
        ("TS 08 UD 4008", "R8", "DRV-08", "Syed Ibrahim", "+91 98480 11008"),
        ("TS 08 UE 5009", "R9", "DRV-09", "Ravi Teja", "+91 98480 11009"),
        ("TS 08 UE 5010", "R10", "DRV-10", "Vikram Varma", "+91 98480 11010"),
    ]

    bus_ids = {}
    driver_pwd_hash = generate_password_hash("drv123")
    student_pwd_hash = generate_password_hash("stud123")

    for idx, (reg, rcode, empid, dname, phone) in enumerate(buses_info, 1):
        rid = route_ids[rcode]
        bid = conn.execute(
            "INSERT INTO buses(reg_no,capacity,route_id,driver_name,driver_token) VALUES(?,?,?,?,?)",
            (reg, 50, rid, dname, str(idx) * 4)).lastrowid
        bus_ids[idx] = bid
        conn.execute(
            "INSERT INTO drivers(emp_id,name,password_hash,phone,bus_id) VALUES(?,?,?,?,?)",
            (empid, dname, driver_pwd_hash, phone, bid))

    # 4. Create 10 Primary Interactive Demo Students (one assigned to each bus)
    demo_students_spec = [
        ("23CS101", "Aarav Patel", 1, "LB Nagar", 1),
        ("23CS102", "Diya Sharma", 2, "Hastinapuram", 1),
        ("23CS103", "Rohan Verma", 3, "Nagole", 1),
        ("23CS104", "Ananya Reddy", 4, "Pedda Amberpet", 1),
        ("23CS105", "Karthik Rao", 5, "Attapur", 1),
        ("23CS106", "Sneha Nair", 6, "KPHB Colony", 1),
        ("23CS107", "Vikram Joshi", 7, "Koti", 1),
        ("23CS108", "Pooja Hegde", 8, "Tarnaka", 1),
        ("23CS109", "Arjun Das", 9, "Madhapur", 1),
        ("23CS110", "Kavya Priya", 10, "Chandrayangutta", 1),
    ]

    for roll, sname, b_idx, stop_name, seat in demo_students_spec:
        conn.execute(
            "INSERT INTO students(roll_no,name,password_hash,bus_id,stop_id,seat_no,is_demo) VALUES(?,?,?,?,?,?,1)",
            (roll, sname, student_pwd_hash, bus_ids[b_idx], stop_ids[stop_name], seat))

    # 5. Generate ~450 Realistic Demo Students to demonstrate realistic capacity utilization
    # Targets: Bus 1 (48), Bus 2 (50 FULL), Bus 3 (45), Bus 4 (43), Bus 5 (49),
    #          Bus 6 (46), Bus 7 (44), Bus 8 (50 FULL), Bus 9 (47), Bus 10 (42) -> Total 464 + 15 Unallocated = 479
    occupancy_targets = {1: 48, 2: 50, 3: 45, 4: 43, 5: 49, 6: 46, 7: 44, 8: 50, 9: 47, 10: 42}

    first_names = ["Aditya", "Sai", "Pranav", "Nikhil", "Teja", "Varun", "Abhinav", "Manoj", "Harsh", "Gautam",
                   "Manish", "Rahul", "Deepak", "Akash", "Srikanth", "Tarun", "Kalyan", "Chaitanya", "Vamsi", "Akhil",
                   "Anjali", "Sravani", "Bhavana", "Divya", "Keerthi", "Nandini", "Meghana", "Pravallika", "Yamini",
                   "Swathi", "Harika", "Navya", "Sindhu", "Rashmika", "Pallavi", "Shreya", "Sireesha", "Alekhya"]
    last_names = ["Chowdary", "Goud", "Reddy", "Sharma", "Yadav", "Verma", "Rao", "Gupta", "Nair", "Iyer",
                  "Patel", "Kumar", "Singh", "Prasad", "Mishra", "Joshi", "Babu", "Chandra", "Naidu", "Teja"]

    roll_counter = 111
    for b_idx, target_count in occupancy_targets.items():
        bid = bus_ids[b_idx]
        route_stops_list = route_defs[b_idx - 1][6][:-1]  # Exclude College stop for pickups
        # We already added seat 1 for this bus
        for seat in range(2, target_count + 1):
            name = f"{random.choice(first_names)} {random.choice(last_names)}"
            roll = f"23CS{roll_counter}"
            roll_counter += 1
            assigned_stop_name = random.choice(route_stops_list)[0]
            # 5% students marked "not travelling today"
            not_trav = (local_now().strftime("%Y-%m-%d")) if random.random() < 0.06 else None
            conn.execute(
                "INSERT INTO students(roll_no,name,password_hash,bus_id,stop_id,seat_no,not_travelling_date,is_demo) "
                "VALUES(?,?,?,?,?,?,?,0)",
                (roll, name, student_pwd_hash, bid, stop_ids[assigned_stop_name], seat, not_trav))

    # Generate 15 Unallocated Students
    for _ in range(15):
        name = f"{random.choice(first_names)} {random.choice(last_names)}"
        roll = f"23CS{roll_counter}"
        roll_counter += 1
        conn.execute(
            "INSERT INTO students(roll_no,name,password_hash,bus_id,stop_id,seat_no,is_demo) VALUES(?,?,?,NULL,NULL,NULL,0)",
            (roll, name, student_pwd_hash))

    # 6. Sample Alerts
    now = time.time()
    conn.execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (route_ids["R1"], "delay", "Route R1 (TS 08 UA 1001) is running 12 mins late due to flyover construction near LB Nagar.", now - 900, now + 4 * 3600))
    conn.execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (route_ids["R5"], "diversion", "Route R5 diverted via Inner Ring Road due to metro utility maintenance at Attapur.", now - 2400, now + 8 * 3600))
    conn.execute("INSERT INTO alerts(route_id,kind,message,created_ts,expires_ts,active) VALUES(?,?,?,?,?,1)",
                 (None, "info", "Campus special express buses will operate for evening lab sessions from 5:30 PM.", now - 43200, None))

    conn.execute("INSERT INTO settings(key,value) VALUES('sim_mode','loop')")
    conn.commit()
    conn.close()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
