import streamlit as st
import pandas as pd
import gspread
from datetime import date, timedelta

from query_layer import (
    query_calls,
    get_group_stats,
    extract_qc_field,
    render_query_layer_explorer,
)

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
# A call counts as "AI QC completed" only when the report has the structured result.
# Rows holding "Processing error: ..." text (failed AI runs) are NOT completed.
QC_DONE_PAT = r"^\s*CALL TYPE:"


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
METRICS = COUNT_METRICS + [
    "Qualification %", "Spam %", "VoIP %", "QC Completion %", "Avg Score", "Avg Duration (sec)",
]
DEFAULT_COMPARE_METRICS = [
    "Calls", "Qualified", "Spam", "VoIP", "Qualification %", "Spam %", "VoIP %", "Avg Score",
]


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


def slice_window(frame, start_ts, end_ts_excl):
    """Rows whose Parsed_Date is in [start_ts, end_ts_excl). Rows with no date never match."""
    d = frame["Parsed_Date"]
    return frame[(d >= start_ts) & (d < end_ts_excl)]


def slice_period(frame, start_d, end_d):
    """Rows from start_d to end_d, both days included."""
    return slice_window(frame, pd.Timestamp(start_d), pd.Timestamp(end_d) + pd.Timedelta(days=1))


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
    f["_qc_done"] = (
        f[qc_col].astype(str).str.contains(QC_DONE_PAT, case=False, na=False, regex=True)
        if qc_col != "None" else False
    )
    f["_line_done"] = (f[voip_col].astype(str).str.strip() != "") if voip_col != "None" else False
    return f.groupby(cols).agg(
        **{
            "Calls": ("_row", "sum"),
            "Qualified": ("_is_qual", "sum"),
            "Spam": ("_is_spam", "sum"),
            "VoIP": ("_is_voip", "sum"),
            "Avg Score": ("Quality_Score_Num", "mean"),
            "Avg Duration (sec)": ("Duration_Num", "mean"),
            "QC Done": ("_qc_done", "sum"),
            "Line Done": ("_line_done", "sum"),
        }
    )


