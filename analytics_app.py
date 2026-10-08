import re
from dataclasses import dataclass, field

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


def add_numeric_columns(frame, score_col, dur_col):
    """Quality_Score_Num and Duration_Num, used by the dashboard AND the Query Layer so both
    always read the sheet the same way. A missing column gives NaN (never 0)."""
    if score_col:
        frame["Quality_Score_Num"] = pd.to_numeric(
            frame[score_col].astype(str).str.extract(r"(-?\d+\.?\d*)")[0], errors="coerce"
        )
    else:
        frame["Quality_Score_Num"] = float("nan")
    if dur_col:
        frame["Duration_Num"] = frame[dur_col].apply(parse_duration)
    else:
        frame["Duration_Num"] = float("nan")
    return frame


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
# Step 5A: Analytics Query Layer
# Deterministic Python / pandas only (no AI, no LLM). Every function works on ANY field or
# combination of fields from the A-O sheet columns, never on Publisher alone.
#
#   prepare_query_frame()      one-time preparation of the sheet data (dates, numbers, QC fields)
#   make_timeline()            "yesterday", "last week", custom dates ... (reuses the sidebar date logic)
#   filter_calls()             filter calls by any combination of fields, text and meaning
#   search_calls()             text search in the AI QC Report (primary), ShortSummary and Note
#   get_group_stats()          standard metrics for the network or ANY grouping (Buyer, Publisher + Campaign ...)
#   compare_group_periods()    selected period vs previous equivalent period, with trends and changes
#   rank_groups()              rank any metric (highest / lowest, biggest improvement / decline)
#   time_series()              daily / weekly / monthly breakdown
#   detect_anomalies()         transparent threshold rules (no AI)
#   insurance_breakdown(), repeat_callers()   examples of call-level intelligence
#   run_analytics_query()      one call: filter + stats + comparison + anomalies + daily series
#
# Reliability rule: nothing is estimated or guessed. When the data cannot answer a question the
# result carries UNAVAILABLE_MSG / INSUFFICIENT_MSG instead of a number.
# ---------------------------------------------------------------
UNAVAILABLE_MSG = "Information not available in the current data."
INSUFFICIENT_MSG = "Insufficient data for this query."

NONQUAL_PAT = r"CALL TYPE:\s*NON"
WRONG_PAT = r"CALL TYPE:\s*WRONG"
SILENT_PAT = r"CALL TYPE:\s*SILENT"
INFO_ONLY_PAT = r"CALL TYPE:\s*INFORMATION"
OTHER_TYPE_PAT = r"CALL TYPE:\s*OTHER"


@dataclass
class QueryResult:
    """Structured answer. `data` is a table, `scalars` the headline numbers, `notes` the caveats.
    `message` is set (and `available` is False) when the data cannot answer the question."""
    title: str = ""
    data: pd.DataFrame = None
    scalars: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    message: str = None
    extra: dict = field(default_factory=dict)

    @property
    def available(self):
        return self.message is None


def _unavailable(title, detail=None, message=UNAVAILABLE_MSG):
    return QueryResult(title=title, message=message + (f" ({detail})" if detail else ""))


# ---------------- Field mapping (A-O columns) ----------------
QUERY_FIELDS = {  # logical name: (exact header names, header keywords)
    "date": (["Call Date"], ["date"]),
    "buyer": (["Buyer"], ["buyer"]),
    "publisher": (["Publisher"], ["publisher"]),
    "campaign": (["Campaign"], ["campaign"]),
    "caller_id": (["Caller ID"], ["caller id", "caller_id"]),
    "duration": (["Duration"], ["duration"]),
    "note": (["Note"], ["note"]),
    "summary": (["ShortSummary"], ["summary"]),
    "recording": (["Recording"], ["recording"]),
    "hangup": (["Hangup By"], ["hangup", "hang up"]),
    "qc": (["AI QC Report"], ["ai qc", "qc report"]),
    "score": (["Quality Score"], ["score"]),
    "line_type": (["Line Type"], ["line type"]),
    "phone_company": (["Phone Company"], ["phone company", "carrier"]),
    "fake": (["Fake Number"], ["fake"]),
}
QUERY_LABELS = {
    "date": "Call Date", "buyer": "Buyer", "publisher": "Publisher", "campaign": "Campaign",
    "caller_id": "Caller ID", "duration": "Duration", "note": "Note", "summary": "ShortSummary",
    "recording": "Recording", "hangup": "Hangup By", "qc": "AI QC Report", "score": "Quality Score",
    "line_type": "Line Type", "phone_company": "Phone Company", "fake": "Fake Number",
}
GROUPABLE = ["buyer", "publisher", "campaign", "hangup", "line_type", "phone_company", "fake", "caller_id"]


def resolve_query_columns(available_columns, overrides=None):
    """Map the logical fields to the real sheet headers (None when a column does not exist)."""
    cols = {k: find_col(available_columns, exact, kws) for k, (exact, kws) in QUERY_FIELDS.items()}
    for k, v in (overrides or {}).items():
        if v and v != "None" and v in available_columns:
            cols[k] = v
    return cols


# ---------------- AI QC Report parsing ----------------
QC_PREFIX = "QC: "
QC_ALIASES = {"Why Called": "Why They Called", "Service Interest": "Treatment/Service Interest"}
_QC_KEY = re.compile(r"[A-Za-z][A-Za-z /&()'-]*")
_NOT_AVAILABLE = {
    "", "n/a", "na", "none", "unknown", "not mentioned", "not provided", "not specified", "unspecified",
    "not available", "no information", "null", "nan", "-", "--", "not stated", "not discussed",
}


def is_unavailable(value):
    """True for blank / 'N/A' / 'None' / 'Unknown' style values: they carry no information."""
    return str(value).strip().lower().rstrip(".") in _NOT_AVAILABLE


def parse_qc_text(text):
    """'Call Type: X | Insurance: Y | ...' -> {'Call Type': 'X', 'Insurance': 'Y', ...}.
    A part without a 'Key:' prefix continues the previous value."""
    out, last = {}, None
    for part in str(text).split("|"):
        key, sep, val = part.partition(":")
        k = key.strip()
        if sep and 0 < len(k) <= 40 and _QC_KEY.fullmatch(k):
            canon = QC_ALIASES.get(k, k)
            if canon in out:
                last = None
            else:
                out[canon] = val.strip()
                last = canon
        elif last is not None and part.strip():
            out[last] += " | " + part.strip()
    return out


FAKE_YES_VALUES = {"yes", "y", "true", "1", "fake", "detected", "detected fake", "fake detected", "fake number"}
FAKE_NO_VALUES = {"no", "n", "false", "0", "not fake", "not a fake"}


def prepare_query_frame(df, cols):
    """Add the helper columns every query uses (Parsed_Date, Quality_Score_Num, Duration_Num,
    Caller_ID_Norm, Fake_Value / Fake_Flag / Fake_Known and one 'QC: <field>' column per AI QC field).
    The sheet columns themselves are never changed."""
    q = df.copy()
    if cols.get("date"):
        q["Parsed_Date"] = parse_dates(q[cols["date"]])
    else:
        q["Parsed_Date"] = pd.Series(pd.NaT, index=q.index, dtype="datetime64[ns]")
    q = add_numeric_columns(q, cols.get("score"), cols.get("duration"))
    if cols.get("caller_id"):
        digits = q[cols["caller_id"]].astype(str).str.strip().str.replace(r"\.0+$", "", regex=True)
        q["Caller_ID_Norm"] = digits.str.replace(r"\D", "", regex=True)
    else:
        q["Caller_ID_Norm"] = ""
    # Fake Number: only an explicit Yes or No counts. Blank / missing / unreadable = Unknown (never No).
    if cols.get("fake"):
        raw = q[cols["fake"]].astype(str).str.strip().str.lower()
        q["Fake_Value"] = "Unknown"
        q.loc[raw.isin(FAKE_NO_VALUES), "Fake_Value"] = "No"
        q.loc[raw.isin(FAKE_YES_VALUES), "Fake_Value"] = "Yes"
    else:
        q["Fake_Value"] = "Unknown"
    q["Fake_Known"] = q["Fake_Value"] != "Unknown"
    q["Fake_Flag"] = q["Fake_Value"] == "Yes"
    if cols.get("qc"):
        parsed = pd.DataFrame([parse_qc_text(t) for t in q[cols["qc"]]], index=q.index)
        for c in parsed.columns:
            q[QC_PREFIX + c] = parsed[c].fillna("")
    return q


def apply_timeline(frame, timeline):
    """Rows inside the timeline (both end dates included). All time / None keeps every row,
    including rows with no readable date."""
    if not timeline or timeline.get("start") is None or timeline.get("end") is None:
        return frame
    return slice_period(frame, timeline["start"], timeline["end"])


def make_timeline(preset, today=None, start=None, end=None, tz=DEFAULT_TIMEZONE):
    """Timeline dict {'preset','start','end'} from a preset name (case-insensitive), reusing the
    same date logic as the sidebar. Custom needs start and end. All time has no dates."""
    names = {p.lower(): p for p in DATE_PRESETS}
    names.update({"custom": "Custom date range", "alltime": "All time"})
    key = str(preset).strip().lower()
    if key not in names:
        raise ValueError(f"Unknown timeline '{preset}'")
    name = names[key]
    if name == "All time":
        return {"preset": name}
    if name == "Custom date range":
        if start is None or end is None:
            raise ValueError("A custom timeline needs a start and an end date")
        return {"preset": name, "start": start, "end": end}
    s, e = date_filter_range(name, today or get_today(tz)[0])
    return {"preset": name, "start": s, "end": e}


