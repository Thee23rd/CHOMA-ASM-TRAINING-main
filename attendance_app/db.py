import csv
import os
import random
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _default_db_path() -> str:
    # Store alongside this file so it's easy to find.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "attendance.sqlite3")


DB_PATH = _default_db_path()


@contextmanager
def connect(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              cooperative_name TEXT NOT NULL,
              group_venue TEXT,
              district_location TEXT NOT NULL,
              contact_person_details TEXT NOT NULL,
              sex TEXT NOT NULL,
              nrc_details TEXT NOT NULL UNIQUE,
              contact_number TEXT,
              created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
              person_id INTEGER NOT NULL,
              attendance_date TEXT NOT NULL, -- YYYY-MM-DD
              status INTEGER NOT NULL, -- 1 = present, 0 = absent
              marked_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
              PRIMARY KEY (person_id, attendance_date),
              FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )

        # Migration for existing DBs created before `contact_number` existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(people);").fetchall()}
        if "contact_number" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN contact_number TEXT;")
        if "group_venue" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN group_venue TEXT;")
        if "is_cooperative_placeholder" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN is_cooperative_placeholder INTEGER DEFAULT 0;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cooperative_registry (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              official_name TEXT NOT NULL UNIQUE,
              cooperative_number TEXT,
              license_number TEXT,
              preferred_mining_area TEXT,
              created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        reg_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(cooperative_registry);").fetchall()
        }
        if "preferred_mining_area" not in reg_cols:
            conn.execute(
                "ALTER TABLE cooperative_registry ADD COLUMN preferred_mining_area TEXT;"
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_daily_register (
              attendance_date TEXT PRIMARY KEY,
              participant_count INTEGER NOT NULL,
              day_label TEXT
            );
            """
        )
        # Official programme register: recorded **present** headcount per day (March 2025).
        _pr_seed_2025 = [
            ("2025-03-16", 293, "Day 1"),
            ("2025-03-17", 343, "Day 2"),
            ("2025-03-18", 351, "Day 3"),
            ("2025-03-19", 362, "Day 4"),
            ("2025-03-20", 366, "Day 5"),
            ("2025-03-21", 341, "Day 6"),
            ("2025-03-22", 0, "Day 6A"),
            ("2025-03-23", 368, "Day 7"),
            ("2025-03-24", 361, "Day 8"),
            ("2025-03-25", 367, "Day 9"),
            ("2025-03-26", 375, "Day 10"),
            ("2025-03-27", 391, "Day 11"),
            ("2025-03-28", 347, "Day 12"),
            ("2025-03-30", 362, "Day 13"),
        ]
        has_2025_anchor = conn.execute(
            "SELECT 1 AS o FROM programme_daily_register WHERE attendance_date = '2025-03-16' LIMIT 1;"
        ).fetchone()
        if not has_2025_anchor:
            conn.execute("DELETE FROM programme_daily_register;")
            conn.executemany(
                """
                INSERT INTO programme_daily_register (attendance_date, participant_count, day_label)
                VALUES (?, ?, ?);
                """,
                _pr_seed_2025,
            )
        # Restore printed programme figures if rows were overwritten by earlier syncs.
        for _ds, _pc, _dl in _pr_seed_2025:
            conn.execute(
                """
                UPDATE programme_daily_register
                SET participant_count = ?, day_label = ?
                WHERE attendance_date = ?;
                """,
                (_pc, _dl, _ds),
            )


def cooperative_registry_count(db_path: str = DB_PATH) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM cooperative_registry;").fetchone()
        return int(row["c"] or 0)


def get_cooperative_registry_rows(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT id, official_name, cooperative_number, license_number,
                       preferred_mining_area, created_at
                FROM cooperative_registry
                ORDER BY id ASC;
                """
            ).fetchall()
        )


def get_cooperative_number_lookup(db_path: str = DB_PATH) -> Dict[str, str]:
    """official_name -> cooperative_number (empty string if none)."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT official_name, cooperative_number FROM cooperative_registry;"
        ).fetchall()
        return {
            str(r["official_name"]): (str(r["cooperative_number"]).strip() if r["cooperative_number"] else "")
            for r in rows
        }


def import_cooperative_registry_csv(
    csv_path: str,
    db_path: str = DB_PATH,
    encoding: str = "latin-1",
) -> Dict[str, int]:
    """
    Load official cooperatives from export CSV: cooperative name, cooperative number, license.
    Uses latin-1 by default (matches Excel exports); try utf-8-sig if you know the file is UTF-8.
    Later rows with the same official_name update numbers/license.
    """
    csv_path = os.path.abspath(csv_path)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    def clean_cell(s: Optional[str]) -> str:
        if s is None:
            return ""
        t = str(s).replace("\t", " ").replace("\r", " ").replace("\n", " ")
        return " ".join(t.split()).strip()

    loaded = 0
    with open(csv_path, newline="", encoding=encoding, errors="replace") as f:
        reader = csv.reader(f)
        try:
            next(reader)  # header
        except StopIteration:
            return {"rows_loaded": 0}

        with connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            for parts in reader:
                if not parts:
                    continue
                official = clean_cell(parts[0])
                if not official:
                    continue
                coop_no = clean_cell(parts[1]) if len(parts) > 1 else ""
                lic = clean_cell(parts[2]) if len(parts) > 2 else ""
                conn.execute(
                    """
                    INSERT INTO cooperative_registry (official_name, cooperative_number, license_number)
                    VALUES (?, ?, ?)
                    ON CONFLICT (official_name) DO UPDATE SET
                      cooperative_number = excluded.cooperative_number,
                      license_number = excluded.license_number;
                    """,
                    (official, coop_no or None, lic or None),
                )
                loaded += 1

    return {"rows_loaded": loaded}


def upsert_cooperative_registry_entry(
    official_name: str,
    cooperative_number: Optional[str] = None,
    license_number: Optional[str] = None,
    db_path: str = DB_PATH,
) -> int:
    """
    Insert or update a cooperative_registry row by official_name.
    Returns the registry row id.
    """
    name = (official_name or "").strip()
    if not name:
        raise ValueError("official_name is required")
    coop_no = (cooperative_number or "").strip() or None
    lic = (license_number or "").strip() or None
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO cooperative_registry (official_name, cooperative_number, license_number)
            VALUES (?, ?, ?)
            ON CONFLICT (official_name) DO UPDATE SET
              cooperative_number = excluded.cooperative_number,
              license_number = excluded.license_number;
            """,
            (name, coop_no, lic),
        )
        row = conn.execute(
            "SELECT id FROM cooperative_registry WHERE official_name = ?;",
            (name,),
        ).fetchone()
        return int(row["id"])


