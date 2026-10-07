import streamlit as st
import pandas as pd
import gspread
from datetime import date, timedelta

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
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")

    # 1) Rows WITHOUT a year, e.g. 'Oct 05 7:32:57 PM' (Google Sheets display format).
    #    Parsed explicitly: pandas would otherwise silently use year 0001 or 1900.
    #    Assume the current year, and roll back a year if that lands in the future.
    no_year = s.str.match(r"^[A-Za-z]{3}\s+\d{1,2}\s")
    if no_year.any():
        now = pd.Timestamp.now()
        fixed = pd.to_datetime(
            s[no_year] + " " + str(now.year), errors="coerce", format="%b %d %I:%M:%S %p %Y"
        )
        fixed = fixed.where(fixed <= now + pd.Timedelta(days=1), fixed - pd.DateOffset(years=1))
        out[no_year] = fixed.astype("datetime64[ns]")

    # 2) Everything else ('10/05/2026 10:23:18', '9/29/2026 22:38:23', ISO dates...), row by row
    rest = ~no_year
    if rest.any():
        out[rest] = pd.to_datetime(s[rest], errors="coerce", format="mixed").astype("datetime64[ns]")
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
# Period comparison (any date range vs any date range)
# ---------------------------------------------------------------
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PRESETS = [
    "Custom dates",
    "Same weekday: this week vs last week",
    "This week so far vs last week (same days)",
    "This week vs last week (full weeks)",
    "Last 7 days vs previous 7 days",
    "Latest day vs the day before",
]
COUNT_METRICS = ["Calls", "Qualified", "Spam", "VoIP"]
METRICS = COUNT_METRICS + ["Avg Score", "Avg Duration (sec)"]


def preset_ranges(preset, anchor, weekday_idx):
    """Return ((a_start, a_end), (b_start, b_end)). A = earlier period, B = later period.
    `anchor` is the latest call date in the data. Weeks start on Monday."""
    day = timedelta(days=1)
    this_mon = anchor - timedelta(days=anchor.weekday())
    last_mon = this_mon - 7 * day
    if preset == PRESETS[1]:  # same weekday, latest occurrence vs the week before
        b = anchor - timedelta(days=(anchor.weekday() - weekday_idx) % 7)
        a = b - 7 * day
        return (a, a), (b, b)
    if preset == PRESETS[2]:  # this week so far vs the same number of days last week
        span = (anchor - this_mon).days
        return (last_mon, last_mon + span * day), (this_mon, anchor)
    if preset == PRESETS[3]:  # full Mon-Sun weeks
        return (last_mon, last_mon + 6 * day), (this_mon, this_mon + 6 * day)
    if preset == PRESETS[4]:
        return (anchor - 13 * day, anchor - 7 * day), (anchor - 6 * day, anchor)
    if preset == PRESETS[5]:
        return (anchor - day, anchor - day), (anchor, anchor)
    # Custom: start from "same weekday last week vs latest day"
    return (anchor - 7 * day, anchor - 7 * day), (anchor, anchor)


def as_range(value):
    """date_input returns a date, or a tuple with 1 or 2 dates while the user is still picking."""
    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            return value[0], value[1]
        if len(value) == 1:
            return value[0], value[0]
        return None
    return value, value


def slice_period(frame, start_d, end_d):
    d = frame["Parsed_Date"]
    return frame[(d >= pd.Timestamp(start_d)) & (d < pd.Timestamp(end_d) + pd.Timedelta(days=1))]


def describe_period(start_d, end_d):
    if start_d == end_d:
        return f"{start_d:%a %b %d, %Y}"
    return f"{start_d:%a %b %d, %Y} to {end_d:%a %b %d, %Y}"