# ---------------- Text search that never guesses ----------------
_NEGATION = re.compile(
    r"(?:\bno|\bnot|\bwithout|\bnever|n't)\s+"
    r"(?:(?!because\b|but\b|so\b|due\b|since\b|as\b)\w+\s+){0,2}$"
)


def _norm_text(s, lower=True):
    s = re.sub("[‐-―−]", "-", str(s))
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower() if lower else s


def _source_text(frame, cols, source, lower=True):
    """Text of one search source: 'qc' (whole AI QC Report), 'qc:<Field>' (one parsed field),
    'summary' or 'note'. None when that source does not exist in the data."""
    if source == "qc":
        col = cols.get("qc")
    elif source.startswith("qc:"):
        col = QC_PREFIX + source[3:]
        if col not in frame.columns:
            return None
    else:
        col = cols.get(source)
    if not col or col not in frame.columns:
        return None
    return frame[col].map(lambda v: _norm_text(v, lower))


def _term_hits(texts, pattern, flags=re.IGNORECASE, negation=True):
    """Boolean Series: the pattern is found and is not negated ('no Medicaid', 'does not have ...')."""
    rx = re.compile(pattern, flags)

    def hit(t):
        for m in rx.finditer(t):
            if negation and _NEGATION.search(t[max(0, m.start() - 30):m.start()]):
                continue
            return True
        return False

    return texts.map(hit).astype(bool)


def _first_hits(frame, cols, steps):
    """steps = [(source, regex, lower_case, negation)] in priority order. Returns
    (hit, matched_in, has_info): has_info = at least one searched source holds real text."""
    hit = pd.Series(False, index=frame.index)
    where = pd.Series("", index=frame.index, dtype=object)
    info = pd.Series(False, index=frame.index)
    for source, pattern, lower, negation in steps:
        texts = _source_text(frame, cols, source, lower)
        if texts is None:
            continue
        info |= ~texts.map(is_unavailable)
        found = _term_hits(texts, pattern, 0 if not lower else re.IGNORECASE, negation) & ~hit
        hit |= found
        where[found] = source
    return hit, where, info


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _qc_sources(frame, words):
    """'qc:<Field>' sources for every parsed AI QC field whose name contains one of the words, in sheet
    order. This keeps the Query Layer independent of any single campaign's QC template (a rehab
    report has 'Insurance' / 'Treatment/Service Interest', another campaign may name its fields
    differently or not have them at all)."""
    names = [c[len(QC_PREFIX):] for c in frame.columns if c.startswith(QC_PREFIX)]
    return ["qc:" + n for n in names if any(w in n.lower() for w in words)]


INSURANCE_FIELD_WORDS = ("insurance", "coverage", "payer")
LOCATION_FIELD_WORDS = ("location", "state", "city", "zip", "area", "region", "address")
SERVICE_FIELD_WORDS = ("service", "treatment", "interest", "product", "topic", "intent", "why")


def _terms_regex(terms, regex=False):
    """Search terms (a list, or one string where a comma means 'any of') as one regex."""
    if isinstance(terms, str):
        terms = terms.split(",")
    terms = [str(t).strip() for t in _as_list(terms) if str(t).strip()]
    if not terms:
        return None
    return "|".join(terms if regex else (re.escape(_norm_text(t)) for t in terms))


SEARCH_SOURCES = ("qc", "summary", "note")


def search_calls(frame, cols, terms, sources=SEARCH_SOURCES, regex=False, title="Call search"):
    """Calls whose text contains ANY of the terms. The AI QC Report is searched first, then
    ShortSummary, then Note. 'Matched In' shows which source answered. Negated mentions
    ('no Medicaid') are not counted. Calls with no usable text in any source cannot answer and
    are reported separately, never counted as 'no match'."""
    pattern = _terms_regex(terms, regex)
    if pattern is None:
        return _unavailable(title, "no search term given", INSUFFICIENT_MSG)
    steps = [(s, pattern, True, True) for s in sources]
    if all(_source_text(frame, cols, s) is None for s in sources):
        return _unavailable(title, "none of the searched columns exist")
    hit, where, info = _first_hits(frame, cols, steps)
    out = frame[hit].copy()
    out["Matched In"] = where[hit].map(lambda s: {"qc": "AI QC Report", "summary": "ShortSummary",
                                                  "note": "Note"}.get(s, s.replace("qc:", "AI QC: ")))
    res = QueryResult(title=title, data=out)
    res.scalars = {"Calls searched": len(frame), "Matching calls": int(hit.sum()),
                   "Calls with searchable text": int(info.sum())}
    if len(frame) - int(info.sum()):
        res.notes.append(
            f"{len(frame) - int(info.sum()):,} calls have no usable text in the searched sources, "
            "so they could not be checked."
        )
    return res


# ---------------- Insurance / location / service (meaning-based, explicit terms only) ----------------
# ---------------------------------------------------------------------------------------------
# INSURANCE RULES  (owner's STRICT rule - do not loosen or override):
#   * An insurer NAME alone (Aetna, Cigna, Blue Cross Blue Shield, United Healthcare ...) is NEVER
#     counted as private insurance. The same name can be a Medicaid / state / marketplace plan.
#   * A call is PRIVATE only when the call data explicitly says private / commercial insurance.
#   * The decision is made only after reading ShortSummary, Note and the AI QC Report together.
#   * Medi-Cal is the same type as Medicaid.
#   * Nothing is inferred; no explicit term means "Type Not Stated" or "Insurer Named".
# ---------------------------------------------------------------------------------------------
INSURANCE_TYPE_PATTERNS = {
    "medicaid": r"medicaid|medi[- ]cal\b",
    "medicare": r"medicare",
    "marketplace": r"marketplace|obamacare|affordable care act|healthcare\.gov|\baca\b",
    "public_general": (
        r"public (?:health )?insurance"
        r"|government(?:-| )(?:funded |sponsored |run )?(?:health )?(?:insurance|plan)"
        r"|state(?:-| )(?:funded |run |sponsored )?(?:health )?(?:insurance|plan)"
    ),
    "private": r"private (?:health )?(?:insurance|plan)|commercial (?:health )?(?:insurance|plan)",
    "none": (
        r"\b(?:no|without) (?:any |health )?insurance\b"
        r"|\b(?:doesn'?t|does not|don'?t|do not) have (?:any |health )?insurance\b|\buninsured\b|\bno coverage\b"
    ),
}
# Named insurers: they say WHO the carrier is, not WHICH kind of plan (so they never mean 'private' by themselves).
INSURER_NAME_PATTERN = (
    r"blue ?cross|blue ?shield|\bbcbs\b|aetna|cigna|united ?health ?care|\buhc\b|anthem|humana|kaiser"
    r"|molina|wellcare|amerigroup|ambetter|health ?net|highmark|premera|regence|oscar health"
)
INSURANCE_PUBLIC_TYPES = ("medicaid", "medicare", "marketplace", "public_general")
# Only inside the dedicated insurance field, a bare 'public' / 'government' is explicit enough.
INSURANCE_FIELD_PUBLIC = r"\bpublic\b|\bgovernment\b"
INSURANCE_LABELS = {
    "public": "Public (Medicaid / Medi-Cal / Medicare / marketplace / state / government)",
    "private": "Private (explicitly stated private / commercial insurance)",
    "insurer_named": "Insurer named, plan type not confirmed (not counted as private)",
    "none": "No insurance",
    "mixed": "Mixed (more than one explicit insurance type)",
    "unspecified": "Type Not Stated (insurance mentioned, no explicit type)",
    "unknown": "No insurance information",
}
INSURANCE_TYPE_LABELS = {
    "medicaid": "Medicaid / Medi-Cal", "medicare": "Medicare", "marketplace": "Marketplace (ACA)",
    "public_general": "Public / Government", "private": "Private insurance (explicitly stated)",
    "none": "No insurance",
}
INSURER_NAMED_LABEL = "Insurer Named - Plan Type Not Confirmed"