def update_cooperative_registry_by_id(
    registry_id: int,
    new_official_name: str,
    cooperative_number: Optional[str] = None,
    license_number: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """
    Update registry numbers and/or official name. Renaming updates people.cooperative_name everywhere.
    """
    new_name = (new_official_name or "").strip()
    if not new_name:
        raise ValueError("official_name is required")
    coop_no = (cooperative_number or "").strip() or None
    lic = (license_number or "").strip() or None
    rid = int(registry_id)
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        old_row = conn.execute(
            "SELECT official_name FROM cooperative_registry WHERE id = ?;",
            (rid,),
        ).fetchone()
        if not old_row:
            raise ValueError("Unknown cooperative registry id")
        old_name = str(old_row["official_name"])
        if new_name != old_name:
            clash = conn.execute(
                "SELECT 1 FROM cooperative_registry WHERE official_name = ? AND id <> ?;",
                (new_name, rid),
            ).fetchone()
            if clash:
                raise sqlite3.IntegrityError("Another cooperative already uses this official name")
            conn.execute(
                "UPDATE people SET cooperative_name = ? WHERE cooperative_name = ?;",
                (new_name, old_name),
            )
        conn.execute(
            """
            UPDATE cooperative_registry
            SET official_name = ?, cooperative_number = ?, license_number = ?
            WHERE id = ?;
            """,
            (new_name, coop_no, lic, rid),
        )


def set_preferred_mining_area(
    registry_id: int,
    preferred_mining_area: Optional[str],
    db_path: str = DB_PATH,
) -> None:
    """
    Preferred mining area for a cooperative (registry row only).
    Does not update people.district_location or any participant fields.
    """
    val = (preferred_mining_area or "").strip() or None
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE cooperative_registry
            SET preferred_mining_area = ?
            WHERE id = ?;
            """,
            (val, int(registry_id)),
        )


def ensure_cooperative_registry_entry(official_name: str, db_path: str = DB_PATH) -> int:
    """
    Ensure a cooperative_registry row exists for this official name (minimal insert).
    Used when assigning preferred mining area before a full CSV import.
    """
    name = (official_name or "").strip()
    if not name:
        raise ValueError("Cooperative name is required")
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        row = conn.execute(
            "SELECT id FROM cooperative_registry WHERE official_name = ?;",
            (name,),
        ).fetchone()
        if row:
            return int(row["id"])
        conn.execute(
            "INSERT INTO cooperative_registry (official_name) VALUES (?);",
            (name,),
        )
        row = conn.execute(
            "SELECT id FROM cooperative_registry WHERE official_name = ?;",
            (name,),
        ).fetchone()
        return int(row["id"])


def get_area_allocation_overview(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    """
    Cooperatives from participants plus registry-only rows, with member counts,
    cooperative_number, license_number, and preferred_mining_area when set.
    registry_id may be NULL until a preference is saved or a full registry import adds the row.
    """
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                WITH pc AS (
                  SELECT cooperative_name AS cname, COUNT(*) AS member_count
                  FROM people
                  WHERE COALESCE(is_cooperative_placeholder, 0) = 0
                    AND cooperative_name IS NOT NULL AND TRIM(cooperative_name) <> ''
                  GROUP BY cooperative_name
                )
                SELECT
                  COALESCE(r.official_name, pc.cname) AS name,
                  r.id AS registry_id,
                  r.cooperative_number AS cooperative_number,
                  r.license_number AS license_number,
                  r.preferred_mining_area AS preferred_mining_area,
                  COALESCE(pc.member_count, 0) AS member_count
                FROM pc
                LEFT JOIN cooperative_registry r ON r.official_name = pc.cname
                UNION
                SELECT
                  r.official_name AS name,
                  r.id AS registry_id,
                  r.cooperative_number AS cooperative_number,
                  r.license_number AS license_number,
                  r.preferred_mining_area AS preferred_mining_area,
                  COALESCE(pc.member_count, 0) AS member_count
                FROM cooperative_registry r
                LEFT JOIN pc ON pc.cname = r.official_name
                WHERE pc.cname IS NULL
                ORDER BY name COLLATE NOCASE;
                """
            ).fetchall()
        )