def period_kpis(frame, qc_col, voip_col):
    return {
        "Calls": len(frame),
        "Qualified": count_match(frame[qc_col], QUAL_PAT) if qc_col != "None" else 0,
        "Spam": count_match(frame[qc_col], SPAM_PAT) if qc_col != "None" else 0,
        "VoIP": count_match(frame[voip_col], VOIP_PAT) if voip_col != "None" else 0,
        "Avg Score": frame["Quality_Score_Num"].mean(),
        "QC Done": count_match(frame[qc_col], QC_DONE_PAT) if qc_col != "None" else 0,
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
    base = normalize_groups(base_df, group_cols)
    base["_label"] = base[group_cols].apply(" | ".join, axis=1)

    # --- Row 3: metrics and optional value filter ---
    r3 = st.columns(2)
    show_metrics = r3[0].multiselect(
        "Metrics to show:", METRICS, default=DEFAULT_COMPARE_METRICS, key="cmp_metrics"
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
    stats_a = add_percentages(period_stats(frame_a, group_cols, qc_col, voip_col))
    stats_b = add_percentages(period_stats(frame_b, group_cols, qc_col, voip_col))
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
    st.caption(
        "Δ = B minus A (for % metrics it is in percentage points). "
        "Percentages are a share of all calls in that period; a group with no calls in a period shows blank. "
        "Avg Score ignores calls that have no AI QC result yet."
    )
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
    "This month",
    "Last month",
    "Last 6 months",
    "This year",
    "Last year",
    "Custom date range",
]
# Which calendar day counts as "Today". Change to the timezone your call times are in,
# for example "America/New_York" or "UTC".
DEFAULT_TIMEZONE = "America/New_York"


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
    if preset == "This month":
        return today.replace(day=1), today
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
# Percentages, health status, trends and insights
# Pure Python + pandas rules (no AI), so every result is reproducible and explainable.
# ---------------------------------------------------------------
def safe_pct(numer, denom):
    """numer / denom * 100, or NaN when denom is 0 (never divides by zero).
    Works for single numbers and for pandas Series."""
    if isinstance(denom, pd.Series):
        return numer / denom.where(denom > 0) * 100
    return float("nan") if not denom else numer / denom * 100


def fmt_pct(x):
    return "–" if pd.isna(x) else f"{x:.1f}%"


def count_with_pct(count, total):
    """'62 (21.1%)' for the summary cards; 0 calls shows 0.0%."""
    pct = 0.0 if not total else count / total * 100
    return f"{count:,} ({pct:.1f}%)"


def normalize_groups(frame, cols):
    """Blank or missing group values become 'Unknown', so a missing Publisher never crashes."""
    out = frame.copy()
    for c in cols:
        out[c] = out[c].fillna("Unknown").astype(str).str.strip()
        out.loc[out[c] == "", c] = "Unknown"
    return out


def add_percentages(stats):
    """Add the percentage columns to a period_stats() table. All are a share of TOTAL calls:
    Qualification % = Qualified / Calls, Spam % = Spam / Calls, VoIP % = VoIP / Calls,
    QC Completion % = calls with a completed AI QC / Calls."""
    out = stats.copy()
    out["Qualification %"] = safe_pct(out["Qualified"], out["Calls"])
    out["Spam %"] = safe_pct(out["Spam"], out["Calls"])
    out["VoIP %"] = safe_pct(out["VoIP"], out["Calls"])
    out["QC Completion %"] = safe_pct(out["QC Done"], out["Calls"])
    out["Line Type Completion %"] = safe_pct(out["Line Done"], out["Calls"])  # used by trend guards
    return out


# ---------------- Health status ----------------
# Transparent thresholds. Percentages are a share of all calls in the selected period.
HEALTH_RULES = {
    "min_calls": 15,              # fewer calls than this -> INSUFFICIENT DATA
    "min_calls_high_risk": 30,    # fewer calls than this can never be HIGH RISK (capped at WATCH)
    "min_qc_completion": 70.0,    # % of calls that must have a completed AI QC
    "min_qc_calls": 10,           # and at least this many QC-completed calls
    "spam": (8.0, 20.0),          # (watch at or above, serious at or above)   spam %
    "voip": (30.0, 50.0),         # (watch at or above, serious at or above)   VoIP %
    "qualification": (12.0, 5.0), # (watch below, serious below)               qualification %
    "score": (45.0, 30.0),        # (watch below, serious below)               avg quality score
}


def _join_phrases(parts):
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def health_status(calls, qc_done, qc_pct, qual_pct, spam_pct, voip_pct, avg_score):
    """Return (status, reason). Deterministic: the same numbers always give the same answer."""
    R = HEALTH_RULES
    calls, qc_done = int(calls), int(qc_done)

    if calls < R["min_calls"]:
        return "⚪ INSUFFICIENT DATA", (
            f"Only {calls} call{'s' if calls != 1 else ''} in this period (need at least {R['min_calls']})"
        )
    if qc_done < R["min_qc_calls"] or qc_pct < R["min_qc_completion"]:
        return "⚪ INSUFFICIENT DATA", (
            f"Only {qc_done} of {calls} calls ({qc_pct:.0f}%) have a completed AI QC "
            f"(need at least {R['min_qc_completion']:.0f}% and {R['min_qc_calls']} calls)"
        )

    flags = []  # (level, order, text); level 2 = serious, 1 = watch
    watch, serious = R["spam"]
    if spam_pct >= serious:
        flags.append((2, 0, f"high spam rate ({spam_pct:.1f}%)"))
    elif spam_pct >= watch:
        flags.append((1, 0, f"elevated spam rate ({spam_pct:.1f}%)"))
    watch, serious = R["qualification"]
    if qual_pct < serious:
        flags.append((2, 1, f"very low qualification rate ({qual_pct:.1f}%)"))
    elif qual_pct < watch:
        flags.append((1, 1, f"qualification rate is below the normal range ({qual_pct:.1f}%)"))
    if not pd.isna(avg_score):
        watch, serious = R["score"]
        if avg_score < serious:
            flags.append((2, 2, f"very low average quality score ({avg_score:.1f})"))
        elif avg_score < watch:
            flags.append((1, 2, f"low average quality score ({avg_score:.1f})"))
    watch, serious = R["voip"]
    if voip_pct >= serious:
        flags.append((2, 3, f"very high VoIP share ({voip_pct:.1f}%)"))
    elif voip_pct >= watch:
        flags.append((1, 3, f"high VoIP share ({voip_pct:.1f}%)"))

    if not flags:
        score_txt = "" if pd.isna(avg_score) else f", avg score {avg_score:.1f}"
        return "🟢 HEALTHY", (
            f"Qualification {qual_pct:.1f}%, spam {spam_pct:.1f}%, VoIP {voip_pct:.1f}%{score_txt}: "
            "all within the normal range"
        )

    flags.sort(key=lambda f: (-f[0], f[1]))
    texts = [f[2] for f in flags]
    if len(texts) > 3:  # keep it short: the three most important flags, then a count
        reason = ", ".join(texts[:3]) + f" and {len(texts) - 3} more"
    else:
        reason = _join_phrases(texts)
    reason = reason[:1].upper() + reason[1:]

    n_serious = sum(1 for f in flags if f[0] == 2)
    if spam_pct >= R["spam"][1] or n_serious >= 2:
        if calls >= R["min_calls_high_risk"]:
            return "🔴 HIGH RISK", reason
        return "🟡 WATCH", f"{reason} (only {calls} calls, so not marked high risk)"
    return "🟡 WATCH", reason


def health_rules_markdown():
    """The thresholds in plain words, generated from HEALTH_RULES so the text never goes stale."""
    R = HEALTH_RULES
    return (
        "Status comes from fixed rules (no AI), so the same data always gives the same result. "
        "Every percentage is a share of **total calls** in the selected timeline: "
        "Qualification % = qualified ÷ total, Spam % = spam ÷ total, VoIP % = VoIP ÷ total, "
        "QC Completion % = calls with a completed AI QC ÷ total.\n\n"
        f"- ⚪ **INSUFFICIENT DATA**: fewer than {R['min_calls']} calls, or fewer than "
        f"{R['min_qc_completion']:.0f}% of calls (or fewer than {R['min_qc_calls']} calls) have a completed AI QC.\n"
        "- 🟢 **HEALTHY**: none of the flags below.\n"
        f"- 🟡 **WATCH**: at least one flag: spam ≥ {R['spam'][0]:.0f}%, qualification < "
        f"{R['qualification'][0]:.0f}%, avg quality score < {R['score'][0]:.0f}, VoIP ≥ {R['voip'][0]:.0f}%.\n"
        f"- 🔴 **HIGH RISK**: spam ≥ {R['spam'][1]:.0f}%, or two serious flags (qualification < "
        f"{R['qualification'][1]:.0f}%, avg quality score < {R['score'][1]:.0f}, VoIP ≥ {R['voip'][1]:.0f}%), "
        f"and at least {R['min_calls_high_risk']} calls. With fewer calls it stays WATCH."
    )


def health_columns(stats):
    """Health and Health_Reason lists for every row of add_percentages(period_stats(...))."""
    results = [
        health_status(
            r["Calls"], r["QC Done"], r["QC Completion %"], r["Qualification %"],
            r["Spam %"], r["VoIP %"], r["Avg Score"],
        )
        for _, r in stats.iterrows()
    ]
    return [x[0] for x in results], [x[1] for x in results]


# ---------------- Health thresholds adjustable from the sidebar ----------------
HEALTH_DEFAULTS = dict(HEALTH_RULES)  # the built-in values (all numbers / tuples, so a shallow copy is safe)
HEALTH_WIDGETS = [
    # (rule key, index in tuple or None, label, min, max, step, kind)
    ("min_calls", None, "Minimum calls for a status", 1, 10000, 1, int),
    ("min_calls_high_risk", None, "Minimum calls for HIGH RISK", 1, 10000, 1, int),
    ("min_qc_completion", None, "Minimum QC completion (%)", 0.0, 100.0, 5.0, float),
    ("min_qc_calls", None, "Minimum QC-completed calls", 0, 10000, 1, int),
    ("spam", 0, "Spam % – WATCH at or above", 0.0, 100.0, 1.0, float),
    ("spam", 1, "Spam % – HIGH RISK at or above", 0.0, 100.0, 1.0, float),
    ("voip", 0, "VoIP % – WATCH at or above", 0.0, 100.0, 1.0, float),
    ("voip", 1, "VoIP % – serious at or above", 0.0, 100.0, 1.0, float),
    ("qualification", 0, "Qualification % – WATCH below", 0.0, 100.0, 1.0, float),
    ("qualification", 1, "Qualification % – serious below", 0.0, 100.0, 1.0, float),
    ("score", 0, "Avg score – WATCH below", 0.0, 100.0, 1.0, float),
    ("score", 1, "Avg score – serious below", 0.0, 100.0, 1.0, float),
]


def health_widget_key(rule, idx):
    return f"health_{rule}_{idx}"


def reset_health_settings():
    for rule, idx, *_ in HEALTH_WIDGETS:
        st.session_state.pop(health_widget_key(rule, idx), None)


def apply_health_settings():
    """Sidebar controls for the health thresholds. The defaults equal HEALTH_RULES, so nothing
    changes until the user edits a value. Invalid pairs (for example a serious level that is
    milder than the watch level) fall back to the defaults, with a warning."""
    values = {}
    with st.sidebar.expander("🩺 Health Status Thresholds"):
        st.caption("Change when a publisher / buyer / campaign counts as WATCH or HIGH RISK.")
        for rule, idx, label, lo, hi, step, kind in HEALTH_WIDGETS:
            default = HEALTH_DEFAULTS[rule] if idx is None else HEALTH_DEFAULTS[rule][idx]
            values[(rule, idx)] = st.number_input(
                label, min_value=kind(lo), max_value=kind(hi), value=kind(default), step=kind(step),
                key=health_widget_key(rule, idx),
            )
        st.button("Reset to defaults", on_click=reset_health_settings, key="health_reset")

    new_rules = dict(HEALTH_DEFAULTS)
    for rule, idx, *_ in HEALTH_WIDGETS:
        if idx is None:
            new_rules[rule] = values[(rule, idx)]
    problems = []
    for rule, rising in (("spam", True), ("voip", True), ("qualification", False), ("score", False)):
        watch, serious = values[(rule, 0)], values[(rule, 1)]
        ok = serious >= watch if rising else serious <= watch
        if ok:
            new_rules[rule] = (watch, serious)
        else:
            problems.append(rule)
    if values[("min_calls_high_risk", None)] < values[("min_calls", None)]:
        problems.append("minimum calls")
        new_rules["min_calls"] = HEALTH_DEFAULTS["min_calls"]
        new_rules["min_calls_high_risk"] = HEALTH_DEFAULTS["min_calls_high_risk"]
    if problems:
        st.sidebar.warning(
            "Health thresholds for " + ", ".join(problems) + " are inconsistent (the serious level must "
            "be stricter than the watch level, and HIGH RISK needs at least as many calls as a status). "
            "The default values are used for those."
        )
    HEALTH_RULES.update(new_rules)
    changed = [k for k in HEALTH_DEFAULTS if HEALTH_RULES[k] != HEALTH_DEFAULTS[k]]
    if changed:
        st.sidebar.caption("🩺 Custom health thresholds are active.")


# ---------------- Failed AI QC runs ----------------
QC_FAIL_PAT = r"PROCESSING ERROR|ANALYSIS PROVIDERS FAILED|^\s*FAILED\s*\|"
QC_FAIL_MIN_ROWS = 3        # a group is flagged when it has at least this many failed rows ...
QC_FAIL_MIN_SHARE = 20.0    # ... and at least this % of its calls failed


def qc_failed_mask(frame):
    """True for calls whose AI QC run failed ('Processing error ...' in the QC report, or
    'FAILED | All configured AI analysis providers failed ...' in any column)."""
    if frame.empty:
        return pd.Series(False, index=frame.index)
    text = frame.astype(str)
    hits = text.apply(lambda c: c.str.contains(QC_FAIL_PAT, case=False, na=False, regex=True))
    return hits.any(axis=1)


def qc_failure_groups(grouped, failed, dim):
    """Groups where AI QC keeps failing. grouped = normalized frame; failed = boolean array."""
    tmp = pd.DataFrame({dim: grouped[dim].values, "_failed": list(failed)})
    agg = tmp.groupby(dim).agg(Failed=("_failed", "sum"), Calls=("_failed", "size")).reset_index()
    agg["Share"] = safe_pct(agg["Failed"], agg["Calls"])
    flagged = agg[(agg["Failed"] >= QC_FAIL_MIN_ROWS) & (agg["Share"] >= QC_FAIL_MIN_SHARE)]
    return flagged.sort_values(["Failed", "Share"], ascending=False)


# ---------------- Total report (selected rows, or everything on the page) ----------------
def breakdown_column_config(dim):
    """Same column widths for the breakdown table and the Totals row, so the columns line up."""
    cfg = {dim: st.column_config.Column(width="large")}
    for c in ("Total_Calls", "Avg_Score", "Avg_Duration", "Qualified_Calls", "Spam_Calls", "VoIP_Calls",
              "Qualification_%", "Spam_%", "VoIP_%", "QC_Completion_%"):
        cfg[c] = st.column_config.Column(width="small")
    cfg["Health"] = st.column_config.Column(width="medium")
    cfg["Health_Reason"] = st.column_config.Column(width="large")
    return cfg


def totals_row(frame, dim, label, columns, qc_col, voip_col):
    """One 'Totals' row with exactly the columns of the breakdown table (zero-safe)."""
    k = period_kpis(frame, qc_col, voip_col)
    calls = k["Calls"]
    duration = frame["Duration_Num"].mean() if calls else float("nan")
    qc_pct = safe_pct(k["QC Done"], calls)
    qual_pct = safe_pct(k["Qualified"], calls)
    spam_pct = safe_pct(k["Spam"], calls)
    voip_pct = safe_pct(k["VoIP"], calls)
    status, reason = health_status(calls, k["QC Done"], qc_pct, qual_pct, spam_pct, voip_pct, k["Avg Score"])
    row = {
        dim: label, "Total_Calls": calls, "Avg_Score": k["Avg Score"], "Avg_Duration": duration,
        "Qualified_Calls": k["Qualified"], "Spam_Calls": k["Spam"], "VoIP_Calls": k["VoIP"],
        "Qualification_%": qual_pct, "Spam_%": spam_pct, "VoIP_%": voip_pct, "QC_Completion_%": qc_pct,
        "Health": status, "Health_Reason": reason,
    }
    out = pd.DataFrame([row])[list(columns)]
    for c in ("Avg_Score", "Avg_Duration", "Qualification_%", "Spam_%", "VoIP_%", "QC_Completion_%"):
        out[c] = out[c].astype(float).round(1)
    return out, k


def render_total_report(frame, dim, selected_vals, n_groups, qc_col, voip_col, columns):
    """Totals row directly under the breakdown table (like the 'Totals' line in Ringba): the
    combined total of the ticked rows, or of everything currently on the page when none are ticked."""
    if selected_vals:
        shown = ", ".join(map(str, selected_vals[:8]))
        if len(selected_vals) > 8:
            shown += f" and {len(selected_vals) - 8} more"
        label = f"Totals ({len(selected_vals)} selected)"
        st.markdown(f"**📊 Total Report – selected {dim}:** {shown}")
    else:
        label = f"Totals (all {n_groups:,})"
        st.markdown(
            f"**📊 Total Report – all {dim} values currently shown.** "
            "Nothing is selected; tick rows above to total only those."
        )
    out, k = totals_row(frame, dim, label, columns, qc_col, voip_col)
    st.dataframe(out, width="stretch", hide_index=True, column_config=breakdown_column_config(dim))
    if k["Calls"] - k["QC Done"]:
        st.caption(
            f"{k['Calls'] - k['QC Done']:,} of {k['Calls']:,} calls have no completed AI QC result yet; "
            "all percentages are a share of total calls."
        )


# ---------------- Previous equivalent period ----------------
TREND_NAMES = {
    # selected timeline: (how to say it, how to say its comparison period)
    "Today": ("today", "yesterday"),
    "Yesterday": ("yesterday", "the previous day"),
    "This week": ("this week", "last week"),
    "Last week": ("last week", "the week before last"),
    "Last 7 days": ("the last 7 days", "the previous 7 days"),
    "Last 30 days": ("the last 30 days", "the previous 30 days"),
    "This month": ("this month", "last month"),
    "Last month": ("last month", "the previous month"),
    "Last 6 months": ("the last 6 months", "the previous 6 months"),
    "This year": ("this year", "last year"),
    "Last year": ("last year", "the year before"),
}


def equivalent_previous(preset, start, end):
    """The previous equivalent period for the selected timeline (dates, both included).
    All time has no comparison period and returns None."""
    if preset == "All time" or start is None or end is None:
        return None
    day = timedelta(days=1)
    n_days = (end - start).days + 1
    cur_name, prev_name = TREND_NAMES.get(
        preset,
        ("the selected dates", "the previous day" if n_days == 1 else f"the previous {n_days} days"),
    )
    if preset == "This week":                       # the whole of last week
        prev = (start - 7 * day, start - day)
    elif preset in ("This month", "Last month"):    # the calendar month before
        prev_end = start - day
        prev = (prev_end.replace(day=1), prev_end)
    elif preset == "Last 6 months":                 # the 6 months before
        prev = ((pd.Timestamp(start) - pd.DateOffset(months=6)).date(), start - day)
    elif preset in ("This year", "Last year"):      # the calendar year before
        prev = (date(start.year - 1, 1, 1), date(start.year - 1, 12, 31))
    else:  # Today, Yesterday, Last week, Last 7 / 30 days, Custom: same length, immediately before
        prev = (start - n_days * day, start - day)
    return {"prev": prev, "cur_name": cur_name, "prev_name": prev_name}


def trend_windows(info, start, end, base):
    """Timestamp windows [start, end) for the current and comparison period.
    If the sheet's latest call falls INSIDE the selected period (Today, This week, or a day the
    sheet has not finished filling), the period is still in progress. The comparison window is
    then cut to the same elapsed time, so a partial day / week is never compared with a full
    one (that would show a false 'calls declining'). This uses only the data, so it does not
    depend on the clock or the timezone setting."""
    one_day = pd.Timedelta(days=1)
    cur_start, cur_end = pd.Timestamp(start), pd.Timestamp(end) + one_day
    prev_start, prev_end = pd.Timestamp(info["prev"][0]), pd.Timestamp(info["prev"][1]) + one_day
    last_call = base["Parsed_Date"].max()
    in_progress = bool(pd.notna(last_call) and last_call < cur_end)
    trimmed = False
    if in_progress:
        d = base["Parsed_Date"]
        in_cur = d[(d >= cur_start) & (d < cur_end)]
        elapsed = (in_cur.max() - cur_start + pd.Timedelta(seconds=1)) if len(in_cur) else pd.Timedelta(0)
        if prev_start + elapsed < prev_end:
            prev_end, trimmed = prev_start + elapsed, True
    return {
        "cur": (cur_start, cur_end), "prev": (prev_start, prev_end),
        "trimmed": trimmed, "in_progress": in_progress,
    }


def describe_window(start_ts, end_ts_excl):
    last = end_ts_excl - pd.Timedelta(seconds=1)
    if start_ts == start_ts.normalize() and end_ts_excl == end_ts_excl.normalize():
        return describe_period(start_ts.date(), last.date())
    return f"{start_ts:%a %b %d, %Y %H:%M} to {last:%a %b %d, %Y %H:%M}"


# ---------------- Trend detection ----------------
TREND_MIN_CALLS = 15        # rate / score / duration trends need this many calls in BOTH periods
TREND_MIN_VOLUME = 10       # a Total Calls trend needs this many calls in at least one period
TREND_MIN_COMPLETION = 50.0 # QC (or Line Type) completion % needed in BOTH periods
TREND_MIN_EVENTS = 3        # a rate trend needs at least this many qualified/spam/VoIP calls
TREND_THRESHOLDS = {
    # metric: (meaningful change, significant change)
    "Qualification %": (5.0, 15.0),   # percentage points
    "Spam %": (5.0, 10.0),            # percentage points
    "VoIP %": (10.0, 20.0),           # percentage points
    "Avg Score": (8.0, 15.0),         # score points
}
CALLS_RULE = {"rel": (0.30, 0.60), "abs": (10, 20)}      # relative AND absolute change
DURATION_RULE = {"rel": (0.25, 0.50), "abs": (20, 40)}   # relative AND seconds
EVENT_COLUMN = {"Qualification %": "Qualified", "Spam %": "Spam", "VoIP %": "VoIP"}
# (metric key, label, +1 if higher is better / -1 if lower is better)
TREND_METRICS = [
    ("Calls", "Total Calls", 1),
    ("Qualification %", "Qualification %", 1),
    ("Spam %", "Spam %", -1),
    ("VoIP %", "VoIP %", -1),
    ("Avg Score", "Avg Quality Score", 1),
    ("Avg Duration (sec)", "Avg Duration", 1),
]
TREND_ARROWS = {"improving": "↑", "declining": "↓", "stable": "→"}


def change_level(key, cur, prev):
    """0 = no meaningful change, 1 = meaningful, 2 = significant. Small changes are ignored."""
    diff = cur - prev
    if key in TREND_THRESHOLDS:
        small, big = TREND_THRESHOLDS[key]
        return 2 if abs(diff) >= big else 1 if abs(diff) >= small else 0
    rule = CALLS_RULE if key == "Calls" else DURATION_RULE
    rel = abs(diff) / prev if prev else float("inf")
    for level in (2, 1):
        if abs(diff) >= rule["abs"][level - 1] and rel >= rule["rel"][level - 1]:
            return level
    return 0


def metric_value(row, key):
    if row is None:
        return 0.0 if key == "Calls" else float("nan")
    return row[key]


def trend_eligible(key, cur, prev):
    """Is there enough data in BOTH periods to say anything about this metric?"""
    c, p = metric_value(cur, "Calls"), metric_value(prev, "Calls")
    if key == "Calls":
        return max(c, p) >= TREND_MIN_VOLUME
    if cur is None or prev is None or min(c, p) < TREND_MIN_CALLS:
        return False
    if pd.isna(cur[key]) or pd.isna(prev[key]):
        return False
    if key in ("Qualification %", "Spam %", "Avg Score"):
        if min(cur["QC Completion %"], prev["QC Completion %"]) < TREND_MIN_COMPLETION:
            return False
    if key == "VoIP %":
        if min(cur["Line Type Completion %"], prev["Line Type Completion %"]) < TREND_MIN_COMPLETION:
            return False
    if key in EVENT_COLUMN and max(cur[EVENT_COLUMN[key]], prev[EVENT_COLUMN[key]]) < TREND_MIN_EVENTS:
        return False
    return True


def fmt_trend_value(key, v):
    if pd.isna(v):
        return "–"
    if key == "Calls":
        return f"{int(v):,}"
    if key.endswith("%"):
        return f"{v:.1f}%"
    if key == "Avg Score":
        return f"{v:.1f}"
    return f"{v:.0f}s"


def build_trends(cur_stats, prev_stats):
    """Compare the selected period (cur) with its previous equivalent (prev), per group.
    Returns (records, group_order). One record per group and metric."""
    groups = cur_stats.index.union(prev_stats.index)
    order = sorted(
        groups,
        key=lambda g: (
            -(cur_stats.loc[g, "Calls"] if g in cur_stats.index else 0),
            -(prev_stats.loc[g, "Calls"] if g in prev_stats.index else 0),
            str(g),
        ),
    )
    records = []
    for g in order:
        cur = cur_stats.loc[g] if g in cur_stats.index else None
        prev = prev_stats.loc[g] if g in prev_stats.index else None
        for key, label, good in TREND_METRICS:
            cv, pv = metric_value(cur, key), metric_value(prev, key)
            eligible = trend_eligible(key, cur, prev)
            level, trend = 0, "n/a"
            if eligible:
                level = change_level(key, cv, pv)
                trend = "stable" if level == 0 else ("improving" if (cv - pv) * good > 0 else "declining")
            cell = (
                "n/a" if not eligible
                else f"{TREND_ARROWS[trend]} {fmt_trend_value(key, cv)} (was {fmt_trend_value(key, pv)})"
            )
            records.append({
                "group": g, "key": key, "label": label, "cur": cv, "prev": pv,
                "level": level, "trend": trend, "cell": cell, "cur_row": cur, "prev_row": prev,
            })
    return records, order


def trend_table(records, order, dim):
    """One row per group. Groups with too little data for every metric are left out;
    the second value returned says how many."""
    cells = {g: {dim: g} for g in order}
    for r in records:
        cells[r["group"]][r["label"]] = r["cell"]
    rows = [cells[g] for g in order]
    shown = [row for row in rows if any(v != "n/a" for k, v in row.items() if k != dim)]
    return pd.DataFrame(shown, columns=[dim] + [label for _, label, _ in TREND_METRICS]), len(rows) - len(shown)


# ---------------- Actionable insights ----------------
# {sig} becomes "significant " for large changes. 'cap' = highest severity this metric may reach.
INSIGHT_TEXT = {
    "Spam %": {
        "order": 0, "cap": 2,
        "worse": ("Spam rate increased", "This is a {sig}deterioration in traffic quality.",
                  "Review recent traffic sources and call quality."),
        "better": ("Spam rate decreased", "Traffic quality is improving.",
                   "Keep monitoring; no action needed."),
    },
    "Qualification %": {
        "order": 1, "cap": 2,
        "worse": ("Qualification rate decreased",
                  "Fewer calls are qualifying, which is a {sig}decline in lead quality.",
                  "Check targeting and recent call recordings, and discuss it with the publisher."),
        "better": ("Qualification rate increased", "More calls are turning into qualified leads.",
                   "Find out what changed and consider increasing volume from this source."),
    },
    "Avg Score": {
        "order": 2, "cap": 2,
        "worse": ("Average quality score decreased", "Overall call quality is {sig}dropping.",
                  "Listen to a sample of the low-scoring calls and review the traffic source."),
        "better": ("Average quality score increased", "Overall call quality is improving.",
                   "Keep monitoring; no action needed."),
    },
    "VoIP %": {
        "order": 3, "cap": 2,
        "worse": ("VoIP share increased",
                  "A higher share of VoIP numbers can point to fake or low-quality traffic.",
                  "Check for spoofed or fake numbers and consider blocking VoIP lines."),
        "better": ("VoIP share decreased", "Fewer VoIP numbers suggests cleaner traffic.",
                   "Keep monitoring; no action needed."),
    },
    "Calls": {
        "order": 4, "cap": 1,
        "worse": ("Total calls decreased", "This is a {sig}drop in traffic volume.",
                  "Check whether the publisher paused or capped traffic, or whether tracking changed."),
        "better": ("Total calls increased", "Traffic volume is growing.",
                   "Make sure call quality holds up as volume grows."),
    },
    "Avg Duration (sec)": {
        "order": 5, "cap": 1,
        "worse": ("Average call duration decreased",
                  "Callers are hanging up sooner, which can signal lower-intent traffic.",
                  "Review a sample of the short calls (hang-up reason, call routing)."),
        "better": ("Average call duration increased", "Longer calls usually mean more engaged callers.",
                   "Keep monitoring; no action needed."),
    },
}
INSIGHT_ICONS = {2: "🔴", 1: "🟡", 0: "🟢"}


def _insight_value(key, row, value):
    """'18.2% (6 of 33 calls)' for rates, plain numbers for the rest."""
    if key in EVENT_COLUMN and row is not None:
        return f"{value:.1f}% ({int(row[EVENT_COLUMN[key]])} of {int(row['Calls'])} calls)"
    return fmt_trend_value(key, value)


def build_insights(records, prev_name):
    """Turn meaningful trends into prioritised, plain-language insights (no AI).
    Only metrics with enough data in both periods are used. Worsening trends are always
    shown (red = significant, yellow = meaningful); improvements only when significant."""
    insights = []
    for r in records:
        if r["trend"] not in ("declining", "improving") or r["level"] == 0:
            continue
        cfg = INSIGHT_TEXT[r["key"]]
        if r["trend"] == "declining":
            severity = min(2 if r["level"] == 2 else 1, cfg["cap"])
            what, why, action = cfg["worse"]
        else:
            if r["level"] < 2:
                continue
            severity = 0
            what, why, action = cfg["better"]
        why = why.format(sig="significant " if r["level"] == 2 else "")
        prev_txt = _insight_value(r["key"], r["prev_row"], r["prev"])
        cur_txt = _insight_value(r["key"], r["cur_row"], r["cur"])
        text = (
            f"{INSIGHT_ICONS[severity]} **{r['group']}** — {what} from {prev_txt} to {cur_txt} "
            f"compared with {prev_name}. {why} Suggested action: {action}"
        )
        calls_now = metric_value(r["cur_row"], "Calls")
        insights.append({
            "severity": severity, "order": cfg["order"], "calls": calls_now, "text": text,
            "short": f"**{r['group']}** – {what} from {prev_txt} to {cur_txt}",
        })
    # most important first: red, yellow, green; then Spam, Qualification, Score, VoIP, Calls, Duration
    insights.sort(key=lambda i: (-i["severity"], i["order"], -i["calls"]))
    return insights


def compute_trend_context(timeline, work_df, base_df, dim, qc_col, voip_col):
    """Selected timeline -> previous equivalent period -> trends -> insights, as plain data.
    Used by both the 'Top issues' banner and the Trends section, so they always agree.
    status: 'no_timeline' | 'empty_current' | 'no_previous' | 'ok'."""
    preset = (timeline or {}).get("preset")
    start, end = (timeline or {}).get("start"), (timeline or {}).get("end")
    info = equivalent_previous(preset, start, end) if timeline else None
    if info is None or base_df is None:
        return {"status": "no_timeline"}

    win = trend_windows(info, start, end, base_df)
    cur_df = normalize_groups(work_df, [dim])
    prev_df = normalize_groups(slice_window(base_df, *win["prev"]), [dim])
    ctx = {"status": "ok", "info": info, "win": win, "start": start, "end": end,
           "first_call": base_df["Parsed_Date"].min(), "insights": [], "table": None, "n_hidden": 0}
    if cur_df.empty:
        ctx["status"] = "empty_current"
        return ctx
    if prev_df.empty:
        ctx["status"] = "no_previous"
        return ctx

    cur_stats = add_percentages(period_stats(cur_df, [dim], qc_col, voip_col))
    prev_stats = add_percentages(period_stats(prev_df, [dim], qc_col, voip_col))
    records, order = build_trends(cur_stats, prev_stats)
    ctx["table"], ctx["n_hidden"] = trend_table(records, order, dim)
    ctx["insights"] = build_insights(records, info["prev_name"])
    return ctx


def render_top_issues(ctx, dim):
    """One-line banner: the (up to) two most important high-priority insights. Hidden when there
    is no comparison period or nothing is high priority."""
    if not ctx or ctx.get("status") != "ok":
        return
    reds = [i for i in ctx["insights"] if i["severity"] == 2][:2]
    if not reds:
        return
    info = ctx["info"]
    st.error(
        f"🚨 **Top issues for {info['cur_name']}** (vs {info['prev_name']}, by {dim}): "
        + "  |  ".join(i["short"] for i in reds)
        + "  — details in *Actionable Insights* below."
    )


def render_trends_and_insights(timeline, work_df, base_df, dim, qc_col, voip_col, ctx=None):
    """Selected timeline -> previous equivalent period -> trends -> actionable insights."""
    st.markdown("---")
    st.subheader(f"📉 Trends vs Previous Equivalent Period (by {dim})")

    if ctx is None:
        ctx = compute_trend_context(timeline, work_df, base_df, dim, qc_col, voip_col)
    if ctx["status"] == "no_timeline":
        st.info(
            "Trends compare the selected timeline with the previous equivalent period. "
            "There is no meaningful comparison period for **All time**, so pick a timeline in the "
            "sidebar (for example Last 7 days, This week or Custom date range) to see trends and insights."
        )
        return

    info, win, start = ctx["info"], ctx["win"], ctx["start"]
    st.caption(
        f"**Current ({info['cur_name']}):** {describe_period(start, ctx['end'])}"
        f"{' (so far)' if win['in_progress'] else ''}   |   "
        f"**Comparison ({info['prev_name']}):** {describe_window(*win['prev'])}"
        f"{', same elapsed time' if win['trimmed'] else ''}"
    )
    first_call = ctx["first_call"]
    if pd.notna(first_call) and first_call > win["prev"][0]:
        st.warning(
            f"The comparison period starts before the first call in your sheet "
            f"({first_call:%b %d, %Y}), so it may be incomplete."
        )
    if ctx["status"] == "empty_current":
        st.info("There are no calls in the selected period yet, so no trend can be calculated.")
        return
    if ctx["status"] == "no_previous":
        st.warning(
            "There are no calls in the comparison period, so trends and insights are not available."
        )
        return

    table = ctx["table"]
    st.dataframe(table, width="stretch", hide_index=True)
    st.download_button(
        label="📥 Download Trends Table as CSV",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name="trends_vs_previous_period.csv",
        mime="text/csv",
        key="dl_trends",
    )
    if ctx["n_hidden"]:
        st.caption(
            f"{ctx['n_hidden']} {dim} value(s) are not shown because they have too little data "
            "in one of the two periods."
        )
    st.caption(
        "Each cell shows the current value and, in brackets, the previous value. "
        "↑ Improving · ↓ Declining · → Stable · n/a = not enough data in one of the periods. "
        "The arrow shows quality, so a rising spam or VoIP rate shows ↓ Declining. "
        f"Small changes count as Stable (for example Qualification, Spam: under "
        f"{TREND_THRESHOLDS['Qualification %'][0]:.0f} percentage points)."
    )

    # ---- Actionable insights (same selected timeline and comparison period) ----
    st.subheader("💡 Actionable Insights")
    insights = ctx["insights"]
    if not insights:
        st.info(
            f"No meaningful changes found for {info['cur_name']} compared with {info['prev_name']}, "
            "or there is not enough data to be sure."
        )
        return
    n_red = sum(1 for i in insights if i["severity"] == 2)
    n_yellow = sum(1 for i in insights if i["severity"] == 1)
    n_green = sum(1 for i in insights if i["severity"] == 0)
    st.caption(
        f"{info['cur_name'].capitalize()} vs {info['prev_name']}: "
        f"🔴 {n_red} high priority · 🟡 {n_yellow} to watch · 🟢 {n_green} positive. "
        "Rule-based (no AI): most important issues first."
    )
    for item in insights[:15]:
        st.markdown(item["text"])
    if len(insights) > 15:
        with st.expander(f"Show {len(insights) - 15} more insights"):
            for item in insights[15:]:
                st.markdown(item["text"])


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

    # --- Health status thresholds (sidebar, defaults = built-in rules) ---
    apply_health_settings()

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
    timeline = None      # the selected timeline, used for trends and insights

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
            timeline = {"preset": quick_range}
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
                timeline["start"], timeline["end"] = start_d, end_d
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
    # KPI metrics: counts with their percentage of total calls (selected timeline)
    # ---------------------------------------------------------------
    top_issues_slot = st.container()  # filled further down, once the group-by choice is known
    st.markdown("### 📈 Network Overview & Key Metrics")
    if len(work_df) == 0:
        st.warning("No calls match the current filters (date range / search). Try a wider range.")
    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)

    kpi = period_kpis(work_df, selected_qc_col, selected_voip_col)
    n_calls = kpi["Calls"]

    kpi1.metric("Total Filtered Calls", f"{n_calls:,}")
    kpi2.metric("Qualified Calls", count_with_pct(kpi["Qualified"], n_calls))
    kpi3.metric("Spam / Fake Calls", count_with_pct(kpi["Spam"], n_calls))
    kpi4.metric("VoIP Calls", count_with_pct(kpi["VoIP"], n_calls))
    kpi5.metric("Avg Quality Score", f"{kpi['Avg Score']:.1f}" if not pd.isna(kpi["Avg Score"]) else "0.0")
    qc_completion = 0.0 if not n_calls else kpi["QC Done"] / n_calls * 100
    kpi6.metric(
        "QC Completion",
        f"{qc_completion:.1f}%",
        delta=f"{kpi['QC Done']:,} of {n_calls:,} calls",
        delta_color="off",
    )

    if selected_qc_col != "None":
        pending = n_calls - kpi["QC Done"]
        if pending:
            st.caption(
                f"ℹ️ {pending:,} of {n_calls:,} filtered calls have no completed AI QC result yet "
                "(blank in the sheet, or the AI run failed). They count in Total Calls but not as "
                "Qualified / Spam, and are left out of Avg Quality Score. All percentages are a share "
                "of total calls."
            )

    qc_failed = qc_failed_mask(work_df)
    if int(qc_failed.sum()):
        st.caption(
            f"⚠️ {int(qc_failed.sum()):,} of {n_calls:,} filtered calls had a failed AI QC run "
            "(Processing error / all AI providers failed). They have no QC result."
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
        temp_df = normalize_groups(work_df, [selected_dimension])
        stats = add_percentages(
            period_stats(temp_df, [selected_dimension], selected_qc_col, selected_voip_col)
        )
        stats["Health"], stats["Health_Reason"] = health_columns(stats)

        summary = stats.reset_index().rename(columns={
            "Calls": "Total_Calls",
            "Avg Score": "Avg_Score",
            "Avg Duration (sec)": "Avg_Duration",
            "Qualified": "Qualified_Calls",
            "Spam": "Spam_Calls",
            "VoIP": "VoIP_Calls",
            "Qualification %": "Qualification_%",
            "Spam %": "Spam_%",
            "VoIP %": "VoIP_%",
            "QC Completion %": "QC_Completion_%",
        })
        summary = summary[[
            selected_dimension, "Total_Calls", "Avg_Score", "Avg_Duration",
            "Qualified_Calls", "Spam_Calls", "VoIP_Calls",
            "Qualification_%", "Spam_%", "VoIP_%", "QC_Completion_%",
            "Health", "Health_Reason",
        ]]
        for col in ("Avg_Score", "Avg_Duration", "Qualification_%", "Spam_%", "VoIP_%", "QC_Completion_%"):
            summary[col] = summary[col].round(1)
        summary = summary.sort_values(by="Total_Calls", ascending=False).reset_index(drop=True)

        st.subheader(f"Performance Breakdown by {selected_dimension}")

        failing = qc_failure_groups(temp_df, qc_failed.values, selected_dimension)
        if not failing.empty:
            parts = [
                f"**{r[selected_dimension]}** ({int(r['Failed'])} of {int(r['Calls'])} calls, {r['Share']:.0f}%)"
                for _, r in failing.head(5).iterrows()
            ]
            more = f" and {len(failing) - 5} more" if len(failing) > 5 else ""
            st.warning(
                f"⚠️ AI QC keeps failing for {selected_dimension}: {', '.join(parts)}{more}. "
                "These calls have no Qualified / Spam result, so their numbers and health status may be "
                "incomplete. Check the AI QC automation (API limits or errors) for these rows."
            )

        if len(summary) > 0:
            health_counts = summary["Health"].value_counts()
            st.caption(
                "Health status: "
                + " · ".join(
                    f"{label} {int(health_counts.get(label, 0))}"
                    for label in ("🟢 HEALTHY", "🟡 WATCH", "🔴 HIGH RISK", "⚪ INSUFFICIENT DATA")
                )
            )
            st.bar_chart(summary.set_index(selected_dimension)["Total_Calls"].head(15))

        with st.expander("How health status and percentages are calculated"):
            st.markdown(health_rules_markdown())

        st.markdown("💡 *Select multiple rows in the table below to view and export their combined raw details:*")

        event = st.dataframe(
            summary,
            width="stretch",
            on_select="rerun",
            selection_mode="multi-row",
            column_config=breakdown_column_config(selected_dimension),
        )

        selected_rows = event.selection.rows if hasattr(event, "selection") else []

        # Total report: the ticked rows, or everything currently on the page when none are ticked
        selected_vals = summary.iloc[selected_rows][selected_dimension].tolist() if selected_rows else []
        report_frame = temp_df[temp_df[selected_dimension].isin(selected_vals)] if selected_vals else temp_df
        render_total_report(
            report_frame, selected_dimension, selected_vals, len(summary),
            selected_qc_col, selected_voip_col, summary.columns,
        )

        st.download_button(
            label="📥 Download Breakdown Table as CSV",
            data=summary.to_csv(index=False).encode("utf-8"),
            file_name=f"breakdown_by_{selected_dimension}.csv".replace(" ", "_").replace("/", "-"),
            mime="text/csv",
            key="dl_breakdown",
        )

        if selected_rows:

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

        # Selected timeline -> previous equivalent period -> trends -> actionable insights
        trend_ctx = compute_trend_context(
            timeline, work_df, compare_base, selected_dimension, selected_qc_col, selected_voip_col
        )
        with top_issues_slot:
            render_top_issues(trend_ctx, selected_dimension)
        render_trends_and_insights(
            timeline, work_df, compare_base, selected_dimension, selected_qc_col, selected_voip_col,
            ctx=trend_ctx,
        )

    render_period_comparison(compare_base, available_columns, selected_qc_col, selected_voip_col)


# ---------------------------------------------------------------
# Step 5A Query Layer Interactive Explorer Component
# ---------------------------------------------------------------
st.markdown("---")

with st.expander("🛠️ Step 5A Query Layer Interactive Explorer (Multi-Field & Semantic)", expanded=False):
    st.markdown("Test the dimension-independent query engine across all A–O columns and AI QC attributes.")

    q_col1, q_col2, q_col3 = st.columns(3)
    exp_pub = q_col1.text_input("Filter Publisher (Query Layer):", "", key="exp_pub_input")
    exp_buy = q_col2.text_input("Filter Buyer (Query Layer):", "", key="exp_buy_input")
    exp_cmp = q_col3.text_input("Filter Campaign (Query Layer):", "", key="exp_cmp_input")

    q_col4, q_col5, q_col6 = st.columns(3)
    exp_ins = q_col4.text_input("Semantic Insurance Search (e.g. Medicaid):", "", key="exp_ins_input")
    exp_line = q_col5.text_input("Filter Line Type (e.g. VoIP):", "", key="exp_line_input")
    exp_dur = q_col6.number_input("Minimum Duration (seconds):", value=0, step=15, key="exp_dur_input")

    # Initialize session state to keep query results persistent across reruns
    if "query_result_df" not in st.session_state:
        st.session_state.query_result_df = None

    if st.button("Execute Advanced Query", key="execute_advanced_query_btn"):
        st.session_state.query_result_df = query_calls(
            work_df,
            publisher=exp_pub if exp_pub else None,
            buyer=exp_buy if exp_buy else None,
            campaign=exp_cmp if exp_cmp else None,
            insurance_query=exp_ins if exp_ins else None,
            line_type=exp_line if exp_line else None,
            min_duration=exp_dur if exp_dur > 0 else None,
        )

    # Render results from session state safely
    if st.session_state.query_result_df is not None:
        query_result_df = st.session_state.query_result_df
        st.success(f"Query Engine matched **{len(query_result_df):,}** calls.")
        
        if not query_result_df.empty:
            st.dataframe(query_result_df.head(100), use_container_width=True)
            st.download_button(
                label="📥 Download Query Results as CSV",
                data=query_result_df.to_csv(index=False).encode("utf-8"),
                file_name="query_layer_results.csv",
                mime="text/csv",
                key="download_query_layer_csv",
            )

# Render secondary explorer with safe fallback defaults
render_query_layer_explorer(
    base_df=compare_base if 'compare_base' in locals() and compare_base is not None else work_df,
    timeline=timeline if 'timeline' in locals() else None,
    qc_col=selected_qc_col if 'selected_qc_col' in locals() else None,
    voip_col=selected_voip_col if 'selected_voip_col' in locals() else None,
    date_col=date_col_name if 'date_cols' in locals() and date_cols else None,
    health_rules=HEALTH_RULES if 'HEALTH_RULES' in locals() else {},
)
