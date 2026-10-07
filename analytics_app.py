import streamlit as st
import pandas as pd
import gspread

# ---------------------------------------------------------------
# Page config
# ---------------------------------------------------------------
st.set_page_config(
    page_title="Network & Campaign Intelligence Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 Network & Campaign Intelligence Dashboard")
st.markdown("Advanced Publisher & Campaign Intelligence Engine")

# ---------------------------------------------------------------
# Matching patterns (AI QC Report format:
#   "Call Type: QUALIFIED | ... | Spam/Robot: NO | ...")
# ---------------------------------------------------------------
QUAL_PAT = r"CALL TYPE:\s*QUAL"
SPAM_PAT = r"CALL TYPE:\s*SPAM|SPAM/ROBOT:\s*YES"
VOIP_PAT = r"VOIP"


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def count_match(series, pattern):
    return int(series.astype(str).str.contains(pattern, case=False, na=False, regex=True).sum())


def non_empty(series):
    return int((series.astype(str).str.strip() != "").sum())


def find_col(columns, exact_names, keywords):
    lowered = {c.lower().strip(): c for c in columns}
    for name in exact_names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    for kw in keywords:
        for c in columns:
            if kw in c.lower():
                return c
    return None


def parse_duration(val):
    try:
        val_str = str(val).strip()
        if ":" in val_str:
            parts = list(map(float, val_str.split(":")))
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
        return float(val_str)
    except Exception:
        return float("nan")


def parse_dates(series):
    """Parse a column with MIXED date formats, row by row.
    Handles '10/05/2026 10:23:18', '9/29/2026 22:38:23' and 'Oct 05 7:32:57 PM' (no year)."""
    s = series.astype(str).str.strip()
    out = pd.to_datetime(s, errors="coerce", format="mixed")
    # Rows without a 4-digit year (e.g. 'Oct 05 7:32:57 PM'): assume current year
    # (and roll back a year if that would put the date in the future).
    no_year = out.isna() & s.str.match(r"^[A-Za-z]{3}\s+\d{1,2}\s")
    if no_year.any():
        now = pd.Timestamp.now()
        fixed = pd.to_datetime(
            s[no_year] + " " + str(now.year), errors="coerce", format="%b %d %I:%M:%S %p %Y"
        )
        fixed = fixed.where(fixed <= now + pd.Timedelta(days=1), fixed - pd.DateOffset(years=1))
        out = out.copy()
        out[no_year] = fixed
    return out


def get_client():
    try:
        return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
    except Exception:
        return gspread.service_account(filename="service_account.json")


def open_spreadsheet(gc, name, ref):
    """Open by URL / ID if given, otherwise by name. Returns (spreadsheet, all_matches)."""
    ref = ref.strip()
    if ref:
        if ref.startswith("http"):
            return gc.open_by_url(ref), []
        return gc.open_by_key(ref), []
    matches = gc.openall(name)
    if not matches:
        raise ValueError(f"No spreadsheet named '{name}' is shared with the service account.")
    return matches[0], matches


def get_worksheet(ss, tab):
    sheets = ss.worksheets()
    for ws in sheets:
        if ws.title.strip().lower() == tab.strip().lower():
            return ws
    raise ValueError(
        f"Tab '{tab}' not found. Available tabs: " + ", ".join(f"'{w.title}'" for w in sheets)
    )


# ---------------------------------------------------------------
# Sidebar: connection
# ---------------------------------------------------------------
st.sidebar.header("⚙️ Configuration & Filters")

target_sheet_name = st.sidebar.text_input("Google Sheet Name:", "Ringba to Sheet QC")
target_tab_name = st.sidebar.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")
sheet_ref = st.sidebar.text_input(
    "Sheet URL or ID (optional, overrides the name):",
    "",
    help="Paste the full Google Sheet link if several sheets share the same name.",
)