def period_stats(frame, cols, qc_col, voip_col):
    f = frame.copy()
    f["_row"] = 1
    f["_is_qual"] = (
        f[qc_col].astype(str).str.contains(QUAL_PAT, case=False, na=False, regex=True)
        if qc_col != "None" else False
    )
    f["_is_spam"] = (
        f[qc_col].astype(str).str.contains(SPAM_PAT, case=False, na=False, regex=True)
        if qc_col != "None" else False
    )
    f["_is_voip"] = (
        f[voip_col].astype(str).str.contains(VOIP_PAT, case=False, na=False, regex=True)
        if voip_col != "None" else False
    )
    return f.groupby(cols).agg(
        **{
            "Calls": ("_row", "sum"),
            "Qualified": ("_is_qual", "sum"),
            "Spam": ("_is_spam", "sum"),
            "VoIP": ("_is_voip", "sum"),
            "Avg Score": ("Quality_Score_Num", "mean"),
            "Avg Duration (sec)": ("Duration_Num", "mean"),
        }
    )


def period_kpis(frame, qc_col, voip_col):
    return {
        "Calls": len(frame),
        "Qualified": count_match(frame[qc_col], QUAL_PAT) if qc_col != "None" else 0,
        "Spam": count_match(frame[qc_col], SPAM_PAT) if qc_col != "None" else 0,
        "VoIP": count_match(frame[voip_col], VOIP_PAT) if voip_col != "None" else 0,
        "Avg Score": frame["Quality_Score_Num"].mean(),
    }


