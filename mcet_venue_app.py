"""
MCET Smart Hall & Venue Availability Management System
Dr. Mahalingam College of Engineering and Technology, Pollachi

BACKEND (Flask + SQLite + JSON API). The whole frontend is the single file index.html next to this one.

HOW TO RUN IN VS CODE
  1. Install Python 3.10+ (python.org). On Windows tick "Add Python to PATH".
  2. Save this file as  mcet_venue_app.py  in an empty folder and open that folder in VS Code.
  3. In the VS Code terminal run:
         pip install flask openpyxl
         python mcet_venue_app.py
  4. Open http://127.0.0.1:5000 in your browser.

On first run a database file  mcet_venue.db  is created next to this file with sample venues,
sample bookings and these demo logins (change the passwords after signing in):
     Admin            ID: admin                 password: admin@123
     Hall Supervisor  ID: sup-cse               password: super@123
     Student          email: student@mcet.local password: student@123
To start fresh, stop the app, delete mcet_venue.db and run again.

Three logins: Student, Hall Supervisor, Admin.
Conflict prevention: SQLite triggers make overlapping approved bookings on the same venue impossible;
approving one request auto-rejects every overlapping pending request.
"""
import io
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, session,
)
from werkzeug.security import check_password_hash, generate_password_hash


# ===========================================================================
# PART 1 — DATABASE (schema, conflict triggers, sample data)
# ===========================================================================
DB_PATH = Path(__file__).with_name("mcet_venue.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS departments (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT NOT NULL CHECK (role IN ('student', 'faculty', 'supervisor', 'admin')),
    full_name     TEXT NOT NULL,
    login_id      TEXT NOT NULL UNIQUE,          -- roll number (students) or staff ID
    email         TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    department    TEXT REFERENCES departments(code),
    phone         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS venues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    venue_type    TEXT NOT NULL DEFAULT 'SEMINAR_HALL',
    block         TEXT NOT NULL DEFAULT '',
    capacity      INTEGER NOT NULL CHECK (capacity > 0),
    facilities    TEXT NOT NULL DEFAULT '',      -- comma separated
    description   TEXT,
    supervisor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    open_time     TEXT NOT NULL DEFAULT '08:00',
    close_time    TEXT NOT NULL DEFAULT '18:00',
    is_active     INTEGER NOT NULL DEFAULT 1,
    CHECK (close_time > open_time)
);

CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_no       TEXT UNIQUE,
    venue_id         INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    requested_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    department       TEXT,
    event_title      TEXT NOT NULL,
    purpose          TEXT,
    attendees        INTEGER NOT NULL DEFAULT 1 CHECK (attendees > 0),
    faculty_incharge TEXT,
    start_at         TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM' (college local time)
    end_at           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','APPROVED','REJECTED','CANCELLED','BLOCKED')),
    decided_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decision_note    TEXT,
    decided_at       TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (end_at > start_at)
);
CREATE INDEX IF NOT EXISTS idx_bookings_venue_time ON bookings (venue_id, start_at);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status);

-- ===== THE conflict guarantee =====
CREATE TRIGGER IF NOT EXISTS bookings_no_overlap_insert
BEFORE INSERT ON bookings
WHEN NEW.status IN ('APPROVED', 'BLOCKED')
BEGIN
    SELECT RAISE(ABORT, 'SLOT_CONFLICT: this slot is already allotted')
    WHERE EXISTS (
        SELECT 1 FROM bookings
        WHERE venue_id = NEW.venue_id
          AND status IN ('APPROVED', 'BLOCKED')
          AND start_at < NEW.end_at AND NEW.start_at < end_at
    );
END;

CREATE TRIGGER IF NOT EXISTS bookings_no_overlap_update
BEFORE UPDATE OF status, start_at, end_at, venue_id ON bookings
WHEN NEW.status IN ('APPROVED', 'BLOCKED')
BEGIN
    SELECT RAISE(ABORT, 'SLOT_CONFLICT: this slot is already allotted')
    WHERE EXISTS (
        SELECT 1 FROM bookings
        WHERE venue_id = NEW.venue_id
          AND id <> NEW.id
          AND status IN ('APPROVED', 'BLOCKED')
          AND start_at < NEW.end_at AND NEW.start_at < end_at
    );
END;

-- Booking numbers like BK-1001
CREATE TRIGGER IF NOT EXISTS bookings_number
AFTER INSERT ON bookings
WHEN NEW.booking_no IS NULL
BEGIN
    UPDATE bookings SET booking_no = 'BK-' || (1000 + NEW.id) WHERE id = NEW.id;