def classify_insurance(frame, cols):
    """One row per call, from EXPLICIT terms only (never inferred), reading the QC insurance field(s),
    the whole AI QC Report, ShortSummary and Note TOGETHER:

      Insurance_Type       Medicaid / Medi-Cal / Medicare / Marketplace (ACA) / Public / Government /
                           Private insurance (explicitly stated) / No insurance / Mixed /
                           Insurer Named - Plan Type Not Confirmed / Type Not Stated
      Insurance_Category   public / private / insurer_named / none / mixed / unspecified / unknown
      Insurance_Source     where the first explicit term was found
      Has_Medicaid / Has_Medicare / Has_Marketplace / Has_Public / Has_Private / Has_None / Has_Insurer_Name

    Rules (owner's strict rule, see INSURANCE RULES above):
      * 'Aetna', 'Cigna', 'Blue Cross Blue Shield', 'United Healthcare' ... only name a carrier. They are
        NOT private. With an explicit public program in the call data (Medicaid, Medi-Cal, Medicare,
        state / public / government insurance, marketplace) the call takes that program; with an explicit
        'private' / 'commercial' statement it is Private; with neither it stays 'Insurer Named'.
      * Two different explicit types (Medicaid + Medicare, Medicaid + explicit private ...) are Mixed.
        A generic 'public insurance' / 'marketplace' mention adds nothing once a more specific program
        is named. A carrier name never creates 'Mixed'.
      * 'Discussed insurance' or just 'state' is Type Not Stated. Negated mentions ('no Medicaid') are ignored.
    Works for any campaign: fields are found by name, and the full text is always searched."""
    idx = frame.index
    ins_fields = _qc_sources(frame, INSURANCE_FIELD_WORDS)
    found = {k: pd.Series(False, index=idx) for k in INSURANCE_TYPE_PATTERNS}
    insurer = pd.Series(False, index=idx)
    source = pd.Series("", index=idx, dtype=object)
    has_field_text = pd.Series(False, index=idx)
    for src in ins_fields + ["qc", "summary", "note"]:
        texts = _source_text(frame, cols, src)
        if texts is None:
            continue
        is_ins_field = src in ins_fields
        if is_ins_field:
            has_field_text |= ~texts.map(is_unavailable).astype(bool)
        any_hit = pd.Series(False, index=idx)
        for kind, pat in INSURANCE_TYPE_PATTERNS.items():
            if is_ins_field and kind == "public_general":
                pat = pat + "|" + INSURANCE_FIELD_PUBLIC
            hits = _term_hits(texts, pat, negation=(kind != "none"))
            found[kind] |= hits
            any_hit |= hits
        named = _term_hits(texts, INSURER_NAME_PATTERN)
        insurer |= named
        any_hit |= named
        source[any_hit & (source == "")] = src
    specific = found["medicaid"] | found["medicare"]
    found["marketplace"] = found["marketplace"] & ~specific
    found["public_general"] = found["public_general"] & ~(specific | found["marketplace"])
    kinds = list(INSURANCE_TYPE_PATTERNS)
    n_types = sum(found[k].astype(int) for k in kinds)
    mixed = n_types > 1
    cat = pd.Series("unknown", index=idx, dtype=object)
    cat[has_field_text] = "unspecified"
    typ = pd.Series("Type Not Stated", index=idx, dtype=object)
    only_insurer = insurer & (n_types == 0)
    typ[only_insurer] = INSURER_NAMED_LABEL
    cat[only_insurer] = "insurer_named"
    for k in kinds:
        only = found[k] & (n_types == 1)
        typ[only] = INSURANCE_TYPE_LABELS[k]
        cat[only] = "public" if k in INSURANCE_PUBLIC_TYPES else k
    typ[mixed] = "Mixed"
    cat[mixed] = "mixed"
    out = pd.DataFrame({"Insurance_Type": typ, "Insurance_Category": cat, "Insurance_Source": source}, index=idx)
    out["Has_Medicaid"] = found["medicaid"]
    out["Has_Medicare"] = found["medicare"]
    out["Has_Marketplace"] = found["marketplace"]
    out["Has_Public"] = found["medicaid"] | found["medicare"] | found["marketplace"] | found["public_general"]
    out["Has_Private"] = found["private"]
    out["Has_None"] = found["none"]
    out["Has_Insurer_Name"] = insurer
    return out


def _insurance_term_regex(term):
    """Pattern for a named insurance term. Medicaid and Medi-Cal are the same type, so either word finds both."""
    t = _norm_text(term)
    if t in ("medicaid", "medi-cal", "medi cal", "medicaid / medi-cal"):
        return INSURANCE_TYPE_PATTERNS["medicaid"]
    return re.escape(t)


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def _place_steps(place, frame):
    """Search steps for a place: the QC location-type fields first (state abbreviations such as 'CA'
    count only there, upper-case), then the rest of the AI QC Report, ShortSummary and Note
    (full names only)."""
    p = _norm_text(place)
    name = r"\b" + re.escape(p) + r"\b"
    fields = _qc_sources(frame, LOCATION_FIELD_WORDS)
    steps = [(f, name, True, True) for f in fields]
    if p in US_STATES:
        steps += [(f, r"(?<![A-Za-z])" + US_STATES[p] + r"(?![A-Za-z])", False, False) for f in fields]
    steps += [("qc", name, True, True), ("summary", name, True, True), ("note", name, True, True)]
    return steps


def _service_steps(terms, frame):
    """Service / treatment / product interest: the QC fields that describe it (any campaign), then the
    whole AI QC Report, ShortSummary and Note."""
    pat = _terms_regex(terms)
    return ([(f, pat, True, True) for f in _qc_sources(frame, SERVICE_FIELD_WORDS)]
            + [("qc", pat, True, True), ("summary", pat, True, True), ("note", pat, True, True)])


# ---------------- filter_calls ----------------
CALL_TYPE_PATTERNS = {
    "qualified": QUAL_PAT, "non-qualified": NONQUAL_PAT, "spam": SPAM_PAT, "wrong number": WRONG_PAT,
    "silent": SILENT_PAT, "information only": INFO_ONLY_PAT, "other": OTHER_TYPE_PAT,
}
FILTER_COLUMN_KEYS = ("buyer", "publisher", "campaign", "hangup", "line_type", "phone_company")


def _isin_ci(series, values):
    s = series.astype(str).str.strip().str.lower().replace("", "unknown")
    return s.isin({str(v).strip().lower() for v in values})


def describe_query(spec, matched=None, before=None):
    """One plain-language line for the user: which filters were applied, which period, how many calls matched.
    Built only from the spec, so it always matches what was actually filtered."""
    spec = spec or {}
    tl = spec.get("timeline")
    if not tl:
        period = "all calls in the data (no period filter)"
    elif tl.get("start") is None:
        period = "All time (no comparison period exists)"
    else:
        period = f"{tl.get('preset', 'Period')}: {pd.Timestamp(tl['start']).strftime('%b %d, %Y')} to {pd.Timestamp(tl['end']).strftime('%b %d, %Y')}"
    labels = {"buyer": "Buyer", "publisher": "Publisher", "campaign": "Campaign", "hangup": "Hangup By",
              "line_type": "Line Type", "phone_company": "Phone Company", "fake_number": "Fake Number",
              "caller_id": "Caller ID contains", "recording": "Recording", "call_type": "Call type",
              "qc_text": "AI QC Report contains", "summary_text": "ShortSummary contains", "note_text": "Note contains",
              "any_text": "Anywhere contains", "insurance": "Insurance", "location": "Location", "service": "Service / treatment",
              "duration_min": "Duration >= (sec)", "duration_max": "Duration <= (sec)",
              "score_min": "Quality Score >=", "score_max": "Quality Score <="}
    parts = []
    for k, lab in labels.items():
        v = spec.get(k)
        if v is None or v == "" or v == []:
            continue
        v = ", ".join(map(str, v)) if isinstance(v, (list, tuple, set)) else v
        parts.append(f"{lab} = {v}")
    out = f"Period: {period}. Filters: " + ("; ".join(parts) if parts else "none") + "."
    if matched is not None:
        out += f" Matched {matched:,}" + (f" of {before:,} calls in the data." if before is not None else " calls.")
    return out


