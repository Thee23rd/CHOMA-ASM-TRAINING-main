"""
Import bigbackup-style CSV (id, Name, Contact Number, Cooperative, Group/Venue, Sex,
District/Location, NRC, Present) into people + attendance.

- Deduplicates rows in the file by NRC (last row wins).
- Upserts into SQLite by NRC (matches existing import_attendance_csv behaviour).
- Applies Present to Mar 16–18 2025 by default (same flag for each day).
- Optionally runs dedupe_people_same_name_and_cooperative after import.
"""
import csv
import os
import sys
from typing import Dict, List, Optional, Tuple

here = os.path.abspath(os.path.dirname(__file__))
if here not in sys.path:
    sys.path.insert(0, here)

from cooperative_matching import resolve_cooperative_for_storage  # noqa: E402
from db import (  # noqa: E402
    DB_PATH,
    dedupe_people_same_name_and_cooperative,
    init_db,
    search_people,
    set_attendance_for_date,
    upsert_person,
)
from import_attendance_csv import _clean, _map_sex  # noqa: E402


def _dedupe_rows_by_nrc(
    rows: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], int, int]:
    """Keep one row per NRC; later rows override earlier. Returns (deduped, empty_nrc, dup_nrc_dropped)."""
    by_nrc: Dict[str, Dict[str, str]] = {}
    empty_nrc = 0
    for row in rows:
        nrc = _clean(row.get("NRC"))
        if not nrc:
            empty_nrc += 1
            continue
        by_nrc[nrc] = row
    dup_dropped = len(rows) - empty_nrc - len(by_nrc)
    return list(by_nrc.values()), empty_nrc, dup_dropped


def import_bigbackup_csv(
    csv_path: str,
    attendance_dates: Optional[List[str]] = None,
    db_path: str = DB_PATH,
    run_name_coop_dedupe: bool = True,
) -> dict:
    csv_path = os.path.abspath(csv_path)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    if attendance_dates is None:
        attendance_dates = ["2025-03-16", "2025-03-17", "2025-03-18"]

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        raw = list(reader)

    rows, empty_nrc_count, intra_file_dupes = _dedupe_rows_by_nrc(raw)
    upserted = 0
    per_date_statuses: Dict[str, Dict[int, int]] = {d: {} for d in attendance_dates}

    for row in rows:
        name = _clean(row.get("Name"))
        nrc = _clean(row.get("NRC"))
        coop = resolve_cooperative_for_storage(_clean(row.get("Cooperative")), db_path)
        district = _clean(row.get("District/Location") or row.get("District"))
        group_venue = _clean(row.get("Group/Venue"))
        sex = _map_sex(row.get("Sex"))
        contact = _clean(row.get("Contact Number"))

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

        people = search_people(nrc=nrc, exclude_placeholders=True, limit=1, db_path=db_path)
        if not people:
            continue
        pid = int(people[0]["id"])
        present_val = str(row.get("Present", "false")).strip().lower()
        status = 1 if present_val in ("true", "1", "yes") else 0
        for d in attendance_dates:
            per_date_statuses[d][pid] = status

    attendance_rows = 0
    for d in attendance_dates:
        st = per_date_statuses[d]
        set_attendance_for_date(d, st, db_path=db_path)
        attendance_rows += len(st)

    dedupe_stats = None
    if run_name_coop_dedupe:
        dedupe_stats = dedupe_people_same_name_and_cooperative(db_path=db_path)

    return {
        "csv_path": csv_path,
        "rows_read": len(raw),
        "rows_skipped_empty_nrc": empty_nrc_count,
        "rows_after_nrc_dedupe": len(rows),
        "intra_file_duplicate_nrc_rows_dropped": intra_file_dupes,
        "people_upserted": upserted,
        "attendance_cells_written": attendance_rows,
        "name_coop_dedupe": dedupe_stats,
    }


def main():
    init_db(DB_PATH)
    default_csv = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bigbackup.csv"))
    path = sys.argv[1] if len(sys.argv) > 1 else default_csv
    res = import_bigbackup_csv(path, db_path=DB_PATH)
    for k, v in res.items():
        print(f"{k}: {v}")
    print("Done.")


if __name__ == "__main__":
    main()