if st.sidebar.button("🔄 Connect & Load Fresh Data") or "sheet_loaded" not in st.session_state:
    try:
        with st.spinner("Connecting to Google Sheets & fetching fresh data..."):
            gc = get_client()
            spreadsheet, matches = open_spreadsheet(gc, target_sheet_name, sheet_ref)
            worksheet = get_worksheet(spreadsheet, target_tab_name)
            rows = worksheet.get_all_values()

        if not rows or len(rows) < 2:
            st.sidebar.warning("The sheet is empty or contains no data rows.")
            st.session_state["sheet_loaded"] = False
        else:
            headers = [str(h).strip() for h in rows[0]]
            cleaned_headers = [h if h != "" else f"Unnamed_{i}" for i, h in enumerate(headers)]

            df = pd.DataFrame(rows[1:], columns=cleaned_headers)
            df = df[(df.astype(str).apply(lambda c: c.str.strip()) != "").any(axis=1)]
            # drop template/placeholder rows such as "[Call:CreatedAt]" / "[tag:Buyer:Name]"
            is_placeholder = df.astype(str).apply(lambda c: c.str.strip().str.match(r"^\[[^\]]+\]$")).any(axis=1)
            df = df[~is_placeholder].reset_index(drop=True)

            st.session_state["df"] = df
            st.session_state["meta"] = {
                "title": spreadsheet.title,
                "tab": worksheet.title,
                "url": spreadsheet.url,
                "duplicates": [m.url for m in matches] if len(matches) > 1 else [],
            }
            st.session_state["sheet_loaded"] = True
            st.sidebar.success(f"Loaded {len(df):,} records successfully!")

    except Exception as e:
        st.sidebar.error(f"Failed: {str(e)}")
        st.session_state["sheet_loaded"] = False