END;
"""

DEPARTMENTS = [
    ("AUTO", "Automobile Engineering"),
    ("CIVIL", "Civil Engineering"),
    ("CSE", "Computer Science and Engineering"),
    ("AIDS", "Artificial Intelligence and Data Science"),
    ("ECE", "Electronics and Communication Engineering"),
    ("EEE", "Electrical and Electronics Engineering"),
    ("EIE", "Electronics and Instrumentation Engineering"),
    ("IT", "Information Technology"),
    ("MECH", "Mechanical Engineering"),
    ("MCT", "Mechatronics Engineering"),
    ("SH", "Science and Humanities"),
    ("MBA", "Master of Business Administration"),
    ("MCA", "Master of Computer Applications"),
    ("CLUBS", "Clubs, Chapters and Associations"),
    ("ADMIN", "Administration / Office"),
]

# Sample venues — edit these from Admin → Venues to match the real halls.
VENUES = [
    # code, name, type, block, capacity, facilities, opens, closes
    ("MECH-HALL", "Mechanic Hall", "SEMINAR_HALL", "Mechanical Block", 192, "Projector, Sound system, AC", "08:00", "18:00"),
    ("CS-HALL", "CS Hall", "SEMINAR_HALL", "CSE Block", 500, "Projector, Sound system, AC, Stage", "08:00", "18:00"),
    ("NEW-SEM", "New Seminar Hall", "SEMINAR_HALL", "Main Block", 192, "Projector, Sound system, AC", "08:00", "18:00"),
    ("EEE-HALL", "Electrical Hall", "SEMINAR_HALL", "EEE Block", 177, "Projector, Sound system, AC", "08:00", "18:00"),
    ("CC-HALL", "CC Hall", "AUDITORIUM", "CC Block", 3000, "Projector, Sound system, Stage, AC, Green room", "08:00", "20:00"),
    ("A416", "A416", "CLASSROOM", "A Block", 100, "Projector, Wi-Fi", "08:00", "18:00"),
    ("A415", "A415", "CLASSROOM", "A Block", 100, "Projector, Wi-Fi", "08:00", "18:00"),
    ("A414", "A414", "CLASSROOM", "A Block", 100, "Projector, Wi-Fi", "08:00", "18:00"),
    ("A413", "A413", "CLASSROOM", "A Block", 100, "Projector, Wi-Fi", "08:00", "18:00"),
    ("A412", "A412", "CLASSROOM", "A Block", 100, "Projector, Wi-Fi", "08:00", "18:00"),
    ("NPT-SEM1", "NPT Seminar Hall 1", "SEMINAR_HALL", "NPT Block", 120, "Projector, Sound system, AC", "08:00", "18:00"),
    ("NPT-SEM2", "NPT Seminar Hall 2", "SEMINAR_HALL", "NPT Block", 120, "Projector, Sound system, AC", "08:00", "18:00"),
]

# Demo accounts created on first run (change the passwords after signing in!)
DEMO_USERS = [
    # role, full name, login_id, email, password, dept
    ("admin", "Admin Office", "admin", None, "admin@123", "ADMIN"),
    ("supervisor", "Mr. K. Senthil", "sup-cse", None, "super@123", "CSE"),
    ("supervisor", "Ms. P. Divya", "sup-aud", None, "super@123", "EEE"),
    ("faculty", "Dr. S. Kumar", "fac-cse", "faculty@mcet.local", "faculty@123", "CSE"),
    ("student", "Demo Student", "727622bcs001", "student@mcet.local", "student@123", "CSE"),
    ("student", "Arjun S", "727622bec012", "arjun@mcet.local", "student@123", "ECE"),
]


def get_db():
    """One SQLite connection per request."""
    if "db" not in g:
        g.db = connect()
    return g.db


def connect():
    conn = sqlite3.connect(DB_PATH, detect_types=0, isolation_level=None)  # autocommit; we BEGIN explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(with_demo_data=True):
    """Create tables and, on a brand-new database, seed departments, venues, demo users and bookings."""
    fresh = not DB_PATH.exists()
    conn = connect()
    conn.executescript(SCHEMA)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    if "'faculty'" not in ddl:  # upgrade an older database: allow the new faculty role
        new = re.search(r"CREATE TABLE IF NOT EXISTS users \(.*?\n\);", SCHEMA, re.S).group(0).replace("IF NOT EXISTS users", "users_new")
        conn.executescript("PRAGMA foreign_keys=OFF; BEGIN;" + new + "INSERT INTO users_new SELECT * FROM users;"
                           "DROP TABLE users; ALTER TABLE users_new RENAME TO users; COMMIT; PRAGMA foreign_keys=ON;")
    if not fresh and not conn.execute("SELECT 1 FROM users WHERE role='faculty'").fetchone():
        conn.execute("INSERT OR IGNORE INTO users (role, full_name, login_id, email, password_hash, department) VALUES (?,?,?,?,?,?)",
                     ("faculty", "Dr. S. Kumar", "fac-cse", "faculty@mcet.local", generate_password_hash("faculty@123"), "CSE"))
    if fresh:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO departments (code, name, sort_order) VALUES (?, ?, ?)",
            [(c, n, i) for i, (c, n) in enumerate(DEPARTMENTS)],
        )
        conn.executemany(
            "INSERT INTO venues (code, name, venue_type, block, capacity, facilities, open_time, close_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            VENUES,
        )
        ids = {}
        for role, name, login_id, email, pw, dept in DEMO_USERS:
            cur = conn.execute(
                "INSERT INTO users (role, full_name, login_id, email, password_hash, department) VALUES (?,?,?,?,?,?)",
                (role, name, login_id, email, generate_password_hash(pw), dept),
            )
            ids[login_id] = cur.lastrowid
        # assign supervisors to halls
        conn.execute("UPDATE venues SET supervisor_id=? WHERE code IN ('CS-HALL','A416','A415','A414','A413','A412')", (ids["sup-cse"],))
        conn.execute("UPDATE venues SET supervisor_id=? WHERE code IN ('CC-HALL','NEW-SEM')", (ids["sup-aud"],))
        if with_demo_data:
            _seed_bookings(conn, ids)
        conn.execute("COMMIT")
    conn.close()
    return fresh


def _seed_bookings(conn, ids):
    venue = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM venues")}
    today = datetime.now().date()

    def at(day, hhmm):
        return f"{today + timedelta(days=day):%Y-%m-%d} {hhmm}"

    rows = [
        # venue, title, day, start, end, status, requester, dept, attendees
        ("CC-HALL", "Annual Day Rehearsal", 0, "09:00", "12:30", "APPROVED", "sup-aud", "CLUBS", 2000),
        ("CC-HALL", "Placement Orientation", 1, "14:00", "16:00", "PENDING", "727622bec012", "ECE", 900),
        ("CS-HALL", "IEEE Guest Lecture: GenAI", 0, "10:00", "12:00", "APPROVED", "727622bcs001", "CSE", 300),
        ("CS-HALL", "Coding Club Meetup", 1, "13:00", "14:30", "PENDING", "727622bcs001", "CSE", 60),
        ("CS-HALL", "Robotics Workshop", 1, "13:30", "15:30", "PENDING", "727622bec012", "ECE", 90),
        ("CS-HALL", "Projector repair", 1, "16:00", "17:00", "BLOCKED", "sup-cse", "CSE", 1),
        ("EEE-HALL", "EEE Symposium", 0, "09:30", "13:00", "APPROVED", "727622bec012", "EEE", 150),
        ("MECH-HALL", "FDP on CAD", 1, "11:00", "17:00", "APPROVED", "sup-cse", "MECH", 50),
        ("A416", "Hackathon Round 1", 1, "08:30", "11:30", "APPROVED", "727622bcs001", "IT", 90),
        ("NEW-SEM", "IQAC meeting", 0, "15:00", "16:30", "APPROVED", "admin", "ADMIN", 30),
        ("NPT-SEM1", "Music club practice", 2, "15:00", "17:00", "PENDING", "727622bec012", "CLUBS", 40),
    ]
    for code, title, day, s, e, status, who, dept, att in rows:
        decided = None if status == "PENDING" else ids["admin"]
        conn.execute(
            "INSERT INTO bookings (venue_id, requested_by, department, event_title, attendees, faculty_incharge, "
            "start_at, end_at, status, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (venue[code], ids[who], dept, title, att, "Dr. S. Kumar", at(day, s), at(day, e), status, decided,
             None if decided is None else datetime.now().strftime("%Y-%m-%d %H:%M")),
        )



app = Flask(__name__, static_folder=None)

_key_file = Path(__file__).with_name(".secret_key")
if not _key_file.exists():
    _key_file.write_text(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or _key_file.read_text().strip()
app.teardown_appcontext(close_db)

COLLEGE = {
    "name": "Dr. Mahalingam College of Engineering and Technology",
    "short": "MCET",
    "place": "Pollachi, Coimbatore District",
    "app": "Smart Hall & Venue Availability",
}

ROLE_META = {
    "student": {
        "label": "Student", "home": "student_dashboard", "icon": "🎓",
        "id_label": "College email or roll number",
        "blurb": "Check which halls are free, request a venue for your club or department event, and track approval.",
        "points": ["See free halls by date & time", "Request a venue for an event", "Track approval status"],
    },
    "faculty": {
        "label": "Faculty", "home": "dash", "icon": "👩‍🏫",
        "id_label": "Staff ID or college email",
        "blurb": "Check free halls, request a venue for a department event or class activity, and track approval.",
        "points": ["See free halls by date & time", "Request a venue", "Track approval status"],
    },
    "supervisor": {
        "label": "Hall Supervisor", "home": "supervisor_dashboard", "icon": "📋",
        "id_label": "Staff ID",
        "blurb": "Approve or reject requests for the halls you look after, block slots for maintenance, see the schedule.",
        "points": ["Approve or reject requests", "Block slots for maintenance", "Schedule of your halls"],
    },
    "admin": {
        "label": "Admin", "home": "admin_dashboard", "icon": "🛡️",
        "id_label": "Admin ID",
        "blurb": "Manage every venue, supervisor and booking across departments, resolve conflicts and see utilisation.",
        "points": ["Add & manage all venues", "Create supervisor accounts", "Utilisation reports & Excel export"],
    },
}

VENUE_TYPES = {
    "AUDITORIUM": "Auditorium",
    "SEMINAR_HALL": "Seminar hall",
    "CONFERENCE_ROOM": "Conference room",
    "LAB": "Laboratory",
    "OUTDOOR": "Outdoor venue",
    "SPORTS": "Sports venue",
    "CLASSROOM": "Classroom",
}

STATUS_LABEL = {
    "PENDING": "Pending", "APPROVED": "Approved", "REJECTED": "Rejected",
    "CANCELLED": "Cancelled", "BLOCKED": "Blocked",
}
ACTIVE = ("PENDING", "APPROVED", "BLOCKED")
ALLOTTED = ("APPROVED", "BLOCKED")


class BookingError(Exception):
    """A booking rule was broken; the message is shown to the user."""



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_str():
    return date.today().isoformat()


def parse_date(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return default or today_str()


def add_days(d, n):
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def to_min(hhmm):
    h, m = hhmm[:5].split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def valid_time(s):
    try:
        datetime.strptime(s, "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def nicedate(s):
    if not s:
        return "—"
    d = datetime.strptime(s[:10], "%Y-%m-%d")
    return d.strftime("%a, %d %b %Y")


def longdate(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%A, %d %B %Y")


def t12(s):
    """'2026-11-03 14:30' or '14:30' -> '02:30 PM'"""
    if not s:
        return ""
    hhmm = s[-5:]
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p")




# --- CSRF protection for every form ---------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:
            session.clear()
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not token or token != session.get("_csrf"):
            abort(400, "Session expired — reload the page and try again.")


def safe_back(url):
    """Only allow redirects back to pages on this site."""
    return url if url and url.startswith("/") and not url.startswith("//") else ""


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def venues(active_only=True):
    sql = "SELECT v.*, u.full_name AS supervisor_name FROM venues v LEFT JOIN users u ON u.id = v.supervisor_id"
    if active_only:
        sql += " WHERE v.is_active = 1"
    return get_db().execute(sql + " ORDER BY v.name").fetchall()


def departments():
    return get_db().execute("SELECT * FROM departments ORDER BY sort_order").fetchall()


BOOKING_SELECT = """
SELECT b.*, v.name AS venue_name, v.code AS venue_code, v.capacity AS venue_capacity, v.supervisor_id,
       u.full_name AS requester_name, u.login_id AS requester_login, u.role AS requester_role,
       d.full_name AS decided_by_name
