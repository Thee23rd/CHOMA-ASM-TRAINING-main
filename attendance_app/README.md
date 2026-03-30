# CHOMA Training Attendance App (Streamlit + SQLite)

This is a small Streamlit web app for:
- Registering people (with cooperative, sex, district/location, contact details, NRC)
- Searching people by **Name**, **NRC**, and **Cooperative**
- Marking **attendance per date** (Present/Absent)
- Login required (admin user stored in SQLite)

## Setup

1. Install Python 3.10+.
2. Install dependencies:

```bash
cd "attendance_app"
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

On first run, the sidebar will ask you to create the first admin account.

## Database

The SQLite database is stored here:
`attendance_app/attendance.sqlite3`

If you delete the database, you’ll need to re-create the admin account and all registered people will be lost.