def remap_people_to_cooperative_registry(
    db_path: str = DB_PATH,
    similarity_threshold: float = 0.78,
    ambiguity_band: float = 0.03,
) -> Dict[str, Any]:
    """
    Set each person's cooperative_name to the best matching cooperative_registry.official_name.
    Exact normalized match first; else fuzzy match. When several registry names score similarly,
    prefer the registry row with the highest id (newest inserted).
    """
    from cooperative_matching import resolve_cooperative_to_registry

    registry = get_cooperative_registry_rows(db_path=db_path)
    if not registry:
        return {"skipped": True, "reason": "cooperative_registry is empty"}

    name_changes: Dict[str, str] = {}
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT cooperative_name FROM people
            WHERE cooperative_name IS NOT NULL AND trim(cooperative_name) <> '';
            """
        ).fetchall()
        for r in rows:
            raw = str(r["cooperative_name"])
            resolved = resolve_cooperative_to_registry(
                raw,
                registry,
                similarity_threshold=similarity_threshold,
                ambiguity_band=ambiguity_band,
            )
            if resolved and resolved != raw:
                name_changes[raw] = resolved

        people_updated = 0
        for old_name, new_name in name_changes.items():
            cur = conn.execute(
                "UPDATE people SET cooperative_name = ? WHERE cooperative_name = ?",
                (new_name, old_name),
            )
            people_updated += cur.rowcount

    return {
        "distinct_names_rewritten": len(name_changes),
        "people_rows_updated": people_updated,
    }


def count_users(db_path: str = DB_PATH) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users;").fetchone()
        return int(row["c"])


def count_people(db_path: str = DB_PATH, exclude_cooperative_placeholders: bool = False) -> int:
    with connect(db_path) as conn:
        sql = "SELECT COUNT(*) AS c FROM people"
        if exclude_cooperative_placeholders:
            sql += " WHERE COALESCE(is_cooperative_placeholder, 0) = 0"
        row = conn.execute(sql + ";").fetchone()
        return int(row["c"])


def normalize_sex_for_stats(sex: Optional[str]) -> str:
    """
    Map stored `people.sex` to Male / Female / Other for charts and aggregates.
    Mis-typed imports (e.g. a person's name in the sex field) become Other.
    """
    s = (sex or "").strip()
    if not s:
        return "Other"
    k = s.casefold()
    if k in ("male", "m"):
        return "Male"
    if k in ("female", "f"):
        return "Female"
    if k in ("other", "o", "non-binary", "nonbinary"):
        return "Other"
    return "Other"


def _sql_sex_normalize_expr(column: str = "sex") -> str:
    """SQLite CASE expression matching normalize_sex_for_stats (Male/Female/Other)."""
    return f"""
      CASE
        WHEN LOWER(TRIM(COALESCE({column}, ''))) IN ('male', 'm') THEN 'Male'
        WHEN LOWER(TRIM(COALESCE({column}, ''))) IN ('female', 'f') THEN 'Female'
        WHEN LOWER(TRIM(COALESCE({column}, ''))) IN ('other', 'o', 'non-binary', 'nonbinary', '') THEN 'Other'
        ELSE 'Other'
      END
    """.strip()


def create_user(username: str, password_hash: str, db_path: str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?);",
            (username, password_hash),
        )


def get_user_by_username(username: str, db_path: str = DB_PATH) -> Optional[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?;",
            (username,),
        ).fetchone()


def add_person(
    cooperative_name: str,
    group_venue: Optional[str],
    district_location: str,
    contact_person_details: str,
    sex: str,
    nrc_details: str,
    contact_number: Optional[str] = None,
    db_path: str = DB_PATH,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO people (
              cooperative_name,
              group_venue,
              district_location,
              contact_person_details,
              sex,
              nrc_details,
              contact_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                cooperative_name,
                group_venue,
                district_location,
                contact_person_details,
                sex,
                nrc_details,
                contact_number,
            ),
        )
        return int(cur.lastrowid)


def upsert_person(
    cooperative_name: str,
    group_venue: Optional[str],
    district_location: str,
    contact_person_details: str,
    sex: str,
    nrc_details: str,
    contact_number: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """
    Insert a person; if `nrc_details` already exists, update the other fields.
    """
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO people (
              cooperative_name,
              group_venue,
              district_location,
              contact_person_details,
              sex,
              nrc_details,
              contact_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (nrc_details)
            DO UPDATE SET
              cooperative_name = excluded.cooperative_name,
              group_venue = excluded.group_venue,
              district_location = excluded.district_location,
              contact_person_details = excluded.contact_person_details,
              sex = excluded.sex,
              contact_number = excluded.contact_number;
            """,
            (
                cooperative_name,
                group_venue,
                district_location,
                contact_person_details,
                sex,
                nrc_details,
                contact_number,
            ),
        )


def add_cooperative_placeholder(
    cooperative_name: str,
    district_location: str,
    db_path: str = DB_PATH,
) -> Optional[int]:
    """Add a placeholder for a cooperative with no members. Returns new id or None if exists."""
    import uuid
    with connect(db_path) as conn:
        existing = conn.execute(
            """SELECT id FROM people WHERE cooperative_name=? AND district_location=? AND is_cooperative_placeholder=1 LIMIT 1""",
            (cooperative_name.strip(), district_location.strip()),
        ).fetchone()
        if existing:
            return None
        nrc = f"COOP-PH-{uuid.uuid4().hex[:12]}"
        cur = conn.execute(
            """INSERT INTO people (cooperative_name,group_venue,district_location,contact_person_details,sex,nrc_details,contact_number,is_cooperative_placeholder)
               VALUES (?,NULL,?,'(No members yet)','Other',?,NULL,1)""",
            (cooperative_name.strip(), district_location.strip(), nrc),
        )
        return int(cur.lastrowid)


def get_cooperative_placeholders(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    """Return cooperative placeholder rows (cooperatives with no members yet)."""
    with connect(db_path) as conn:
        return list(conn.execute(
            """SELECT id,cooperative_name,district_location,contact_person_details,nrc_details FROM people WHERE is_cooperative_placeholder=1 ORDER BY cooperative_name,district_location"""
        ).fetchall())


def delete_cooperative_placeholder(placeholder_id: int, db_path: str = DB_PATH) -> None:
    """Remove a cooperative placeholder (e.g. when first member is added)."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM people WHERE id=? AND is_cooperative_placeholder=1", (int(placeholder_id),))


def delete_person(person_id: int, db_path: str = DB_PATH) -> None:
    """Delete a person. Attendance rows are removed by CASCADE."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM people WHERE id = ?", (int(person_id),))


def delete_people(person_ids: Iterable[int], db_path: str = DB_PATH) -> int:
    """Delete multiple people. Returns count deleted. Attendance cascades."""
    ids = [int(i) for i in person_ids]
    if not ids:
        return 0
    with connect(db_path) as conn:
        placeholders = ",".join(["?"] * len(ids))
        cur = conn.execute(f"DELETE FROM people WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def _merge_attendance_onto_keeper(
    conn: sqlite3.Connection, keeper_id: int, duplicate_id: int
) -> None:
    """Copy duplicate_id's attendance onto keeper_id (present wins), then caller deletes duplicate."""
    rows = conn.execute(
        "SELECT attendance_date, status FROM attendance WHERE person_id = ?",
        (int(duplicate_id),),
    ).fetchall()
    for r in rows:
        date_str = str(r["attendance_date"])
        status = int(r["status"])
        existing = conn.execute(
            "SELECT status FROM attendance WHERE person_id = ? AND attendance_date = ?",
            (int(keeper_id), date_str),
        ).fetchone()
        merged = max(status, int(existing["status"])) if existing is not None else status
        conn.execute(
            """
            INSERT INTO attendance (person_id, attendance_date, status, marked_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (person_id, attendance_date)
            DO UPDATE SET
              status = excluded.status,
              marked_at = CURRENT_TIMESTAMP;
            """,
            (int(keeper_id), date_str, merged),
        )


