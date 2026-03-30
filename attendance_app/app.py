import hashlib
import os
import sys
import sqlite3
from datetime import date
from typing import Dict, List, Optional, Tuple

# Ensure local package path is available no matter CWD (important for Streamlit Cloud). 
# This allows `from db import ...` when running from repo root.
here = os.path.abspath(os.path.dirname(__file__))
if here not in sys.path:
    sys.path.insert(0, here)

import re
import pandas as pd
import streamlit as st
from werkzeug.security import check_password_hash, generate_password_hash

from db import (
    DB_PATH,
    add_person,
    cooperative_registry_count,
    delete_cooperative_placeholder,
    get_all_cooperative_names,
    get_cooperative_placeholders,
    count_users,
    count_people,
    count_cooperatives,
    count_gender_distribution,
    count_district_distribution,
    count_province_distribution,
    create_user,
    get_attendance_for_date,
    get_cooperative_number_lookup,
    get_attendance_roster_for_date,
    get_attendance_summary_for_range,
    get_chart_present_absent_by_date_range,
    get_present_absent_counts_by_sex_range,
    get_top_cooperatives_by_present,
    get_people_by_ids,
    init_db,
    upsert_cooperative_registry_entry,
    update_cooperative_registry_by_id,
    normalize_sex_for_stats,
    get_user_by_username,
    remap_people_to_cooperative_registry,
    search_people,
    set_attendance_for_date,
    sync_programme_register_present_from_attendance,
    set_preferred_mining_area,
    summarize_people_not_on_registry,
    get_area_allocation_overview,
    get_members_bundled_by_cooperative,
    ensure_cooperative_registry_entry,
    update_person,
    iso_date_str,
    get_programme_register_row,
    list_programme_register,
    redistribute_attendance_to_match_register,
)

try:
    from db import (
        delete_people,
        get_person_ids_by_cooperative,
        get_cooperatives_with_counts,
        get_people_by_cooperative,
        batch_update_cooperative,
        merge_cooperative_into,
        clear_legacy_cooperative,
    )
except ImportError:
    delete_people = None
    get_person_ids_by_cooperative = None
    get_cooperatives_with_counts = None
    get_people_by_cooperative = None
    batch_update_cooperative = None
    merge_cooperative_into = None
    clear_legacy_cooperative = None

st.set_page_config(
    page_title="Attendance App",
    page_icon="🗂️",
    layout="wide",
)

# Training period: programme days March 2025 (defaults for pickers / stats)
TRAINING_START = date(2025, 3, 16)
TRAINING_END = date(2025, 3, 30)
TRAINING_DATES = [
    date(2025, 3, 16),
    date(2025, 3, 17),
    date(2025, 3, 18),
    date(2025, 3, 19),
    date(2025, 3, 20),
    date(2025, 3, 21),
    date(2025, 3, 22),
    date(2025, 3, 23),
    date(2025, 3, 24),
    date(2025, 3, 25),
    date(2025, 3, 26),
    date(2025, 3, 27),
    date(2025, 3, 28),
    date(2025, 3, 30),
]

# Programme-wide “total marked” shown on Daily Stats (people roster size / expectation across dates).
PROGRAMME_TOTAL_MARKED = 456


def _require_login() -> None:
    """
    Simple local auth using a `users` table in SQLite.
    First run creates an admin user via the UI.
    """
    init_db()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
        st.session_state.username = None

    if st.session_state.authenticated:
        return

    st.sidebar.header("Login")

    if count_users() == 0:
        st.sidebar.warning("No admin user found. Create the first admin account.")
        with st.sidebar.form("create_admin_form"):
            username = st.text_input("Admin username", value="admin")
            password = st.text_input("Admin password", type="password")
            confirm = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create admin")

        if submitted:
            if not username.strip():
                st.sidebar.error("Username is required.")
            elif password != confirm or len(password) < 6:
                st.sidebar.error("Password must match and be at least 6 characters.")
            else:
                # Some Python builds (including certain macOS/Python combinations)
                # do not include `hashlib.scrypt`. Use PBKDF2 which is widely available.
                password_hash = generate_password_hash(password, method="pbkdf2:sha256")
                try:
                    create_user(username.strip(), password_hash)
                    st.sidebar.success("Admin account created. Please log in.")
                except sqlite3.IntegrityError:
                    st.sidebar.error("That username already exists.")

    with st.sidebar.form("login_form", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if submitted:
        row = get_user_by_username(username.strip())
        if not row:
            st.sidebar.error("Invalid username or password.")
            st.stop()

        if not check_password_hash(row["password_hash"], password):
            st.sidebar.error("Invalid username or password.")
            st.stop()

        st.session_state.authenticated = True
        st.session_state.username = row["username"]
        st.sidebar.success(f"Welcome, {row['username']}!")
        st.rerun()

    if not st.session_state.authenticated:
        st.title("Attendance App")
        st.info("Sign in using the **sidebar** (left).")
        st.stop()


def _logout():
    st.session_state.authenticated = False
    st.session_state.username = None
    st.rerun()


def _coerce_present(v) -> int:
    # streamlit checkbox returns bool; we also accept 0/1.
    return 1 if bool(v) else 0


def page_mark_attendance():
    st.header("Mark Attendance (per date)")

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        attendance_date = st.date_input(
            "Attendance date", value=TRAINING_START, key="mark_attendance_date"
        )
        attendance_date_str = iso_date_str(attendance_date)

    with col2:
        search_name = st.text_input(
            "Search by Name (contact person)", value="", key="mark_search_name"
        )
    with col3:
        search_coop = st.text_input("Search by Cooperative", value="", key="mark_search_coop")

    search_nrc = st.text_input("Search by NRC", value="", key="mark_search_nrc")

    filters = {
        "name": search_name or None,
        "cooperative": search_coop or None,
        "nrc": search_nrc or None,
    }
    people = search_people(**filters, limit=500)
    st.caption(f"Showing {len(people)} people.")

    if not people:
        st.info("No people match your search filters. Register a person first.")
        return

    person_ids = [int(p["id"]) for p in people]
    attendance_map = get_attendance_for_date(attendance_date_str, person_ids=person_ids)

    df = pd.DataFrame([dict(p) for p in people])
    df["Present"] = df["id"].apply(lambda pid: bool(int(attendance_map.get(int(pid), 0))))
    coop_no_lookup = get_cooperative_number_lookup(DB_PATH)
    df["cooperative_number"] = df["cooperative_name"].map(
        lambda n: coop_no_lookup.get(str(n).strip(), "") or ""
    )
    df = df.rename(
        columns={
            "contact_person_details": "Name",
            "contact_number": "Contact Number",
            "cooperative_name": "Cooperative",
            "cooperative_number": "Coop No",
            "group_venue": "Group/Venue",
            "district_location": "District/Location",
            "sex": "Sex",
            "nrc_details": "NRC",
        }
    )

    df_view = df[
        [
            "id",
            "Name",
            "Contact Number",
            "Cooperative",
            "Coop No",
            "Group/Venue",
            "Sex",
            "District/Location",
            "NRC",
            "Present",
        ]
    ].copy()

    edited_df = st.data_editor(
        df_view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "id": st.column_config.Column(disabled=True),
            "Name": st.column_config.Column(disabled=True),
            "Contact Number": st.column_config.Column(disabled=True),
            "Cooperative": st.column_config.Column(disabled=True),
            "Coop No": st.column_config.Column(disabled=True),
            "Group/Venue": st.column_config.Column(disabled=True),
            "Sex": st.column_config.Column(disabled=True),
            "District/Location": st.column_config.Column(disabled=True),
            "NRC": st.column_config.Column(disabled=True),
            "Present": st.column_config.CheckboxColumn("Present", required=True),
        },
        key=f"attendance_editor_{attendance_date_str}_{st.session_state.get('username','')}",
    )

    df_out = edited_df if isinstance(edited_df, pd.DataFrame) else df_view

    if st.button("Save attendance", type="primary", key="mark_save_attendance_btn"):
        statuses: Dict[int, int] = {}
        for _, row in df_out.iterrows():
            statuses[int(row["id"])] = _coerce_present(row["Present"])

        set_attendance_for_date(attendance_date_str, statuses)
        sync_programme_register_present_from_attendance(attendance_date_str, db_path=DB_PATH)
        st.success("Attendance saved.")