def render_period_comparison(base_df, available_columns, qc_col, voip_col):
    st.markdown("---")
    st.subheader("⚖ Period Comparison Mode")
    st.caption(
        "Compare any two date ranges (for example last Tuesday vs this Tuesday, or last week vs "
        "this week) by Publisher, Buyer or any other column. A = earlier period, B = later period. "
        "This section ignores the sidebar Date Range filter, but still uses Global Search and "
        "the column mapping."
    )

    if base_df is None or base_df["Parsed_Date"].notna().sum() == 0:
        st.info("Period comparison needs a readable date column.")
        return

    anchor = base_df["Parsed_Date"].max().date()
    default_group = ["Publisher"] if "Publisher" in available_columns else available_columns[:1]

    # --- Row 1: preset, weekday, compare-by columns ---
    r1 = st.columns(3)
    preset = r1[0].selectbox("Quick compare:", PRESETS, key="cmp_preset")
    weekday_idx = anchor.weekday()
    if preset == PRESETS[1]:
        weekday = r1[1].selectbox(
            "Weekday:", WEEKDAYS, index=anchor.weekday(), key="cmp_weekday"
        )
        weekday_idx = WEEKDAYS.index(weekday)
    group_cols = r1[2].multiselect(
        "Compare by (one or more columns):",
        available_columns,
        default=default_group,
        key="cmp_group",
    )
    st.caption(
        f"Latest call date in the data: {anchor:%a %b %d, %Y}. "
        "'This week' and 'latest day' are measured from it; weeks start on Monday."
    )

    # --- Row 2: the two periods (changing the preset resets them) ---
    a_def, b_def = preset_ranges(preset, anchor, weekday_idx)
    suffix = f"{PRESETS.index(preset)}_{weekday_idx}_{anchor}"
    r2 = st.columns(2)
    pick_a = r2[0].date_input("Period A (earlier):", value=a_def, key=f"cmp_a_{suffix}")
    pick_b = r2[1].date_input("Period B (later):", value=b_def, key=f"cmp_b_{suffix}")

    range_a, range_b = as_range(pick_a), as_range(pick_b)
    if range_a is None or range_b is None:
        st.info("Pick both the start and end date for each period (click the same day twice for a single day).")
        return
    if not group_cols:
        st.info("Pick at least one column to compare by.")
        return

    # --- Normalise group columns and build one label per group ---
    base = base_df.copy()
    for c in group_cols:
        base[c] = base[c].fillna("Unknown").astype(str).str.strip()
        base.loc[base[c] == "", c] = "Unknown"
    base["_label"] = base[group_cols].apply(" | ".join, axis=1)

    # --- Row 3: metrics and optional value filter ---
    r3 = st.columns(2)
    show_metrics = r3[0].multiselect(
        "Metrics to show:", METRICS, default=METRICS[:5], key="cmp_metrics"
    ) or ["Calls"]
    labels_by_freq = base["_label"].value_counts().index.tolist()
    only = r3[1].multiselect(
        "Only these values (leave empty for all):",
        labels_by_freq,
        key="cmp_only_" + "|".join(group_cols),
    )
    if only:
        base = base[base["_label"].isin(only)]

    frame_a, frame_b = slice_period(base, *range_a), slice_period(base, *range_b)
    st.caption(
        f"**A:** {describe_period(*range_a)}, {len(frame_a):,} calls   |   "
        f"**B:** {describe_period(*range_b)}, {len(frame_b):,} calls"
    )
    if len(frame_a) == 0 and len(frame_b) == 0:
        st.warning("No calls found in either period.")
        return
    if len(frame_a) == 0 or len(frame_b) == 0:
        st.warning(
            "One of the two periods has no calls, so the comparison is one-sided. "
            "Check the dates (your data starts/ends on a different day)."
        )

    # --- Summary cards: B value, difference vs A ---
    kpi_a = period_kpis(frame_a, qc_col, voip_col)
    kpi_b = period_kpis(frame_b, qc_col, voip_col)
    cards = st.columns(5)
    for card, m in zip(cards, ["Calls", "Qualified", "Spam", "VoIP", "Avg Score"]):
        a, b = kpi_a[m], kpi_b[m]
        if pd.isna(b):
            card.metric(f"{m} (B)", "n/a")
        elif pd.isna(a):
            card.metric(f"{m} (B)", f"{b:.1f}" if m == "Avg Score" else f"{b:,}")
        else:
            diff = round(b - a, 1) if m == "Avg Score" else b - a
            value = f"{b:.1f}" if m == "Avg Score" else f"{b:,}"
            delta = (
                f"{diff:+.1f} vs A ({a:.1f})" if m == "Avg Score" else f"{diff:+,} vs A ({a:,})"
            )
            color = "off" if diff == 0 else ("inverse" if m == "Spam" else "normal")
            card.metric(f"{m} (B)", value, delta=delta, delta_color=color)

    # --- Per-group comparison table ---
    stats_a = period_stats(frame_a, group_cols, qc_col, voip_col)
    stats_b = period_stats(frame_b, group_cols, qc_col, voip_col)
    idx = stats_a.index.union(stats_b.index).set_names(group_cols)
    stats_a, stats_b = stats_a.reindex(idx), stats_b.reindex(idx)

    res = pd.DataFrame(index=idx)
    for m in METRICS:
        a, b = stats_a[m], stats_b[m]
        if m in COUNT_METRICS:
            a, b = a.fillna(0).astype(int), b.fillna(0).astype(int)
            diff = b - a
        else:
            a, b = a.round(1), b.round(1)
            diff = (b - a).round(1)
        res[f"{m} A"], res[f"{m} B"], res[f"{m} Δ"] = a, b, diff
    res["_total"] = res["Calls A"] + res["Calls B"]
    res = res.sort_values("_total", ascending=False).drop(columns="_total").reset_index()

    table_cols = group_cols + [f"{m} {s}" for m in show_metrics for s in ("A", "B", "Δ")]
    table = res[table_cols]

    st.markdown("**Calls per group: A vs B** (top 15)")
    chart = res.copy()
    chart["Group"] = chart[group_cols].astype(str).apply(" | ".join, axis=1)
    st.bar_chart(chart.set_index("Group")[["Calls A", "Calls B"]].head(15))

    st.dataframe(table, width="stretch", hide_index=True)
    st.caption("Δ = B minus A. Avg Score ignores calls that have no AI QC result yet.")
    st.download_button(
        label="📥 Download Comparison as CSV",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name="period_comparison.csv",
        mime="text/csv",
        key="cmp_download",
    )