def dedupe_people_same_name_and_cooperative(db_path: str = DB_PATH) -> Dict[str, int]:
    """
    Merge rows that share the same cooperative + contact name (trimmed, case-insensitive)
    but different ids (e.g. duplicate NRC typos). Keeps the lowest person id, merges
    attendance (present wins), deletes the other rows.
    """
    groups_merged = 0
    people_removed = 0
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        groups = conn.execute(
            """
            SELECT MIN(id) AS keeper_id,
                   GROUP_CONCAT(id) AS all_ids
            FROM people
            WHERE COALESCE(is_cooperative_placeholder, 0) = 0
            GROUP BY lower(trim(cooperative_name)), lower(trim(contact_person_details))
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for g in groups:
            keeper_id = int(g["keeper_id"])
            raw = str(g["all_ids"] or "")
            all_ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
            for dup_id in all_ids:
                if dup_id == keeper_id:
                    continue
                _merge_attendance_onto_keeper(conn, keeper_id, dup_id)
                conn.execute("DELETE FROM people WHERE id = ?", (dup_id,))
                people_removed += 1
            groups_merged += 1
    return {"groups_merged": groups_merged, "people_removed": people_removed}


def update_person(
    person_id: int,
    cooperative_name: str,
    group_venue: Optional[str],
    district_location: str,
    contact_person_details: str,
    sex: str,
    nrc_details: str,
    contact_number: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Update an existing person record by ID."""
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE people
            SET cooperative_name = ?,
                group_venue = ?,
                district_location = ?,
                contact_person_details = ?,
                sex = ?,
                nrc_details = ?,
                contact_number = ?
            WHERE id = ?;
            """,
            (
                cooperative_name,
                group_venue,
                district_location,
                contact_person_details,
                sex,
                nrc_details,
                contact_number,
                person_id,
            ),
        )


def _like_filter(column: str, value: Optional[str]) -> Tuple[str, List[Any]]:
    if value is None:
        return "", []
    value = value.strip()
    if not value:
        return "", []
    return f" AND {column} LIKE ? ", [f"%{value}%"]


def search_people(
    name: Optional[str] = None,
    cooperative: Optional[str] = None,
    nrc: Optional[str] = None,
    exclude_placeholders: bool = True,
    db_path: str = DB_PATH,
    limit: int = 200,
) -> List[sqlite3.Row]:
    """
    Search strategy:
    - "name" searches contact_person_details
    - "cooperative" searches cooperative_name
    - "nrc" searches nrc_details
    - exclude_placeholders: when True, omit cooperative placeholder rows
    """
    with connect(db_path) as conn:
        params: List[Any] = []
        sql = """
            SELECT
              id,
              cooperative_name,
              group_venue,
              district_location,
              contact_person_details,
              contact_number,
              sex,
              nrc_details,
              created_at
            FROM people
            WHERE 1=1
        """
        if exclude_placeholders:
            sql += " AND (is_cooperative_placeholder IS NULL OR is_cooperative_placeholder = 0)"

        extra, p = _like_filter("contact_person_details", name)
        sql += extra
        params.extend(p)

        extra, p = _like_filter("cooperative_name", cooperative)
        sql += extra
        params.extend(p)

        extra, p = _like_filter("nrc_details", nrc)
        sql += extra
        params.extend(p)

        sql += " ORDER BY id DESC LIMIT ?;"
        params.append(limit)
        return list(conn.execute(sql, params).fetchall())


def get_people_by_ids(person_ids: Iterable[int], db_path: str = DB_PATH) -> List[sqlite3.Row]:
    ids = list(person_ids)
    if not ids:
        return []

    placeholders = ",".join(["?"] * len(ids))
    with connect(db_path) as conn:
        return list(
            conn.execute(
                f"""
                SELECT
                  id,
                  cooperative_name,
                  group_venue,
                  district_location,
                  contact_person_details,
                  contact_number,
                  sex,
                  nrc_details,
                  created_at
                FROM people
                WHERE id IN ({placeholders})
                ORDER BY id ASC;
                """,
                ids,
            ).fetchall()
        )


def get_attendance_for_date(
    attendance_date: str, person_ids: Optional[Iterable[int]] = None, db_path: str = DB_PATH
) -> Dict[int, int]:
    """
    Returns mapping: person_id -> status (1 present / 0 absent).
    """
    with connect(db_path) as conn:
        params: List[Any] = [attendance_date]
        sql = "SELECT person_id, status FROM attendance WHERE attendance_date = ?"

        if person_ids is not None:
            ids = list(person_ids)
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                sql += f" AND person_id IN ({placeholders})"
                params.extend(ids)
            else:
                return {}

        sql += ";"
        rows = conn.execute(sql, params).fetchall()
        return {int(r["person_id"]): int(r["status"]) for r in rows}


def get_attendance_roster_for_date(
    attendance_date: str,
    status: Optional[int] = None,  # 1 present / 0 absent
    db_path: str = DB_PATH,
    limit: int = 5000,
) -> List[sqlite3.Row]:
    """
    Returns roster joined with people details for the given date.
    This shows only rows that have been saved into `attendance`.
    """
    with connect(db_path) as conn:
        sql = """
            SELECT
              p.id AS person_id,
              p.cooperative_name,
              p.group_venue,
              p.district_location,
              p.contact_person_details,
              p.contact_number,
              p.sex,
              p.nrc_details,
              COALESCE(p.is_cooperative_placeholder, 0) AS is_cooperative_placeholder,
              a.status,
              a.marked_at
            FROM attendance a
            JOIN people p ON p.id = a.person_id
            WHERE a.attendance_date = ?
        """
        params: List[Any] = [attendance_date]

        if status is not None:
            sql += " AND a.status = ?"
            params.append(int(status))

        sql += " ORDER BY p.id DESC LIMIT ?;"
        params.append(int(limit))

        return list(conn.execute(sql, params).fetchall())


def get_programme_register_row(
    attendance_date: str, db_path: str = DB_PATH
) -> Optional[sqlite3.Row]:
    """Official **present** count from the programme register for this calendar day, if any."""
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT attendance_date, participant_count, day_label
            FROM programme_daily_register
            WHERE attendance_date = ?;
            """,
            (attendance_date,),
        ).fetchone()