def filter_calls(frame, cols, spec=None, title="Matching calls"):
    """Filter calls by any combination of these spec keys (all optional, all combined with AND):

      timeline            dict from make_timeline()
      buyer, publisher, campaign, hangup, line_type, phone_company     value or list of values
      caller_id           digits (or part of them)
      recording           True (has a recording link) / False (no link) / text contained in the link
      duration_min / duration_max      seconds
      score_min / score_max            quality score
      fake_number         True (explicit Yes) / False (explicit No) / 'unknown' (blank or unreadable)
      call_type           qualified | non-qualified | spam | wrong number | silent | information only | other
      qc_text, summary_text, note_text   text contained in that column (comma = any of)
      any_text            text found in the AI QC Report, ShortSummary or Note
      insurance           public | private | none | mixed | unspecified | any named insurer ('Aetna')
      location            place name (state name, city ...)
      service             treatment / service words ('inpatient', 'detox')

    Returns a QueryResult whose `data` holds the matching rows. A filter that needs a column which
    does not exist makes the result 'not available' instead of silently ignoring it."""
    spec = spec or {}
    f = frame
    notes = []
    scalars = {"Calls before filters": len(frame)}

    def need(key):
        return None if cols.get(key) else QUERY_LABELS[key]

    if spec.get("timeline"):
        f = apply_timeline(f, spec["timeline"])
        scalars["Calls in timeline"] = len(f)

    for key in FILTER_COLUMN_KEYS:
        vals = _as_list(spec.get(key))
        if vals:
            if need(key):
                return _unavailable(title, f"column '{QUERY_LABELS[key]}' not found")
            f = f[_isin_ci(f[cols[key]], vals)]

    if spec.get("fake_number") is not None:
        if need("fake"):
            return _unavailable(title, "column 'Fake Number' not found")
        want = spec["fake_number"]
        want = want.strip().lower() if isinstance(want, str) else want
        if want is True or want in ("yes", "true"):
            f = f[f["Fake_Flag"]]
        elif want is False or want in ("no", "false"):
            f = f[f["Fake_Known"] & ~f["Fake_Flag"]]
            notes.append("'Fake Number = No' counts only calls where the column explicitly says No; "
                         "blank or unreadable values are Unknown and are not included.")
        elif want in ("unknown", "blank", "not checked"):
            f = f[~f["Fake_Known"]]
            notes.append("'Fake Number = Unknown' means the column is blank or not readable (not the same as No).")
        else:
            return _unavailable(title, f"unknown Fake Number value '{spec['fake_number']}'", INSUFFICIENT_MSG)

    rec = spec.get("recording")
    if rec is not None and rec != "":
        if need("recording"):
            return _unavailable(title, "column 'Recording' not found")
        rec_txt = f[cols["recording"]].fillna("").astype(str).str.strip()
        has_rec = rec_txt.ne("") & ~rec_txt.str.lower().isin(["nan", "none", "n/a", "-"])
        if rec is True or str(rec).strip().lower() in ("yes", "has", "with"):
            f = f[has_rec]
        elif rec is False or str(rec).strip().lower() in ("no", "none", "without", "missing"):
            f = f[~has_rec]
        else:
            f = f[has_rec & rec_txt.str.contains(str(rec).strip(), case=False, regex=False)]

    cid = str(spec.get("caller_id") or "").strip()
    if cid:
        if need("caller_id"):
            return _unavailable(title, "column 'Caller ID' not found")
        digits = re.sub(r"\D", "", re.sub(r"\.0+$", "", cid))
        if digits:
            f = f[f["Caller_ID_Norm"].str.contains(digits, regex=False)]
        else:
            f = f[f[cols["caller_id"]].astype(str).str.contains(cid, case=False, regex=False)]

    for key, col_key, num_col, label, op in (
        ("duration_min", "duration", "Duration_Num", "Duration", ">="),
        ("duration_max", "duration", "Duration_Num", "Duration", "<="),
        ("score_min", "score", "Quality_Score_Num", "Quality Score", ">="),
        ("score_max", "score", "Quality_Score_Num", "Quality Score", "<="),
    ):
        if spec.get(key) is not None:
            if need(col_key):
                return _unavailable(title, f"column '{QUERY_LABELS[col_key]}' not found")
            known = f[num_col].notna()
            if (~known).any():
                notes.append(f"{int((~known).sum()):,} calls without a {label} value were left out of the {label} filter.")
            f = f[known & ((f[num_col] >= spec[key]) if op == ">=" else (f[num_col] <= spec[key]))]

    ctype = str(spec.get("call_type") or "").strip().lower()
    if ctype:
        if ctype == "voip":
            if need("line_type"):
                return _unavailable(title, "column 'Line Type' not found")
            f = f[f[cols["line_type"]].astype(str).str.contains(VOIP_PAT, case=False, regex=True)]
        elif ctype in CALL_TYPE_PATTERNS:
            if need("qc"):
                return _unavailable(title, "column 'AI QC Report' not found")
            f = f[f[cols["qc"]].astype(str).str.contains(CALL_TYPE_PATTERNS[ctype], case=False, regex=True)]
        else:
            return _unavailable(title, f"unknown call type '{ctype}'", INSUFFICIENT_MSG)

    for key, col_key in (("qc_text", "qc"), ("summary_text", "summary"), ("note_text", "note")):
        txt = str(spec.get(key) or "").strip()
        if txt:
            if need(col_key):
                return _unavailable(title, f"column '{QUERY_LABELS[col_key]}' not found")
            parts = [p.strip() for p in txt.split(",") if p.strip()]
            f = f[f[cols[col_key]].astype(str).map(lambda t: any(p.lower() in t.lower() for p in parts))]

    if spec.get("any_text"):
        sub = search_calls(f, cols, spec["any_text"], title=title)
        if not sub.available:
            return sub
        f = sub.data.drop(columns=["Matched In"])
        notes += sub.notes

    # ---- meaning-based filters: only calls whose data actually answers the question ----
    ins = spec.get("insurance")
    if ins:
        key = str(ins).strip().lower()
        if key in INSURANCE_LABELS:
            cls = classify_insurance(f, cols)
            known = cls["Insurance_Category"] != "unknown"
            if not known.any():
                return _unavailable(title, "no insurance information in these calls")
            notes.append(
                f"Insurance information exists for {int(known.sum()):,} of {len(f):,} calls; the other "
                f"{int((~known).sum()):,} are left out of this insurance filter."
            )
            if key in ("public", "private", "none"):   # includes Mixed calls that explicitly have that type
                match = cls[{"public": "Has_Public", "private": "Has_Private", "none": "Has_None"}[key]]
            else:
                match = cls["Insurance_Category"] == key
            f = f[match]
        else:
            pat = _insurance_term_regex(ins)
            steps = [(x, pat, True, True) for x in _qc_sources(f, INSURANCE_FIELD_WORDS) + ["qc", "summary", "note"]]
            hit, _, info = _first_hits(f, cols, steps)
            if not info.any():
                return _unavailable(title, "no insurance information in these calls")
            notes.append("A named insurer is only searched as text; it is not classified as private or public.")
            f = f[hit]
    loc = str(spec.get("location") or "").strip()
    if loc:
        hit, _, info = _first_hits(f, cols, _place_steps(loc, f))
        if not info.any():
            return _unavailable(title, "no location information in these calls")
        notes.append(f"{int(info.sum()):,} of {len(f):,} calls have text in the searched sources (AI QC Report, ShortSummary, Note).")
        f = f[hit]
    svc = _terms_regex(spec.get("service"))
    if svc:
        hit, _, info = _first_hits(f, cols, _service_steps(spec.get("service"), f))
        if not info.any():
            return _unavailable(title, "no treatment / service information in these calls")
        f = f[hit]

    scalars["Matching calls"] = len(f)
    notes.insert(0, describe_query(spec, len(f), len(frame)))
    res = QueryResult(title=title, data=f, scalars=scalars, notes=notes)
    if f.empty:
        res.notes.append("No calls match these filters.")
    return res


# ---------------- Grouping and standard statistics ----------------
STAT_COLUMNS = [
    "Calls", "Qualified", "Non-Qualified", "Spam", "VoIP", "Wrong Number", "Silent", "Fake Numbers",
    "Fake Unknown", "Avg Score", "Avg Duration (sec)", "Qualification %", "Spam %", "VoIP %", "Fake %",
    "QC Completion %", "QC Done",
]
RANK_METRICS = [
    "Calls", "Qualified", "Non-Qualified", "Spam", "VoIP", "Wrong Number", "Silent", "Fake Numbers",
    "Avg Score", "Avg Duration (sec)", "Qualification %", "Spam %", "VoIP %", "Fake %", "QC Completion %",
]
RATE_METRICS = {"Avg Score", "Avg Duration (sec)", "Qualification %", "Spam %", "VoIP %", "Fake %", "QC Completion %"}


def _with_group_columns(frame, cols, by):
    """(frame copy with clean group columns, group column names, missing field labels).
    `by` items: a logical field ('publisher'), a sheet header, or day / week / month.
    Blank values become 'Unknown'. An empty `by` means the whole network."""
    f = frame.copy()
    gcols, missing = [], []
    for item in _as_list(by):
        key = str(item).strip()
        low = key.lower()
        if low in ("day", "week", "month"):
            d = f["Parsed_Date"].dt.normalize()
            if low == "week":
                d = d - pd.to_timedelta(d.dt.weekday, unit="D")
            elif low == "month":
                d = d.dt.to_period("M").dt.to_timestamp()
            name = low.capitalize()
            f[name] = d.dt.strftime("%Y-%m-%d")
        elif low in cols:
            if not cols[low]:
                missing.append(QUERY_LABELS[low])
                continue
            name = "Caller_ID_Norm" if low == "caller_id" else cols[low]
            if low == "fake":
                f[name] = f["Fake_Value"]   # Yes / No / Unknown, never blank-as-No
        elif key in f.columns:
            name = key
        else:
            missing.append(key)
            continue
        if name not in gcols:
            gcols.append(name)
    f = normalize_groups(f, gcols) if gcols else f
    if not gcols and not missing:
        f["Network"] = "All calls"
        gcols = ["Network"]
    return f, gcols, missing


def _stats_indexed(frame, cols, gcols):
    """Standard metrics per group, indexed by the group columns (reuses period_stats /
    add_percentages, so the numbers equal the dashboard's)."""
    qc = cols.get("qc") or "None"
    vo = cols.get("line_type") or "None"
    stats = add_percentages(period_stats(frame, gcols, qc, vo))
    t = frame[gcols].copy()
    qcs = frame[qc].astype(str) if qc != "None" else pd.Series("", index=frame.index)
    t["_nq"] = qcs.str.contains(NONQUAL_PAT, case=False, na=False, regex=True)
    t["_wn"] = qcs.str.contains(WRONG_PAT, case=False, na=False, regex=True)
    t["_si"] = qcs.str.contains(SILENT_PAT, case=False, na=False, regex=True)
    t["_fk"] = frame["Fake_Flag"]
    t["_fc"] = frame["Fake_Known"]
    extra = t.groupby(gcols).sum()
    stats["Non-Qualified"] = extra["_nq"]
    stats["Wrong Number"] = extra["_wn"]
    stats["Silent"] = extra["_si"]
    if cols.get("fake"):
        stats["Fake Numbers"] = extra["_fk"]
        stats["Fake Checked"] = extra["_fc"]
        stats["Fake Unknown"] = stats["Calls"] - stats["Fake Checked"]   # blank / unreadable: neither Yes nor No
        stats["Fake %"] = safe_pct(stats["Fake Numbers"], stats["Fake Checked"].where(stats["Fake Checked"] > 0))
    else:
        stats["Fake Numbers"] = float("nan")
        stats["Fake Checked"] = float("nan")
        stats["Fake Unknown"] = float("nan")
        stats["Fake %"] = float("nan")
    return stats