def page_register_person():
    st.header("Register Person")

    all_coops = get_all_cooperative_names()
    registry_on = cooperative_registry_count() > 0

    with st.form("register_form"):
        if registry_on:
            st.caption("Pick a registered cooperative or add a new one (saved to the registry for everyone).")
        coop_options = (all_coops or []) + ["+ Add new cooperative..."]
        coop_choice = st.selectbox(
            "NAME OF COOPERATIVE",
            options=coop_options,
            key="register_cooperative_choice",
        )
        if coop_choice == "+ Add new cooperative...":
            cooperative_name = st.text_input(
                "New cooperative name (official name)", key="register_cooperative_new"
            )
            new_reg_coop_no = st.text_input(
                "Cooperative No (optional)", key="register_new_coop_registry_no"
            )
            new_reg_license = st.text_input(
                "License No (optional)", key="register_new_coop_registry_lic"
            )
        else:
            cooperative_name = coop_choice
            new_reg_coop_no = ""
            new_reg_license = ""

        group_venue_option = st.selectbox(
            "Group/Venue",
            options=[
                "Group A - Grand Hotel",
                "Group B - Royal Eagles Hotel",
                "Other",
            ],
            index=0,
            key="register_group_venue_option",
        )
        if group_venue_option == "Other":
            group_venue = st.text_input(
                "Other Group/Venue", key="register_group_venue_custom"
            )
        else:
            group_venue = group_venue_option

        district_location = st.text_input(
            "DISTRICT / LOCATION", key="register_district_location"
        )
        contact_person_details = st.text_input(
            "CONTACT PERSON / DETAILS", key="register_contact_person_details"
        )
        sex = st.selectbox("SEX", options=["Male", "Female", "Other"], key="register_sex")
        nrc_details = st.text_input("NRC DETAILS", key="register_nrc_details")

        submitted = st.form_submit_button("Register", key="register_submit_btn")

    if submitted:
        missing = []
        if not cooperative_name.strip():
            missing.append("NAME OF COOPERATIVE")
        if not district_location.strip():
            missing.append("DISTRICT / LOCATION")
        if not contact_person_details.strip():
            missing.append("CONTACT PERSON / DETAILS")
        if not sex.strip():
            missing.append("SEX")
        if not nrc_details.strip():
            missing.append("NRC DETAILS")

        if missing:
            st.error("Missing: " + ", ".join(missing))
            return

        try:
            init_db(DB_PATH)
            if (
                registry_on
                and coop_choice == "+ Add new cooperative..."
                and cooperative_name.strip()
            ):
                upsert_cooperative_registry_entry(
                    cooperative_name.strip(),
                    cooperative_number=new_reg_coop_no.strip() or None,
                    license_number=new_reg_license.strip() or None,
                    db_path=DB_PATH,
                )
            # Try to extract a phone number from the free-text field.
            # (If none found, we store NULL.)
            phone_match = re.search(r"\b\d{9,}\b", contact_person_details)
            contact_number = phone_match.group(0) if phone_match else None
            person_id = add_person(
                cooperative_name=cooperative_name.strip(),
                group_venue=group_venue.strip() if group_venue else None,
                district_location=district_location.strip(),
                contact_person_details=contact_person_details.strip(),
                sex=sex.strip(),
                nrc_details=nrc_details.strip(),
                contact_number=contact_number,
            )
            st.success(f"Registered successfully (ID: {person_id}).")
        except sqlite3.IntegrityError:
            st.error("NRC already exists. Use a different NRC DETAILS value.")