def list_programme_register(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    """All programme register rows, chronological."""
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT attendance_date, participant_count, day_label
                FROM programme_daily_register
                ORDER BY attendance_date ASC;
                """
            ).fetchall()
        )


def sync_programme_register_present_from_attendance(
    attendance_date: str,
    db_path: str = DB_PATH,
    only_insert_missing: bool = False,
) -> bool:
    """
    Set programme register ``participant_count`` for this date to the count of present rows
    in ``attendance`` (``status = 1``). Inserts a row if none exists (any calendar date).

    If ``only_insert_missing`` is True, an existing row is left unchanged (so opening Daily Stats
    does not overwrite seeded programme figures; use False after **Save attendance** to refresh).
    """
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM attendance
            WHERE attendance_date = ? AND status = 1;
            """,
            (attendance_date,),
        ).fetchone()
        n_present = int(row["c"] or 0)
        existing = conn.execute(
            """
            SELECT 1 AS o FROM programme_daily_register
            WHERE attendance_date = ? LIMIT 1;
            """,
            (attendance_date,),
        ).fetchone()
        if existing:
            if only_insert_missing:
                return True
            meta = conn.execute(
                "SELECT day_label FROM programme_daily_register WHERE attendance_date = ?;",
                (attendance_date,),
            ).fetchone()
            if meta and meta["day_label"] is not None and str(meta["day_label"]).strip() != "":
                return True
            conn.execute(
                """
                UPDATE programme_daily_register
                SET participant_count = ?
                WHERE attendance_date = ?;
                """,
                (n_present, attendance_date),
            )
        else:
            conn.execute(
                """
                INSERT INTO programme_daily_register (attendance_date, participant_count, day_label)
                VALUES (?, ?, NULL);
                """,
                (attendance_date, n_present),
            )
        return True


def redistribute_attendance_to_match_register(
    attendance_date: str,
    db_path: str = DB_PATH,
    random_seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    For everyone who already has an attendance row on this date, rewrite present/absent so the
    number marked present equals the official register **Present** (if possible).

    Present people are picked at random, **preferring real participants with a cooperative name**;
    any remaining slots are filled at random from other marked people (e.g. no cooperative on file).

    If register present >= number of marked people, all marked people are set present.
    If register present is 0, all are set absent.
    """
    reg = get_programme_register_row(attendance_date, db_path=db_path)
    if reg is None:
        return {"ok": False, "skipped": True, "reason": "no_register_row"}

    target = max(0, int(reg["participant_count"]))
    rng = random.Random(random_seed) if random_seed is not None else random.Random()

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT a.person_id AS pid,
                   COALESCE(p.is_cooperative_placeholder, 0) AS ph,
                   trim(coalesce(p.cooperative_name, '')) AS cname
            FROM attendance a
            JOIN people p ON p.id = a.person_id
            WHERE a.attendance_date = ?;
            """,
            (attendance_date,),
        ).fetchall()

    marked = [int(r["pid"]) for r in rows]
    if not marked:
        return {
            "ok": False,
            "skipped": True,
            "reason": "no_attendance_rows",
            "target": target,
        }

    coop_ids = [
        int(r["pid"])
        for r in rows
        if int(r["ph"] or 0) == 0 and str(r["cname"] or "").strip() != ""
    ]
    coop_set = set(coop_ids)
    non_coop_ids = [pid for pid in marked if pid not in coop_set]

    n = len(marked)
    note: Optional[str] = None
    if target >= n:
        statuses = {pid: 1 for pid in marked}
        note = (
            f"Register present ({target}) is at least the {n} people with saved attendance; "
            "all were marked present."
        )
    elif target == 0:
        statuses = {pid: 0 for pid in marked}
    else:
        chosen: List[int] = []
        if len(coop_ids) >= target:
            chosen = rng.sample(coop_ids, target)
        else:
            chosen = list(coop_ids)
            need = target - len(chosen)
            chosen.extend(rng.sample(non_coop_ids, need))
        present_set = set(chosen)
        statuses = {pid: (1 if pid in present_set else 0) for pid in marked}

    set_attendance_for_date(attendance_date, statuses, db_path=db_path)
    sync_programme_register_present_from_attendance(attendance_date, db_path=db_path)
    present_after = sum(1 for v in statuses.values() if int(v) == 1)
    return {
        "ok": True,
        "target": target,
        "marked": n,
        "cooperative_candidates": len(coop_ids),
        "present_after": present_after,
        "note": note,
    }


def set_attendance_for_date(
    attendance_date: str, statuses: Dict[int, int], db_path: str = DB_PATH
) -> None:
    """
    Upsert attendance rows for (person_id, attendance_date).
    """
    if not statuses:
        return

    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        for person_id, status in statuses.items():
            conn.execute(
                """
                INSERT INTO attendance (person_id, attendance_date, status, marked_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (person_id, attendance_date)
                DO UPDATE SET
                  status = excluded.status,
                  marked_at = CURRENT_TIMESTAMP;
                """,
                (int(person_id), attendance_date, int(status)),
            )