def _round(df):
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].round(1)
    return out


def get_group_stats(frame, cols, by=None, health=True, title="Statistics"):
    """The standard metrics for the network (by=None) or ANY grouping: ['buyer'], ['publisher',
    'campaign'], ['phone_company'], ['hangup'], 'day' ... Same engine for every dimension.
    Percentages are a share of TOTAL calls. Fake % is a share of calls with an explicit Yes / No Fake Number;
    blank or unreadable values are 'Fake Unknown' and are never counted as No."""
    f, gcols, missing = _with_group_columns(frame, cols, by)
    if missing:
        return _unavailable(title, "column not found: " + ", ".join(missing))
    if f.empty:
        return QueryResult(title=title, data=pd.DataFrame(columns=gcols + STAT_COLUMNS),
                           scalars={"Calls": 0, "Qualified": 0, "Spam": 0, "VoIP": 0, "Avg Score": float("nan"), "QC Done": 0},
                           notes=["No calls match, so there is nothing to calculate."])
    stats = _stats_indexed(f, cols, gcols)
    if health:
        stats["Health"], stats["Health_Reason"] = health_columns(stats)
    out = stats.reset_index().sort_values("Calls", ascending=False).reset_index(drop=True)
    keep = gcols + STAT_COLUMNS + (["Health", "Health_Reason"] if health else [])
    out = _round(out[keep])
    res = QueryResult(title=title, data=out, scalars=period_kpis(f, cols.get("qc") or "None", cols.get("line_type") or "None"))
    n_qc = res.scalars["QC Done"]
    if cols.get("fake") and "Fake_Known" in f.columns and (~f["Fake_Known"]).any():
        res.notes.append(
            f"Fake Number is blank or unreadable for {int((~f['Fake_Known']).sum()):,} of {len(f):,} calls: "
            "that is Unknown, not No. Fake % is a share of the calls where it is Yes or No."
        )
    if cols.get("qc") and n_qc < len(f):
        res.notes.append(
            f"Qualified / Non-Qualified / Spam / Wrong Number / Silent are known only for calls with a "
            f"completed AI QC ({n_qc:,} of {len(f):,})."
        )
    return res


# ---------------- Period comparison ----------------
CHANGE_COLUMN = {
    "Calls": "Volume change", "Qualification %": "Qualification % change", "Spam %": "Spam % change",
    "VoIP %": "VoIP % change", "Avg Score": "Avg Score change", "Avg Duration (sec)": "Avg Duration change",
    "Fake %": "Fake % change",
}
TREND_LABELS = {"improving": "↑ Improving", "declining": "↓ Declining", "stable": "→ Stable", "n/a": "n/a"}


def _compare_core(frame, cols, by, timeline):
    """Shared by compare_group_periods() and detect_anomalies()."""
    if not timeline or timeline.get("start") is None or timeline.get("end") is None:
        return None, _unavailable("Period comparison", "All time has no comparison period")
    info = equivalent_previous(timeline["preset"], timeline["start"], timeline["end"])
    if info is None:
        return None, _unavailable("Period comparison", "no comparison period for this timeline")
    win = trend_windows(info, timeline["start"], timeline["end"], frame)
    cur = slice_window(frame, *win["cur"])
    prev = slice_window(frame, *win["prev"])
    cur_f, gcols, missing = _with_group_columns(cur, cols, by)
    if missing:
        return None, _unavailable("Period comparison", "column not found: " + ", ".join(missing))
    prev_f, _, _ = _with_group_columns(prev, cols, by)
    for g in (cur_f, prev_f):
        g["_group"] = g[gcols].astype(str).agg(" | ".join, axis=1) if len(g) else pd.Series(dtype=object)
    parts = pd.concat([cur_f[["_group"] + gcols], prev_f[["_group"] + gcols]]).drop_duplicates("_group").set_index("_group")
    core = {"info": info, "win": win, "gcols": gcols, "parts": parts, "cur_n": len(cur_f), "prev_n": len(prev_f)}
    if cur_f.empty and prev_f.empty:
        return core, _unavailable("Period comparison", "no calls in either period", INSUFFICIENT_MSG)
    empty = pd.DataFrame(columns=["Calls"])
    cur_stats = _stats_indexed(cur_f, cols, ["_group"]) if len(cur_f) else empty
    prev_stats = _stats_indexed(prev_f, cols, ["_group"]) if len(prev_f) else empty
    if cur_f.empty or prev_f.empty:
        core.update(cur_stats=cur_stats, prev_stats=prev_stats, records=[], order=[])
        return core, None
    records, order = build_trends(cur_stats, prev_stats)
    core.update(cur_stats=cur_stats, prev_stats=prev_stats, records=records, order=order)
    return core, None


def _pp(cur, prev):
    return float("nan") if pd.isna(cur) or pd.isna(prev) else cur - prev


def compare_group_periods(frame, cols, by=None, timeline=None, title="Period comparison"):
    """Selected period vs its previous equivalent period for the network or ANY grouping.
    Columns: now / before / change for volume, qualification, spam, VoIP, score, duration, fake,
    plus a trend arrow per metric (same rules and minimum-data guards as the dashboard trends)."""
    core, err = _compare_core(frame, cols, by, timeline)
    if err:
        err.title = title
        return err
    info, win, gcols, parts = core["info"], core["win"], core["gcols"], core["parts"]
    cur_s, prev_s = core["cur_stats"], core["prev_stats"]
    trend_of = {(r["group"], r["key"]): r["trend"] for r in core["records"]}
    groups = core["order"] or sorted(set(cur_s.index) | set(prev_s.index))
    cur_h = dict(zip(cur_s.index, health_columns(add_percentages(cur_s))[0])) if len(cur_s) and "QC Done" in cur_s else {}
    prev_h = dict(zip(prev_s.index, health_columns(add_percentages(prev_s))[0])) if len(prev_s) and "QC Done" in prev_s else {}
    rows = []
    for g in groups:
        c = cur_s.loc[g] if g in cur_s.index else None
        p = prev_s.loc[g] if g in prev_s.index else None
        val = lambda row, k: float("nan") if row is None else row[k]
        calls_c, calls_p = (0 if c is None else c["Calls"]), (0 if p is None else p["Calls"])
        row = {name: parts.loc[g, name] for name in gcols}
        row.update({
            "Calls (now)": int(calls_c), "Calls (before)": int(calls_p), "Volume change": int(calls_c - calls_p),
            "Volume change %": (calls_c - calls_p) / calls_p * 100 if calls_p else float("nan"),
            "Qualified (now)": 0 if c is None else int(c["Qualified"]),
            "Qualified (before)": 0 if p is None else int(p["Qualified"]),
        })
        for key, label in (("Qualification %", "Qualification %"), ("Spam %", "Spam %"), ("VoIP %", "VoIP %"),
                           ("Fake %", "Fake %"), ("Avg Score", "Avg Score"), ("Avg Duration (sec)", "Avg Duration")):
            row[f"{label} (now)"], row[f"{label} (before)"] = val(c, key), val(p, key)
            row[CHANGE_COLUMN[key]] = _pp(val(c, key), val(p, key))
        for key, label, _ in TREND_METRICS:
            row[f"Trend: {label}"] = TREND_LABELS.get(trend_of.get((g, key), "n/a"), "n/a")
        row["Enough data"] = "Yes" if min(calls_c, calls_p) >= TREND_MIN_CALLS else f"No (< {TREND_MIN_CALLS} calls in a period)"
        row["Health (now)"] = cur_h.get(g, "–")
        row["Health (before)"] = prev_h.get(g, "–")
        rows.append(row)
    out = _round(pd.DataFrame(rows))
    res = QueryResult(title=title, data=out, extra=core)
    res.scalars = {
        "Current period": f"{info['cur_name']}: {describe_period(timeline['start'], timeline['end'])}"
                          f"{' (so far)' if win['in_progress'] else ''}",
        "Comparison period": f"{info['prev_name']}: {describe_window(*win['prev'])}"
                             f"{', same elapsed time' if win['trimmed'] else ''}",
        "Calls (now)": core["cur_n"], "Calls (before)": core["prev_n"],
    }
    if core["cur_n"] == 0 or core["prev_n"] == 0:
        res.notes.append("One of the two periods has no calls, so trends are not available.")
    first = frame["Parsed_Date"].min()
    if pd.notna(first) and first > win["prev"][0]:
        res.notes.append(f"The comparison period starts before the first call in the sheet ({first:%b %d, %Y}); it may be incomplete.")
    return res


