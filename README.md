# CollegeNav 2.0 • Campus Fleet Tracking & Transit Management System

Intelligent College Bus Navigation & Real-Time Fleet Platform for **CVR College of Engineering**.
Built with Python 3 + Flask, SQLite (WAL mode), Jinja2 templates, Leaflet (OpenStreetMap), Tailwind CDN, and the **Crimson Night** unified design system.

---

## 🚀 Fast Launch

```bash
# Using uv (recommended)
uv run --with flask,tzdata python app.py

# Or standard virtual environment:
python -m venv venv
venv\Scripts\activate       # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
python app.py
```
App runs locally at: **`http://localhost:5000`**

---

## 🔑 Login Credentials

Direct Unified Sign In at **`/login`**:

| Role | Identifier | Password | Access / Purpose |
| :--- | :--- | :--- | :--- |
| **Student** | Roll No: `23CS101` (Aarav Patel) | `stud123` | My Bus ETA hero, assigned bus GPS map, 5-min alert, "Not travelling today" skip, Help & SOS |
| **Driver (Regular)** | Emp ID: `DRV-01` (Ramesh Kumar) | `drv123` | Duty console, Live GPS broadcast, **"Arrived at Stop"** countdown, 6 one-tap student alerts, student manifest |
| **Driver (Substitute)** | Emp ID: `DRV-SUB` (Suresh Babu) | `drv123` | Guided substitute mode with turn-by-turn navigation, voice directions, stop photos, and route tips |
| **Transport Admin** | Username: `admin` | `admin123` | 236px desktop control room, active SOS alert banner, 50-seat hard capacity guard, Help Desk, substitute dispatcher |

---

## 🌟 Key CollegeNav 2.0 Features

### 1. "Crimson Night" Design System
* **Deep Navy & Crimson Palette**: `#161E2F` (page bg), `#242F49` (cards), `#FFA586` (primary peach accent), `#B51A2B` → `#541A2E` (hero gradient), `#E5384B` (urgent emergency).
* **Typography**: Plus Jakarta Sans everywhere (Google Fonts).
* **Custom SVG Brand**: Custom bus-in-pin logo (`/static/logo.svg` & `/static/favicon.svg`).
* **Clean Line Icons**: Inline SVGs with 2px stroke and round caps (no emoji icons).

### 2. Student Experience (Mobile-First 390px)
* **Order of Interface**:
  1. Header with greeting and route chip (`R1 · TS 08 UA 1001`).
  2. **Arrival Hero Card**: Massive ETA countdown, expected time, stops away, and green gradient when bus is at stop or ≤ 2 min away.
  3. **Live GPS Map**: Centered strictly on the student's assigned bus and route.
  4. **Driver Card**: Avatar initial, driver name, and direct phone link (no capacity/seat crowding shown to students).
  5. **5-Min Approaching Alert**: Toggle with sound and browser notification preference saved per student.
  6. **"Not travelling today"**: One-tap skip with instant Undo option.
* **4 Floating Tabs**: My Bus, Routes (evening destination search & 1-day pass requests), Notices, and Help.

### 3. Emergency SOS & Help Desk
* **3-Second Hold SOS**: Circular SVG progress ring button prevents accidental triggers.
* **Incident Reasons**: Medical, Harassment, Accident, Feeling Unsafe, Other.
* Transmits live coordinates and displays an acknowledgement badge when the transport desk acknowledges the alert.
* **Help Desk**: Centralized ticketing for complaints, bus-missed reports, and driver breakdown alerts.

### 4. Driver Duty Console
* **GPS Telemetry**: Live phone GPS broadcasting taking full priority.
* **"Arrived at Stop"**: Instantly signals "Bus is here!" to students at that stop with a 2-minute countdown timer.
* **6 Quick Messages**: Late 5m, Late 15m, Heavy Traffic, Route Diverted, Leaving Now, Breakdown (Urgent).
* **Passenger Manifest**: Live counters for boarding, skipping (crossed out in red), and picked up (one-tap tickbox).
* **Guided Substitute Mode**: Automatic route guidance, turn-by-turn Google Maps link, browser speech synthesis voice guide, landmark directions, and stop preview.

### 5. Fleet Control & Capacity
* **Strict 50-Seat Limit**: Hard capacity limit prevents over-allocation.
* **Substitute Driver Dispatcher**: Easily assign substitute drivers with date ranges and automated student notifications.

---

## ⏱️ 2-Minute Presentation Demo Script

1. **Step 1: Student View (0:00 - 0:40)**
   - Sign in as Student (`23CS101` / `stud123`).
   - Highlight the **Crimson Night** design, ETA hero countdown, and strict assigned bus map.
   - Show the "Not travelling today" button with Undo.
   - Switch to **Help** tab and demonstrate the 3-second hold SOS trigger.
2. **Step 2: Admin Control Room (0:40 - 1:15)**
   - Sign in as Admin (`admin` / `admin123`).
   - Point out the active emergency banner for the SOS with student details and acknowledge button.
   - Show the 4 stats cards, live fleet radar, and 50-seat bus capacity strip.
   - Open **Help Desk** to review tickets and assign a substitute driver in **Buses & Drivers**.
3. **Step 3: Driver Console (1:15 - 2:00)**
   - Sign in as Driver (`DRV-01` / `drv123`).
   - Tap **Start Trip** (starts live GPS broadcasting).
   - Tap **Arrived at Stop** and demonstrate the 2-minute wait timer.
   - Tap a quick message (e.g. **Late 5 min**) and open **Students** manifest to tick off passengers.