def propagate_present_for_eligible_attendees(
    eligibility_dates: List[str],
    range_start: str,
    range_end: str,
    db_path: str = DB_PATH,
) -> Dict[str, int]:
    """
    Everyone with status=present (1) on any date in `eligibility_dates` gets
    attendance status=present for every calendar day from range_start through
    range_end inclusive (ISO dates YYYY-MM-DD).
    """
    if not eligibility_dates:
        return {"people": 0, "dates": 0, "cells_upserted": 0}

    d0 = date.fromisoformat(range_start)
    d1 = date.fromisoformat(range_end)
    if d1 < d0:
        raise ValueError("range_end must be on or after range_start")

    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        placeholders = ",".join(["?"] * len(eligibility_dates))
        rows = conn.execute(
            f"""
            SELECT DISTINCT person_id FROM attendance
            WHERE attendance_date IN ({placeholders}) AND status = 1
            """,
            eligibility_dates,
        ).fetchall()
        person_ids = [int(r["person_id"]) for r in rows]
        if not person_ids:
            return {"people": 0, "dates": 0, "cells_upserted": 0}

        cells = 0
        cur = d0
        while cur <= d1:
            ds = cur.isoformat()
            for pid in person_ids:
                conn.execute(
                    """
                    INSERT INTO attendance (person_id, attendance_date, status, marked_at)
                    VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT (person_id, attendance_date)
                    DO UPDATE SET
                      status = 1,
                      marked_at = CURRENT_TIMESTAMP;
                    """,
                    (pid, ds),
                )
                cells += 1
            cur += timedelta(days=1)

        n_days = (d1 - d0).days + 1
        return {"people": len(person_ids), "dates": n_days, "cells_upserted": cells}


def get_attendance_summary_for_range(
    start_date: str, end_date: str, db_path: str = DB_PATH
) -> Dict[str, int]:
    """
    Summary stats over attendance_date between start_date and end_date (inclusive).
    Counts are based on the `attendance` table rows saved by the app.
    """
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS total_rows,
              SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS present_rows,
              SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END) AS absent_rows,
              COUNT(DISTINCT attendance_date) AS distinct_dates,
              COUNT(DISTINCT person_id) AS distinct_people
            FROM attendance
            WHERE attendance_date BETWEEN ? AND ?;
            """,
            (start_date, end_date),
        ).fetchone()

        return {
            "total_rows": int(row["total_rows"] or 0),
            "present_rows": int(row["present_rows"] or 0),
            "absent_rows": int(row["absent_rows"] or 0),
            "distinct_dates": int(row["distinct_dates"] or 0),
            "distinct_people": int(row["distinct_people"] or 0),
        }


def get_present_absent_counts_by_date_range(
    start_date: str, end_date: str, db_path: str = DB_PATH
) -> List[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT
                  attendance_date,
                  SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS present_count,
                  SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END) AS absent_count
                FROM attendance
                WHERE attendance_date BETWEEN ? AND ?
                GROUP BY attendance_date
                ORDER BY attendance_date ASC;
                """,
                (start_date, end_date),
            ).fetchall()
        )


def get_chart_present_absent_by_date_range(
    start_date: str,
    end_date: str,
    programme_total_marked: int,
    db_path: str = DB_PATH,
) -> List[Dict[str, Any]]:
    """
    One row per calendar day between ``start_date`` and ``end_date`` inclusive.

    - Days that are **printed programme** rows (``day_label`` set in ``programme_daily_register``):
      **Present** = stored notebook figure; **Absent** = ``programme_total_marked`` − Present.
    - All other days: **Present** / **Absent** from ``attendance`` sums (0 if no rows).
    """
    d0 = date.fromisoformat(start_date)
    d1 = date.fromisoformat(end_date)
    with connect(db_path) as conn:
        reg_rows = conn.execute(
            """
            SELECT attendance_date, participant_count, day_label
            FROM programme_daily_register
            WHERE attendance_date BETWEEN ? AND ?;
            """,
            (start_date, end_date),
        ).fetchall()
        att_rows = conn.execute(
            """
            SELECT
              attendance_date,
              SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS present_count,
              SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END) AS absent_count
            FROM attendance
            WHERE attendance_date BETWEEN ? AND ?
            GROUP BY attendance_date;
            """,
            (start_date, end_date),
        ).fetchall()
    reg_map = {str(r["attendance_date"]): r for r in reg_rows}
    att_map = {str(r["attendance_date"]): r for r in att_rows}
    out: List[Dict[str, Any]] = []
    cur = d0
    ptot = int(programme_total_marked)
    while cur <= d1:
        ds = cur.isoformat()
        r = reg_map.get(ds)
        use_notebook = (
            r is not None
            and r["day_label"] is not None
            and str(r["day_label"]).strip() != ""
        )
        if use_notebook:
            p = int(r["participant_count"])
            a = max(0, ptot - p)
        else:
            ar = att_map.get(ds)
            if ar:
                p = int(ar["present_count"] or 0)
                a = int(ar["absent_count"] or 0)
            else:
                p, a = 0, 0
        out.append(
            {
                "attendance_date": ds,
                "present_count": p,
                "absent_count": a,
            }
        )
        cur += timedelta(days=1)
    return out


def get_present_absent_counts_by_sex_range(
    start_date: str, end_date: str, db_path: str = DB_PATH
) -> List[sqlite3.Row]:
    sx = _sql_sex_normalize_expr("p.sex")
    with connect(db_path) as conn:
        return list(
            conn.execute(
                f"""
                SELECT
                  {sx} AS sex,
                  SUM(CASE WHEN a.status = 1 THEN 1 ELSE 0 END) AS present_count,
                  SUM(CASE WHEN a.status = 0 THEN 1 ELSE 0 END) AS absent_count
                FROM attendance a
                JOIN people p ON p.id = a.person_id
                WHERE a.attendance_date BETWEEN ? AND ?
                  AND COALESCE(p.is_cooperative_placeholder, 0) = 0
                GROUP BY sex
                ORDER BY sex ASC;
                """,
                (start_date, end_date),
            ).fetchall()
        )