# ---------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------
if st.session_state.get("sheet_loaded", False) and "df" in st.session_state:
    df = st.session_state["df"]
    meta = st.session_state.get("meta", {})
    available_columns = [c for c in df.columns if not c.startswith("Unnamed_")]

    if not available_columns:
        st.warning("No valid column headers found in the Sheet.")
        st.stop()

    # Which sheet was actually opened
    if meta:
        st.sidebar.caption(f"Opened: **{meta['title']}** → tab **{meta['tab']}**")
        st.sidebar.markdown(f"[Open this sheet]({meta['url']})")
        if meta["duplicates"]:
            st.sidebar.warning(
                f"{len(meta['duplicates'])} sheets share this name. Using the first one. "
                "Paste the exact link in the URL box above to be sure."
            )
            for u in meta["duplicates"]:
                st.sidebar.write(u)

    work_df = df.copy()

    # --- Numeric pre-processing ---
    score_col_name = find_col(available_columns, ["Quality Score"], ["score"])
    if score_col_name:
        work_df["Quality_Score_Num"] = pd.to_numeric(
            work_df[score_col_name].astype(str).str.extract(r"(-?\d+\.?\d*)")[0],
            errors="coerce",
        )
    else:
        work_df["Quality_Score_Num"] = float("nan")

    dur_col_name = find_col(available_columns, ["Duration"], ["duration"])
    if dur_col_name:
        work_df["Duration_Num"] = work_df[dur_col_name].apply(parse_duration)
    else:
        work_df["Duration_Num"] = float("nan")

    # --- Column mapping ---
    st.sidebar.markdown("---")
    st.sidebar.subheader("📌 Column Mapping Settings")

    options = ["None"] + available_columns
    default_voip = find_col(available_columns, ["Line Type"], ["line type", "voip"]) or "None"
    default_qc = find_col(available_columns, ["AI QC Report"], ["ai qc", "qc report", "qc"]) or "None"

    selected_voip_col = st.sidebar.selectbox(
        "Select VoIP / Line Type Column:", options, index=options.index(default_voip), key="map_voip"
    )
    selected_qc_col = st.sidebar.selectbox(
        "Select AI QC / Status Column:", options, index=options.index(default_qc), key="map_qc"
    )

    # --- Data health check: warn loudly if key columns are empty ---
    empty_cols = []
    if selected_qc_col != "None" and non_empty(df[selected_qc_col]) == 0:
        empty_cols.append(selected_qc_col)
    if selected_voip_col != "None" and non_empty(df[selected_voip_col]) == 0:
        empty_cols.append(selected_voip_col)
    if score_col_name and non_empty(df[score_col_name]) == 0:
        empty_cols.append(score_col_name)
    if empty_cols:
        st.error(
            "These columns are completely empty in the data this app loaded: "
            + ", ".join(f"**{c}**" for c in empty_cols)
            + ". That is why their numbers show 0. Check the sheet/tab shown in the sidebar "
            "(use the 'Open this sheet' link) and make sure it is the one with the QC values."
        )

    # --- Global search ---
    st.sidebar.markdown("---")
    st.sidebar.subheader("🔍 Global Search")
    search_query = st.sidebar.text_input("Search Phone, Caller ID, Note, etc.:", "").strip()

    if search_query:
        str_df = work_df.astype(str)
        mask = str_df.apply(
            lambda col: col.str.contains(search_query, case=False, na=False, regex=False)
        ).any(axis=1)
        work_df = work_df[mask].copy()

    # --- Date range filter ---
    st.sidebar.markdown("---")
    st.sidebar.subheader("📅 Date Range Filter")
    date_cols = [
        c for c in available_columns
        if "date" in c.lower() or "time" in c.lower() or "day" in c.lower()
    ]

    if date_cols:
        date_col_name = st.sidebar.selectbox("Select Date Column:", date_cols)
        work_df["Parsed_Date"] = parse_dates(work_df[date_col_name])
        valid_dates = work_df["Parsed_Date"].dropna()
        unreadable = int(work_df["Parsed_Date"].isna().sum())

        if not valid_dates.empty:
            min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
            date_range = st.sidebar.date_input("Select Date Range:", (min_d, max_d))
            if unreadable:
                st.sidebar.caption(f"{unreadable} rows have no readable date; they are always kept.")

            if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
                start_d, end_d = date_range
                in_range = (
                    (work_df["Parsed_Date"].dt.date >= start_d)
                    & (work_df["Parsed_Date"].dt.date <= end_d)
                )
                work_df = work_df[in_range | work_df["Parsed_Date"].isna()]
        else:
            st.sidebar.info("Date values could not be parsed.")
    else:
        st.sidebar.info("No date/time column detected.")

    # --- Debug panel (collapsed) ---
    with st.sidebar.expander("🧪 Debug: loaded data"):
        st.write("Non-empty cells per column:")
        st.write({c: non_empty(df[c]) for c in available_columns})
        st.write("Last 5 rows:")
        show_cols = [c for c in ["Call Date", "Duration", "AI QC Report", "Quality Score", "Line Type"] if c in df.columns]
        st.dataframe(df[show_cols].tail(5))

    # ---------------------------------------------------------------
    # KPI metrics (counts only, no percentages)
    # ---------------------------------------------------------------
    st.markdown("### 📈 Network Overview & Key Metrics")
    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)

    kpi1.metric("Total Filtered Calls", f"{len(work_df):,}")

    if selected_qc_col != "None":
        kpi2.metric("Qualified Calls", f"{count_match(work_df[selected_qc_col], QUAL_PAT):,}")
        kpi3.metric("Spam / Fake Calls", f"{count_match(work_df[selected_qc_col], SPAM_PAT):,}")
    else:
        kpi2.metric("Qualified Calls", "0")
        kpi3.metric("Spam / Fake Calls", "0")

    if selected_voip_col != "None":
        kpi4.metric("VoIP Calls", f"{count_match(work_df[selected_voip_col], VOIP_PAT):,}")
    else:
        kpi4.metric("VoIP Calls", "0")

    avg_scr = work_df["Quality_Score_Num"].mean()
    kpi5.metric("Avg Quality Score", f"{avg_scr:.1f}" if not pd.isna(avg_scr) else "0.0")

    if selected_qc_col != "None":
        pending = int((work_df[selected_qc_col].astype(str).str.strip() == "").sum())
        if pending:
            st.caption(
                f"ℹ️ {pending:,} of {len(work_df):,} filtered calls have no AI QC result yet "
                "(blank in the sheet). They count in Total Calls but not as Qualified / Spam / VoIP, "
                "and are left out of Avg Quality Score."
            )

    st.markdown("---")

    # ---------------------------------------------------------------
    # Dimension grouping
    # ---------------------------------------------------------------
    default_dim = available_columns.index("Publisher") if "Publisher" in available_columns else 0
    selected_dimension = st.selectbox(
        "Group / Analyze By (All Sheet Headings):", available_columns, index=default_dim
    )

    if selected_dimension:
        temp_df = work_df.copy()
        temp_df[selected_dimension] = (
            temp_df[selected_dimension].fillna("Unknown").astype(str).str.strip()
        )
        temp_df.loc[temp_df[selected_dimension] == "", selected_dimension] = "Unknown"

        temp_df["_row"] = 1
        temp_df["_is_qual"] = (
            temp_df[selected_qc_col].astype(str).str.contains(QUAL_PAT, case=False, na=False, regex=True)
            if selected_qc_col != "None" else False
        )
        temp_df["_is_spam"] = (
            temp_df[selected_qc_col].astype(str).str.contains(SPAM_PAT, case=False, na=False, regex=True)
            if selected_qc_col != "None" else False
        )
        temp_df["_is_voip"] = (
            temp_df[selected_voip_col].astype(str).str.contains(VOIP_PAT, case=False, na=False, regex=True)
            if selected_voip_col != "None" else False
        )

        summary = (
            temp_df.groupby(selected_dimension)
            .agg(
                Total_Calls=("_row", "sum"),
                Avg_Score=("Quality_Score_Num", "mean"),
                Avg_Duration=("Duration_Num", "mean"),
                Qualified_Calls=("_is_qual", "sum"),
                Spam_Calls=("_is_spam", "sum"),
                VoIP_Calls=("_is_voip", "sum"),
            )
            .reset_index()
        )

        summary["Avg_Score"] = summary["Avg_Score"].round(1)
        summary["Avg_Duration"] = summary["Avg_Duration"].round(1)
        summary = summary.sort_values(by="Total_Calls", ascending=False).reset_index(drop=True)

        st.subheader(f"Performance Breakdown by {selected_dimension}")

        if len(summary) > 0:
            st.bar_chart(summary.set_index(selected_dimension)["Total_Calls"].head(15))

        st.markdown("💡 *Select multiple rows in the table below to view and export their combined raw details:*")

        event = st.dataframe(
            summary,
            width="stretch",
            on_select="rerun",
            selection_mode="multi-row",
        )

        selected_rows = event.selection.rows if hasattr(event, "selection") else []

        if selected_rows:
            selected_vals = summary.iloc[selected_rows][selected_dimension].tolist()

            st.markdown("---")
            st.subheader(
                f"🔍 Full Details for selected `{selected_dimension}`: "
                f"{', '.join(map(str, selected_vals))}"
            )

            filtered_rows = work_df.loc[temp_df[selected_dimension].isin(selected_vals)]
            st.info(f"Total matching records found: {len(filtered_rows):,}")
            st.dataframe(filtered_rows, width="stretch")

            st.download_button(
                label="📥 Download Filtered Rows as CSV",
                data=filtered_rows.to_csv(index=False).encode("utf-8"),
                file_name="filtered_network_records.csv",
                mime="text/csv",
            )

        st.markdown("---")
        st.subheader("⚖ Side-by-Side Comparison Mode")
        compare_vals = st.multiselect(
            f"Select multiple items from '{selected_dimension}' to compare directly:",
            summary[selected_dimension].tolist(),
        )

        if compare_vals:
            st.dataframe(
                summary[summary[selected_dimension].isin(compare_vals)],
                width="stretch",
            )