def rank_groups(table, metric, n=10, ascending=False, min_calls=None, calls_cols=("Calls",), title=None):
    """Rank ANY table of groups by ANY numeric column (from get_group_stats() or
    compare_group_periods()). Rates and averages need a minimum number of calls (default
    TREND_MIN_CALLS) so a group with 2 calls can never be 'best'. Ties: more calls first."""
    title = title or f"Ranking by {metric}"
    if table is None or metric not in table.columns:
        return _unavailable(title, f"'{metric}' is not in the data")
    if min_calls is None:
        min_calls = TREND_MIN_CALLS if (metric in RATE_METRICS or metric.endswith("change") or metric.endswith("change %")) else 1
    df = table[pd.to_numeric(table[metric], errors="coerce").notna()]
    no_value = len(table) - len(df)
    keep = pd.Series(True, index=df.index)
    for c in calls_cols:
        if c in df.columns:
            keep &= df[c] >= min_calls
    left_out = int((~keep).sum())
    ranked = df[keep].copy()
    ranked[metric] = pd.to_numeric(ranked[metric])
    sort_cols = [metric] + [c for c in calls_cols if c in ranked.columns][:1]
    ranked = ranked.sort_values(sort_cols, ascending=[ascending] + [False] * (len(sort_cols) - 1), kind="mergesort")
    if ranked.empty:
        return _unavailable(title, "no group has enough data", INSUFFICIENT_MSG)
    ranked.insert(0, "Rank", range(1, len(ranked) + 1))
    res = QueryResult(title=title, data=ranked.head(n).reset_index(drop=True))
    if left_out:
        res.notes.append(f"{left_out} group(s) with fewer than {min_calls} calls were left out of this ranking.")
    if no_value:
        res.notes.append(f"{no_value} group(s) have no value for {metric} (not available in the data).")
    return res


def rank_improvement(comparison, key, improving=True, n=10):
    """Biggest improvement (or decline) of a metric between the two periods. 'Improvement' means
    better quality: a lower spam % / VoIP % is an improvement, a higher qualification % / score is."""
    if comparison is None or not comparison.available or key not in CHANGE_COLUMN:
        return _unavailable(f"Biggest {'improvement' if improving else 'decline'}", "no comparison available")
    good = {k: g for k, _, g in TREND_METRICS}.get(key, 1)
    tbl = comparison.data.copy()
    col = CHANGE_COLUMN[key]
    tbl["_quality_change"] = pd.to_numeric(tbl[col], errors="coerce") * good
    res = rank_groups(tbl, "_quality_change", n=n, ascending=not improving,
                      calls_cols=("Calls (now)", "Calls (before)"),
                      title=f"Biggest {'improvement' if improving else 'decline'} in {key}")
    if res.available:
        res.data = res.data.drop(columns=["_quality_change"])
    return res


# ---------------- Daily / weekly / monthly series ----------------
SERIES_COLUMNS = ["Calls", "Qualified", "Spam", "VoIP", "Avg Score", "Qualification %", "Spam %",
                  "VoIP %", "Avg Duration (sec)", "QC Completion %"]


def time_series(frame, cols, timeline=None, by=None, freq="day", title=None):
    """Daily (default), weekly (Monday start) or monthly breakdown of the standard metrics for
    the network or any grouping. Days without calls are shown with 0 calls and blank rates."""
    freq = str(freq).lower()
    title = title or f"{freq.capitalize()} breakdown"
    if freq not in ("day", "week", "month"):
        return _unavailable(title, f"unknown frequency '{freq}'", INSUFFICIENT_MSG)
    if not cols.get("date"):
        return _unavailable(title, "column 'Call Date' not found")
    cur = apply_timeline(frame, timeline)
    dated = cur[cur["Parsed_Date"].notna()]
    undated = len(cur) - len(dated)
    f, gcols, missing = _with_group_columns(dated, cols, by if _as_list(by) else None)
    if missing:
        return _unavailable(title, "column not found: " + ", ".join(missing))
    network = gcols == ["Network"]
    gcols = [] if network else gcols
    res = QueryResult(title=title)
    if undated:
        res.notes.append(f"{undated:,} calls have no readable date and are not in this series.")
    if f.empty:
        res.data = pd.DataFrame(columns=["Period"] + gcols + SERIES_COLUMNS)
        res.notes.append("No dated calls in this timeline.")
        return res
    d = f["Parsed_Date"].dt.normalize()
    if freq == "week":
        d = d - pd.to_timedelta(d.dt.weekday, unit="D")
    elif freq == "month":
        d = d.dt.to_period("M").dt.to_timestamp()
    f["Period"] = d
    stats = _stats_indexed(f, cols, ["Period"] + gcols)
    if network:
        step = {"day": "D", "week": "7D", "month": "MS"}[freq]
        lo, hi = d.min(), d.max()
        tl = timeline or {}
        if tl.get("start") is not None and tl.get("end") is not None:
            lo, hi = min(lo, pd.Timestamp(tl["start"])), max(hi, pd.Timestamp(tl["end"]))
            if freq == "week":
                lo = lo - pd.Timedelta(days=lo.weekday())
            elif freq == "month":
                lo = lo.to_period("M").to_timestamp()
        full = pd.date_range(lo, hi, freq=step)
        if len(full) <= 800:
            stats = stats.reindex(full)
            stats.index.name = "Period"
            for c in ("Calls", "Qualified", "Spam", "VoIP", "QC Done", "Non-Qualified", "Wrong Number", "Silent"):
                stats[c] = stats[c].fillna(0).astype(int)
    out = stats.reset_index()
    label = out["Period"].dt.strftime({"day": "%a %Y-%m-%d", "week": "Week of %Y-%m-%d", "month": "%Y-%m"}[freq])
    out.insert(0, "Label", label)
    out = _round(out[["Label", "Period"] + gcols + SERIES_COLUMNS])
    out["Period"] = out["Period"].dt.date
    res.data = out.sort_values(["Period"] + gcols).reset_index(drop=True)
    res.scalars = {"Periods": int(out["Period"].nunique()), "Calls": int(out["Calls"].sum())}
    return res


# ---------------- Anomaly detection ----------------
ANOMALY_LABELS = {
    "Spam %": "Sudden spam increase", "Qualification %": "Sudden qualification decline",
    "Avg Score": "Significant score decline", "Calls": "Unusual call-volume change",
    "VoIP %": "Significant VoIP increase", "Avg Duration (sec)": "Unusual duration change",
    "Fake %": "Sudden increase in fake numbers",
}
ANOMALY_BAD_DIRECTION = {"Spam %": 1, "VoIP %": 1, "Qualification %": -1, "Avg Score": -1}  # +1 = a rise is bad
# fake-number share up by 5 points, at least 3 fake numbers now, and at least TREND_MIN_CALLS calls with an
# explicit Yes / No in BOTH periods (blank = Unknown is never treated as a legitimate No)
ANOMALY_FAKE = {"pp": 5.0, "min_events": 3, "min_checked": TREND_MIN_CALLS}


def anomaly_rules_text():
    """The anomaly thresholds in plain words, generated from the same numbers the trends use."""
    t = TREND_THRESHOLDS
    return (
        f"Spam % up ≥ {t['Spam %'][1]:.0f} points · Qualification % down ≥ {t['Qualification %'][1]:.0f} points · "
        f"Avg score down ≥ {t['Avg Score'][1]:.0f} · VoIP % up ≥ {t['VoIP %'][1]:.0f} points · "
        f"Calls change ≥ {CALLS_RULE['rel'][1]:.0%} and ≥ {CALLS_RULE['abs'][1]} calls · "
        f"Avg duration change ≥ {DURATION_RULE['rel'][1]:.0%} and ≥ {DURATION_RULE['abs'][1]}s · "
        f"Fake-number share up ≥ {ANOMALY_FAKE['pp']:.0f} points with ≥ {ANOMALY_FAKE['min_events']} fake numbers "
        f"(needs ≥ {ANOMALY_FAKE['min_checked']} calls with an explicit Yes / No in both periods; blank is Unknown, not No). "
        f"Only groups with ≥ {TREND_MIN_CALLS} calls in both periods are checked (volume needs ≥ {TREND_MIN_VOLUME})."
    )


def _fmt_change(key, diff, prev):
    if key == "Calls":
        rel = f" ({diff / prev:+.0%})" if prev else ""
        return f"{diff:+,.0f} calls{rel}"
    if key.endswith("%"):
        return f"{diff:+.1f} points"
    if key == "Avg Score":
        return f"{diff:+.1f}"
    return f"{diff:+.0f}s"