def get_top_cooperatives_by_present(
    start_date: str, end_date: str, limit: int = 10, db_path: str = DB_PATH
) -> List[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT
                  p.cooperative_name AS cooperative_name,
                  SUM(CASE WHEN a.status = 1 THEN 1 ELSE 0 END) AS present_count
                FROM attendance a
                JOIN people p ON p.id = a.person_id
                WHERE a.attendance_date BETWEEN ? AND ?
                  AND COALESCE(p.is_cooperative_placeholder, 0) = 0
                GROUP BY p.cooperative_name
                ORDER BY present_count DESC
                LIMIT ?;
                """,
                (start_date, end_date, int(limit)),
            ).fetchall()
        )


def count_cooperatives(db_path: str = DB_PATH) -> int:
    with connect(db_path) as conn:
        reg = conn.execute("SELECT COUNT(*) AS c FROM cooperative_registry;").fetchone()
        n_reg = int(reg["c"] or 0)
        if n_reg > 0:
            return n_reg
        row = conn.execute("SELECT COUNT(DISTINCT cooperative_name) AS c FROM people;").fetchone()
        return int(row["c"] or 0)


def get_all_cooperative_names(db_path: str = DB_PATH) -> List[str]:
    """When cooperative_registry is loaded, only those official names (dropdown source)."""
    with connect(db_path) as conn:
        reg = conn.execute("SELECT COUNT(*) AS c FROM cooperative_registry;").fetchone()
        if int(reg["c"] or 0) > 0:
            rows = conn.execute(
                """
                SELECT official_name FROM cooperative_registry
                ORDER BY official_name COLLATE NOCASE ASC;
                """
            ).fetchall()
            return [str(r["official_name"]) for r in rows]
        rows = conn.execute(
            """
            SELECT DISTINCT cooperative_name
            FROM people
            WHERE cooperative_name IS NOT NULL AND cooperative_name <> ''
            ORDER BY cooperative_name COLLATE NOCASE ASC;
            """
        ).fetchall()
        return [str(r["cooperative_name"]) for r in rows]


def get_members_bundled_by_cooperative(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    """
    Real participants only (not cooperative placeholders), with a non-empty cooperative.
    One row per person: cooperative name, contact name, NRC. Sorted by cooperative then name.
    """
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT
                  cooperative_name AS cooperative,
                  contact_person_details AS member_name,
                  nrc_details AS nrc
                FROM people
                WHERE COALESCE(is_cooperative_placeholder, 0) = 0
                  AND cooperative_name IS NOT NULL AND TRIM(cooperative_name) <> ''
                ORDER BY cooperative_name COLLATE NOCASE,
                         contact_person_details COLLATE NOCASE;
                """
            ).fetchall()
        )


def get_cooperatives_with_counts(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    """
    Rows have: name, member_count, cooperative_number, license_number (when registry is in use).
    """
    with connect(db_path) as conn:
        reg = conn.execute("SELECT COUNT(*) AS c FROM cooperative_registry;").fetchone()
        if int(reg["c"] or 0) > 0:
            return list(
                conn.execute(
                    """
                    SELECT
                      r.id AS registry_id,
                      r.official_name AS name,
                      r.cooperative_number AS cooperative_number,
                      r.license_number AS license_number,
                      r.preferred_mining_area AS preferred_mining_area,
                      SUM(
                        CASE
                          WHEN p.id IS NOT NULL
                           AND COALESCE(p.is_cooperative_placeholder, 0) = 0 THEN 1
                          ELSE 0
                        END
                      ) AS member_count
                    FROM cooperative_registry r
                    LEFT JOIN people p ON p.cooperative_name = r.official_name
                    GROUP BY r.id
                    ORDER BY r.official_name COLLATE NOCASE ASC;
                    """
                ).fetchall()
            )
        return list(
            conn.execute(
                """
                SELECT NULL AS registry_id, cooperative_name AS name, NULL AS cooperative_number,
                       NULL AS license_number, NULL AS preferred_mining_area, COUNT(*) AS member_count
                FROM people
                WHERE cooperative_name IS NOT NULL AND cooperative_name <> ''
                  AND (is_cooperative_placeholder IS NULL OR is_cooperative_placeholder = 0)
                GROUP BY cooperative_name
                ORDER BY cooperative_name COLLATE NOCASE ASC;
                """
            ).fetchall()
        )


def summarize_people_not_on_registry(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    """Distinct cooperative_name values on people that do not match any registry official_name (trimmed)."""
    with connect(db_path) as conn:
        if int(
            conn.execute("SELECT COUNT(*) AS c FROM cooperative_registry;").fetchone()["c"] or 0
        ) == 0:
            return []
        return list(
            conn.execute(
                """
                SELECT p.cooperative_name AS cooperative_name, COUNT(*) AS person_count
                FROM people p
                WHERE p.cooperative_name IS NOT NULL AND trim(p.cooperative_name) <> ''
                  AND NOT EXISTS (
                    SELECT 1 FROM cooperative_registry r
                    WHERE TRIM(r.official_name) = TRIM(p.cooperative_name)
                  )
                GROUP BY p.cooperative_name
                ORDER BY person_count DESC, p.cooperative_name COLLATE NOCASE ASC;
                """
            ).fetchall()
        )


def get_person_ids_by_cooperative(
    cooperative_name: str, include_placeholders: bool = True, db_path: str = DB_PATH
) -> List[int]:
    """Return all person IDs in a cooperative. Use for bulk delete."""
    with connect(db_path) as conn:
        sql = "SELECT id FROM people WHERE TRIM(cooperative_name) = TRIM(?)"
        if not include_placeholders:
            sql += " AND (is_cooperative_placeholder IS NULL OR is_cooperative_placeholder = 0)"
        sql += ";"
        rows = conn.execute(sql, (cooperative_name,)).fetchall()
        return [int(r["id"]) for r in rows]


def get_people_by_cooperative(
    cooperative_name: str, db_path: str = DB_PATH, limit: int = 2000
) -> List[sqlite3.Row]:
    """Return people in a given cooperative (excludes placeholders). Matches trimmed names."""
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT id, cooperative_name, group_venue, district_location,
                       contact_person_details, sex, nrc_details, contact_number
                FROM people
                WHERE TRIM(cooperative_name) = TRIM(?)
                  AND (is_cooperative_placeholder IS NULL OR is_cooperative_placeholder = 0)
                ORDER BY contact_person_details COLLATE NOCASE ASC
                LIMIT ?;
                """,
                (cooperative_name, int(limit)),
            ).fetchall()
        )


def batch_update_cooperative(
    person_ids: Iterable[int],
    new_cooperative_name: str,
    include_placeholders: bool = False,
    db_path: str = DB_PATH,
) -> int:
    """Update cooperative_name for multiple people. Returns count updated."""
    ids = list(person_ids)
    if not ids:
        return 0
    with connect(db_path) as conn:
        placeholders = ",".join(["?"] * len(ids))
        sql = f"""
            UPDATE people SET cooperative_name = ?
            WHERE id IN ({placeholders})
        """
        if not include_placeholders:
            sql += " AND (is_cooperative_placeholder IS NULL OR is_cooperative_placeholder = 0)"
        sql += ";"
        cur = conn.execute(
            sql,
            [new_cooperative_name.strip()] + [int(i) for i in ids],
        )
        return cur.rowcount


def merge_cooperative_into(
    source_cooperative: str, target_cooperative: str, db_path: str = DB_PATH
) -> int:
    """Move all members and placeholders from source cooperative to target. Returns count moved."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM people WHERE TRIM(cooperative_name) = TRIM(?)",
            (source_cooperative,),
        ).fetchall()
    ids = [int(r["id"]) for r in rows]
    return batch_update_cooperative(ids, target_cooperative, include_placeholders=True, db_path=db_path)


def clear_legacy_cooperative(cooperative_name: str, db_path: str = DB_PATH) -> int:
    """
    Set cooperative_name to '' for every row whose label matches (trimmed), including cooperative placeholders.
    Does not delete people. Removes the legacy string from the database so it no longer appears in lists.
    """
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE people SET cooperative_name = ''
            WHERE TRIM(cooperative_name) = TRIM(?);
            """,
            (cooperative_name,),
        )
        return int(cur.rowcount or 0)


def count_gender_distribution(db_path: str = DB_PATH) -> Dict[str, int]:
    """
    Male / Female / Other counts for real participants only (excludes cooperative placeholders).
    Invalid or mis-imported `sex` values are grouped as Other.
    """
    sx = _sql_sex_normalize_expr("sex")
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT {sx} AS sex_norm, COUNT(*) AS c
            FROM people
            WHERE COALESCE(is_cooperative_placeholder, 0) = 0
            GROUP BY sex_norm
            ORDER BY sex_norm ASC;
            """
        ).fetchall()
        return {str(r["sex_norm"]): int(r["c"]) for r in rows}


