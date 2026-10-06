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
# Matching patterns (based on the AI QC Report text format)
#   e.g. "Call Type: QUALIFIED | ... | Spam/Robot: NO | ..."
# ---------------------------------------------------------------
QUAL_PAT = r"CALL TYPE:\s*QUAL"
SPAM_PAT = r"CALL TYPE:\s*SPAM|SPAM/ROBOT:\s*YES"
VOIP_PAT = r"VOIP"


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def count_match(series, pattern):
    """Count rows in a Series that match a regex pattern (case-insensitive)."""
    return int(series.astype(str).str.contains(pattern, case=False, na=False, regex=True).sum())


def find_col(columns, exact_names, keywords):
    """Find a column by exact name first, then by keyword. Returns None if not found."""
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
    """Convert 'H:MM:SS', 'MM:SS' or plain seconds into seconds."""
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
        return 0.0


# ---------------------------------------------------------------
# Sidebar: connection
# ---------------------------------------------------------------
st.sidebar.header("⚙️ Configuration & Filters")

target_sheet_name = st.sidebar.text_input("Google Sheet Name:", "Ringba to Sheet QC")
target_tab_name = st.sidebar.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")

if st.sidebar.button("🔄 Connect & Load Fresh Data") or "sheet_loaded" not in st.session_state:
    try:
        with st.spinner("Connecting to Google Sheets & fetching fresh data..."):
            try:
                gc = gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
            except Exception:
                gc = gspread.service_account(filename="service_account.json")

            sheet = gc.open(target_sheet_name).worksheet(target_tab_name)
            rows = sheet.get_all_values()

        if not rows or len(rows) < 2:
            st.sidebar.warning("The sheet is empty or contains no data rows.")
            st.session_state["sheet_loaded"] = False
        else:
            headers = [str(h).strip() for h in rows[0]]
            cleaned_headers = [h if h != "" else f"Unnamed_{i}" for i, h in enumerate(headers)]

            df = pd.DataFrame(rows[1:], columns=cleaned_headers)
            # Drop completely empty rows
            df = df[(df.astype(str).apply(lambda c: c.str.strip()) != "").any(axis=1)].reset_index(drop=True)

            st.session_state["df"] = df
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
    available_columns = [c for c in df.columns if not c.startswith("Unnamed_")]

    if not available_columns:
        st.warning("No valid column headers found in the Sheet.")
        st.stop()

    work_df = df.copy()

    # --- Numeric pre-processing ---
    score_col_name = find_col(available_columns, ["Quality Score"], ["score"])
    if score_col_name:
        work_df["Quality_Score_Num"] = pd.to_numeric(
            work_df[score_col_name].astype(str).str.extract(r"(-?\d+\.?\d*)")[0],
            errors="coerce",
        ).fillna(0)
    else:
        work_df["Quality_Score_Num"] = 0.0

    dur_col_name = find_col(available_columns, ["Duration"], ["duration"])
    if dur_col_name:
        work_df["Duration_Num"] = work_df[dur_col_name].apply(parse_duration)
    else:
        work_df["Duration_Num"] = 0.0

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
        work_df["Parsed_Date"] = pd.to_datetime(work_df[date_col_name], errors="coerce")
        valid_dates = work_df["Parsed_Date"].dropna()

        if not valid_dates.empty:
            min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
            date_range = st.sidebar.date_input("Select Date Range:", (min_d, max_d))

            if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
                start_d, end_d = date_range
                work_df = work_df[
                    (work_df["Parsed_Date"].dt.date >= start_d)
                    & (work_df["Parsed_Date"].dt.date <= end_d)
                ]
        else:
            st.sidebar.info("Date values could not be parsed.")
    else:
        st.sidebar.info("No date/time column detected.")

    # --- Debug panel (collapsed by default) ---
    with st.sidebar.expander("🧪 Debug mapped columns"):
        st.write("Score column:", score_col_name)
        st.write("Duration column:", dur_col_name)
        for label, c in [("VoIP", selected_voip_col), ("QC", selected_qc_col)]:
            st.write(f"**{label} → {c}**")
            if c != "None":
                non_empty = int((df[c].astype(str).str.strip() != "").sum())
                st.write(f"non-empty: {non_empty} of {len(df)}")
                st.write(df[c].astype(str).str[:50].value_counts().head(3))

    # ---------------------------------------------------------------
    # KPI metrics (counts only, no percentages)
    # ---------------------------------------------------------------
    st.markdown("### 📈 Network Overview & Key Metrics")
    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)

    total_calls_count = len(work_df)
    kpi1.metric("Total Filtered Calls", f"{total_calls_count:,}")

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

        # Helper flag columns (avoids lambdas in the aggregation)
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
                Total_Calls=("Quality_Score_Num", "count"),
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
            chart_data = summary.set_index(selected_dimension)["Total_Calls"].head(15)
            st.bar_chart(chart_data)

        st.markdown("💡 *Select multiple rows in the table below to view and export their combined raw details:*")

        # --- Multi-row selection table ---
        event = st.dataframe(
            summary,
            use_container_width=True,
            on_select="rerun",
            selection_mode="multi-row",
        )

        # --- Drill-down & CSV export ---
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

            st.dataframe(filtered_rows, use_container_width=True)

            csv_data = filtered_rows.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 Download Filtered Rows as CSV",
                data=csv_data,
                file_name="filtered_network_records.csv",
                mime="text/csv",
            )

        # --- Side-by-side comparison ---
        st.markdown("---")
        st.subheader("⚖ Side-by-Side Comparison Mode")
        compare_vals = st.multiselect(
            f"Select multiple items from '{selected_dimension}' to compare directly:",
            summary[selected_dimension].tolist(),
        )

        if compare_vals:
            comparison_df = summary[summary[selected_dimension].isin(compare_vals)]
            st.dataframe(comparison_df, use_container_width=True)