def detect_anomalies(frame, cols, by=None, timeline=None, title="Anomalies"):
    """Compare the selected period with the previous equivalent period and flag only SIGNIFICANT
    moves (the same 'significant' thresholds as the dashboard trends). Pure threshold rules; groups
    with too little data are skipped, never guessed. Works for the network or any grouping."""
    core, err = _compare_core(frame, cols, by, timeline)
    if err:
        err.title = title
        return err
    gcols, parts, info = core["gcols"], core["parts"], core["info"]
    rows = []
    for r in core["records"]:
        key = r["key"]
        if r["trend"] == "n/a" or r["level"] != 2:
            continue
        diff = r["cur"] - r["prev"]
        bad = ANOMALY_BAD_DIRECTION.get(key)
        if bad is not None and diff * bad <= 0:
            continue
        rows.append((r["group"], key, r["prev"], r["cur"], diff, r["cur_row"], r["prev_row"]))
    skipped = 0
    for g in core["order"]:  # fake-number rule (not part of the dashboard trends)
        if g not in core["cur_stats"].index or g not in core["prev_stats"].index:
            skipped += 1
            continue
        c, p = core["cur_stats"].loc[g], core["prev_stats"].loc[g]
        if (min(c["Calls"], p["Calls"]) < TREND_MIN_CALLS or pd.isna(c["Fake %"]) or pd.isna(p["Fake %"])
                or min(c["Fake Checked"], p["Fake Checked"]) < ANOMALY_FAKE["min_checked"]):
            skipped += 1
            continue
        diff = c["Fake %"] - p["Fake %"]
        if diff >= ANOMALY_FAKE["pp"] and c["Fake Numbers"] >= ANOMALY_FAKE["min_events"]:
            rows.append((g, "Fake %", p["Fake %"], c["Fake %"], diff, c, p))
    records = []
    for g, key, pv, cv, diff, c, p in rows:
        rec = {name: parts.loc[g, name] for name in gcols}
        rec.update({
            "Anomaly": ANOMALY_LABELS[key], "Severity": "🔴 High", "Metric": key,
            "Previous": fmt_trend_value(key, pv), "Current": fmt_trend_value(key, cv),
            "Change": _fmt_change(key, diff, pv), "Calls (now)": int(c["Calls"]), "Calls (before)": int(p["Calls"]),
            "Details": f"{ANOMALY_LABELS[key]}: {fmt_trend_value(key, pv)} → {fmt_trend_value(key, cv)} "
                       f"({_fmt_change(key, diff, pv)}) compared with {info['prev_name']}.",
        })
        records.append(rec)
    cols_out = gcols + ["Anomaly", "Severity", "Metric", "Previous", "Current", "Change", "Calls (now)", "Calls (before)", "Details"]
    out = pd.DataFrame(records, columns=cols_out)
    res = QueryResult(title=title, data=out, extra=core, scalars={"Anomalies found": len(out)})
    if out.empty:
        res.notes.append("No anomalies found: no significant change, or not enough data in both periods.")
    res.notes.append("Rules: " + anomaly_rules_text())
    return res


# ---------------- Call-level intelligence examples ----------------
def insurance_breakdown(frame, cols, title="Insurance breakdown"):
    """How many calls fall in each insurance category, using only explicit terms (never guessed),
    plus the distinct values of the QC 'Insurance' field so any named insurer can be queried."""
    if not cols.get("qc"):
        return _unavailable(title, "column 'AI QC Report' not found")
    cls = classify_insurance(frame, cols)
    counts = cls["Insurance_Category"].value_counts()
    known = int((cls["Insurance_Category"] != "unknown").sum())
    if known == 0:
        return _unavailable(title, "no insurance information in these calls")
    rows = [{"Category": INSURANCE_LABELS[k], "Calls": int(counts.get(k, 0)),
             "% of calls with insurance info": (counts.get(k, 0) / known * 100) if k != "unknown" else float("nan")}
            for k in ("public", "private", "insurer_named", "none", "mixed", "unspecified", "unknown")]
    res = QueryResult(title=title, data=_round(pd.DataFrame(rows)),
                      scalars={"Calls": len(frame), "Calls with insurance info": known})
    res.notes.append("Only explicit terms are classified. An insurer name (Aetna, Cigna, Blue Cross Blue Shield, "
                     "United Healthcare ...) is NOT counted as private: it stays 'insurer named' unless the call data "
                     "also states Medicaid / state / marketplace (public) or private / commercial. 'Discussed "
                     "insurance' or 'state' alone stay 'type not stated'. Calls with no insurance information are not counted.")
    type_rows = [{"Insurance type": INSURANCE_TYPE_LABELS[k], "Calls": int((cls["Insurance_Type"] == INSURANCE_TYPE_LABELS[k]).sum())}
                 for k in ("medicaid", "medicare", "marketplace", "public_general", "private", "none")]
    type_rows += [{"Insurance type": "Mixed", "Calls": int((cls["Insurance_Type"] == "Mixed").sum())},
                  {"Insurance type": INSURER_NAMED_LABEL, "Calls": int((cls["Insurance_Category"] == "insurer_named").sum())},
                  {"Insurance type": "Type Not Stated", "Calls": int((cls["Insurance_Category"] == "unspecified").sum())},
                  {"Insurance type": "No insurance information (left out of insurance filters)",
                   "Calls": int((cls["Insurance_Category"] == "unknown").sum())}]
    res.extra["types"] = pd.DataFrame(type_rows)
    ins_cols = [c for c in frame.columns if c.startswith(QC_PREFIX) and any(w in c.lower() for w in INSURANCE_FIELD_WORDS)]
    if ins_cols:
        vals = frame[ins_cols[0]].map(_norm_text)
        vals = vals[~vals.map(is_unavailable)].value_counts().reset_index()
        vals.columns = ["Insurance value in the QC report", "Calls"]
        res.extra["values"] = vals
    return res


def repeat_callers(frame, cols, min_calls=2, title="Repeat Caller IDs"):
    """Caller IDs that called more than once (the same Caller ID repeating)."""
    if not cols.get("caller_id"):
        return _unavailable(title, "column 'Caller ID' not found")
    f = frame[frame["Caller_ID_Norm"] != ""]
    if f.empty:
        return _unavailable(title, "no Caller ID values")
    g = f.groupby("Caller_ID_Norm")
    out = pd.DataFrame({
        "Calls": g.size(), "First call": g["Parsed_Date"].min(), "Last call": g["Parsed_Date"].max(),
    })
    if cols.get("publisher"):
        out["Publishers"] = g[cols["publisher"]].nunique()
    if cols.get("qc"):
        out["Qualified"] = g[cols["qc"]].apply(lambda s: int(count_match(s, QUAL_PAT)))
        out["Spam"] = g[cols["qc"]].apply(lambda s: int(count_match(s, SPAM_PAT)))
    out = out[out["Calls"] >= min_calls].sort_values("Calls", ascending=False).reset_index()
    out = out.rename(columns={"Caller_ID_Norm": "Caller ID"})
    res = QueryResult(title=title, data=out, scalars={
        "Repeat Caller IDs": len(out), "Calls from repeat Caller IDs": int(out["Calls"].sum()) if len(out) else 0,
        "Calls with a Caller ID": len(f)})
    if out.empty:
        res.notes.append(f"No Caller ID appears {min_calls} or more times.")
    return res


# ---------------- One-call query ----------------
def run_analytics_query(frame, cols, spec=None, group_by=None):
    """Filter + statistics + comparison + anomalies + daily series in one call.
    `spec` is the same dict as filter_calls (its 'timeline' is the CURRENT period; all other
    filters apply to both periods so the comparison is like-for-like)."""
    spec = dict(spec or {})
    timeline = spec.pop("timeline", None)
    scope = filter_calls(frame, cols, spec, title="Calls in scope")
    out = {"scope": scope}
    if not scope.available:
        return out
    cur = apply_timeline(scope.data, timeline)
    out["calls"] = QueryResult(title="Matching calls", data=cur, scalars={"Matching calls": len(cur)},
                               notes=list(scope.notes))
    out["stats"] = get_group_stats(cur, cols, group_by)
    out["comparison"] = compare_group_periods(scope.data, cols, group_by, timeline)
    out["anomalies"] = detect_anomalies(scope.data, cols, group_by, timeline)
    out["daily"] = time_series(scope.data, cols, timeline, by=group_by)
    return out


# ---------------- Streamlit panel for the Query Layer ----------------
def _distinct_values(frame, col, limit=500):
    s = frame[col].astype(str).str.strip()
    return list(s[s != ""].value_counts().head(limit).index)


def show_query_result(res, key, file_stub):
    """Notes, table and CSV export of a QueryResult; the 'not available' message when it cannot answer."""
    if res is None:
        return
    if not res.available:
        st.warning(res.message)
        return
    for n in res.notes:
        st.caption(n)
    if res.data is None or res.data.empty:
        st.info("No results for the selected filters.")
        return
    st.dataframe(res.data, width="stretch", hide_index=True)
    st.download_button(
        label="📥 Download as CSV",
        data=res.data.to_csv(index=False).encode("utf-8"),
        file_name=f"{file_stub}.csv",
        mime="text/csv",
        key=f"q_dl_{key}",
    )