def count_district_distribution(
    db_path: str = DB_PATH, limit: int = 30
) -> List[Dict[str, Any]]:
    """
    District counts merged case-insensitively (e.g. CHOMA, Choma, choma → one row).
    The displayed name is the spelling that appears most often in the data.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT TRIM(district_location) AS district_location, COUNT(*) AS c
            FROM people
            WHERE district_location IS NOT NULL AND TRIM(district_location) <> ''
              AND COALESCE(is_cooperative_placeholder, 0) = 0
            GROUP BY TRIM(district_location);
            """
        ).fetchall()

    buckets: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        raw = str(r["district_location"] or "").strip()
        if not raw:
            continue
        key = raw.casefold()
        n = int(r["c"])
        if key not in buckets:
            buckets[key] = {"total": 0, "variants": defaultdict(int)}
        buckets[key]["total"] += n
        buckets[key]["variants"][raw] += n

    merged: List[Dict[str, Any]] = []
    for data in buckets.values():
        total = int(data["total"])
        variants = data["variants"]
        canonical = max(variants.items(), key=lambda x: (x[1], x[0]))[0]
        merged.append({"district_location": canonical, "c": total})

    merged.sort(key=lambda x: (-int(x["c"]), str(x["district_location"]).casefold()))
    return merged[: int(limit)]


def count_group_distribution(db_path: str = DB_PATH) -> List[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT cooperative_name AS group_name, COUNT(*) AS c
                FROM people
                WHERE cooperative_name IS NOT NULL AND cooperative_name <> ''
                GROUP BY cooperative_name
                ORDER BY c DESC;
                """
            ).fetchall()
        )


def _extract_province_from_text(district_location: str) -> Optional[str]:
    """
    Heuristic province extraction using word-boundary style checks so random text
    (e.g. names in the district field) does not match province substrings.
    """
    if not district_location:
        return None
    text = district_location.strip().lower()
    if not text:
        return None
    # Longer / compound names first so "north western" wins before "western".
    patterns: List[Tuple[str, str]] = [
        ("North-Western", r"(?<![a-z0-9])(?:north[\s-]western|northwestern)(?![a-z0-9])"),
        ("Copperbelt", r"(?<![a-z0-9])copperbelt(?![a-z0-9])"),
        ("Muchinga", r"(?<![a-z0-9])muchinga(?![a-z0-9])"),
        ("Luapula", r"(?<![a-z0-9])luapula(?![a-z0-9])"),
        ("Lusaka", r"(?<![a-z0-9])lusaka(?![a-z0-9])"),
        ("Eastern", r"(?<![a-z0-9])eastern(?![a-z0-9])"),
        ("Central", r"(?<![a-z0-9])central(?![a-z0-9])"),
        ("Northern", r"(?<![a-z0-9])northern(?![a-z0-9])"),
        ("Southern", r"(?<![a-z0-9])southern(?![a-z0-9])"),
        ("Western", r"(?<![a-z0-9])western(?![a-z0-9])"),
    ]
    for label, pat in patterns:
        if re.search(pat, text):
            return label
    return None


def count_province_distribution(db_path: str = DB_PATH) -> Dict[str, int]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT district_location
            FROM people
            WHERE district_location IS NOT NULL AND TRIM(district_location) <> ''
              AND COALESCE(is_cooperative_placeholder, 0) = 0;
            """
        ).fetchall()
        counts: Dict[str, int] = {}
        for r in rows:
            prov = _extract_province_from_text(str(r["district_location"]))
            if not prov:
                continue
            counts[prov] = counts.get(prov, 0) + 1
        return counts


def reset_people_and_attendance(db_path: str = DB_PATH) -> None:
    """
    DANGER: This deletes `people` (and cascading attendance rows) and keeps only auth users.
    Use before re-importing so participants from DOCX are split correctly.
    """
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("DELETE FROM attendance;")
        conn.execute("DELETE FROM people;")


def iso_date_str(d: Optional[date] = None) -> str:
    if d is None:
        d = date.today()
    return d.isoformat()

