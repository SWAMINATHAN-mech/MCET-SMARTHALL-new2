MCET Smart Hall & Venue Availability  (single-file frontend)

Files: mcet_venue_app.py (Flask + SQLite backend + JSON API)  and  index.html (the whole frontend).
Keep both in the same folder.

1. Install Python 3.10+
2. pip install flask openpyxl
3. python mcet_venue_app.py
4. Open http://127.0.0.1:5000

Existing mcet_venue.db files are upgraded automatically (adds the Faculty role). No data is lost.

Demo logins:
  Admin       admin / admin@123
  Supervisor  sup-cse / super@123
  Faculty     fac-cse / faculty@123
  Student     student@mcet.local / student@123
Set FLASK_DEBUG=1 only for development.