# ---------------------------------------------------------------
# Sidebar date filter presets
# ---------------------------------------------------------------
DATE_PRESETS = [
    "All time",
    "Today",
    "Yesterday",
    "This week",
    "Last week",
    "Last 7 days",
    "Last 30 days",
    "Last month",
    "Last 6 months",
    "This year",
    "Last year",
    "Custom date range",
]
# Which calendar day counts as "Today". Change to the timezone your call times are in,
# for example "America/New_York" or "UTC".
DEFAULT_TIMEZONE = "Asia/Dhaka"


def get_today(tz_name):
    """Today's date in the given timezone. Returns (date, warning_or_None)."""
    try:
        return pd.Timestamp.now(tz=tz_name.strip()).date(), None
    except Exception:
        return pd.Timestamp.now(tz="UTC").date(), f"Unknown timezone '{tz_name}', using UTC."


def date_filter_range(preset, today):
    """(start, end) dates, both inclusive, for a preset. Weeks start on Monday.
    'Last 7 days' / 'Last 30 days' / 'Last 6 months' include today."""
    day = timedelta(days=1)
    this_mon = today - timedelta(days=today.weekday())
    last_month_end = today.replace(day=1) - day
    if preset == "Today":
        return today, today
    if preset == "Yesterday":
        return today - day, today - day
    if preset == "This week":
        return this_mon, today
    if preset == "Last week":
        return this_mon - 7 * day, this_mon - day
    if preset == "Last 7 days":
        return today - 6 * day, today
    if preset == "Last 30 days":
        return today - 29 * day, today
    if preset == "Last month":
        return last_month_end.replace(day=1), last_month_end
    if preset == "Last 6 months":
        start = pd.Timestamp(today) - pd.DateOffset(months=6) + pd.Timedelta(days=1)
        return start.date(), today
    if preset == "This year":
        return today.replace(month=1, day=1), today
    if preset == "Last year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return None, None  # "All time" / custom: handled by the caller


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

    compare_base = None  # data for Period Comparison (ignores the sidebar date range)

    if date_cols:
        date_col_name = st.sidebar.selectbox("Select Date Column:", date_cols)
        work_df["Parsed_Date"] = parse_dates(work_df[date_col_name])
        compare_base = work_df.copy()
        valid_dates = work_df["Parsed_Date"].dropna()
        unreadable = int(work_df["Parsed_Date"].isna().sum())

        if not valid_dates.empty:
            min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
            quick_range = st.sidebar.selectbox("Quick range:", DATE_PRESETS, key="date_preset")

            start_d = end_d = None
            if quick_range == "Custom date range":
                picked = as_range(
                    st.sidebar.date_input("Pick start and end date:", (min_d, max_d), key="date_custom")
                )
                if picked:
                    start_d, end_d = picked
            elif quick_range != "All time":
                tz_name = st.sidebar.text_input(
                    "Timezone for 'Today':", DEFAULT_TIMEZONE, key="date_tz"
                )
                today, tz_warning = get_today(tz_name)
                if tz_warning:
                    st.sidebar.warning(tz_warning)
                start_d, end_d = date_filter_range(quick_range, today)

            if start_d is not None:
                in_range = (
                    (work_df["Parsed_Date"] >= pd.Timestamp(start_d))
                    & (work_df["Parsed_Date"] < pd.Timestamp(end_d) + pd.Timedelta(days=1))
                )
                work_df = work_df[in_range]
                st.sidebar.caption(f"Showing: {describe_period(start_d, end_d)}")
                if unreadable:
                    st.sidebar.caption(
                        f"{unreadable} rows with no readable date are left out while a date filter is on."
                    )
            elif unreadable:
                st.sidebar.caption(f"{unreadable} rows have no readable date (shown under 'All time' only).")
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
    if len(work_df) == 0:
        st.warning("No calls match the current filters (date range / search). Try a wider range.")
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

    render_period_comparison(compare_base, available_columns, selected_qc_col, selected_voip_col)