def render_query_layer(df, available_columns, overrides, sidebar_timeline):
    """Panel for the Analytics Query Layer: choose timeline, grouping and filters, then read
    statistics, rankings, daily breakdown, period comparison, anomalies and the matching calls."""
    cols = resolve_query_columns(available_columns, overrides)
    qf = prepare_query_frame(df, cols)
    st.markdown("---")
    with st.expander("🔎 Analytics Query Layer (any field, any combination)"):
        st.caption(
            "Answers come only from your sheet data (no AI, nothing estimated). Pick a timeline, optionally group by "
            "one or more fields, and add any filters. If the data cannot answer, you will see "
            f"“{UNAVAILABLE_MSG}”."
        )
        missing = [QUERY_LABELS[k] for k, v in cols.items() if v is None]
        if missing:
            st.info("Columns not found in the sheet: " + ", ".join(missing) + ". Queries that need them report that the information is not available.")

        # ---- timeline (same date logic as the sidebar) ----
        choice = st.selectbox("Timeline:", ["Same as sidebar"] + DATE_PRESETS, key="q_timeline")
        if choice == "Same as sidebar":
            tl = sidebar_timeline or {"preset": "All time"}
        elif choice == "Custom date range":
            valid = qf["Parsed_Date"].dropna()
            lo = valid.min().date() if len(valid) else date.today()
            hi = valid.max().date() if len(valid) else date.today()
            picked = as_range(st.date_input("Custom dates:", (lo, hi), key="q_custom"))
            tl = make_timeline("Custom date range", start=picked[0], end=picked[1]) if picked else {"preset": "All time"}
        else:
            today, _ = get_today(st.session_state.get("date_tz", DEFAULT_TIMEZONE))
            tl = make_timeline(choice, today=today)
        if tl.get("start") is not None:
            st.caption(f"Timeline: {tl['preset']} · {describe_period(tl['start'], tl['end'])}")
        else:
            st.caption("Timeline: All time (no comparison period).")

        # ---- grouping ----
        labels = {QUERY_LABELS[k]: k for k in GROUPABLE if cols.get(k)}
        picked_by = st.multiselect("Group by (leave empty for the whole network):", list(labels), key="q_by")
        by = [labels[x] for x in picked_by]

        # ---- filters ----
        st.markdown("**Filters** (all optional, combined with AND)")
        spec = {}
        cs = st.columns(3)
        for i, key in enumerate(FILTER_COLUMN_KEYS):
            if cols.get(key):
                chosen = cs[i % 3].multiselect(QUERY_LABELS[key] + ":", _distinct_values(qf, cols[key]), key=f"q_f_{key}")
                if chosen:
                    spec[key] = chosen
        cs = st.columns(3)
        fake = cs[0].selectbox(
            "Fake Number (blank = Unknown, not No):", ["Any", "Yes", "No", "Unknown (blank / not checked)"], key="q_fake"
        )
        if fake != "Any":
            spec["fake_number"] = True if fake == "Yes" else False if fake == "No" else "unknown"
        ctype = cs[1].selectbox(
            "Call type:", ["Any", "Qualified", "Non-qualified", "Spam", "Wrong number", "Silent",
                           "Information only", "Other", "VoIP line"], key="q_ctype")
        if ctype != "Any":
            spec["call_type"] = "voip" if ctype == "VoIP line" else ctype.lower()
        cid = cs[2].text_input("Caller ID contains:", "", key="q_cid").strip()
        if cid:
            spec["caller_id"] = cid
        recs = st.selectbox("Recording:", ["Any", "Has a recording link", "No recording link"], key="q_rec")
        if recs != "Any":
            spec["recording"] = recs.startswith("Has")
        cs = st.columns(4)
        for col_ui, (lab, key, step) in zip(cs, (("Min duration (sec, 0 = off)", "duration_min", 10),
                                                 ("Max duration (sec, 0 = off)", "duration_max", 10),
                                                 ("Min quality score (0 = off)", "score_min", 5),
                                                 ("Max quality score (0 = off)", "score_max", 5))):
            v = col_ui.number_input(lab, min_value=0, value=0, step=step, key=f"q_{key}")
            if v:
                spec[key] = v
        cs = st.columns(4)
        for col_ui, (lab, key) in zip(cs, (("AI QC Report contains:", "qc_text"), ("ShortSummary contains:", "summary_text"),
                                           ("Note contains:", "note_text"), ("Anywhere (QC / summary / note):", "any_text"))):
            v = col_ui.text_input(lab, "", key=f"q_{key}").strip()
            if v:
                spec[key] = v
        cs = st.columns(4)
        ins_options = ["Any"] + list(INSURANCE_LABELS.values())
        ins = cs[0].selectbox("Insurance type:", ins_options, key="q_ins")
        named = cs[1].text_input("…or a named insurer (e.g. Aetna):", "", key="q_ins_name").strip()
        if named:
            spec["insurance"] = named
        elif ins != "Any":
            spec["insurance"] = {v: k for k, v in INSURANCE_LABELS.items()}[ins]
        v = cs[2].text_input("Location (state, city …):", "", key="q_loc").strip()
        if v:
            spec["location"] = v
        v = cs[3].text_input("Service / treatment / interest (any campaign):", "", key="q_svc").strip()
        if v:
            spec["service"] = v

        # ---- run ----
        results = run_analytics_query(qf, cols, {**spec, "timeline": tl}, by)
        scope = results["scope"]
        if not scope.available:
            st.warning(scope.message)
            return
        for n in scope.notes:
            st.caption(n)
        calls_df = results["calls"].data
        st.markdown(f"**{len(calls_df):,} calls** match the timeline and filters.")

        t_stats, t_rank, t_daily, t_comp, t_anom, t_calls = st.tabs(
            ["📊 Statistics", "🏆 Rankings", "📅 Daily breakdown", "↔️ Period comparison", "🚨 Anomalies", "📞 Matching calls"]
        )
        with t_stats:
            stats = results["stats"]
            if stats.available:
                k = stats.scalars
                n = k["Calls"]
                m = st.columns(6)
                m[0].metric("Calls", f"{n:,}")
                m[1].metric("Qualified", count_with_pct(k["Qualified"], n))
                m[2].metric("Spam / Fake", count_with_pct(k["Spam"], n))
                m[3].metric("VoIP", count_with_pct(k["VoIP"], n))
                m[4].metric("Avg Score", "–" if pd.isna(k["Avg Score"]) else f"{k['Avg Score']:.1f}")
                m[5].metric("QC Completion", "–" if not n else f"{k['QC Done'] / n * 100:.1f}%")
            show_query_result(stats, "stats", "query_statistics")

        with t_rank:
            how = st.selectbox("Rank by:", ["Current values", "Change vs previous period"], key="q_rank_how")
            top_n = st.number_input("Show top:", min_value=1, max_value=200, value=10, step=1, key="q_rank_n")
            if how == "Current values":
                metric = st.selectbox("Metric:", RANK_METRICS, key="q_rank_metric")
                order = st.selectbox("Order:", ["Highest first", "Lowest first"], key="q_rank_order")
                res = rank_groups(stats.data if stats.available else None, metric, n=int(top_n),
                                  ascending=order == "Lowest first")
            else:
                metric = st.selectbox("Metric:", list(CHANGE_COLUMN), key="q_rank_cmetric")
                order = st.selectbox("Order:", ["Biggest improvement", "Biggest decline"], key="q_rank_corder")
                res = rank_improvement(results["comparison"], metric, improving=order == "Biggest improvement", n=int(top_n))
            show_query_result(res, "rank", "query_ranking")

        with t_daily:
            freq = st.selectbox("Breakdown:", ["Day", "Week", "Month"], key="q_freq")
            ts = results["daily"] if freq == "Day" else time_series(scope.data, cols, tl, by=by, freq=freq.lower())
            show_query_result(ts, "daily", "query_" + freq.lower() + "ly")
            if ts.available and ts.data is not None and not ts.data.empty and not by:
                st.line_chart(ts.data.set_index("Label")[["Calls"]])

        with t_comp:
            comp = results["comparison"]
            if comp.available:
                st.caption(f"**Current:** {comp.scalars['Current period']}  |  **Comparison:** {comp.scalars['Comparison period']}")
            show_query_result(comp, "comp", "query_period_comparison")

        with t_anom:
            show_query_result(results["anomalies"], "anom", "query_anomalies")

        with t_calls:
            st.caption(f"{len(calls_df):,} matching calls (showing the first 1,000).")
            view = calls_df[[c for c in calls_df.columns if c in df.columns]]
            st.dataframe(view.head(1000), width="stretch")
            st.download_button(
                label="📥 Download Matching Calls as CSV",
                data=view.to_csv(index=False).encode("utf-8"),
                file_name="query_matching_calls.csv",
                mime="text/csv",
                key="q_dl_calls",
            )
            st.markdown("**Repeat Caller IDs** (the same Caller ID calling more than once)")
            show_query_result(repeat_callers(calls_df, cols), "repeat", "query_repeat_callers")
            st.markdown("**Insurance breakdown** (explicit terms only)")
            ib = insurance_breakdown(calls_df, cols)
            show_query_result(ib, "ins", "query_insurance")
            if ib.available and "types" in ib.extra:
                st.dataframe(ib.extra["types"], width="stretch", hide_index=True)
            if ib.available and "values" in ib.extra:
                st.dataframe(ib.extra["values"], width="stretch", hide_index=True)


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
    dur_col_name = find_col(available_columns, ["Duration"], ["duration"])
    work_df = add_numeric_columns(work_df, score_col_name, dur_col_name)

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

    # Step 5A: Analytics Query Layer (reads the loaded sheet; does not change anything above)
    render_query_layer(
        df,
        available_columns,
        {
            "qc": selected_qc_col,
            "line_type": selected_voip_col,
            "date": date_col_name if date_cols else None,
        },
        timeline,
    )