FROM bookings b
JOIN venues v ON v.id = b.venue_id
LEFT JOIN users u ON u.id = b.requested_by
LEFT JOIN users d ON d.id = b.decided_by
"""


def bookings_between(start, end, statuses=None, venue_ids=None, user_id=None, order="ASC"):
    sql = BOOKING_SELECT + " WHERE b.start_at < ? AND b.end_at > ?"
    args = [end, start]
    if statuses:
        sql += f" AND b.status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    if venue_ids is not None:
        if not venue_ids:
            return []
        sql += f" AND b.venue_id IN ({','.join('?' * len(venue_ids))})"
        args += list(venue_ids)
    if user_id:
        sql += " AND b.requested_by = ?"
        args.append(user_id)
    sql += f" ORDER BY b.start_at {order}"
    return get_db().execute(sql, args).fetchall()


def get_booking(bid):
    return get_db().execute(BOOKING_SELECT + " WHERE b.id = ?", (bid,)).fetchone()


def can_manage(user, venue_row):
    """Admins manage every venue; supervisors manage the venues assigned to them."""
    if not user or not venue_row:
        return False
    return user["role"] == "admin" or (
        user["role"] == "supervisor" and venue_row["supervisor_id"] == user["id"]
    )


def overlapping(venue_id, start, end, statuses, exclude_id=None):
    sql = (BOOKING_SELECT + " WHERE b.venue_id = ? AND b.start_at < ? AND b.end_at > ?"
           f" AND b.status IN ({','.join('?' * len(statuses))})")
    args = [venue_id, end, start, *statuses]
    if exclude_id:
        sql += " AND b.id <> ?"
        args.append(exclude_id)
    return get_db().execute(sql, args).fetchall()


# ---------------------------------------------------------------------------
# Booking rules (the heart of conflict avoidance)
# ---------------------------------------------------------------------------
def check_request(user, venue, day, start, end, attendees, kind="request"):
    """Return (problems, allotted_clashes, pending_overlaps, alternatives) for a proposed slot."""
    problems = []
    if not venue:
        return ["Choose a venue."], [], [], []
    if not (valid_time(start) and valid_time(end)):
        return ["Enter a valid start and end time."], [], [], []
    s, e = f"{day} {start}", f"{day} {end}"
    if e <= s:
        problems.append("End time must be after start time.")
    if not venue["is_active"]:
        problems.append(f"{venue['name']} is not open for booking.")
    if kind == "request":
        if attendees > venue["capacity"]:
            problems.append(f"{venue['name']} holds {venue['capacity']} people; you entered {attendees}.")
        if user["role"] in ("student", "faculty") and (start < venue["open_time"] or end > venue["close_time"]):
            problems.append(f"{venue['name']} is open {venue['open_time']} – {venue['close_time']} only.")
    if user["role"] != "admin" and s < now_str():
        problems.append("That time has already passed.")

    clashes = overlapping(venue["id"], s, e, ALLOTTED) if e > s else []
    pending = overlapping(venue["id"], s, e, ("PENDING",)) if e > s else []

    alternatives = []
    if kind == "request" and e > s:
        busy = {r["venue_id"] for r in bookings_between(s, e, ALLOTTED)}
        for v in venues():
            if (v["id"] != venue["id"] and v["id"] not in busy and v["capacity"] >= attendees
                    and v["open_time"] <= start and end <= v["close_time"]):
                alternatives.append(v)
        alternatives = sorted(alternatives, key=lambda v: v["capacity"])[:4]
    return problems, clashes, pending, alternatives


def create_booking(user, form):
    conn = get_db()
    kind = "block" if form.get("kind") == "block" else "request"
    venue = conn.execute("SELECT * FROM venues WHERE id = ?", (form.get("venue_id"),)).fetchone()
    day = parse_date(form.get("date"))
    start, end = form.get("start", ""), form.get("end", "")
    title = (form.get("event_title") or "").strip()
    try:
        attendees = max(1, int(form.get("attendees") or 1))
    except ValueError:
        attendees = 1
    if kind == "block":
        attendees = 1

    if len(title) < 3:
        raise BookingError("Please enter an event title (at least 3 characters).")
    manage = can_manage(user, venue)
    if kind == "block" and not manage:
        raise BookingError("Only the hall supervisor or admin can block a slot.")
    if user["role"] == "student" and not (form.get("faculty_incharge") or "").strip():
        raise BookingError("Please name the faculty in-charge for your event.")

    problems, clashes, _pending, _alt = check_request(user, venue, day, start, end, attendees, kind)
    if problems:
        raise BookingError(" ".join(problems))
    if clashes:
        c = clashes[0]
        raise BookingError(
            f"Not available: {c['booking_no']} “{c['event_title']}” has {t12(c['start_at'])} – {t12(c['end_at'])}."
        )

    s, e = f"{day} {start}", f"{day} {end}"
    if kind == "block":
        status = "BLOCKED"
    elif form.get("direct") == "1" and manage:
        status = "APPROVED"
    else:
        status = "PENDING"
        dup = conn.execute(
            "SELECT 1 FROM bookings WHERE venue_id=? AND requested_by=? AND status='PENDING' "
            "AND start_at < ? AND end_at > ?", (venue["id"], user["id"], e, s)).fetchone()
        if dup:
            raise BookingError("You already have a pending request for this slot.")

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO bookings (venue_id, requested_by, department, event_title, purpose, attendees, "
            "faculty_incharge, start_at, end_at, status, decided_by, decided_at, decision_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (venue["id"], user["id"], form.get("department") or user["department"], title,
             (form.get("purpose") or "").strip() or None, attendees,
             (form.get("faculty_incharge") or "").strip() or None, s, e, status,
             None if status == "PENDING" else user["id"], None if status == "PENDING" else now_str(),
             "Direct allotment by staff" if status == "APPROVED" else None),
        )
        bid = cur.lastrowid
        if status in ALLOTTED:
            _auto_reject_overlaps(bid, user["id"])
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        if "SLOT_CONFLICT" in str(exc):
            raise BookingError("That slot was just allotted to someone else. Pick another time or venue.") from exc
        raise BookingError(str(exc)) from exc
    return get_booking(bid)


def _auto_reject_overlaps(bid, actor_id):
    """When a slot is allotted, decline every overlapping pending request with a clear reason."""
    conn = get_db()
    b = conn.execute("SELECT * FROM bookings WHERE id = ?", (bid,)).fetchone()
    conn.execute(
        "UPDATE bookings SET status='REJECTED', decided_by=?, decided_at=?, "
        "decision_note='Auto-rejected: slot allotted to ' || ? || ' (' || ? || ')' "
        "WHERE venue_id=? AND id<>? AND status='PENDING' AND start_at < ? AND end_at > ?",
        (actor_id, now_str(), b["booking_no"], b["event_title"], b["venue_id"], bid, b["end_at"], b["start_at"]),
    )


def change_status(user, booking, action, note=""):
    """approve / reject / cancel with permission checks. Returns a message."""
    conn = get_db()
    venue = conn.execute("SELECT * FROM venues WHERE id = ?", (booking["venue_id"],)).fetchone()
    manage = can_manage(user, venue)
    mine = booking["requested_by"] == user["id"]
    st = booking["status"]

    if action in ("approve", "reject"):
        if not manage:
            raise BookingError("Only the hall supervisor of this venue or an admin can decide.")
        if st != "PENDING" and user["role"] != "admin":
            raise BookingError("This request was already decided.")
        new = "APPROVED" if action == "approve" else "REJECTED"
    elif action == "cancel":
        if not (manage or mine):
            raise BookingError("You cannot cancel this booking.")
        if st not in ACTIVE:
            raise BookingError("Only pending, approved or blocked bookings can be cancelled.")
        if not manage and booking["end_at"] < now_str():
            raise BookingError("This booking is already over.")
        new = "CANCELLED"
    else:
        raise BookingError("Unknown action.")

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE bookings SET status=?, decided_by=?, decided_at=?, "
            "decision_note=COALESCE(NULLIF(?, ''), decision_note) WHERE id=?",
            (new, user["id"] if (manage or new != "CANCELLED") else booking["decided_by"], now_str(),
             note.strip(), booking["id"]),
        )
        rejected = 0
        if new == "APPROVED":
            before = conn.total_changes
            _auto_reject_overlaps(booking["id"], user["id"])
            rejected = conn.total_changes - before
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        if "SLOT_CONFLICT" in str(exc):
            raise BookingError("Cannot approve: this would overlap a slot that is already allotted.") from exc
        raise
    msg = f"{booking['booking_no']} {new.lower()}."
    if rejected:
        msg += f" {rejected} overlapping request(s) were declined automatically."
    return msg


# ---------------------------------------------------------------------------
# Availability grid helper
# ---------------------------------------------------------------------------
def build_grid(day, venue_list, booking_list, me_id=None):
    if not venue_list:
        return None
    day_start = min([8 * 60] + [to_min(v["open_time"]) for v in venue_list])
    day_end = max([18 * 60] + [to_min(v["close_time"]) for v in venue_list])
    span = day_end - day_start

    def pct(m):
        return round((m - day_start) / span * 100, 3)

    def hour_label(m):
        h = m // 60
        return f"{12 if h % 12 == 0 else h % 12}{'pm' if h >= 12 else 'am'}"

    hours = [{"label": hour_label(m), "left": pct(m)} for m in range(day_start, day_end + 1, 60)]
    rows = []
    for v in venue_list:
        items = []
        lane_ends = []
        for b in sorted([b for b in booking_list if b["venue_id"] == v["id"] and b["status"] in ACTIVE],
                        key=lambda b: b["start_at"]):
            s = max(day_start, to_min(b["start_at"][11:]) if b["start_at"][:10] == day else day_start)
            e = min(day_end, to_min(b["end_at"][11:]) if b["end_at"][:10] == day else day_end)
            if e <= s:
                continue
            lane = 0
            if b["status"] == "PENDING":
                lane = next((i for i, end in enumerate(lane_ends) if end <= b["start_at"]), len(lane_ends))
                if lane == len(lane_ends):
                    lane_ends.append(b["end_at"])
                else:
                    lane_ends[lane] = b["end_at"]
            items.append({"b": b, "left": pct(s), "width": pct(e) - pct(s), "lane": lane,
                          "mine": me_id is not None and b["requested_by"] == me_id})
        lanes = max(1, len(lane_ends))
        o, c = to_min(v["open_time"]), to_min(v["close_time"])
        rows.append({"v": v, "slots": items, "lanes": lanes, "height": max(56, lanes * 26 + 8),
                     "closed_before": pct(o) if o > day_start else 0,
                     "closed_after": pct(c) if c < day_end else 100})
    now_pct = None
    if day == today_str():
        n = datetime.now().hour * 60 + datetime.now().minute
        if day_start < n < day_end:
            now_pct = pct(n)
    return {"hours": hours, "rows": rows, "day_start": day_start, "day_end": day_end, "now": now_pct, "date": day}


# ===========================================================================
# PART 5 — JSON API (the single-page frontend index.html talks to these)
# ===========================================================================
def J(**k):
    return jsonify(k)


def rows(rs):
    return [dict(r) for r in rs]


def need(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if g.user is None:
                return J(error="Please sign in."), 401
            if roles and g.user["role"] not in roles:
                return J(error="Not allowed for your role."), 403
            return f(*a, **k)
        return w
    return deco


def pub(u):
    return None if not u else {k: u[k] for k in ("id", "role", "full_name", "login_id", "email", "department", "phone")}


def body():
    return request.get_json(silent=True) or {}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def err(e):
    return J(error=getattr(e, "description", "Error")), e.code


def report_range():
    dto = parse_date(request.args.get("to"))
    dfrom = parse_date(request.args.get("from"), add_days(dto, -29))
    return dfrom, dto


def report_data(dfrom, dto):
    rows = bookings_between(f"{dfrom} 00:00", f"{add_days(dto, 1)} 00:00")
    days = max(1, (datetime.strptime(dto, "%Y-%m-%d") - datetime.strptime(dfrom, "%Y-%m-%d")).days + 1)
    approved = [b for b in rows if b["status"] == "APPROVED"]

    def hours(b):
        return (datetime.strptime(b["end_at"], "%Y-%m-%d %H:%M")
                - datetime.strptime(b["start_at"], "%Y-%m-%d %H:%M")).total_seconds() / 3600

    per_venue = []
    for v in venues(False):
        open_h = (to_min(v["close_time"]) - to_min(v["open_time"])) / 60 * days
        used = sum(hours(b) for b in approved if b["venue_id"] == v["id"])
        per_venue.append({"name": v["name"], "code": v["code"], "hours": round(used, 1),
                          "pct": round(used / open_h * 100) if open_h else 0})
    per_venue.sort(key=lambda x: -x["hours"])
    per_dept = {}
    for b in approved:
        per_dept[b["department"] or "—"] = per_dept.get(b["department"] or "—", 0) + 1
    per_dept = sorted(per_dept.items(), key=lambda x: -x[1])
    per_status = {}
    for b in rows:
        per_status[b["status"]] = per_status.get(b["status"], 0) + 1
    decided = [b for b in rows if b["decided_at"] and b["status"] != "BLOCKED"]
    avg = 0.0
    if decided:
        avg = sum((datetime.strptime(b["decided_at"][:16], "%Y-%m-%d %H:%M")
                   - datetime.strptime(b["created_at"][:16], "%Y-%m-%d %H:%M")).total_seconds()
                  for b in decided) / len(decided) / 3600
    return {
        "dfrom": dfrom, "dto": dto, "rows": rows, "per_venue": per_venue, "per_dept": per_dept,
        "per_status": per_status, "approved_count": len(approved),
        "requests": sum(1 for b in rows if b["status"] != "BLOCKED"),
        "auto_rejected": sum(1 for b in rows if (b["decision_note"] or "").startswith("Auto-rejected")),
        "avg_hours": max(0.0, avg),
        "max_dept": max([c for _, c in per_dept] or [1]),
        "max_status": max(list(per_status.values()) or [1]),
    }


@app.route("/admin/export.xlsx")
@need("admin")
def admin_export():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return "Install openpyxl to export Excel files:  pip install openpyxl", 500
    dfrom, dto = report_range()
    data = report_data(dfrom, dto)
    wb = Workbook()
    ws = wb.active
    ws.title = "Bookings"
    head = ["Booking No", "Status", "Venue", "Date", "From", "To", "Event", "Department", "Attendees",
            "Faculty in-charge", "Requested by", "Roll / Staff ID", "Role", "Decided by", "Note", "Requested at"]
    ws.append(head)
    for b in data["rows"]:
        ws.append([b["booking_no"], b["status"], b["venue_name"], b["start_at"][:10], b["start_at"][11:],
                   b["end_at"][11:], b["event_title"], b["department"], b["attendees"], b["faculty_incharge"],
                   b["requester_name"], (b["requester_login"] or "").upper(), b["requester_role"],
                   b["decided_by_name"], b["decision_note"], b["created_at"]])
    ws2 = wb.create_sheet("Venue utilisation")
    ws2.append(["Venue", "Code", "Hours booked", "Utilisation %"])
    for v in data["per_venue"]:
        ws2.append([v["name"], v["code"], v["hours"], v["pct"]])
    ws3 = wb.create_sheet("By department")
    ws3.append(["Department", "Approved bookings"])
    for d, c in data["per_dept"]:
        ws3.append([d, c])
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1B2A5E")
        for col in sheet.columns:
            width = max(len(str(c.value or "")) for c in col) + 2
            sheet.column_dimensions[col[0].column_letter].width = min(45, width)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"MCET-venue-report-{dfrom}-to-{dto}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")




@app.route("/")
def index():
    return send_file(Path(__file__).with_name("index.html"))


@app.route("/api/bootstrap")
def api_boot():
    return J(user=pub(g.user), csrf=csrf_token(), college=COLLEGE, roles=ROLE_META, types=VENUE_TYPES,
             venues=rows(venues()), departments=rows(departments()))


@app.route("/api/login", methods=["POST"])
def api_login():
    d = body()
    ident = (d.get("ident") or "").strip().lower()
    u = get_db().execute("SELECT * FROM users WHERE lower(login_id)=? OR lower(email)=?", (ident, ident)).fetchone()
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        return J(error="Incorrect ID or password."), 400
    if u["role"] != d.get("role"):
        return J(error=f"This account is registered as {ROLE_META[u['role']]['label']}. Use that login."), 400
    session.clear()
    session["uid"] = u["id"]
    return J(user=pub(u), csrf=csrf_token())


@app.route("/api/register", methods=["POST"])
def api_register():
    f = body()
    role = "faculty" if f.get("role") == "faculty" else "student"
    name, lid, email = (str(f.get(k) or "").strip() for k in ("full_name", "login_id", "email"))
    lid, email, pw = lid.lower(), email.lower(), str(f.get("password") or "")
    if len(name) < 2 or not lid or "@" not in email:
        return J(error="Enter your name, ID and a valid email."), 400
    if len(pw) < 8:
        return J(error="Password must be at least 8 characters."), 400
    try:
        cur = get_db().execute(
            "INSERT INTO users (role, full_name, login_id, email, password_hash, department, phone) VALUES (?,?,?,?,?,?,?)",
            (role, name, lid, email, generate_password_hash(pw), f.get("department") or None, (f.get("phone") or "").strip() or None))
    except sqlite3.IntegrityError:
        return J(error="That ID or email is already registered."), 400
    session.clear()
    session["uid"] = cur.lastrowid
    return J(user=pub(get_db().execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()), csrf=csrf_token())


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return J(ok=True, csrf=csrf_token())


@app.route("/api/password", methods=["POST"])
@need()
def api_password():
    d = body()
    if not check_password_hash(g.user["password_hash"], d.get("current") or ""):
        return J(error="Current password is wrong."), 400
    if len(d.get("new") or "") < 8:
        return J(error="New password must be at least 8 characters."), 400
    get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(d["new"]), g.user["id"]))
    return J(ok=True)


@app.route("/api/availability")
@need()
def api_avail():
    day = parse_date(request.args.get("date"))
    bs = bookings_between(f"{day} 00:00", f"{add_days(day, 1)} 00:00", ACTIVE)
    keep = ("id", "booking_no", "venue_id", "event_title", "start_at", "end_at", "status", "department", "requested_by")
    return J(date=day, venues=rows(venues()), bookings=[{k: b[k] for k in keep} for b in bs])


@app.route("/api/check")
@need()
def api_check():
    venue = get_db().execute("SELECT * FROM venues WHERE id = ?", (request.args.get("venue_id"),)).fetchone()
    try:
        att = max(1, int(request.args.get("attendees") or 1))
    except ValueError:
        att = 1
    kind = request.args.get("kind", "request")
    pr, cl, pe, alts = check_request(g.user, venue, parse_date(request.args.get("date")),
                                     request.args.get("start", ""), request.args.get("end", ""), att, kind)
    return J(problems=pr, pending=len(pe), can_manage=can_manage(g.user, venue),
             clashes=[f"{c['booking_no']} “{c['event_title']}” {t12(c['start_at'])} – {t12(c['end_at'])}" for c in cl],
             alternatives=[{"id": a["id"], "name": a["name"], "capacity": a["capacity"]} for a in alts])


@app.route("/api/bookings", methods=["POST"])
@need()
def api_book():
    d = body()
    if d.get("kind") == "block" and g.user["role"] in ("student", "faculty"):
        return J(error="Only the hall supervisor or admin can block a slot."), 403
    try:
        b = create_booking(g.user, d)
    except BookingError as e:
        return J(error=str(e)), 400
    return J(booking=dict(b))


@app.route("/api/bookings/<int:bid>")
@need()
def api_booking(bid):
    b = get_booking(bid)
    if not b:
        abort(404)
    v = get_db().execute("SELECT * FROM venues WHERE id=?", (b["venue_id"],)).fetchone()
    rivals = rows(overlapping(b["venue_id"], b["start_at"], b["end_at"], ("PENDING",), exclude_id=b["id"])) if b["status"] == "PENDING" else []
    return J(booking=dict(b), venue=dict(v), manage=can_manage(g.user, v), mine=b["requested_by"] == g.user["id"],
             rivals=rivals, upcoming=b["end_at"] > now_str())


@app.route("/api/bookings/<int:bid>/action", methods=["POST"])
@need()
def api_action(bid):
    b = get_booking(bid)
    if not b:
        abort(404)
    d = body()
    try:
        return J(message=change_status(g.user, b, d.get("action", ""), d.get("note", "")))
    except BookingError as e:
        return J(error=str(e)), 400


@app.route("/api/my")
@need("student", "faculty")
def api_my():
    return J(bookings=rows(get_db().execute(BOOKING_SELECT + " WHERE b.requested_by=? ORDER BY b.start_at DESC", (g.user["id"],))), now=now_str())


@app.route("/api/supervisor")
@need("supervisor")
def api_sup():
    mine = [v for v in venues(False) if v["supervisor_id"] == g.user["id"]]
    ids = [v["id"] for v in mine]
    up = rows(bookings_between(now_str(), "9999-12-31 23:59", ACTIVE, venue_ids=ids))
    for b in up:
        b["rivals"] = [o["booking_no"] for o in up if o["status"] == "PENDING" and b["status"] == "PENDING" and o["id"] != b["id"]
                       and o["venue_id"] == b["venue_id"] and o["start_at"] < b["end_at"] and b["start_at"] < o["end_at"]]
    return J(venues=rows(mine), upcoming=up)


@app.route("/api/admin/overview")
@need("admin")
def api_ov():
    t = today_str()
    todays = rows(bookings_between(f"{t} 00:00", f"{add_days(t, 1)} 00:00", ACTIVE))
    pend = rows(bookings_between(now_str(), "9999-12-31 23:59", ("PENDING",)))
    c = get_db()
    cnt = lambda r: c.execute("SELECT COUNT(*) FROM users WHERE role=?", (r,)).fetchone()[0]
    return J(venues=len(venues()), students=cnt("student"), faculty=cnt("faculty"), pending=pend, today=todays,
             unassigned=[v["name"] for v in venues() if not v["supervisor_id"]])


@app.route("/api/admin/bookings")
@need("admin")
def api_abook():
    a = request.args
    st, vid, q = a.get("status", ""), a.get("venue", ""), (a.get("q") or "").strip().lower()
    f, t = parse_date(a.get("from"), add_days(today_str(), -7)), parse_date(a.get("to"), add_days(today_str(), 60))
    rs = rows(bookings_between(f"{f} 00:00", f"{add_days(t, 1)} 00:00", [st] if st else None, [int(vid)] if vid.isdigit() else None))
    if q:
        rs = [b for b in rs if q in " ".join(str(b[k] or "") for k in ("booking_no", "event_title", "requester_name", "requester_login", "department")).lower()]
    return J(bookings=rs, venues=rows(venues(False)), **{"from": f, "to": t})


@app.route("/api/admin/users")
@need("admin")
def api_ausers():
    us = [{k: u[k] for k in ("id", "role", "full_name", "login_id", "email", "department", "phone", "created_at")}
          for u in get_db().execute("SELECT * FROM users ORDER BY created_at DESC")]
    halls = {}
    for v in venues(False):
        if v["supervisor_id"]:
            halls.setdefault(v["supervisor_id"], []).append(v["code"])
    return J(users=us, halls=halls)


@app.route("/api/admin/reports")
@need("admin")
def api_rep():
    d = report_data(*report_range())
    d["rows"] = rows(d["rows"])
    return J(**d)


@app.route("/api/admin/venues")
@need("admin")
def api_avenues():
    staff = get_db().execute("SELECT id, full_name, login_id FROM users WHERE role IN ('supervisor','admin') ORDER BY full_name")
    return J(venues=rows(venues(False)), staff=rows(staff))


@app.route("/api/admin/venue", methods=["POST"])
@need("admin")
def api_avenue():
    f, conn = body(), get_db()
    vid = f.get("id")
    if f.get("delete") and vid:
        if conn.execute("SELECT 1 FROM bookings WHERE venue_id=?", (vid,)).fetchone():
            return J(error="This hall has booking history, so it cannot be deleted. Untick Active to hide it instead."), 400
        conn.execute("DELETE FROM venues WHERE id=?", (vid,))
        return J(ok=True)
    try:
        cap = int(f.get("capacity") or 0)
    except ValueError:
        cap = 0
    row = ((f.get("code") or "").strip().upper(), (f.get("name") or "").strip(), f.get("venue_type") or "SEMINAR_HALL",
           (f.get("block") or "").strip(), cap, ", ".join(x.strip() for x in (f.get("facilities") or "").split(",") if x.strip()),
           (f.get("description") or "").strip() or None, f.get("supervisor_id") or None,
           f.get("open_time") or "08:00", f.get("close_time") or "18:00", 1 if f.get("is_active", True) else 0)
    if not row[0] or not row[1] or cap < 1:
        return J(error="Code, name and a capacity of at least 1 are required."), 400
    if not (valid_time(row[8]) and valid_time(row[9])) or row[9] <= row[8]:
        return J(error="Closing time must be after opening time."), 400
    try:
        if vid:
            conn.execute("UPDATE venues SET code=?, name=?, venue_type=?, block=?, capacity=?, facilities=?, description=?, "
                         "supervisor_id=?, open_time=?, close_time=?, is_active=? WHERE id=?", (*row, vid))
        else:
            conn.execute("INSERT INTO venues (code, name, venue_type, block, capacity, facilities, description, supervisor_id, "
                         "open_time, close_time, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    except sqlite3.IntegrityError:
        return J(error="That venue code already exists."), 400
    return J(ok=True)


@app.route("/api/admin/users/new", methods=["POST"])
@need("admin")
def api_unew():
    f = body()
    role, lid = f.get("role"), (f.get("login_id") or "").strip().lower()
    if role not in ("supervisor", "admin", "faculty") or not lid or len((f.get("full_name") or "").strip()) < 2:
        return J(error="Choose a role and enter a name and staff ID."), 400
    if len(f.get("password") or "") < 8:
        return J(error="Initial password must be at least 8 characters."), 400
    try:
        get_db().execute("INSERT INTO users (role, full_name, login_id, email, password_hash, department, phone) VALUES (?,?,?,?,?,?,?)",
                         (role, f["full_name"].strip(), lid, (f.get("email") or "").strip().lower() or None,
                          generate_password_hash(f["password"]), f.get("department") or None, (f.get("phone") or "").strip() or None))
    except sqlite3.IntegrityError:
        return J(error="That staff ID or email is already taken."), 400
    return J(ok=True)


@app.route("/api/admin/users/<int:uid>/<action>", methods=["POST"])
@need("admin")
def api_uact(uid, action):
    conn, f = get_db(), body()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        abort(404)
    if uid == g.user["id"] and action in ("delete", "role"):
        return J(error="You cannot delete or change the role of your own account."), 400
    if action == "delete":
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    elif action == "reset":
        if len(f.get("password") or "") < 8:
            return J(error="New password must be at least 8 characters."), 400
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), uid))
    elif action == "role" and f.get("role") in ROLE_META:
        conn.execute("UPDATE users SET role=? WHERE id=?", (f["role"], uid))
        if f["role"] in ("student", "faculty"):
            conn.execute("UPDATE venues SET supervisor_id=NULL WHERE supervisor_id=?", (uid,))
    else:
        abort(404)
    return J(ok=True)


if __name__ == "__main__":
    if init_db():
        print("\n  New database created. Demo logins: admin/admin@123, sup-cse/super@123, "
              "fac-cse/faculty@123, student@mcet.local/student@123\n")
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
else:
    init_db()
