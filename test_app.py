import os
os.environ["DB_PATH"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collegenav_test.db")
import time
import json
import pytest
from app import app, db, init_db, MAX_BUS_CAPACITY

# Seed test database once
with app.app_context():
    init_db()

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        with app.test_client() as client:
            yield client



def test_public_pages(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"College" in r.data
    assert b"Nav" in r.data

    r = client.get("/routes")
    assert r.status_code == 200

    r = client.get("/map")
    assert r.status_code == 200

    r = client.get("/alerts")
    assert r.status_code == 200

    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_login_and_demo_pages(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"CollegeNav 2.0" in r.data
    assert b"Student" in r.data
    assert b"Driver" in r.data
    assert b"Admin" in r.data

    # Demo route redirects to login
    r = client.get("/demo")
    assert r.status_code == 302



def test_student_login_and_home(client):
    # Log in as demo student
    r = client.post("/login", data={
        "role": "student",
        "roll_no": "23CS101",
        "password": "stud123"
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Aarav" in r.data or b"My Bus" in r.data
    assert b"ARRIVING AT YOUR STOP" in r.data or b"Bus is here" in r.data or b"On Schedule" in r.data

    # Check notify preference API
    r_pref = client.post("/api/student/notify-pref", json={"notify_5min": True})
    assert r_pref.status_code == 200
    assert r_pref.get_json()["notify_5min"] is True

    # Check status polling API
    r_stat = client.get("/api/student/status")
    assert r_stat.status_code == 200
    assert r_stat.get_json()["ok"] is True


def test_driver_login_and_home(client):
    # Log in as driver
    r = client.post("/login", data={
        "role": "driver",
        "emp_id": "DRV-01",
        "password": "drv123"
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Ramesh" in r.data or b"NEXT STOP" in r.data
    assert b"TELL STUDENTS" in r.data

    # Check status polling API
    r_stat = client.get("/api/driver/status")
    assert r_stat.status_code == 200
    assert r_stat.get_json()["ok"] is True


def test_admin_login_and_overview(client):
    # Log in as admin
    r = client.post("/login", data={
        "role": "admin",
        "password": "admin123"
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Transit Operations" in r.data or b"Buses Right Now" in r.data or b"Fleet Overview" in r.data


def test_sos_lifecycle(client):
    # 1. Create SOS as student
    client.post("/login", data={"role": "student", "roll_no": "23CS101", "password": "stud123"})
    r = client.post("/api/sos", json={"lat": 17.3457, "lng": 78.5522, "reason": "Medical emergency"})
    assert r.status_code == 200
    sos_id = r.get_json()["id"]
    assert sos_id > 0

    # 2. Update location
    r_loc = client.post(f"/api/sos/{sos_id}/location", json={"lat": 17.3460, "lng": 78.5525})
    assert r_loc.status_code == 200

    # 3. Update reason
    r_reas = client.post(f"/api/sos/{sos_id}/reason", json={"reason": "Accident"})
    assert r_reas.status_code == 200

    # 4. Admin acknowledges SOS
    with client.session_transaction() as sess:
        sess["admin"] = True
        sess["role"] = "admin"
    r_ack = client.post(f"/api/admin/sos/{sos_id}/ack")
    assert r_ack.status_code == 200
    assert r_ack.get_json()["ok"] is True

    # Check location update acknowledges flag
    r_loc2 = client.post(f"/api/sos/{sos_id}/location", json={"lat": 17.3460, "lng": 78.5525})
    assert r_loc2.get_json()["acknowledged"] is True

    # 5. Cancel SOS
    r_cancel = client.post(f"/api/sos/{sos_id}/cancel")
    assert r_cancel.status_code == 200
    assert r_cancel.get_json()["ok"] is True


def test_help_requests_and_admin_update(client):
    # Student files complaint
    client.post("/login", data={"role": "student", "roll_no": "23CS101", "password": "stud123"})
    r = client.post("/api/help-requests", json={
        "type": "complaint",
        "reason": "Driving & Speed",
        "message": "Bus driver was driving recklessly near turn."
    })
    assert r.status_code == 200
    req_id = r.get_json()["id"]

    # Student retrieves requests
    r_list = client.get("/api/help-requests")
    assert r_list.status_code == 200
    assert len(r_list.get_json()["requests"]) >= 1

    # Admin updates ticket
    with client.session_transaction() as sess:
        sess["admin"] = True
        sess["role"] = "admin"
    r_upd = client.post(f"/api/admin/help-requests/{req_id}/update", json={
        "status": "resolved",
        "admin_note": "Spoke to driver and issued safety advisory."
    })
    assert r_upd.status_code == 200
    assert r_upd.get_json()["ok"] is True


def test_driver_arrived_and_quick_messages(client):
    client.post("/login", data={"role": "driver", "emp_id": "DRV-01", "password": "drv123"})

    # Arrived at stop
    r_arr = client.post("/api/driver/arrived", json={"stop_id": 1})
    assert r_arr.status_code == 200
    assert r_arr.get_json()["ok"] is True

    # Quick message: Late 5 min
    r_qm = client.post("/api/driver/quick-message", json={"code": "late_5"})
    assert r_qm.status_code == 200
    assert r_qm.get_json()["ok"] is True

    # Quick message: Breakdown (should create urgent help desk request)
    r_bd = client.post("/api/driver/quick-message", json={"code": "breakdown", "stop_name": "LB Nagar"})
    assert r_bd.status_code == 200
    assert r_bd.get_json()["ok"] is True


def test_driver_pickups(client):
    client.post("/login", data={"role": "driver", "emp_id": "DRV-01", "password": "drv123"})

    # Pick student
    r = client.post("/api/driver/pickup", json={"student_id": 1, "action": "pick"})
    assert r.status_code == 200
    assert r.get_json()["picked"] is True

    # Unpick student
    r_un = client.post("/api/driver/pickup", json={"student_id": 1, "action": "unpick"})
    assert r_un.status_code == 200
    assert r_un.get_json()["picked"] is False


def test_substitute_assignment_lifecycle(client):
    # Admin assigns substitute
    with client.session_transaction() as sess:
        sess["admin"] = True
        sess["role"] = "admin"

    sub_driver = db().execute("SELECT id FROM drivers WHERE emp_id='DRV-SUB'").fetchone()
    assert sub_driver is not None

    r = client.post("/admin/substitute/assign", data={
        "bus_id": 1,
        "substitute_driver_id": sub_driver["id"],
        "start_date": "2026-10-15",
        "end_date": "2026-10-25",
        "note": "Ramesh on medical leave"
    }, follow_redirects=True)
    assert r.status_code == 200

    # Log in as substitute driver and verify context
    client.post("/login", data={"role": "driver", "emp_id": "DRV-SUB", "password": "drv123"})
    r_ctx = client.get("/api/driver/substitute-context")
    assert r_ctx.status_code == 200
    ctx = r_ctx.get_json()
    assert ctx["is_substitute"] is True
    assert len(ctx["stops"]) > 0

    # End substitute assignment
    with client.session_transaction() as sess:
        sess["admin"] = True
        sess["role"] = "admin"
    sub_row = db().execute("SELECT id FROM substitute_assignments WHERE active=1 LIMIT 1").fetchone()
    if sub_row:
        r_end = client.post(f"/admin/substitute/{sub_row['id']}/end", follow_redirects=True)
        assert r_end.status_code == 200


def test_routes_search_api(client):
    r = client.get("/api/routes/search?to=LB+Nagar")
    assert r.status_code == 200
    data = r.get_json()
    assert "destination" in data
    assert "results" in data


def test_student_travel_skip_toggle(client):
    client.post("/login", data={"role": "student", "roll_no": "23CS101", "password": "stud123"})
    r = client.post("/api/student/toggle-travel")
    assert r.status_code == 200
    assert "travelling" in r.get_json()


def test_hard_capacity_limit(client):
    with client.session_transaction() as sess:
        sess["admin"] = True
        sess["role"] = "admin"

    # Attempt to allocate to a full bus (Bus 2 is seeded with 50/50 full capacity)
    unalloc = db().execute("SELECT id FROM students WHERE bus_id IS NULL LIMIT 1").fetchone()
    if unalloc:
        r = client.post("/admin/allocations/reassign", data={
            "student_id": unalloc["id"],
            "new_bus_id": "2",  # Bus 2 is full at 50/50
            "new_stop_id": "4",
            "reason": "Test capacity limit"
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b"full capacity" in r.data or b"rejected" in r.data