def page_search_people():
    st.header("Search People")

    col1, col2 = st.columns([1, 1])
    with col1:
        search_name = st.text_input(
            "Search by Name (contact person)", value="", key="search_people_name"
        )
        search_coop = st.text_input("Search by Cooperative", value="", key="search_people_coop")
    with col2:
        search_nrc = st.text_input("Search by NRC", value="", key="search_people_nrc")
        limit = st.slider(
            "Max results", min_value=10, max_value=500, value=200, step=10, key="search_people_limit"
        )

    people = search_people(
        name=search_name or None,
        cooperative=search_coop or None,
        nrc=search_nrc or None,
        limit=limit,
    )

    if not people:
        st.info("No matches.")
        return

    df = pd.DataFrame([dict(p) for p in people])
    df = df.rename(
        columns={
            "contact_person_details": "Name",
            "contact_number": "Contact Number",
            "cooperative_name": "Cooperative",
            "group_venue": "Group/Venue",
            "district_location": "District/Location",
            "sex": "Sex",
            "nrc_details": "NRC",
        }
    )
    st.dataframe(
        df[
            [
                "id",
                "Name",
                "Contact Number",
                "Cooperative",
                "Sex",
                "District/Location",
                "NRC",
                "created_at",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


def page_history():
    st.header("Attendance History (simple)")

    with st.form("history_form"):
        d1 = st.date_input("From date", value=TRAINING_START, key="history_from_date")
        d2 = st.date_input("To date", value=TRAINING_END, key="history_to_date")
        submitted = st.form_submit_button("Load", key="history_load_btn")

    if not submitted:
        return

    if d2 < d1:
        st.error("To date must be on/after From date.")
        return

    # We keep this simple and show for each date the count of present people.
    # For a full roster per date we'd join attendance + people.
    present_rows = []
    # Build all person IDs once to reduce queries.
    # For MVP size, this is fine.
    people = search_people(limit=10000)
    person_ids = [int(p["id"]) for p in people]

    current = d1
    while current <= d2:
        ds = iso_date_str(current)
        attendance_map = get_attendance_for_date(ds, person_ids=person_ids)
        present_count = sum(1 for s in attendance_map.values() if int(s) == 1)
        present_rows.append({"Date": ds, "Present": present_count})
        current = current.fromordinal(current.toordinal() + 1)

    df = pd.DataFrame(present_rows)
    st.dataframe(df, use_container_width=True)


def page_records():
    st.header("Records")

    with st.expander("Official cooperatives (registry + remap participants)", expanded=False):
        st.caption(
            "Registry data in the database powers official names and licence numbers. "
            "**Remap** aligns participant cooperative strings to the closest registry name "
            "(when two registry names tie, the **newest** registry row wins). "
            "To load or replace the registry from CSV, use a terminal script—not this screen."
        )
        if st.button("Remap all participants to registry names", key="registry_remap_btn"):
            try:
                init_db(DB_PATH)
                out = remap_people_to_cooperative_registry(db_path=DB_PATH)
                if out.get("skipped"):
                    st.warning("The cooperative registry is empty in the database.")
                else:
                    st.success(
                        f"Rewrote {out['distinct_names_rewritten']} distinct name(s); "
                        f"updated {out['people_rows_updated']} person row(s)."
                    )
                    st.rerun()
            except Exception as e:
                st.error(f"Remap failed: {e}")

        n_reg = cooperative_registry_count()
        st.caption(f"Registry rows in database: **{n_reg}**.")
        if n_reg > 0:
            off = summarize_people_not_on_registry(db_path=DB_PATH)
            if off:
                st.warning(
                    "These cooperative strings on participant records do not exactly match the registry "
                    "(run **Remap** above, or fix names manually)."
                )
                st.dataframe(
                    pd.DataFrame([dict(r) for r in off]).rename(
                        columns={"cooperative_name": "Cooperative (on record)", "person_count": "People"}
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.success("All participants use an exact registry cooperative name.")

    st.subheader("Registered People")
    with st.expander("Search registered people", expanded=False):
        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            people_name = st.text_input(
                "Search by Name (contact person)", value="", key="records_people_name"
            )
        with c2:
            people_coop = st.text_input(
                "Search by Cooperative", value="", key="records_people_coop"
            )
        with c3:
            people_nrc = st.text_input("Search by NRC", value="", key="records_people_nrc")

    limit = st.slider(
        "Max people to show",
        min_value=50,
        max_value=2000,
        value=200,
        step=50,
        key="records_people_limit",
    )

    people = search_people(
        name=people_name or None,
        cooperative=people_coop or None,
        nrc=people_nrc or None,
        limit=limit,
    )

    if people:
        df_people = pd.DataFrame([dict(p) for p in people]).rename(
            columns={
                "contact_person_details": "Name",
                "contact_number": "Contact Number",
                "cooperative_name": "Cooperative",
                "group_venue": "Group/Venue",
                "district_location": "District/Location",
                "sex": "Sex",
                "nrc_details": "NRC",
            }
        )
        st.dataframe(
            df_people[
                [
                    "id",
                    "Name",
                    "Contact Number",
                    "Cooperative",
                    "Group/Venue",
                    "Sex",
                    "District/Location",
                    "NRC",
                    "created_at",
                ]
            ],
            use_container_width=True,
            hide_index=True,
            key="records_people_table",
        )

        st.subheader("Edit existing person")
        # Work with IDs so selection keeps correct record after filtering.
        person_ids = [int(p["id"]) for p in people]
        selected_person_id = st.selectbox(
            "Select person to edit",
            options=person_ids,
            format_func=lambda idx: f"{idx} - {next(p['contact_person_details'] for p in people if int(p['id']) == idx)} ({next(p['nrc_details'] for p in people if int(p['id']) == idx)})",
            key="records_selected_person_id",
        )

        person_row = next((p for p in people if int(p["id"]) == int(selected_person_id)), None)
        if person_row is None:
            st.warning("Selected person was not found in current results; refresh search.")
            return

        person = dict(person_row)
        with st.form("edit_person_form"):
            widget_suffix = f"_{selected_person_id}"
            coop_names = get_all_cooperative_names()
            cur_coop = (person.get("cooperative_name") or "").strip()
            if cur_coop and cur_coop not in coop_names:
                coop_names = [cur_coop] + coop_names
            if coop_names:
                try:
                    coop_idx = coop_names.index(cur_coop) if cur_coop else 0
                except ValueError:
                    coop_idx = 0
                edit_cooperative_name = st.selectbox(
                    "NAME OF COOPERATIVE",
                    options=coop_names,
                    index=coop_idx,
                    key=f"edit_cooperative_name{widget_suffix}",
                )
            else:
                edit_cooperative_name = st.text_input(
                    "NAME OF COOPERATIVE",
                    value=cur_coop,
                    key=f"edit_cooperative_name{widget_suffix}",
                )

            # Venue/Group assignment for existing person
            current_group_venue = person.get("group_venue") or "Other"
            group_options = ["Group A - Grand Hotel", "Group B - Royal Eagles Hotel", "Other"]
            edit_group = st.selectbox(
                "Group/Venue",
                options=group_options,
                index=group_options.index(current_group_venue) if current_group_venue in group_options else 2,
                key=f"edit_group{widget_suffix}",
            )
            if edit_group == "Other":
                edit_group_venue = st.text_input(
                    "Other group/venue",
                    value=person.get("group_venue", ""),
                    key=f"edit_group_venue{widget_suffix}",
                )
            else:
                edit_group_venue = edit_group

            edit_district_location = st.text_input(
                "DISTRICT / LOCATION",
                value=person.get("district_location", ""),
                key=f"edit_district_location{widget_suffix}",
            )
            edit_contact_person_details = st.text_input(
                "CONTACT PERSON / DETAILS",
                value=person["contact_person_details"],
                key=f"edit_contact_person_details{widget_suffix}",
            )
            edit_sex = st.selectbox(
                "SEX",
                options=["Male", "Female", "Other"],
                index=(
                    ["Male", "Female", "Other"].index(person["sex"]) if person["sex"] in ["Male", "Female", "Other"] else 0
                ),
                key=f"edit_sex{widget_suffix}",
            )
            edit_nrc_details = st.text_input(
                "NRC DETAILS",
                value=person["nrc_details"],
                key=f"edit_nrc_details{widget_suffix}",
            )
            edit_contact_number = st.text_input(
                "CONTACT NUMBER",
                value=(person["contact_number"] if person["contact_number"] is not None else ""),
                key=f"edit_contact_number{widget_suffix}",
            )
            submit_edit = st.form_submit_button("Save changes")

        if submit_edit:
            missing = []
            if not edit_cooperative_name.strip():
                missing.append("NAME OF COOPERATIVE")
            if not edit_district_location.strip():
                missing.append("DISTRICT / LOCATION")
            if not edit_contact_person_details.strip():
                missing.append("CONTACT PERSON / DETAILS")
            if not edit_sex.strip():
                missing.append("SEX")
            if not edit_nrc_details.strip():
                missing.append("NRC DETAILS")

            if missing:
                st.error("Missing: " + ", ".join(missing))
            else:
                try:
                    person_id = int(person["id"])
                    update_person(
                        person_id=person_id,
                        cooperative_name=edit_cooperative_name.strip(),
                        group_venue=(edit_group_venue.strip() if edit_group_venue else None),
                        district_location=edit_district_location.strip(),
                        contact_person_details=edit_contact_person_details.strip(),
                        sex=edit_sex.strip(),
                        nrc_details=edit_nrc_details.strip(),
                        contact_number=edit_contact_number.strip() or None,
                    )
                    st.success("Person record updated successfully. Please refresh the page to see the updated record.")
                    return
                except sqlite3.IntegrityError:
                    st.error("NRC already exists. Choose a different NRC DETAILS value.")

        st.divider()
        st.subheader("Delete participants")
        if delete_people is None:
            st.caption("Delete feature requires an updated deployment.")
        else:
            st.caption("Select participants to remove. Their attendance records will also be deleted.")
            delete_ids = []
            dcols = st.columns(4)
            for i, p in enumerate(people):
                label = f"{p['contact_person_details'][:28]}{'…' if len(p['contact_person_details']) > 28 else ''} ({p['nrc_details']})"
                with dcols[i % 4]:
                    if st.checkbox(label, key=f"delete_person_{p['id']}"):
                        delete_ids.append(int(p["id"]))
            if delete_ids:
                confirm = st.checkbox("I confirm I want to delete the selected participant(s)", key="delete_confirm")
                if confirm and st.button("Delete selected", type="primary", key="delete_btn"):
                    n = delete_people(delete_ids)
                    st.success(f"Deleted {n} participant(s).")
                    st.rerun()
            else:
                st.caption("Select one or more participants above to delete.")

    else:
        st.info("No people match your search filters.")

    st.divider()
    st.subheader("Cooperatives without members")
    placeholders = get_cooperative_placeholders()
    if placeholders:
        df_ph = pd.DataFrame([dict(p) for p in placeholders])
        df_ph = df_ph.rename(columns={"cooperative_name": "Cooperative", "district_location": "District"})
        st.dataframe(df_ph[["id", "Cooperative", "District"]], use_container_width=True, hide_index=True)
        with st.expander("Add member to a cooperative", expanded=False):
            ph_options = {f"{p['cooperative_name']} ({p['district_location']})": p for p in placeholders}
            sel_label = st.selectbox("Select cooperative", options=list(ph_options.keys()), key="add_member_coop_sel")
            if sel_label:
                ph = ph_options[sel_label]
                with st.form("add_member_form"):
                    add_name = st.text_input("CONTACT PERSON / DETAILS", key="add_member_name")
                    add_sex = st.selectbox("SEX", options=["Male", "Female", "Other"], key="add_member_sex")
                    add_nrc = st.text_input("NRC DETAILS", key="add_member_nrc")
                    add_phone = st.text_input("CONTACT NUMBER", key="add_member_phone")
                    if st.form_submit_button("Add member"):
                        if not add_name.strip() or not add_nrc.strip():
                            st.error("Name and NRC are required.")
                        else:
                            try:
                                phone_match = re.search(r"\b\d{9,}\b", add_phone or "")
                                contact_number = phone_match.group(0) if phone_match else None
                                add_person(
                                    cooperative_name=ph["cooperative_name"],
                                    group_venue=None,
                                    district_location=ph["district_location"],
                                    contact_person_details=add_name.strip(),
                                    sex=add_sex,
                                    nrc_details=add_nrc.strip(),
                                    contact_number=contact_number,
                                )
                                delete_cooperative_placeholder(int(ph["id"]))
                                st.success("Member added. Placeholder removed.")
                                st.rerun()
                            except sqlite3.IntegrityError:
                                st.error("NRC already exists.")
    else:
        st.info("No cooperatives without members. Import from documents to add placeholders.")

    st.divider()

    st.subheader("Attendance Records (Roster by Date)")
    records_date = st.date_input(
        "Attendance date",
        value=TRAINING_START,
        key="records_attendance_date",
    )
    records_date_str = iso_date_str(records_date)

    records_mode = st.radio(
        "Show attendance",
        options=["All", "Present", "Absent"],
        horizontal=True,
        key="records_attendance_mode",
    )
    status_map = {"All": None, "Present": 1, "Absent": 0}
    status = status_map[records_mode]

    roster = get_attendance_roster_for_date(
        attendance_date=records_date_str,
        status=status,
        limit=5000,
    )

    if roster:
        df_roster = pd.DataFrame([dict(r) for r in roster]).rename(
            columns={
                "contact_person_details": "Name",
                "contact_number": "Contact Number",
                "cooperative_name": "Cooperative",
                "district_location": "District/Location",
                "sex": "Sex",
                "nrc_details": "NRC",
                "status": "Present",
                "marked_at": "Marked At",
            }
        )
        df_roster["Present"] = df_roster["Present"].apply(lambda v: bool(int(v) if v is not None else 0))

        st.dataframe(
            df_roster[
                [
                    "person_id",
                    "Name",
                    "Contact Number",
                    "Cooperative",
                    "Sex",
                    "District/Location",
                    "NRC",
                    "Present",
                    "Marked At",
                ]
            ],
            use_container_width=True,
            hide_index=True,
            column_config={"Present": st.column_config.CheckboxColumn("Present", disabled=True)},
            key="records_roster_table",
        )

        csv_bytes = df_roster.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download roster CSV",
            data=csv_bytes,
            file_name=f"attendance_roster_{records_date_str}.csv",
            mime="text/csv",
            key="records_roster_download",
        )
    else:
        st.info("No attendance roster found for this date (nothing saved yet).")


def page_cooperatives():
    st.header("Cooperatives")

    if get_cooperatives_with_counts is None:
        st.warning("Cooperatives features require an updated database module. Please deploy the latest code.")
        return

    coops = get_cooperatives_with_counts()
    if not coops:
        st.info("No cooperatives yet. Register people or import from documents.")
        return

    df_coops = pd.DataFrame([dict(c) for c in coops])
    rename_map = {"name": "Cooperative", "member_count": "Members"}
    if cooperative_registry_count() > 0:
        rename_map["cooperative_number"] = "Cooperative No"
        rename_map["license_number"] = "License No"
        if "preferred_mining_area" in df_coops.columns:
            rename_map["preferred_mining_area"] = "Preferred mining area"
    df_coops = df_coops.rename(columns=rename_map)
    coop_names = [c["name"] for c in coops]

    st.subheader("All cooperatives")
    show_cols = ["Cooperative", "Cooperative No", "License No", "Preferred mining area", "Members"]
    if cooperative_registry_count() == 0:
        show_cols = ["Cooperative", "Members"]
    st.dataframe(df_coops[[c for c in show_cols if c in df_coops.columns]], use_container_width=True, hide_index=True)

    if cooperative_registry_count() > 0 and summarize_people_not_on_registry is not None:
        orphans_raw = summarize_people_not_on_registry(db_path=DB_PATH)
        if orphans_raw:
            st.divider()
            st.subheader("Members under a non-registry / legacy cooperative name")
            st.caption(
                "Some people still have an **old or alternate spelling** of a cooperative on their record. "
                "That string is **not** the same as any **official registry** name, so they **do not show up** "
                "when you pick the official cooperative in **Move members** below. Use this section to attach "
                "them to the correct official cooperative."
            )
            df_or = pd.DataFrame([dict(r) for r in orphans_raw]).rename(
                columns={"cooperative_name": "Name on records", "person_count": "People"}
            )
            st.dataframe(df_or, use_container_width=True, hide_index=True)

            legacy_options = [str(r["cooperative_name"]) for r in orphans_raw]
            legacy_pick = st.selectbox(
                "Name currently stored on member records (source)",
                options=legacy_options,
                key="coop_orphan_source_pick",
            )
            n_orphan = int(
                next(r["person_count"] for r in orphans_raw if str(r["cooperative_name"]) == legacy_pick)
            )
            st.caption(f"**{n_orphan}** participant(s) use this cooperative string.")

            target_official = st.selectbox(
                "Move to this official cooperative",
                options=sorted(coop_names, key=str.casefold),
                key="coop_orphan_target_pick",
            )

            if merge_cooperative_into is not None:
                if st.button(
                    f"Move all {n_orphan} member(s) to “{target_official}”",
                    type="primary",
                    key="coop_orphan_move_all_btn",
                ):
                    n = merge_cooperative_into(legacy_pick, target_official)
                    st.success(f"Moved {n} row(s). They now appear under the official cooperative.")
                    st.rerun()

            if get_people_by_cooperative is not None and batch_update_cooperative is not None:
                om = get_people_by_cooperative(legacy_pick, limit=5000)
                if om:
                    st.caption("Or pick specific people to move (leave all unchecked to use **Move all** above):")
                    sel_orphan_ids: List[int] = []
                    ocols = st.columns(4)
                    for i, m in enumerate(om):
                        olab = f"{m['contact_person_details'][:28]}{'…' if len(m['contact_person_details']) > 28 else ''} ({m['nrc_details']})"
                        with ocols[i % 4]:
                            if st.checkbox(olab, key=f"coop_orphan_sel_{m['id']}"):
                                sel_orphan_ids.append(int(m["id"]))
                    if sel_orphan_ids:
                        if st.button(
                            "Move selected only",
                            key="coop_orphan_move_sel_btn",
                        ):
                            n = batch_update_cooperative(sel_orphan_ids, target_official)
                            st.success(f"Moved {n} member(s) to '{target_official}'.")
                            st.rerun()

            if clear_legacy_cooperative is not None:
                _legacy_clear_key = hashlib.md5(legacy_pick.encode("utf-8")).hexdigest()[:16]
                with st.expander("Remove this legacy cooperative label"):
                    st.caption(
                        "If this string is obsolete or wrong and you **do not** want to merge it into an "
                        "official cooperative, you can **clear** it from every matching record. "
                        "**People are not deleted**; they will have no cooperative until you assign one elsewhere."
                    )
                    if st.checkbox(
                        f"I understand — clear cooperative for all {n_orphan} record(s) using “{legacy_pick}”",
                        key=f"coop_legacy_clear_confirm_{_legacy_clear_key}",
                    ):
                        if st.button(
                            "Clear cooperative name from these records",
                            key=f"coop_legacy_clear_btn_{_legacy_clear_key}",
                        ):
                            n_done = clear_legacy_cooperative(legacy_pick)
                            st.success(f"Cleared cooperative on {n_done} record(s).")
                            st.rerun()

    if cooperative_registry_count() > 0:
        st.divider()
        st.subheader("Edit cooperative details")
        st.caption("Change official name, cooperative number, or licence. Renaming updates every participant on that cooperative.")
        reg_rows = []
        for c in coops:
            d = dict(c)
            if d.get("registry_id") is not None:
                reg_rows.append(d)
        if reg_rows:
            sel_name = st.selectbox(
                "Select cooperative",
                options=[r["name"] for r in reg_rows],
                key="edit_registry_coop_select",
            )
            row = next(r for r in reg_rows if r["name"] == sel_name)
            rid = int(row["registry_id"])
            with st.form("edit_coop_registry_form"):
                edit_official = st.text_input(
                    "Official name",
                    value=str(row["name"]),
                    key=f"edit_reg_official_{rid}",
                )
                edit_cno = st.text_input(
                    "Cooperative No",
                    value=str(row["cooperative_number"] or ""),
                    key=f"edit_reg_cno_{rid}",
                )
                edit_lic = st.text_input(
                    "License No",
                    value=str(row["license_number"] or ""),
                    key=f"edit_reg_lic_{rid}",
                )
                save_reg = st.form_submit_button("Save cooperative details")
                if save_reg:
                    try:
                        init_db(DB_PATH)
                        update_cooperative_registry_by_id(
                            rid,
                            edit_official.strip(),
                            cooperative_number=edit_cno.strip() or None,
                            license_number=edit_lic.strip() or None,
                            db_path=DB_PATH,
                        )
                        st.success("Cooperative details saved.")
                        st.rerun()
                    except sqlite3.IntegrityError as exc:
                        st.error(f"Could not save: {exc}")
                    except ValueError as exc:
                        st.error(str(exc))

    st.divider()
    st.subheader("Move members to another cooperative")

    selected_coop = st.selectbox(
        "Select cooperative",
        options=coop_names,
        key="coop_select_members",
    )
    members = get_people_by_cooperative(selected_coop)
    if not members:
        st.info(f"No members in '{selected_coop}'.")
    else:
        df_members = pd.DataFrame([dict(m) for m in members]).rename(
            columns={
                "contact_person_details": "Name",
                "district_location": "District",
                "nrc_details": "NRC",
            }
        )
        with st.expander(f"View all {len(members)} members", expanded=False):
            st.dataframe(df_members[["id", "Name", "District", "NRC"]], use_container_width=True, hide_index=True)

        st.caption("Select members to move:")
        selected_ids = []
        cols = st.columns(4)
        for i, m in enumerate(members):
            label = f"{m['contact_person_details'][:28]}{'…' if len(m['contact_person_details']) > 28 else ''} ({m['nrc_details']})"
            with cols[i % 4]:
                if st.checkbox(label, key=f"coop_sel_{m['id']}"):
                    selected_ids.append(int(m["id"]))

        target_coops = [c for c in coop_names if c != selected_coop]
        if not target_coops:
            st.warning("Need at least one other cooperative to move members to.")
        elif selected_ids:
            target = st.selectbox("Move selected members to", options=target_coops, key="coop_target")
            if st.button("Move members", type="primary", key="coop_move_btn"):
                n = batch_update_cooperative(selected_ids, target)
                st.success(f"Moved {n} member(s) to '{target}'.")
                st.rerun()
        else:
            st.caption("Select one or more members above, then choose destination cooperative.")

    st.divider()
    st.subheader("Merge or delete cooperative")

    merge_source = st.selectbox(
        "Select cooperative to merge/remove",
        options=coop_names,
        key="coop_merge_source",
    )
    merge_targets = [c for c in coop_names if c != merge_source]
    if merge_targets:
        merge_target = st.selectbox(
            "Merge all members into",
            options=merge_targets,
            key="coop_merge_target",
        )
        member_count = len(get_people_by_cooperative(merge_source))
        st.caption(f"This will move all {member_count} member(s) from '{merge_source}' to '{merge_target}'. '{merge_source}' will then have no members.")
        if st.button("Merge cooperative", type="primary", key="coop_merge_btn"):
            n = merge_cooperative_into(merge_source, merge_target)
            st.success(f"Merged {n} member(s) into '{merge_target}'. '{merge_source}' is now empty.")
            st.rerun()
    else:
        st.info("Only one cooperative exists. Add more to merge.")

    st.divider()
    st.subheader("Delete cooperative (remove all members)")

    if delete_people is None or get_person_ids_by_cooperative is None:
        st.caption("Delete-by-cooperative requires an updated deployment.")
    else:
        delete_coop = st.selectbox(
            "Select cooperative to delete",
            options=coop_names,
            key="coop_delete_select",
        )
        delete_ids = get_person_ids_by_cooperative(delete_coop)
        delete_count = len(delete_ids)
        st.caption(f"'{delete_coop}' has {delete_count} member(s). This will permanently remove them and their attendance records.")
        delete_confirm = st.checkbox("I confirm I want to delete this cooperative and all its members", key="coop_delete_confirm")
        if delete_confirm and st.button("Delete cooperative", type="primary", key="coop_delete_btn"):
            n = delete_people(delete_ids)
            st.success(f"Deleted {n} member(s) from '{delete_coop}'.")
            st.rerun()


AREA_ALLOC_MPIKA = "Mpika"
AREA_ALLOC_MUFUMBWE = "Mufumbwe"
AREA_ALLOC_OTHER_LABEL = "Other (type your own)"


def page_bundled_cooperative():
    st.header("Bundled cooperative")
    st.caption(
        "Each row is one participant. The table is sorted by **cooperative**, then **name**, so members of the "
        "same cooperative appear together. Cooperative placeholders are not listed."
    )
    init_db(DB_PATH)
    rows_raw = get_members_bundled_by_cooperative(db_path=DB_PATH)
    if not rows_raw:
        st.info("No participants with a cooperative on their record yet.")
        return
    df = pd.DataFrame([dict(r) for r in rows_raw]).rename(
        columns={"cooperative": "Cooperative", "member_name": "Name", "nrc": "NRC"}
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


def _area_alloc_choice_from_stored(stored: str) -> Tuple[str, str]:
    """Returns (radio option label, other-text default for text input)."""
    s = (stored or "").strip()
    if not s:
        return AREA_ALLOC_MPIKA, ""
    if s.casefold() == AREA_ALLOC_MPIKA.casefold():
        return AREA_ALLOC_MPIKA, ""
    if s.casefold() == AREA_ALLOC_MUFUMBWE.casefold():
        return AREA_ALLOC_MUFUMBWE, ""
    return AREA_ALLOC_OTHER_LABEL, s


def _preferred_area_distribution_df(alloc_rows: List[Dict]) -> pd.DataFrame:
    """Roll up overview rows by trimmed preferred_mining_area (blank → Not set)."""
    members_by: Dict[str, int] = {}
    coops_by: Dict[str, int] = {}
    for r in alloc_rows:
        raw = r.get("preferred_mining_area")
        p = (str(raw) if raw is not None else "").strip()
        label = p if p else "Not set"
        members_by[label] = members_by.get(label, 0) + int(r.get("member_count") or 0)
        coops_by[label] = coops_by.get(label, 0) + 1
    keys = sorted(members_by.keys(), key=lambda x: (-members_by[x], str(x).casefold()))
    return pd.DataFrame(
        [
            {"Preferred area": k, "Cooperatives": coops_by[k], "Members": members_by[k]}
            for k in keys
        ]
    )


def page_area_allocation():
    st.header("Area Allocation")

    st.caption(
        "Pick a cooperative, choose **Mpika** or **Mufumbwe**, or **Other** — the text box appears only when "
        "**Other** is selected. This does **not** change anyone’s **District / Location** or other person data."
    )

    init_db(DB_PATH)
    rows_raw = get_area_allocation_overview(db_path=DB_PATH)
    alloc_rows: List[Dict] = [dict(r) for r in rows_raw]

    if not alloc_rows:
        st.info(
            "No cooperatives yet. Register people with a cooperative name, or add cooperatives under "
            "**Cooperatives** / **Register Person** — then return here to set preferred areas."
        )
        return

    st.subheader("Set preferred mining area")
    name_keys = sorted({str(r["name"]) for r in alloc_rows}, key=str.casefold)
    sel_name = st.selectbox(
        "Cooperative",
        options=name_keys,
        key="area_alloc_coop_select",
    )
    row = next(r for r in alloc_rows if str(r["name"]) == sel_name)
    rid_val = row.get("registry_id")
    current_pref = str(row.get("preferred_mining_area") or "")
    default_choice, default_other_text = _area_alloc_choice_from_stored(current_pref)
    form_suffix = hashlib.md5(sel_name.encode("utf-8")).hexdigest()[:12]
    opts = [AREA_ALLOC_MPIKA, AREA_ALLOC_MUFUMBWE, AREA_ALLOC_OTHER_LABEL]
    try:
        radio_index = opts.index(default_choice)
    except ValueError:
        radio_index = 0
    radio_key = f"area_alloc_radio_{form_suffix}"
    other_key = f"area_alloc_other_{form_suffix}"

    st.radio(
        "Preferred mining area",
        options=opts,
        index=radio_index,
        key=radio_key,
    )
    choice_now = st.session_state.get(radio_key, opts[radio_index])
    if choice_now == AREA_ALLOC_OTHER_LABEL:
        st.text_input(
            "Specify area",
            value=default_other_text,
            placeholder="Type the preferred mining area",
            key=other_key,
        )

    if st.button("Save", type="primary", key=f"area_alloc_save_{form_suffix}"):
        try:
            sel = st.session_state.get(radio_key, AREA_ALLOC_MPIKA)
            if sel == AREA_ALLOC_MPIKA:
                to_store = AREA_ALLOC_MPIKA
            elif sel == AREA_ALLOC_MUFUMBWE:
                to_store = AREA_ALLOC_MUFUMBWE
            else:
                to_store = (st.session_state.get(other_key, "") or "").strip() or None
            rid = int(rid_val) if rid_val is not None else ensure_cooperative_registry_entry(
                sel_name, db_path=DB_PATH
            )
            set_preferred_mining_area(rid, to_store, db_path=DB_PATH)
            st.success("Saved. Participant districts are unchanged.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save: {exc}")

    st.divider()
    df = pd.DataFrame(alloc_rows).rename(
        columns={
            "name": "Cooperative",
            "cooperative_number": "Cooperative No",
            "license_number": "License No",
            "preferred_mining_area": "Preferred mining area",
            "member_count": "Members",
        }
    )
    show = [
        c
        for c in [
            "Cooperative",
            "Cooperative No",
            "License No",
            "Preferred mining area",
            "Members",
        ]
        if c in df.columns
    ]
    st.subheader("Overview")
    st.dataframe(df[show], use_container_width=True, hide_index=True)

    st.subheader("Preferred area distribution")
    st.caption(
        "Each **cooperative** in the overview counts once under its saved preference (or **Not set**). "
        "**Members** is the total participants on those cooperatives—so you see both how many groups and how "
        "many people sit in each area bucket."
    )
    df_dist = _preferred_area_distribution_df(alloc_rows)
    st.dataframe(df_dist, use_container_width=True, hide_index=True)
    if not df_dist.empty:
        if int(df_dist["Members"].sum()) > 0:
            st.bar_chart(df_dist.set_index("Preferred area")[["Members"]], use_container_width=True)
        elif int(df_dist["Cooperatives"].sum()) > 0:
            st.bar_chart(df_dist.set_index("Preferred area")[["Cooperatives"]], use_container_width=True)


VENUE_LABEL_ROYAL_EAGLE = "Royal eagle"
VENUE_LABEL_GRAND_SOUTHEN = "Grand southen"


def _daily_stats_venue_two_sites(raw_df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """
    Map group_venue into exactly two training sites for Daily Stats.
    Returns (summary dataframe with Venue / Present / Absent / Total / Present %, n_excluded).
    """
    if "group_venue" not in raw_df.columns:
        raw_df = raw_df.assign(group_venue="")
    dfv = raw_df.copy()

    def bucket(v) -> Optional[str]:
        s = str(v or "").strip().lower()
        if "royal" in s and "eagle" in s:
            return VENUE_LABEL_ROYAL_EAGLE
        if "grand" in s:
            return VENUE_LABEL_GRAND_SOUTHEN
        return None

    dfv["_site"] = dfv["group_venue"].map(bucket)
    excluded = int(dfv["_site"].isna().sum())

    def present_absent(sub: pd.DataFrame) -> Tuple[int, int]:
        if sub is None or sub.empty:
            return 0, 0
        stt = sub["status"].astype(int)
        return int((stt == 1).sum()), int((stt == 0).sum())

    rows_out = []
    order = [VENUE_LABEL_ROYAL_EAGLE, VENUE_LABEL_GRAND_SOUTHEN]
    for label in order:
        sub = dfv[dfv["_site"] == label]
        pr, ab = present_absent(sub)
        tot = pr + ab
        pct = round(100.0 * pr / tot, 1) if tot else float("nan")
        rows_out.append(
            {"Venue": label, "Present": pr, "Absent": ab, "Total": tot, "Present %": pct}
        )
    return pd.DataFrame(rows_out), excluded


def page_statistics_by_date():
    st.header("Daily Attendance Statistics")

    init_db(DB_PATH)

    stats_date = st.date_input(
        "Select date", value=TRAINING_START, key="daily_stats_date"
    )
    stats_date_str = iso_date_str(stats_date)

    roster = get_attendance_roster_for_date(attendance_date=stats_date_str, limit=5000)
    total_marked = len(roster)
    present_count = sum(1 for r in roster if int(r["status"]) == 1)
    absent_count = sum(1 for r in roster if int(r["status"]) == 0)

    coops_with_present = set()
    for r in roster:
        if int(r["status"]) != 1:
            continue
        if int(r["is_cooperative_placeholder"] or 0) != 0:
            continue
        cname = str(r["cooperative_name"] or "").strip()
        if cname:
            coops_with_present.add(cname)
    cooperatives_present_count = len(coops_with_present)

    if total_marked > 0:
        sync_programme_register_present_from_attendance(
            stats_date_str,
            db_path=DB_PATH,
            only_insert_missing=True,
        )

    reg_row = get_programme_register_row(stats_date_str, db_path=DB_PATH)
    official_present: Optional[int] = (
        int(reg_row["participant_count"]) if reg_row is not None else None
    )

    st.metric("Date", stats_date_str)
    st.metric("Present (checked in app)", present_count)
    st.metric("Total marked (across all dates)", PROGRAMME_TOTAL_MARKED)
    if official_present is not None:
        st.metric("Official register (present)", official_present)
        st.metric("Absent", PROGRAMME_TOTAL_MARKED - official_present)
    else:
        st.metric("Absent (in app)", absent_count)
    st.metric("Cooperatives present", cooperatives_present_count)

    if total_marked > 0:
        present_rate = present_count / total_marked
        st.metric("Present rate (saved rows)", f"{present_rate*100:.1f}%")
        st.caption(
            "Present rate uses **checked present** ÷ **marked people** — only among attendance saved in the app."
        )

    st.caption(
        "**Cooperatives present** = distinct cooperative names with **at least one present** participant on "
        "this date (excludes cooperative placeholders and rows with no cooperative set)."
    )

    if official_present is not None and total_marked > 0:
        _align_key = hashlib.md5(stats_date_str.encode("utf-8")).hexdigest()[:12]
        with st.expander("Match **checked present** to official register (random)", expanded=False):
            st.caption(
                f"Sets **exactly {official_present}** people to **present** (and the rest **absent**) among "
                "everyone who **already has attendance saved** for this date. People **with a cooperative** "
                "on their record are picked first, at random; any remaining slots use other saved rows."
            )
            st.warning("This **overwrites** every present/absent checkbox for that date’s saved rows.")
            _seed_raw = st.text_input(
                "Optional random seed (integer; leave empty for a different random mix each run)",
                value="",
                key=f"align_seed_{_align_key}",
            )
            _align_confirm = st.checkbox(
                "I understand — overwrite present/absent for this date",
                key=f"align_confirm_{_align_key}",
            )
            if _align_confirm and st.button(
                "Apply — random assignment to match register",
                type="primary",
                key=f"align_btn_{_align_key}",
            ):
                try:
                    init_db(DB_PATH)
                    _seed: Optional[int] = None
                    if (_seed_raw or "").strip():
                        _seed = int((_seed_raw or "").strip())
                    out = redistribute_attendance_to_match_register(
                        stats_date_str,
                        db_path=DB_PATH,
                        random_seed=_seed,
                    )
                    if out.get("skipped"):
                        rs = str(out.get("reason", ""))
                        if rs == "no_attendance_rows":
                            st.error("No attendance saved for this date yet — use **Mark Attendance** first.")
                        elif rs == "no_register_row":
                            st.error("No official register entry for this date.")
                        else:
                            st.error(rs or "Could not apply.")
                    elif out.get("ok"):
                        msg = (
                            f"**{out['present_after']}** present of **{out['marked']}** saved rows "
                            f"(register target **{out['target']}**; {out['cooperative_candidates']} "
                            "people with a cooperative were eligible for priority sampling)."
                        )
                        if out.get("note"):
                            msg += f" {out['note']}"
                        st.success(msg)
                        st.rerun()
                except ValueError:
                    st.error("Random seed must be empty or a whole number.")

    pr_all = list_programme_register(db_path=DB_PATH)
    if pr_all:
        with st.expander("Official register — present by day", expanded=False):
            df_reg = pd.DataFrame([dict(r) for r in pr_all]).rename(
                columns={
                    "attendance_date": "Date",
                    "day_label": "Day",
                    "participant_count": "Present",
                }
            )
            st.dataframe(df_reg, use_container_width=True, hide_index=True)

    if roster:
        raw_df = pd.DataFrame([dict(r) for r in roster])

        def _daily_breakdown(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
            if col not in df.columns:
                df = df.assign(**{col: ""})
            s = df[col].fillna("").astype(str).str.strip()
            s = s.replace("", "(not set)")
            tmp = df.assign(_key=s)
            g = tmp.groupby("_key", dropna=False)["status"].agg(
                Present=lambda x: int((x.astype(int) == 1).sum()),
                Absent=lambda x: int((x.astype(int) == 0).sum()),
            )
            out = g.reset_index().rename(columns={"_key": label})
            out["Total"] = out["Present"] + out["Absent"]
            out["Present %"] = (100.0 * out["Present"] / out["Total"].replace(0, float("nan"))).round(1)
            return out.sort_values("Total", ascending=False)

        df_roster = raw_df.rename(
            columns={
                "contact_person_details": "Name",
                "contact_number": "Contact Number",
                "cooperative_name": "Cooperative",
                "group_venue": "Group/Venue",
                "district_location": "District/Location",
                "sex": "Sex",
                "nrc_details": "NRC",
                "status": "Present",
                "marked_at": "Marked At",
            }
        )
        df_roster["Present"] = df_roster["Present"].apply(lambda v: bool(int(v) if v is not None else 0))
        if "is_cooperative_placeholder" in df_roster.columns:
            df_roster = df_roster.drop(columns=["is_cooperative_placeholder"])

        st.subheader("Breakdown by venue")
        st.caption(
            f"Only **{VENUE_LABEL_ROYAL_EAGLE}** and **{VENUE_LABEL_GRAND_SOUTHEN}** "
            "(from Group/Venue: Royal Eagles / Grand Hotel)."
        )
        by_venue, venue_excluded = _daily_stats_venue_two_sites(raw_df)
        st.bar_chart(by_venue.set_index("Venue")[["Present", "Absent"]])
        st.dataframe(by_venue, use_container_width=True, hide_index=True)
        if venue_excluded:
            st.caption(
                f"{venue_excluded} attendance row(s) have another or empty Group/Venue and are not included above."
            )

        st.subheader("Breakdown by gender")
        raw_df = raw_df.copy()
        raw_df["_sex_norm"] = raw_df["sex"].map(normalize_sex_for_stats)
        by_sex = _daily_breakdown(raw_df, "_sex_norm", "Gender")
        st.bar_chart(by_sex.set_index("Gender")[["Present", "Absent"]])
        st.dataframe(by_sex, use_container_width=True, hide_index=True)

        st.subheader("Daily roster")
        st.dataframe(df_roster, use_container_width=True, hide_index=True)
    else:
        st.info("No attendance rows for this date yet.")


def page_statistics_overview():
    st.header("Attendance Dashboard")

    c1, c2 = st.columns(2)
    with c1:
        d1 = st.date_input(
            "From date",
            value=TRAINING_START,
            key="stats_from_date",
        )
    with c2:
        d2 = st.date_input(
            "To date",
            value=TRAINING_END,
            key="stats_to_date",
        )

    if d2 < d1:
        st.error("To date must be on/after From date.")
        return

    start_date_str = iso_date_str(d1)
    end_date_str = iso_date_str(d2)

    total_people = count_people(exclude_cooperative_placeholders=True)
    total_cooperatives = count_cooperatives()
    gender_counts = count_gender_distribution()
    district_counts = count_district_distribution(limit=20)
    province_counts = count_province_distribution()
    summary = get_attendance_summary_for_range(start_date_str, end_date_str)

    present_rate = summary["present_rows"] / summary["total_rows"] if summary["total_rows"] else 0.0

    k1, k2, k3, k4, k5 = st.columns([1, 1, 1, 1, 1])
    with k1:
        st.metric("Registered People", total_people)
    with k2:
        st.metric("Cooperatives Captured", total_cooperatives)
    with k3:
        st.metric("Attendance Rows (range)", summary["total_rows"])
    with k4:
        st.metric("Present Rows (range)", summary["present_rows"])
    with k5:
        st.metric("Present Rate (range)", f"{present_rate*100:.1f}%")

    st.subheader("Gender Numbers (captured people)")
    st.caption("Male / Female / Other only. Cooperative placeholders and invalid sex values are grouped as Other.")
    df_gender = pd.DataFrame(
        [{"Sex": k, "Count": v} for k, v in sorted(gender_counts.items(), key=lambda x: x[0])]
    )
    if not df_gender.empty:
        st.bar_chart(df_gender.set_index("Sex")["Count"])
        st.dataframe(df_gender, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("District Numbers (captured people)")
    if district_counts:
        df_dist = pd.DataFrame([dict(r) for r in district_counts])
        df_dist = df_dist.rename(columns={"district_location": "District/Location", "c": "Count"})
        # sqlite returns "c" as column name alias; handle both cases robustly
        if "Count" not in df_dist.columns and "c" in df_dist.columns:
            df_dist = df_dist.rename(columns={"c": "Count"})
        if "Count" in df_dist.columns:
            st.bar_chart(df_dist.set_index("District/Location")["Count"])
        st.dataframe(df_dist, use_container_width=True, hide_index=True)
    else:
        st.info("No district data found.")

    st.divider()
    st.subheader("Province Numbers (best-effort)")
    if province_counts:
        df_prov = pd.DataFrame([{"Province": k, "Count": v} for k, v in sorted(province_counts.items())])
        st.bar_chart(df_prov.set_index("Province")["Count"])
        st.dataframe(df_prov, use_container_width=True, hide_index=True)
    else:
        st.info(
            "Province names were not found in `DISTRICT / LOCATION` text. "
            "If you want exact province breakdown, we need a district->province mapping."
        )

    st.divider()
    if summary["total_rows"] == 0:
        st.info("No attendance data saved in this date range yet.")
        return

    st.subheader("Present / Absent by Date")
    st.caption(
        "For **printed programme days** (Day 1–13, your notebook figures), **Present** is that total and "
        f"**Absent** is **{PROGRAMME_TOTAL_MARKED} − Present**. Other calendar days use saved attendance only."
    )
    by_date = get_chart_present_absent_by_date_range(
        start_date_str,
        end_date_str,
        PROGRAMME_TOTAL_MARKED,
        db_path=DB_PATH,
    )
    df_date = pd.DataFrame(by_date)
    if not df_date.empty:
        df_date = df_date.rename(
            columns={"attendance_date": "Date", "present_count": "Present", "absent_count": "Absent"}
        )
        st.line_chart(df_date.set_index("Date")[["Present", "Absent"]])

    st.divider()
    st.subheader("Present / Absent by Sex (range)")
    by_sex = get_present_absent_counts_by_sex_range(start_date_str, end_date_str)
    df_sex = pd.DataFrame([dict(r) for r in by_sex])
    if not df_sex.empty:
        df_sex = df_sex.rename(
            columns={"sex": "Sex", "present_count": "Present", "absent_count": "Absent"}
        )
        st.bar_chart(df_sex.set_index("Sex")[["Present", "Absent"]])

    st.divider()
    st.subheader("Top Cooperatives by Present (range)")
    top = get_top_cooperatives_by_present(start_date_str, end_date_str, limit=10)
    if top:
        df_top = pd.DataFrame([dict(r) for r in top])
        df_top = df_top.rename(columns={"cooperative_name": "Cooperative", "present_count": "Present"})
        st.dataframe(df_top, use_container_width=True, hide_index=True)


def main():
    _require_login()

    st.sidebar.markdown("### Actions")
    if st.sidebar.button("Log out"):
        _logout()

    st.sidebar.caption(f"Database: `{DB_PATH}`")
    st.sidebar.caption(f"User: `{st.session_state.get('username')}`")

    tabs = st.tabs(
        [
            "Mark Attendance",
            "Register Person",
            "Search People",
            "Cooperatives",
            "Bundled cooperative",
            "Area Allocation",
            "Records",
            "Daily Stats",
            "Dashboard",
        ]
    )

    with tabs[0]:
        page_mark_attendance()
    with tabs[1]:
        page_register_person()
    with tabs[2]:
        page_search_people()
    with tabs[3]:
        page_cooperatives()
    with tabs[4]:
        page_bundled_cooperative()
    with tabs[5]:
        page_area_allocation()
    with tabs[6]:
        page_records()
    with tabs[7]:
        page_statistics_by_date()
    with tabs[8]:
        page_statistics_overview()


if __name__ == "__main__":
    main()

