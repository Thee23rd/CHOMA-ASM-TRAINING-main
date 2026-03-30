"""
Import attendance from CSV files (16th, 17th, 18th attendance).
1. Upserts every person from the CSV into people table (records exactly as they are)
2. Marks attendance (Present = true/false) for each date
"""
import csv
import os

from cooperative_matching import resolve_cooperative_for_storage
from db import (
    DB_PATH,
    init_db,
    set_attendance_for_date,
    sync_programme_register_present_from_attendance,
    upsert_person,
    search_people,
)


def _clean(s):
    return (s or "").strip()


def _map_sex(s):
    v = _clean(s).upper()
    if v in ("F", "FEMALE"):
        return "Female"
    if v in ("M", "MALE"):
        return "Male"
    return "Other" if v else "Other"


def import_attendance_csv(
    csv_path: str, attendance_date: str, db_path: str = DB_PATH
) -> dict:
    """Import people from CSV (upsert) and set attendance for the date."""
    csv_path = os.path.abspath(csv_path)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    statuses = {}
    upserted = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = _clean(row.get("Name"))
            nrc = _clean(row.get("NRC"))
            coop = resolve_cooperative_for_storage(_clean(row.get("Cooperative")), db_path)
            district = _clean(row.get("District/Location") or row.get("District"))
            group_venue = _clean(row.get("Group/Venue"))
            sex = _map_sex(row.get("Sex"))
            contact = _clean(row.get("Contact Number"))

            if not nrc:
                continue
            if not coop:
                coop = group_venue or "Unknown"
            if not district:
                district = "Unknown"
            if not name:
                name = "(No name)"

            upsert_person(
                cooperative_name=coop,
                group_venue=group_venue or None,
                district_location=district,
                contact_person_details=name,
                sex=sex,
                nrc_details=nrc,
                contact_number=contact or None,
                db_path=db_path,
            )
            upserted += 1

            people = search_people(
                nrc=nrc, exclude_placeholders=True, limit=1, db_path=db_path
            )
            if people:
                pid = int(people[0]["id"])
                present_val = str(row.get("Present", "false")).strip().lower()
                status = 1 if present_val in ("true", "1", "yes") else 0
                statuses[pid] = status

    set_attendance_for_date(attendance_date, statuses, db_path=db_path)
    sync_programme_register_present_from_attendance(attendance_date, db_path=db_path)
    return {
        "people_upserted": upserted,
        "attendance_rows": len(statuses),
        "present": sum(1 for s in statuses.values() if s == 1),
    }


def main():
    init_db(DB_PATH)
    workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    files_dates = [
        ("16th attendance.csv", "2025-03-16"),
        ("17th attendance .csv", "2025-03-17"),
        ("18th attendance.csv", "2025-03-18"),
    ]

    for filename, date_str in files_dates:
        path = os.path.join(workspace, filename)
        if os.path.exists(path):
            res = import_attendance_csv(path, date_str)
            print(f"{filename} -> {date_str}: {res}")
        else:
            print(f"Skipped (not found): {filename}")

    print("Done.")


if __name__ == "__main__":
    main()
