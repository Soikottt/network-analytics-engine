import re
from datetime import date, timedelta
import pandas as pd
import streamlit as st

# ===============================================================
# STEP 5A — COMPLETE ANALYTICS QUERY LAYER (query_layer.py)
# Deterministic, dimension-independent analytics & semantic search
# across all columns A–O and structured AI QC Report fields.
# ===============================================================

QUAL_PAT = r"CALL TYPE:\s*QUAL"
NON_QUAL_PAT = r"CALL TYPE:\s*(?:NON[\s\-]*QUAL|UNQUAL|NOT[\s\-]*QUAL|DISQUAL)"
SPAM_PAT = r"CALL TYPE:\s*SPAM|SPAM/ROBOT:\s*YES"
VOIP_PAT = r"VOIP"
WRONG_NUM_PAT = r"CALL TYPE:\s*WRONG\s*NUMBER|WRONG\s*NUMBER"
SILENT_PAT = r"CALL TYPE:\s*(?:SILENT|DEAD\s*AIR|BLANK)|SILENT\s*CALL|DEAD\s*AIR|NO\s*AUDIO"
FAKE_YES_PAT = r"^(?:YES|TRUE|1|FAKE|Y|SUSPECT|INVALID|SPOOF)"
FAKE_NO_PAT = r"^(?:NO|FALSE|0|REAL|VALID|LEGIT|NONE|N|CLEAN|)$"
QC_DONE_PAT = r"^\s*CALL TYPE:"
QC_FAIL_PAT = r"PROCESSING ERROR|ANALYSIS PROVIDERS FAILED|^\s*FAILED\s*\|"

MSG_NOT_AVAILABLE = "Information not available in the current data."
MSG_INSUFFICIENT_DATA = "Insufficient data for this query."

DEFAULT_TIMEZONE = "Asia/Dhaka"

DATE_PRESETS = [
    "All time", "Today", "Yesterday", "This week", "Last week",
    "Last 7 days", "Last 30 days", "This month", "Last month",
    "Last 6 months", "This year", "Last year", "Custom date range",
]

TREND_NAMES = {
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

DEFAULT_HEALTH_RULES = {
    "min_calls": 15,
    "min_calls_high_risk": 30,
    "min_qc_completion": 70.0,
    "min_qc_calls": 10,
    "spam": (8.0, 20.0),
    "voip": (30.0, 50.0),
    "qualification": (12.0, 5.0),
    "score": (45.0, 30.0),
}

TREND_MIN_CALLS = 15
TREND_MIN_VOLUME = 10
TREND_MIN_COMPLETION = 50.0
TREND_MIN_EVENTS = 3
TREND_THRESHOLDS = {
    "Qualification %": (5.0, 15.0),
    "Spam %": (5.0, 10.0),
    "VoIP %": (10.0, 20.0),
    "Avg Score": (8.0, 15.0),
}
CALLS_RULE = {"rel": (0.30, 0.60), "abs": (10, 20)}
DURATION_RULE = {"rel": (0.25, 0.50), "abs": (20, 40)}
EVENT_COLUMN = {"Qualification %": "Qualified", "Spam %": "Spam", "VoIP %": "VoIP"}
TREND_METRICS = [
    ("Calls", "Total Calls", 1),
    ("Qualification %", "Qualification %", 1),
    ("Spam %", "Spam %", -1),
    ("VoIP %", "VoIP %", -1),
    ("Avg Score", "Avg Quality Score", 1),
    ("Avg Duration (sec)", "Avg Duration", 1),
]
TREND_ARROWS = {"improving": "↑", "declining": "↓", "stable": "→"}

# Canonical A–O Schema Mapping
CANONICAL_SCHEMA = {
    "call_date": (["Call Date", "Date", "Timestamp", "Created At"], ["date", "time", "day"]),
    "buyer": (["Buyer", "Buyer Name"], ["buyer"]),
    "publisher": (["Publisher", "Publisher Name", "Traffic Source"], ["publisher", "pub"]),
    "campaign": (["Campaign", "Campaign Name"], ["campaign"]),
    "caller_id": (["Caller ID", "CallerID", "Phone", "Phone Number", "ANI"], ["caller", "ani", "phone number"]),
    "duration": (["Duration", "Call Duration", "Length"], ["duration", "length"]),
    "note": (["Note", "Notes", "Call Note"], ["note"]),
    "short_summary": (["ShortSummary", "Short Summary", "Summary"], ["shortsummary", "summary"]),
    "recording": (["Recording", "Recording URL", "Audio"], ["recording", "audio"]),
    "hangup_by": (["Hangup By", "Hangup", "Disconnected By", "Hung Up By"], ["hangup", "hung up", "disconnect"]),
    "ai_qc_report": (["AI QC Report", "QC Report", "AI QC"], ["ai qc", "qc report", "qc"]),
    "quality_score": (["Quality Score", "Score", "QC Score"], ["score"]),
    "line_type": (["Line Type", "LineType", "Connection Type"], ["line type", "voip", "line"]),
    "phone_company": (["Phone Company", "Carrier", "Telco", "Provider"], ["phone company", "carrier", "telco"]),
    "fake_number": (["Fake Number", "Fake", "Is Fake", "Spoofed"], ["fake", "spoof"]),
}

# Structured fields inside Column K (AI QC Report)
QC_STRUCTURED_KEYS = {
    "call_type": ["call type"],
    "call_type_reason": ["call type reason"],
    "caller_intent": ["caller intent", "intent"],
    "why_they_called": ["why they called", "reason for calling"],
    "treatment_service_interest": ["treatment/service interest", "treatment interest", "service interest", "treatment", "service"],
    "insurance": ["insurance", "insurance coverage", "insurance type"],
    "location": ["location", "caller location", "state", "city"],
    "outcome": ["outcome", "call outcome", "resolution"],
    "spam_robot": ["spam/robot", "spam", "robot"],
    "spam_confidence": ["spam confidence"],
    "qc_issue": ["qc issue", "issue"],
    "qualification_reason": ["qualification reason"],
}

# Strict non-guessing guard for vague values
VAGUE_VALUE_PAT = re.compile(
    r"^\s*(?:none|n/?a|unknown|not\s+mentioned|not\s+provided|not\s+discussed|"
    r"not\s+applicable|unspecified|unclear|discussed(?:\s+insurance)?|"
    r"insurance\s+discussed|asked\s+about\s+insurance|inquired\s+about\s+insurance|"
    r"mentioned\s+insurance|no\s+specific\s+insurance|pending|tbd|-|\?)\s*$",
    re.IGNORECASE,
)

INSURANCE_TAXONOMY = {
    "medicaid": [
        r"\bmedicaid\b", r"\bmedi-cal\b", r"\bmedical\s*\(\s*public\s*\)",
        r"\btenncare\b", r"\bmasshealth\b", r"\bahcccs\b", r"\bapple\s+health\b",
        r"\bbadgercare\b", r"\bhusky\s+health\b", r"\bsoonercare\b", r"\bpeachcare\b",
    ],
    "medicare": [r"\bmedicare\b"],
    "medi_cal": [r"\bmedi-cal\b"],
    "public_insurance": [
        r"\bpublic\s+insurance\b", r"\bgovernment\s+insurance\b", r"\bstate\s+insurance\b",
        r"\bstate-funded\b", r"\bgovernment-funded\b", r"\bmedicaid\b", r"\bmedicare\b",
        r"\bmedi-cal\b", r"\btricare\b", r"\bva\s+(?:insurance|health|benefits)\b",
        r"\bchampva\b", r"\bchip\b", r"\(\s*public\s*\)",
    ],
    "state_insurance": [
        r"\bstate\s+insurance\b", r"\bpublic\s+insurance\b", r"\bgovernment\s+insurance\b",
        r"\bmedicaid\b", r"\bmedi-cal\b", r"\(\s*public\s*\)",
    ],
    "government_insurance": [
        r"\bgovernment\s+insurance\b", r"\bpublic\s+insurance\b", r"\bstate\s+insurance\b",
        r"\bmedicaid\b", r"\bmedicare\b", r"\bmedi-cal\b", r"\btricare\b",
        r"\bva\s+(?:insurance|health|benefits)\b", r"\(\s*public\s*\)",
    ],
    "private_insurance": [
        r"\bprivate\s+insurance\b", r"\bcommercial\s+insurance\b",
        r"\bemployer(?:\s+sponsored)?\s+insurance\b", r"\bppo\b", r"\bhmo\s*\(\s*private\s*\)",
        r"\bblue\s*cross\b", r"\bbcbs\b", r"\bblue\s*shield\b", r"\baetna\b", r"\bcigna\b",
        r"\bunited\s*healthcare\b", r"\buhc\b", r"\bhumana\b", r"\banthem\b", r"\bkaiser\b",
        r"\bambetter\b", r"\boscar\s+health\b", r"\(\s*private\s*\)",
    ],
    "blue_cross_blue_shield": [r"\bblue\s*cross\b", r"\bblue\s*shield\b", r"\bbcbs\b", r"\banthem\b"],
    "aetna": [r"\baetna\b"],
    "cigna": [r"\bcigna\b"],
    "unitedhealthcare": [r"\bunited\s*healthcare\b", r"\bunited\s*health\b", r"\buhc\b", r"\boptum\b"],
    "humana": [r"\bhumana\b"],
    "kaiser": [r"\bkaiser\b"],
    "ambetter": [r"\bambetter\b"],
    "tricare": [r"\btricare\b"],
    "uninsured": [r"\buninsured\b", r"\bno\s+insurance\b", r"\bself[\s\-]*pay\b", r"\bcash\s+pay\b", r"\bwithout\s+insurance\b"],
}


# ---------------------------------------------------------------
# Core Shared Helpers
# ---------------------------------------------------------------
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


def safe_pct(numer, denom):
    if isinstance(denom, pd.Series):
        return numer / denom.where(denom > 0) * 100
    return float("nan") if not denom else numer / denom * 100


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
    s = series.astype(str).str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    no_year = s.str.match(r"^[A-Za-z]{3}\s+\d{1,2}\s")
    if no_year.any():
        now = pd.Timestamp.now()
        fixed = pd.to_datetime(s[no_year] + " " + str(now.year), errors="coerce", format="%b %d %I:%M:%S %p %Y")
        fixed = fixed.where(fixed <= now + pd.Timedelta(days=1), fixed - pd.DateOffset(years=1))
        out[no_year] = fixed.astype("datetime64[ns]")
    rest = ~no_year
    if rest.any():
        out[rest] = pd.to_datetime(s[rest], errors="coerce", format="mixed").astype("datetime64[ns]")
    return out


def normalize_groups(frame, cols):
    out = frame.copy()
    for c in cols:
        out[c] = out[c].fillna("Unknown").astype(str).str.strip()
        out.loc[out[c] == "", c] = "Unknown"
    return out


def get_today(tz_name=DEFAULT_TIMEZONE):
    try:
        return pd.Timestamp.now(tz=tz_name.strip()).date(), None
    except Exception:
        return pd.Timestamp.now(tz="UTC").date(), f"Unknown timezone '{tz_name}', using UTC."


def date_filter_range(preset, today):
    day = timedelta(days=1)
    this_mon = today - timedelta(days=today.weekday())
    last_month_end = today.replace(day=1) - day
    ranges = {
        "Today": (today, today),
        "Yesterday": (today - day, today - day),
        "This week": (this_mon, today),
        "Last week": (this_mon - 7 * day, this_mon - day),
        "Last 7 days": (today - 6 * day, today),
        "Last 30 days": (today - 29 * day, today),
        "This month": (today.replace(day=1), today),
        "Last month": (last_month_end.replace(day=1), last_month_end),
        "Last 6 months": ((pd.Timestamp(today) - pd.DateOffset(months=6) + pd.Timedelta(days=1)).date(), today),
        "This year": (today.replace(month=1, day=1), today),
        "Last year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    }
    return ranges.get(preset, (None, None))


def equivalent_previous(preset, start, end):
    if preset == "All time" or start is None or end is None:
        return None
    day = timedelta(days=1)
    n_days = (end - start).days + 1
    cur_name, prev_name = TREND_NAMES.get(
        preset, ("the selected dates", "the previous day" if n_days == 1 else f"the previous {n_days} days")
    )
    if preset == "This week":
        prev = (start - 7 * day, start - day)
    elif preset in ("This month", "Last month"):
        prev_end = start - day
        prev = (prev_end.replace(day=1), prev_end)
    elif preset == "Last 6 months":
        prev = ((pd.Timestamp(start) - pd.DateOffset(months=6)).date(), start - day)
    elif preset in ("This year", "Last year"):
        prev = (date(start.year - 1, 1, 1), date(start.year - 1, 12, 31))
    else:
        prev = (start - n_days * day, start - day)
    return {"prev": prev, "cur_name": cur_name, "prev_name": prev_name}


def slice_window(frame, start_ts, end_ts_excl):
    if "Parsed_Date" not in frame.columns:
        return frame.iloc[0:0].copy()
    d = frame["Parsed_Date"]
    return frame[(d >= start_ts) & (d < end_ts_excl)]


def slice_period(frame, start_d, end_d):
    return slice_window(frame, pd.Timestamp(start_d), pd.Timestamp(end_d) + pd.Timedelta(days=1))


def describe_period(start_d, end_d):
    return f"{start_d:%a %b %d, %Y}" if start_d == end_d else f"{start_d:%a %b %d, %Y} to {end_d:%a %b %d, %Y}"


def trend_windows(info, start, end, base):
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
    return {"cur": (cur_start, cur_end), "prev": (prev_start, prev_end), "trimmed": trimmed, "in_progress": in_progress}


def _join_phrases(parts):
    return "".join(parts) if len(parts) <= 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def health_status(calls, qc_done, qc_pct, qual_pct, spam_pct, voip_pct, avg_score, rules=None):
    R = rules or DEFAULT_HEALTH_RULES
    calls, qc_done = int(calls), int(qc_done)
    if calls < R["min_calls"]:
        return "⚪ INSUFFICIENT DATA", f"Only {calls} call{'s' if calls != 1 else ''} in this period (need at least {R['min_calls']})"
    if qc_done < R["min_qc_calls"] or qc_pct < R["min_qc_completion"]:
        return "⚪ INSUFFICIENT DATA", f"Only {qc_done} of {calls} calls ({qc_pct:.0f}%) have a completed AI QC (need at least {R['min_qc_completion']:.0f}% and {R['min_qc_calls']} calls)"

    flags = []
    if spam_pct >= R["spam"][1]:
        flags.append((2, 0, f"high spam rate ({spam_pct:.1f}%)"))
    elif spam_pct >= R["spam"][0]:
        flags.append((1, 0, f"elevated spam rate ({spam_pct:.1f}%)"))
    if qual_pct < R["qualification"][1]:
        flags.append((2, 1, f"very low qualification rate ({qual_pct:.1f}%)"))
    elif qual_pct < R["qualification"][0]:
        flags.append((1, 1, f"qualification rate is below the normal range ({qual_pct:.1f}%)"))
    if not pd.isna(avg_score):
        if avg_score < R["score"][1]:
            flags.append((2, 2, f"very low average quality score ({avg_score:.1f})"))
        elif avg_score < R["score"][0]:
            flags.append((1, 2, f"low average quality score ({avg_score:.1f})"))
    if voip_pct >= R["voip"][1]:
        flags.append((2, 3, f"very high VoIP share ({voip_pct:.1f}%)"))
    elif voip_pct >= R["voip"][0]:
        flags.append((1, 3, f"high VoIP share ({voip_pct:.1f}%)"))

    if not flags:
        score_txt = "" if pd.isna(avg_score) else f", avg score {avg_score:.1f}"
        return "🟢 HEALTHY", f"Qualification {qual_pct:.1f}%, spam {spam_pct:.1f}%, VoIP {voip_pct:.1f}%{score_txt}: all within the normal range"

    flags.sort(key=lambda f: (-f[0], f[1]))
    texts = [f[2] for f in flags]
    reason = (", ".join(texts[:3]) + f" and {len(texts) - 3} more") if len(texts) > 3 else _join_phrases(texts)
    reason = reason[:1].upper() + reason[1:]
    if spam_pct >= R["spam"][1] or sum(1 for f in flags if f[0] == 2) >= 2:
        return ("🔴 HIGH RISK", reason) if calls >= R["min_calls_high_risk"] else ("🟡 WATCH", f"{reason} (only {calls} calls, so not marked high risk)")
    return "🟡 WATCH", reason


def health_columns(stats, rules=None):
    res = [
        health_status(r["Calls"], r["QC Done"], r["QC Completion %"], r["Qualification %"], r["Spam %"], r["VoIP %"], r["Avg Score"], rules=rules)
        for _, r in stats.iterrows()
    ]
    return [x[0] for x in res], [x[1] for x in res]


def change_level(key, cur, prev):
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
    return (0.0 if key == "Calls" else float("nan")) if row is None else row[key]


def trend_eligible(key, cur, prev):
    c, p = metric_value(cur, "Calls"), metric_value(prev, "Calls")
    if key == "Calls":
        return max(c, p) >= TREND_MIN_VOLUME
    if cur is None or prev is None or min(c, p) < TREND_MIN_CALLS or pd.isna(cur[key]) or pd.isna(prev[key]):
        return False
    if key in ("Qualification %", "Spam %", "Avg Score") and min(cur["QC Completion %"], prev["QC Completion %"]) < TREND_MIN_COMPLETION:
        return False
    if key == "VoIP %" and min(cur["Line Type Completion %"], prev["Line Type Completion %"]) < TREND_MIN_COMPLETION:
        return False
    if key in EVENT_COLUMN and max(cur[EVENT_COLUMN[key]], prev[EVENT_COLUMN[key]]) < TREND_MIN_EVENTS:
        return False
    return True


# ---------------------------------------------------------------
# Schema & Call-Level Detection
# ---------------------------------------------------------------
def resolve_schema_columns(df, qc_override=None, voip_override=None, date_override=None):
    cols = [c for c in df.columns if not str(c).startswith("Unnamed_")]
    resolved = {k: find_col(cols, exact, kw) for k, (exact, kw) in CANONICAL_SCHEMA.items()}
    if qc_override and qc_override != "None" and qc_override in df.columns:
        resolved["ai_qc_report"] = qc_override
    if voip_override and voip_override != "None" and voip_override in df.columns:
        resolved["line_type"] = voip_override
    if date_override and date_override != "None" and date_override in df.columns:
        resolved["call_date"] = date_override
    return resolved


def resolve_dimension_columns(df, dimensions, schema=None):
    if schema is None:
        schema = resolve_schema_columns(df)
    if dimensions is None:
        return [], True, []
    if isinstance(dimensions, str):
        if dimensions.strip().lower() in ("network", "all", "overall", "total", "entire network", ""):
            return [], True, []
        dim_list = [d.strip() for d in re.split(r"[+,|]", dimensions) if d.strip()]
    else:
        dim_list = [str(d).strip() for d in dimensions if str(d).strip()]
    if not dim_list or (len(dim_list) == 1 and dim_list[0].lower() in ("network", "all", "overall", "total")):
        return [], True, []

    actual_cols, missing = [], []
    col_lookup = {c.lower().strip(): c for c in df.columns}
    for d in dim_list:
        dl = d.lower().strip()
        key_norm = re.sub(r"[\s\-]+", "_", dl)
        if dl in col_lookup:
            actual_cols.append(col_lookup[dl])
        elif key_norm in schema and schema[key_norm]:
            actual_cols.append(schema[key_norm])
        else:
            found = find_col(list(df.columns), [d], [dl])
            if found:
                actual_cols.append(found)
            else:
                missing.append(d)
    return list(dict.fromkeys(actual_cols)), False, missing


def _detect_fake_series(frame, fake_col=None):
    if fake_col is None or fake_col == "None" or fake_col not in frame.columns:
        fake_col = find_col(frame.columns, CANONICAL_SCHEMA["fake_number"][0], CANONICAL_SCHEMA["fake_number"][1])
    if not fake_col or fake_col not in frame.columns:
        return pd.Series(False, index=frame.index)
    s = frame[fake_col].fillna("").astype(str).str.strip()
    return s.str.contains(FAKE_YES_PAT, case=False, na=False, regex=True) | ((s != "") & (~s.str.match(FAKE_NO_PAT, case=False, na=False)))


def _detect_wrong_number_series(frame, qc_col, sum_col=None, note_col=None):
    res = pd.Series(False, index=frame.index)
    if qc_col and qc_col != "None" and qc_col in frame.columns:
        res |= frame[qc_col].astype(str).str.contains(WRONG_NUM_PAT, case=False, na=False, regex=True)
    for col in (sum_col, note_col):
        if col and col in frame.columns:
            res |= frame[col].astype(str).str.contains(r"\bwrong\s+number\b", case=False, na=False, regex=True)
    return res


def _detect_silent_series(frame, qc_col, sum_col=None, note_col=None):
    res = pd.Series(False, index=frame.index)
    for col in (qc_col if qc_col != "None" else None, sum_col, note_col):
        if col and col in frame.columns:
            res |= frame[col].astype(str).str.contains(SILENT_PAT, case=False, na=False, regex=True)
    return res


def period_stats(frame, cols, qc_col, voip_col):
    f = frame.copy()
    f["_row"] = 1
    has_qc = qc_col != "None" and qc_col in f.columns
    has_voip = voip_col != "None" and voip_col in f.columns
    f["_is_qual"] = (
        f[qc_col].astype(str).str.contains(QUAL_PAT, case=False, na=False, regex=True)
        & ~f[qc_col].astype(str).str.contains(NON_QUAL_PAT, case=False, na=False, regex=True)
        if has_qc else False
    )
    f["_is_non_qual"] = f[qc_col].astype(str).str.contains(NON_QUAL_PAT, case=False, na=False, regex=True) if has_qc else False
    f["_is_spam"] = f[qc_col].astype(str).str.contains(SPAM_PAT, case=False, na=False, regex=True) if has_qc else False
    f["_is_voip"] = f[voip_col].astype(str).str.contains(VOIP_PAT, case=False, na=False, regex=True) if has_voip else False
    f["_qc_done"] = f[qc_col].astype(str).str.contains(QC_DONE_PAT, case=False, na=False, regex=True) if has_qc else False
    f["_line_done"] = (f[voip_col].astype(str).str.strip() != "") if has_voip else False
    sum_col = find_col(f.columns, CANONICAL_SCHEMA["short_summary"][0], CANONICAL_SCHEMA["short_summary"][1])
    note_col = find_col(f.columns, CANONICAL_SCHEMA["note"][0], CANONICAL_SCHEMA["note"][1])
    f["_is_wrong_num"] = _detect_wrong_number_series(f, qc_col if has_qc else None, sum_col, note_col)
    f["_is_silent"] = _detect_silent_series(f, qc_col if has_qc else None, sum_col, note_col)
    f["_is_fake"] = _detect_fake_series(f)

    if "Quality_Score_Num" not in f.columns:
        sc = find_col(f.columns, CANONICAL_SCHEMA["quality_score"][0], CANONICAL_SCHEMA["quality_score"][1])
        f["Quality_Score_Num"] = pd.to_numeric(f[sc].astype(str).str.extract(r"(-?\d+\.?\d*)")[0], errors="coerce") if sc else float("nan")
    if "Duration_Num" not in f.columns:
        dc = find_col(f.columns, CANONICAL_SCHEMA["duration"][0], CANONICAL_SCHEMA["duration"][1])
        f["Duration_Num"] = f[dc].apply(parse_duration) if dc else float("nan")

    return f.groupby(cols, dropna=False).agg(
        Calls=("_row", "sum"),
        Qualified=("_is_qual", "sum"),
        **{"Non-Qualified": ("_is_non_qual", "sum")},
        Spam=("_is_spam", "sum"),
        VoIP=("_is_voip", "sum"),
        **{"Wrong Number": ("_is_wrong_num", "sum")},
        Silent=("_is_silent", "sum"),
        **{
            "Fake Number": ("_is_fake", "sum"),
            "Avg Score": ("Quality_Score_Num", "mean"),
            "Avg Duration (sec)": ("Duration_Num", "mean"),
            "QC Done": ("_qc_done", "sum"),
            "Line Done": ("_line_done", "sum"),
        },
    )


def add_percentages(stats):
    out = stats.copy()
    out["Qualification %"] = safe_pct(out["Qualified"], out["Calls"])
    out["Non-Qualification %"] = safe_pct(out.get("Non-Qualified", 0), out["Calls"])
    out["Spam %"] = safe_pct(out["Spam"], out["Calls"])
    out["VoIP %"] = safe_pct(out["VoIP"], out["Calls"])
    out["Wrong Number %"] = safe_pct(out.get("Wrong Number", 0), out["Calls"])
    out["Silent %"] = safe_pct(out.get("Silent", 0), out["Calls"])
    out["Fake Number %"] = safe_pct(out["Fake Number"] if "Fake Number" in out else 0, out["Calls"])
    out["QC Completion %"] = safe_pct(out["QC Done"], out["Calls"])
    out["Line Type Completion %"] = safe_pct(out["Line Done"], out["Calls"])
    return out


# ---------------------------------------------------------------
# QC Parsing & Semantic Classification
# ---------------------------------------------------------------
def parse_qc_report_row(text):
    out = {k: "" for k in QC_STRUCTURED_KEYS}
    if not text or pd.isna(text):
        return out
    raw = str(text).strip()
    if not raw or re.search(QC_FAIL_PAT, raw, flags=re.IGNORECASE):
        return out
    alias_map = {alias.lower().strip(): k for k, aliases in QC_STRUCTURED_KEYS.items() for alias in aliases}
    for seg in raw.split("|"):
        if ":" in seg:
            k_part, v_part = seg.split(":", 1)
            k_clean = k_part.strip().lower()
            if k_clean in alias_map:
                out[alias_map[k_clean]] = v_part.strip()
    return out


def extract_qc_field(qc_report_text, field_name):
    parsed = parse_qc_report_row(qc_report_text)
    key_norm = re.sub(r"[\s\-/]+", "_", str(field_name).strip().lower())
    alias_map = {}
    for k, aliases in QC_STRUCTURED_KEYS.items():
        for a in aliases:
            alias_map[a.replace(" ", "_")] = k
        alias_map[k] = k
    target_key = alias_map.get(key_norm, key_norm)
    return parsed.get(target_key, "")


def ensure_query_columns(df, qc_override=None, voip_override=None, date_override=None):
    schema = resolve_schema_columns(df, qc_override, voip_override, date_override)
    out = df.copy()
    if "Quality_Score_Num" not in out.columns:
        sc = schema.get("quality_score")
        out["Quality_Score_Num"] = pd.to_numeric(out[sc].astype(str).str.extract(r"(-?\d+\.?\d*)")[0], errors="coerce") if sc and sc in out.columns else float("nan")
    if "Duration_Num" not in out.columns:
        dc = schema.get("duration")
        out["Duration_Num"] = out[dc].apply(parse_duration) if dc and dc in out.columns else float("nan")
    if "Parsed_Date" not in out.columns:
        dtc = schema.get("call_date")
        out["Parsed_Date"] = parse_dates(out[dtc]) if dtc and dtc in out.columns else pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
    qc_col = schema.get("ai_qc_report")
    if not all(f"_qc_{k}" in out.columns for k in QC_STRUCTURED_KEYS):
        if qc_col and qc_col in out.columns:
            parsed_df = pd.DataFrame([parse_qc_report_row(v) for v in out[qc_col].tolist()], index=out.index)
            for k in QC_STRUCTURED_KEYS:
                out[f"_qc_{k}"] = parsed_df[k]
        else:
            for k in QC_STRUCTURED_KEYS:
                out[f"_qc_{k}"] = ""
    return out, schema


def is_meaningful_semantic_value(val):
    if val is None or pd.isna(val):
        return False
    s = str(val).strip()
    return bool(s) and not bool(VAGUE_VALUE_PAT.match(s))


def classify_insurance_text(text, query_term=None):
    if not is_meaningful_semantic_value(text):
        return False if query_term is not None else []
    s_clean = re.sub(r"^\s*insurance(?:\s+coverage|\s+type)?\s*:\s*", "", str(text).strip(), flags=re.IGNORECASE).strip()
    if not is_meaningful_semantic_value(s_clean):
        return False if query_term is not None else []
    matched = [cat for cat, pats in INSURANCE_TAXONOMY.items() if any(re.search(p, s_clean, flags=re.IGNORECASE) for p in pats)]
    if query_term is None:
        return matched
    q_norm = re.sub(r"[\s\-]+", "_", str(query_term).strip().lower())
    alias_map = {
        "state": "state_insurance", "state_insurance": "state_insurance",
        "public": "public_insurance", "public_insurance": "public_insurance",
        "government": "government_insurance", "gov": "government_insurance", "government_insurance": "government_insurance",
        "private": "private_insurance", "commercial": "private_insurance", "private_insurance": "private_insurance",
        "medicaid": "medicaid", "medicare": "medicare", "medi_cal": "medi_cal", "medical": "medi_cal",
        "bcbs": "blue_cross_blue_shield", "blue_cross": "blue_cross_blue_shield", "blue_shield": "blue_cross_blue_shield", "blue_cross_blue_shield": "blue_cross_blue_shield",
        "aetna": "aetna", "cigna": "cigna", "uhc": "unitedhealthcare", "united": "unitedhealthcare", "united_healthcare": "unitedhealthcare", "unitedhealthcare": "unitedhealthcare",
        "humana": "humana", "kaiser": "kaiser", "ambetter": "ambetter", "tricare": "tricare", "uninsured": "uninsured", "no_insurance": "uninsured", "self_pay": "uninsured",
    }
    target = alias_map.get(q_norm)
    if target and target in matched:
        return True
    q_raw = str(query_term).strip()
    if q_raw.lower() in ("insurance", "discussed insurance", "discussed"):
        return False
    return bool(re.search(r"\b" + re.escape(q_raw) + r"\b", s_clean, flags=re.IGNORECASE))


def normalize_preset_name(preset):
    if not preset:
        return "All time"
    p = str(preset).strip().lower()
    mapping = {
        "all time": "All time", "all_time": "All time", "all": "All time", "today": "Today", "yesterday": "Yesterday",
        "this week": "This week", "this_week": "This week", "last week": "Last week", "last_week": "Last week",
        "last 7 days": "Last 7 days", "last_7_days": "Last 7 days", "past 7 days": "Last 7 days",
        "last 30 days": "Last 30 days", "last_30_days": "Last 30 days", "past 30 days": "Last 30 days",
        "this month": "This month", "this_month": "This month", "last month": "Last month", "last_month": "Last month",
        "last 6 months": "Last 6 months", "last_6_months": "Last 6 months", "this year": "This year", "this_year": "This year",
        "last year": "Last year", "last_year": "Last year", "custom": "Custom date range", "custom date range": "Custom date range",
    }
    return mapping.get(p, preset if preset in DATE_PRESETS else "All time")


def resolve_query_timeline(df, preset="All time", start_date=None, end_date=None, tz_name=DEFAULT_TIMEZONE, reference_today=None):
    canonical_preset = normalize_preset_name(preset)
    today = reference_today if reference_today is not None else get_today(tz_name)[0]
    if canonical_preset == "Custom date range" or (start_date is not None and end_date is not None):
        s_d = pd.Timestamp(start_date).date() if start_date is not None else None
        e_d = pd.Timestamp(end_date).date() if end_date is not None else s_d
        return {"preset": "Custom date range", "start": s_d, "end": e_d, "today": today}
    if canonical_preset == "All time":
        return {"preset": "All time", "start": None, "end": None, "today": today}
    s_d, e_d = date_filter_range(canonical_preset, today)
    return {"preset": canonical_preset, "start": s_d, "end": e_d, "today": today}


def _apply_numeric_condition(series, cond):
    s = pd.to_numeric(series, errors="coerce")
    if isinstance(cond, (int, float)):
        return s == float(cond)
    if isinstance(cond, (tuple, list)):
        if len(cond) == 2:
            op, val = str(cond[0]).strip(), float(cond[1])
            return {">": s > val, "gt": s > val, ">=": s >= val, "gte": s >= val, "<": s < val, "lt": s < val, "<=": s <= val, "lte": s <= val, "!=": s != val, "<>": s != val, "ne": s != val}.get(op, s == val)
        if len(cond) == 3 and str(cond[0]).lower() == "between":
            return (s >= float(cond[1])) & (s <= float(cond[2]))
    if isinstance(cond, dict):
        mask = pd.Series(True, index=s.index)
        for k, op_fn in (("min", lambda x, v: x >= v), ("max", lambda x, v: x <= v), ("gt", lambda x, v: x > v), ("lt", lambda x, v: x < v)):
            if cond.get(k) is not None:
                mask &= op_fn(s, float(cond[k]))
        if "op" in cond and "value" in cond:
            mask &= _apply_numeric_condition(s, (cond["op"], cond["value"]))
        return mask
    if isinstance(cond, str):
        c_str = cond.strip()
        m_bw = re.match(r"^(-?\d+\.?\d*)\s*(?:-|to)\s*(-?\d+\.?\d*)$", c_str, flags=re.IGNORECASE)
        if m_bw:
            return (s >= float(m_bw.group(1))) & (s <= float(m_bw.group(2)))
        m_op = re.match(r"^(>=|<=|!=|<>|>|<|=|==)\s*(-?\d+\.?\d*)", c_str)
        if m_op:
            return _apply_numeric_condition(s, (m_op.group(1), float(m_op.group(2))))
        try:
            return s == float(c_str)
        except Exception:
            return pd.Series(False, index=s.index)
    return pd.Series(True, index=s.index)


def _apply_text_condition(series, cond, exact=False):
    s = series.fillna("").astype(str).str.strip()
    if isinstance(cond, (list, tuple, set)):
        targets = [str(x).strip() for x in cond if str(x).strip()]
        if not targets:
            return pd.Series(True, index=s.index)
        return s.str.lower().isin({t.lower() for t in targets}) if exact else s.str.contains("|".join(re.escape(t) for t in targets), case=False, na=False, regex=True)
    c_str = str(cond).strip()
    return (s.str.lower() == c_str.lower()) if exact else s.str.contains(re.escape(c_str), case=False, na=False, regex=True)


def _semantic_match_rows(work, schema, attribute, query_term):
    qc_col, sum_col, note_col = schema.get("ai_qc_report"), schema.get("short_summary"), schema.get("note")
    qc_field_col = f"_qc_{attribute}"
    mask = pd.Series(False, index=work.index)
    sources = pd.Series("", index=work.index, dtype="object")
    extracted = pd.Series("", index=work.index, dtype="object")
    q_str = str(query_term).strip()

    for idx, row in work.iterrows():
        val_qc = row.get(qc_field_col, "") if qc_field_col in work.columns else ""
        if attribute == "insurance":
            if is_meaningful_semantic_value(val_qc) and classify_insurance_text(val_qc, q_str):
                mask.at[idx], sources.at[idx], extracted.at[idx] = True, "AI QC Report (Insurance)", str(val_qc)
                continue
            for fb in ("_qc_call_type_reason", "_qc_qualification_reason", "_qc_outcome"):
                f_val = row.get(fb, "")
                if is_meaningful_semantic_value(f_val) and classify_insurance_text(f_val, q_str):
                    mask.at[idx], sources.at[idx], extracted.at[idx] = True, f"AI QC Report ({fb.replace('_qc_', '')})", str(f_val)
                    break
            if mask.at[idx]:
                continue
            for src_label, col_n in (("ShortSummary", sum_col), ("Note", note_col)):
                if col_n and col_n in work.columns:
                    v_txt = str(row.get(col_n, "")).strip()
                    if is_meaningful_semantic_value(v_txt) and classify_insurance_text(v_txt, q_str):
                        mask.at[idx], sources.at[idx], extracted.at[idx] = True, src_label, v_txt
                        break
        else:
            pat = re.compile(r"\b" + re.escape(q_str) + r"\b", flags=re.IGNORECASE)
            if is_meaningful_semantic_value(val_qc) and pat.search(str(val_qc)):
                mask.at[idx], sources.at[idx], extracted.at[idx] = True, f"AI QC Report ({attribute})", str(val_qc)
                continue
            companion_map = {
                "treatment_service_interest": ["_qc_caller_intent", "_qc_why_they_called", "_qc_call_type_reason"],
                "caller_intent": ["_qc_why_they_called", "_qc_treatment_service_interest"],
                "why_they_called": ["_qc_caller_intent", "_qc_treatment_service_interest"],
                "qualification_reason": ["_qc_call_type_reason"],
                "call_type_reason": ["_qc_qualification_reason"],
            }
            for comp in companion_map.get(attribute, []):
                c_val = row.get(comp, "")
                if is_meaningful_semantic_value(c_val) and pat.search(str(c_val)):
                    mask.at[idx], sources.at[idx], extracted.at[idx] = True, f"AI QC Report ({comp.replace('_qc_', '')})", str(c_val)
                    break
            if mask.at[idx]:
                continue
            for src_label, col_n in (("ShortSummary", sum_col), ("Note", note_col)):
                if col_n and col_n in work.columns:
                    v_txt = str(row.get(col_n, "")).strip()
                    if is_meaningful_semantic_value(v_txt) and pat.search(v_txt):
                        mask.at[idx], sources.at[idx], extracted.at[idx] = True, src_label, v_txt
                        break
            if not mask.at[idx] and qc_col and qc_col in work.columns and not is_meaningful_semantic_value(val_qc):
                raw_qc = str(row.get(qc_col, "")).strip()
                if raw_qc and not re.search(QC_FAIL_PAT, raw_qc, flags=re.IGNORECASE) and pat.search(raw_qc):
                    mask.at[idx], sources.at[idx], extracted.at[idx] = True, "AI QC Report", raw_qc
    return {"mask": mask, "sources": sources, "extracted": extracted}


# ---------------------------------------------------------------
# Public Query Layer API Functions
# ---------------------------------------------------------------
def filter_calls(
    df, preset="All time", start_date=None, end_date=None, tz_name=DEFAULT_TIMEZONE,
    reference_today=None, buyer=None, publisher=None, campaign=None, caller_id=None,
    duration=None, note=None, short_summary=None, recording=None, hangup_by=None,
    ai_qc_report=None, quality_score=None, line_type=None, phone_company=None,
    fake_number=None, call_type=None, qualified=None, spam=None, voip=None,
    wrong_number=None, silent=None, insurance=None, location=None, treatment=None,
    service=None, caller_intent=None, why_called=None, outcome=None,
    qualification_reason=None, qc_issue=None, repeat_caller_only=False,
    min_caller_id_count=2, exact_match=False, qc_override=None, voip_override=None, date_override=None,
):
    if df is None or df.empty:
        return {"status": "no_data", "message": MSG_NOT_AVAILABLE, "count": 0, "df": pd.DataFrame(), "applied_filters": {}, "timeline": None}
    work, schema = ensure_query_columns(df, qc_override, voip_override, date_override)
    qc_col, voip_col = schema.get("ai_qc_report") or "None", schema.get("line_type") or "None"
    applied = {}
    tl = resolve_query_timeline(work, preset=preset, start_date=start_date, end_date=end_date, tz_name=tz_name, reference_today=reference_today)
    if tl["start"] is not None and tl["end"] is not None:
        if work["Parsed_Date"].notna().sum() == 0:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": {"timeline": tl}, "timeline": tl}
        work = slice_period(work, tl["start"], tl["end"])
        applied["timeline"] = f"{tl['preset']} ({describe_period(tl['start'], tl['end'])})"
    else:
        applied["timeline"] = "All time"

    for key, val in [
        ("buyer", buyer), ("publisher", publisher), ("campaign", campaign), ("caller_id", caller_id),
        ("note", note), ("short_summary", short_summary), ("recording", recording), ("hangup_by", hangup_by),
        ("ai_qc_report", ai_qc_report), ("line_type", line_type), ("phone_company", phone_company),
    ]:
        if val is not None and str(val).strip() != "":
            col_name = schema.get(key)
            if not col_name or col_name not in work.columns:
                return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
            work = work[_apply_text_condition(work[col_name], val, exact=(exact_match if key in ("buyer", "publisher", "campaign") else False))]
            applied[key] = val

    if duration is not None:
        if schema.get("duration") is None and work["Duration_Num"].notna().sum() == 0:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        work = work[_apply_numeric_condition(work["Duration_Num"], duration)]
        applied["duration"] = duration

    if quality_score is not None:
        if schema.get("quality_score") is None and work["Quality_Score_Num"].notna().sum() == 0:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        work = work[_apply_numeric_condition(work["Quality_Score_Num"], quality_score)]
        applied["quality_score"] = quality_score

    if fake_number is not None:
        fk_col = schema.get("fake_number")
        if not fk_col or fk_col not in work.columns:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        is_fk = _detect_fake_series(work, fk_col)
        if isinstance(fake_number, bool):
            work = work[is_fk if fake_number else ~is_fk]
        elif str(fake_number).strip().lower() in ("yes", "true", "1", "fake"):
            work = work[is_fk]
        elif str(fake_number).strip().lower() in ("no", "false", "0", "real", "valid"):
            work = work[~is_fk]
        else:
            work = work[_apply_text_condition(work[fk_col], fake_number)]
        applied["fake_number"] = fake_number

    if qualified is not None:
        if qc_col == "None" or qc_col not in work.columns:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        is_q = work[qc_col].astype(str).str.contains(QUAL_PAT, case=False, na=False, regex=True) & ~work[qc_col].astype(str).str.contains(NON_QUAL_PAT, case=False, na=False, regex=True)
        is_nq = work[qc_col].astype(str).str.contains(NON_QUAL_PAT, case=False, na=False, regex=True)
        work = work[is_q if bool(qualified) else is_nq]
        applied["qualified"] = bool(qualified)

    if call_type is not None:
        ct = str(call_type).strip().lower()
        if ct in ("qualified", "qual"):
            return filter_calls(work, preset="All time", qualified=True, qc_override=qc_col, voip_override=voip_col)
        if ct in ("non-qualified", "non qualified", "unqualified", "not qualified"):
            return filter_calls(work, preset="All time", qualified=False, qc_override=qc_col, voip_override=voip_col)
        work = work[_apply_text_condition(work["_qc_call_type"], call_type)]
        applied["call_type"] = call_type

    if spam is not None:
        if qc_col == "None" or qc_col not in work.columns:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        is_sp = work[qc_col].astype(str).str.contains(SPAM_PAT, case=False, na=False, regex=True)
        work = work[is_sp if bool(spam) else ~is_sp]
        applied["spam"] = bool(spam)

    if voip is not None:
        if voip_col == "None" or voip_col not in work.columns:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        is_vp = work[voip_col].astype(str).str.contains(VOIP_PAT, case=False, na=False, regex=True)
        work = work[is_vp if bool(voip) else ~is_vp]
        applied["voip"] = bool(voip)

    if wrong_number is not None:
        is_wn = _detect_wrong_number_series(work, qc_col if qc_col != "None" else None, schema.get("short_summary"), schema.get("note"))
        work = work[is_wn if bool(wrong_number) else ~is_wn]
        applied["wrong_number"] = bool(wrong_number)

    if silent is not None:
        is_sl = _detect_silent_series(work, qc_col if qc_col != "None" else None, schema.get("short_summary"), schema.get("note"))
        work = work[is_sl if bool(silent) else ~is_sl]
        applied["silent"] = bool(silent)

    for sem_key, sem_val in [
        ("insurance", insurance), ("location", location),
        ("treatment_service_interest", treatment if treatment is not None else service),
        ("caller_intent", caller_intent), ("why_they_called", why_called),
        ("outcome", outcome), ("qualification_reason", qualification_reason), ("qc_issue", qc_issue),
    ]:
        if sem_val is not None and str(sem_val).strip() != "":
            sem_res = _semantic_match_rows(work, schema, sem_key, sem_val)
            work = work[sem_res["mask"]].copy()
            work["_semantic_match_source"] = sem_res["sources"][sem_res["mask"]]
            work["_semantic_match_value"] = sem_res["extracted"][sem_res["mask"]]
            applied[sem_key] = sem_val

    if repeat_caller_only:
        cid_col = schema.get("caller_id")
        if not cid_col or cid_col not in work.columns:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "count": 0, "df": work.iloc[0:0].copy(), "applied_filters": applied, "timeline": tl}
        s_cid = work[cid_col].fillna("").astype(str).str.strip()
        valid_cid = (s_cid != "") & (~s_cid.str.lower().isin(["unknown", "anonymous", "restricted", "none", "n/a"]))
        counts = s_cid[valid_cid].value_counts()
        repeat_ids = set(counts[counts >= int(min_caller_id_count)].index)
        work = work[s_cid.isin(repeat_ids)].copy()
        applied["repeat_caller_only"] = f">= {min_caller_id_count} calls"

    return {"status": "ok", "message": "OK" if len(work) > 0 else "No matching calls found for the selected criteria.", "count": int(len(work)), "df": work, "applied_filters": applied, "timeline": tl}


# Alias query_calls to filter_calls for compatibility
query_calls = filter_calls


def search_calls(
    df, query=None, attribute=None, preset="All time", start_date=None, end_date=None,
    tz_name=DEFAULT_TIMEZONE, reference_today=None, qc_override=None, voip_override=None,
    date_override=None, **filter_kwargs,
):
    if df is None or df.empty:
        return {"status": "no_data", "message": MSG_NOT_AVAILABLE, "count": 0, "df": pd.DataFrame(), "source_breakdown": {}, "matched_values": {}}
    attr_kwargs = dict(filter_kwargs)
    if attribute and query is not None:
        attr_norm = re.sub(r"[\s\-/]+", "_", str(attribute).strip().lower())
        attr_alias = {
            "insurance": "insurance", "location": "location", "treatment": "treatment", "service": "service",
            "treatment_service_interest": "treatment", "caller_intent": "caller_intent", "intent": "caller_intent",
            "why_they_called": "why_called", "why_called": "why_called", "outcome": "outcome",
            "qualification_reason": "qualification_reason", "call_type_reason": "qualification_reason", "qc_issue": "qc_issue",
        }.get(attr_norm)
        if attr_alias:
            attr_kwargs[attr_alias] = query
            query = None
    base_res = filter_calls(df, preset=preset, start_date=start_date, end_date=end_date, tz_name=tz_name, reference_today=reference_today, qc_override=qc_override, voip_override=voip_override, date_override=date_override, **attr_kwargs)
    if base_res["status"] != "ok":
        return {"status": base_res["status"], "message": base_res["message"], "count": 0, "df": base_res["df"], "source_breakdown": {}, "matched_values": {}, "applied_filters": base_res["applied_filters"], "timeline": base_res["timeline"]}
    work = base_res["df"]
    schema = resolve_schema_columns(work, qc_override, voip_override, date_override)
    if query is not None and str(query).strip() != "":
        q_str = str(query).strip()
        q_lower = re.sub(r"[\s\-]+", "_", q_str.lower())
        if q_lower in ("medicaid", "medicare", "medi_cal", "public_insurance", "state_insurance", "government_insurance", "private_insurance", "blue_cross_blue_shield", "blue_cross", "bcbs", "aetna", "cigna", "unitedhealthcare", "uhc", "humana", "kaiser", "ambetter", "tricare", "uninsured", "self_pay"):
            sem = _semantic_match_rows(work, schema, "insurance", q_str)
            work = work[sem["mask"]].copy()
            work["_semantic_match_source"] = sem["sources"][sem["mask"]]
            work["_semantic_match_value"] = sem["extracted"][sem["mask"]]
        else:
            qc_col, sum_col, note_col = schema.get("ai_qc_report"), schema.get("short_summary"), schema.get("note")
            other_cols = [c for c in work.columns if not str(c).startswith("_") and c not in (qc_col, sum_col, note_col, "Parsed_Date", "Quality_Score_Num", "Duration_Num")]
            pat = re.escape(q_str)
            m_qc = work[qc_col].astype(str).str.contains(pat, case=False, na=False, regex=True) if qc_col and qc_col in work.columns else pd.Series(False, index=work.index)
            m_sum = work[sum_col].astype(str).str.contains(pat, case=False, na=False, regex=True) if sum_col and sum_col in work.columns else pd.Series(False, index=work.index)
            m_note = work[note_col].astype(str).str.contains(pat, case=False, na=False, regex=True) if note_col and note_col in work.columns else pd.Series(False, index=work.index)
            m_other = work[other_cols].astype(str).apply(lambda c: c.str.contains(pat, case=False, na=False, regex=True)).any(axis=1) if other_cols else pd.Series(False, index=work.index)
            combined = m_qc | m_sum | m_note | m_other
            src = pd.Series("", index=work.index, dtype="object").where(~m_other, "Other Columns").where(~m_note, "Note").where(~m_sum, "ShortSummary").where(~m_qc, "AI QC Report")
            work = work[combined].copy()
            work["_semantic_match_source"] = src[combined]
            work["_semantic_match_value"] = q_str
    return {
        "status": "ok", "message": "OK" if len(work) > 0 else "No matching calls found.", "count": int(len(work)), "df": work,
        "source_breakdown": work["_semantic_match_source"].value_counts().to_dict() if "_semantic_match_source" in work.columns and not work.empty else {},
        "matched_values": work["_semantic_match_value"].value_counts().head(20).to_dict() if "_semantic_match_value" in work.columns and not work.empty else {},
        "applied_filters": base_res["applied_filters"], "timeline": base_res["timeline"],
    }


def get_dimension_stats(
    df, dimensions="Network", preset="All time", start_date=None, end_date=None,
    tz_name=DEFAULT_TIMEZONE, reference_today=None, include_comparison=True,
    qc_override=None, voip_override=None, date_override=None, health_rules=None, **filter_kwargs,
):
    if df is None or df.empty:
        return {"status": "no_data", "message": MSG_NOT_AVAILABLE, "dimensions": [], "stats_df": pd.DataFrame(), "timeline": None}
    full_df, schema = ensure_query_columns(df, qc_override, voip_override, date_override)
    qc_col, voip_col = schema.get("ai_qc_report") or "None", schema.get("line_type") or "None"
    dim_cols, is_network, missing_dims = resolve_dimension_columns(full_df, dimensions, schema)
    if missing_dims:
        return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "missing_dimensions": missing_dims, "dimensions": dim_cols, "stats_df": pd.DataFrame(), "timeline": None}
    pre_filter = filter_calls(full_df, preset="All time", qc_override=qc_col, voip_override=voip_col, date_override=schema.get("call_date"), **filter_kwargs)
    if pre_filter["status"] != "ok":
        return {"status": pre_filter["status"], "message": pre_filter["message"], "dimensions": dim_cols, "stats_df": pd.DataFrame(), "timeline": pre_filter["timeline"]}
    filtered_base = pre_filter["df"].copy()
    tl = resolve_query_timeline(filtered_base, preset=preset, start_date=start_date, end_date=end_date, tz_name=tz_name, reference_today=reference_today)
    if tl["start"] is not None and tl["end"] is not None:
        if filtered_base["Parsed_Date"].notna().sum() == 0:
            return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "dimensions": dim_cols, "stats_df": pd.DataFrame(), "timeline": tl}
        cur_df = slice_period(filtered_base, tl["start"], tl["end"])
    else:
        cur_df = filtered_base.copy()
    group_cols = ["Network"] if is_network else dim_cols
    if is_network:
        cur_df["Network"] = "Entire Network"
        filtered_base["Network"] = "Entire Network"
    else:
        cur_df = normalize_groups(cur_df, group_cols)
        filtered_base = normalize_groups(filtered_base, group_cols)
    if cur_df.empty:
        return {"status": "insufficient_data", "message": MSG_INSUFFICIENT_DATA, "dimensions": group_cols, "stats_df": pd.DataFrame(), "timeline": tl}
    cur_stats = add_percentages(period_stats(cur_df, group_cols, qc_col, voip_col))
    cur_stats["Health Status"], cur_stats["Health Reason"] = health_columns(cur_stats, rules=health_rules)
    prev_info = equivalent_previous(tl["preset"], tl["start"], tl["end"]) if include_comparison else None
    if prev_info is not None and filtered_base["Parsed_Date"].notna().sum() > 0:
        win = trend_windows(prev_info, tl["start"], tl["end"], filtered_base)
        prev_df = slice_window(filtered_base, *win["prev"])
        prev_stats = add_percentages(period_stats(prev_df, group_cols, qc_col, voip_col)) if not prev_df.empty else pd.DataFrame(columns=cur_stats.columns, index=cur_stats.index[:0])
        idx = cur_stats.index.union(prev_stats.index).set_names(group_cols[0] if len(group_cols) == 1 else group_cols)
        cur_stats_full, prev_stats_full = cur_stats.reindex(idx), prev_stats.reindex(idx)
        for count_c in ("Calls", "Qualified", "Non-Qualified", "Spam", "VoIP", "Wrong Number", "Silent", "Fake Number", "QC Done", "Line Done"):
            cur_stats_full[count_c] = cur_stats_full[count_c].fillna(0).astype(int)
            prev_stats_full[count_c] = prev_stats_full[count_c].fillna(0).astype(int)
        cur_stats_full["Health Status"], cur_stats_full["Health Reason"] = health_columns(cur_stats_full, rules=health_rules)
        for col_m in ("Calls", "Qualified", "Spam", "VoIP", "Fake Number", "Avg Score", "Avg Duration (sec)", "Qualification %", "Spam %", "VoIP %", "Fake Number %"):
            cur_stats_full[f"Prev {col_m}"] = prev_stats_full[col_m]
        cur_stats_full["Volume Change"] = cur_stats_full["Calls"] - cur_stats_full["Prev Calls"]
        cur_stats_full["Volume Change %"] = safe_pct(cur_stats_full["Volume Change"], cur_stats_full["Prev Calls"])
        cur_stats_full["Qualification Change"] = cur_stats_full["Qualification %"] - cur_stats_full["Prev Qualification %"]
        cur_stats_full["Spam Change"] = cur_stats_full["Spam %"] - cur_stats_full["Prev Spam %"]
        cur_stats_full["VoIP Change"] = cur_stats_full["VoIP %"] - cur_stats_full["Prev VoIP %"]
        cur_stats_full["Fake Number Change"] = cur_stats_full["Fake Number %"] - cur_stats_full["Prev Fake Number %"]
        cur_stats_full["Score Change"] = cur_stats_full["Avg Score"] - cur_stats_full["Prev Avg Score"]
        cur_stats_full["Duration Change"] = cur_stats_full["Avg Duration (sec)"] - cur_stats_full["Prev Avg Duration (sec)"]
        trend_dirs = []
        for g in cur_stats_full.index:
            c_row = cur_stats.loc[g] if g in cur_stats.index else None
            p_row = prev_stats.loc[g] if g in prev_stats.index else None
            g_trends = []
            for key, label, good in TREND_METRICS:
                if trend_eligible(key, c_row, p_row):
                    cv, pv = metric_value(c_row, key), metric_value(p_row, key)
                    lvl = change_level(key, cv, pv)
                    t_dir = "stable" if lvl == 0 else ("improving" if (cv - pv) * good > 0 else "declining")
                    g_trends.append(f"{label}: {TREND_ARROWS[t_dir]}")
            trend_dirs.append(" | ".join(g_trends) if g_trends else "Insufficient comparison data")
        cur_stats_full["Trends"] = trend_dirs
        result_df = cur_stats_full.sort_values("Calls", ascending=False).reset_index()
    else:
        for col_na in ("Prev Calls", "Prev Qualified", "Prev Spam", "Prev VoIP", "Prev Fake Number", "Prev Avg Score", "Prev Avg Duration (sec)", "Prev Qualification %", "Prev Spam %", "Prev VoIP %", "Prev Fake Number %", "Volume Change", "Volume Change %", "Qualification Change", "Spam Change", "VoIP Change", "Fake Number Change", "Score Change", "Duration Change"):
            cur_stats[col_na] = float("nan")
        cur_stats["Trends"] = "No comparison period (All time)"
        result_df = cur_stats.sort_values("Calls", ascending=False).reset_index()
    return {"status": "ok", "message": "OK", "dimensions": group_cols, "stats_df": result_df, "timeline": tl, "comparison": prev_info}


def get_group_stats(df, dimensions="Network", group_values=None, **kwargs):
    res = get_dimension_stats(df, dimensions=dimensions, **kwargs)
    if res["status"] != "ok" or res["stats_df"].empty:
        return res
    table, dim_cols = res["stats_df"], res["dimensions"]
    if group_values is not None:
        if isinstance(group_values, dict):
            for k, v in group_values.items():
                mc = find_col(dim_cols, [k], [k.lower()])
                if mc:
                    table = table[_apply_text_condition(table[mc], v)]
        elif isinstance(group_values, (list, tuple)) and len(group_values) == len(dim_cols):
            for c, v in zip(dim_cols, group_values):
                table = table[_apply_text_condition(table[c], v)]
        else:
            table = table[_apply_text_condition(table[dim_cols[0]], group_values)]
        if table.empty:
            return {"status": "not_found", "message": MSG_NOT_AVAILABLE, "dimensions": dim_cols, "stats_df": table, "timeline": res["timeline"]}
    res["stats_df"] = table.reset_index(drop=True)
    return res


def compare_group_periods(
    df, dimensions="Publisher", period_a=None, period_b=None,
    preset=None, tz_name=DEFAULT_TIMEZONE, reference_today=None,
    qc_override=None, voip_override=None, date_override=None, **filter_kwargs,
):
    if df is None or df.empty:
        return {"status": "no_data", "message": MSG_NOT_AVAILABLE, "stats_df": pd.DataFrame()}
    if preset and (period_a is None or period_b is None):
        return get_dimension_stats(df, dimensions=dimensions, preset=preset, tz_name=tz_name, reference_today=reference_today, include_comparison=True, qc_override=qc_override, voip_override=voip_override, date_override=date_override, **filter_kwargs)
    if period_a is None or period_b is None:
        return {"status": "insufficient_data", "message": MSG_INSUFFICIENT_DATA, "stats_df": pd.DataFrame()}
    full_df, schema = ensure_query_columns(df, qc_override, voip_override, date_override)
    qc_col, voip_col = schema.get("ai_qc_report") or "None", schema.get("line_type") or "None"
    dim_cols, is_network, missing_dims = resolve_dimension_columns(full_df, dimensions, schema)
    if missing_dims:
        return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "stats_df": pd.DataFrame()}
    pre = filter_calls(full_df, preset="All time", qc_override=qc_col, voip_override=voip_col, **filter_kwargs)
    if pre["status"] != "ok":
        return {"status": pre["status"], "message": pre["message"], "stats_df": pd.DataFrame()}
    base = pre["df"].copy()
    group_cols = ["Network"] if is_network else dim_cols
    if is_network:
        base["Network"] = "Entire Network"
    else:
        base = normalize_groups(base, group_cols)
    a_start, a_end = pd.Timestamp(period_a[0]).date(), pd.Timestamp(period_a[1]).date()
    b_start, b_end = pd.Timestamp(period_b[0]).date(), pd.Timestamp(period_b[1]).date()
    frame_a, frame_b = slice_period(base, a_start, a_end), slice_period(base, b_start, b_end)
    if frame_a.empty and frame_b.empty:
        return {"status": "insufficient_data", "message": MSG_INSUFFICIENT_DATA, "stats_df": pd.DataFrame()}
    stats_a = add_percentages(period_stats(frame_a, group_cols, qc_col, voip_col)) if not frame_a.empty else pd.DataFrame()
    stats_b = add_percentages(period_stats(frame_b, group_cols, qc_col, voip_col)) if not frame_b.empty else pd.DataFrame()
    idx = stats_a.index.union(stats_b.index).set_names(group_cols[0] if len(group_cols) == 1 else group_cols)
    stats_a, stats_b = stats_a.reindex(idx), stats_b.reindex(idx)
    out = pd.DataFrame(index=idx)
    for m in ("Calls", "Qualified", "Non-Qualified", "Spam", "VoIP", "Wrong Number", "Silent", "Fake Number"):
        a_v, b_v = (stats_a[m].fillna(0).astype(int) if m in stats_a else 0), (stats_b[m].fillna(0).astype(int) if m in stats_b else 0)
        out[f"{m} A"], out[f"{m} B"], out[f"{m} Δ"] = a_v, b_v, b_v - a_v
    for m in ("Qualification %", "Spam %", "VoIP %", "QC Completion %", "Avg Score", "Avg Duration (sec)"):
        a_v, b_v = (stats_a[m].round(1) if m in stats_a else float("nan")), (stats_b[m].round(1) if m in stats_b else float("nan"))
        out[f"{m} A"], out[f"{m} B"], out[f"{m} Δ"] = a_v, b_v, (b_v - a_v).round(1)
    return {"status": "ok", "message": "OK", "dimensions": group_cols, "period_a": (a_start, a_end), "period_b": (b_start, b_end), "stats_df": out.sort_values("Calls B", ascending=False).reset_index()}


def rank_groups(
    df, dimensions="Publisher", ranking_type="best", top_n=10, min_calls=5,
    preset="All time", start_date=None, end_date=None, tz_name=DEFAULT_TIMEZONE,
    reference_today=None, qc_override=None, voip_override=None, date_override=None, **filter_kwargs,
):
    res = get_dimension_stats(df, dimensions=dimensions, preset=preset, start_date=start_date, end_date=end_date, tz_name=tz_name, reference_today=reference_today, include_comparison=True, qc_override=qc_override, voip_override=voip_override, date_override=date_override, **filter_kwargs)
    if res["status"] != "ok" or res["stats_df"].empty:
        return res
    table = res["stats_df"][res["stats_df"]["Calls"] >= 1].copy()
    if table.empty:
        return {"status": "insufficient_data", "message": MSG_INSUFFICIENT_DATA, "dimensions": res["dimensions"], "stats_df": pd.DataFrame()}
    table["Share of Total Calls %"] = safe_pct(table["Calls"], int(table["Calls"].sum())).round(1)
    rt = re.sub(r"[\s\-]+", "_", str(ranking_type).strip().lower())
    qual_pool = table[table["Calls"] >= int(min_calls)].copy()
    if qual_pool.empty:
        qual_pool = table.copy()
    if rt in ("highest_volume", "volume", "most_calls", "distribution"):
        ranked = table.sort_values(["Calls", "Qualified"], ascending=[False, False])
    elif rt in ("lowest_volume", "least_calls"):
        ranked = table.sort_values(["Calls", "Qualified"], ascending=[True, True])
    elif rt in ("highest_spam", "spam", "most_spam"):
        ranked = qual_pool.sort_values(["Spam %", "Spam", "Calls"], ascending=[False, False, False])
    elif rt in ("highest_voip", "voip", "most_voip"):
        ranked = qual_pool.sort_values(["VoIP %", "VoIP", "Calls"], ascending=[False, False, False])
    elif rt in ("highest_qualification", "qualification", "most_qualified"):
        ranked = qual_pool.sort_values(["Qualification %", "Qualified", "Calls"], ascending=[False, False, False])
    elif rt in ("best", "top", "best_performing", "worst", "bottom", "worst_performing"):
        qual_pool["_comp"] = qual_pool["Qualification %"].fillna(0) * 1.5 - qual_pool["Spam %"].fillna(0) * 1.5 + qual_pool["Avg Score"].fillna(50) * 0.4
        is_worst = rt in ("worst", "bottom", "worst_performing")
        ranked = qual_pool.sort_values(["_comp", "Spam %" if is_worst else "Qualified", "Calls"], ascending=[is_worst, False, False]).drop(columns=["_comp"])
    elif rt in ("biggest_improvement", "improvement", "most_improved", "biggest_decline", "decline", "most_declined"):
        has_cmp = qual_pool.dropna(subset=["Qualification Change", "Spam Change"], how="all").copy()
        if has_cmp.empty:
            return {"status": "insufficient_data", "message": MSG_INSUFFICIENT_DATA, "dimensions": res["dimensions"], "stats_df": pd.DataFrame()}
        has_cmp["_imp"] = has_cmp["Qualification Change"].fillna(0) - has_cmp["Spam Change"].fillna(0) + has_cmp["Score Change"].fillna(0) * 0.5
        ranked = has_cmp.sort_values("_imp", ascending=(rt in ("biggest_decline", "decline", "most_declined"))).drop(columns=["_imp"])
    else:
        ranked = table.sort_values("Calls", ascending=False)
    ranked = ranked.head(int(top_n)).reset_index(drop=True)
    ranked.insert(0, "Rank", range(1, len(ranked) + 1))
    return {"status": "ok", "message": "OK", "ranking_type": ranking_type, "dimensions": res["dimensions"], "stats_df": ranked, "timeline": res["timeline"]}


def get_daily_breakdown(
    df, dimensions=None, preset="All time", start_date=None, end_date=None,
    tz_name=DEFAULT_TIMEZONE, reference_today=None, qc_override=None,
    voip_override=None, date_override=None, **filter_kwargs,
):
    base_res = filter_calls(df, preset=preset, start_date=start_date, end_date=end_date, tz_name=tz_name, reference_today=reference_today, qc_override=qc_override, voip_override=voip_override, date_override=date_override, **filter_kwargs)
    if base_res["status"] != "ok":
        return {"status": base_res["status"], "message": base_res["message"], "daily_df": pd.DataFrame(), "formatted_lines": []}
    work = base_res["df"].dropna(subset=["Parsed_Date"]).copy()
    if work.empty:
        return {"status": "insufficient_data", "message": MSG_INSUFFICIENT_DATA, "daily_df": pd.DataFrame(), "formatted_lines": []}
    schema = resolve_schema_columns(work, qc_override, voip_override, date_override)
    qc_col, voip_col = schema.get("ai_qc_report") or "None", schema.get("line_type") or "None"
    work["Date"] = work["Parsed_Date"].dt.date
    work["Day_Name"] = work["Parsed_Date"].dt.strftime("%A")
    dim_cols, is_network, missing_dims = resolve_dimension_columns(work, dimensions, schema)
    if missing_dims:
        return {"status": "not_available", "message": MSG_NOT_AVAILABLE, "daily_df": pd.DataFrame(), "formatted_lines": []}
    group_cols = ["Date", "Day_Name"] + ([] if is_network else dim_cols)
    if not is_network:
        work = normalize_groups(work, dim_cols)
    daily = add_percentages(period_stats(work, group_cols, qc_col, voip_col)).reset_index().sort_values("Date", ascending=True).reset_index(drop=True)
    for c in ("Qualification %", "Spam %", "VoIP %", "QC Completion %", "Avg Score", "Avg Duration (sec)"):
        if c in daily.columns:
            daily[c] = daily[c].round(1)
    lines = [f"{r['Day_Name']} ({r['Date']}): {int(r['Calls']):,} calls / {0.0 if pd.isna(r['Qualification %']) else r['Qualification %']:.0f}% qualified" for _, r in daily.iterrows()]
    return {"status": "ok", "message": "OK", "daily_df": daily, "formatted_lines": lines, "timeline": base_res["timeline"]}


def detect_anomalies(
    df, dimensions="Publisher", preset="Last 7 days", start_date=None, end_date=None,
    tz_name=DEFAULT_TIMEZONE, reference_today=None, qc_override=None,
    voip_override=None, date_override=None, **filter_kwargs,
):
    res = get_dimension_stats(df, dimensions=dimensions, preset=preset, start_date=start_date, end_date=end_date, tz_name=tz_name, reference_today=reference_today, include_comparison=True, qc_override=qc_override, voip_override=voip_override, date_override=date_override, **filter_kwargs)
    if res["status"] != "ok" or res["stats_df"].empty:
        return {"status": res["status"], "message": res.get("message", MSG_INSUFFICIENT_DATA), "anomalies": [], "anomalies_df": pd.DataFrame()}
    if res.get("comparison") is None:
        return {"status": "insufficient_data", "message": "Anomaly detection requires a comparison period (choose a preset other than All time).", "anomalies": [], "anomalies_df": pd.DataFrame()}
    stats_df, dim_cols, prev_name = res["stats_df"], res["dimensions"], res["comparison"]["prev_name"]
    anomalies = []
    for _, row in stats_df.iterrows():
        grp = " | ".join(str(row[c]) for c in dim_cols)
        c_calls, p_calls = int(row.get("Calls", 0)), int(row.get("Prev Calls", 0))
        if max(c_calls, p_calls) >= TREND_MIN_VOLUME:
            v_diff = c_calls - p_calls
            v_rel = abs(v_diff) / p_calls if p_calls > 0 else float("inf")
            if abs(v_diff) >= CALLS_RULE["abs"][0] and v_rel >= CALLS_RULE["rel"][0]:
                sev = "HIGH" if (abs(v_diff) >= CALLS_RULE["abs"][1] and v_rel >= CALLS_RULE["rel"][1]) else "MEDIUM"
                dir_txt = "surge" if v_diff > 0 else "drop"
                anomalies.append({"group": grp, "type": f"Unusual Volume {dir_txt.title()}", "severity": sev, "current": c_calls, "previous": p_calls, "delta": v_diff, "explanation": f"{grp}: Call volume {dir_txt} from {p_calls:,} to {c_calls:,} ({v_diff:+,} calls) vs {prev_name}."})
        if min(c_calls, p_calls) < TREND_MIN_CALLS:
            continue
        for chg_col, cur_col, prev_col, thresh, label, rising_bad in [
            ("Spam Change", "Spam %", "Prev Spam %", TREND_THRESHOLDS["Spam %"], "Sudden Spam Increase", True),
            ("Qualification Change", "Qualification %", "Prev Qualification %", TREND_THRESHOLDS["Qualification %"], "Qualification Decline", False),
            ("Score Change", "Avg Score", "Prev Avg Score", TREND_THRESHOLDS["Avg Score"], "Score Decline", False),
            ("VoIP Change", "VoIP %", "Prev VoIP %", TREND_THRESHOLDS["VoIP %"], "VoIP Increase", True),
            ("Fake Number Change", "Fake Number %", "Prev Fake Number %", (5.0, 10.0), "Fake-Number Increase", True),
        ]:
            val = row.get(chg_col, float("nan"))
            if pd.notna(val) and ((val >= thresh[0]) if rising_bad else (val <= -thresh[0])):
                sev = "HIGH" if abs(val) >= thresh[1] else "MEDIUM"
                unit = " pts" if "%" in cur_col else ""
                anomalies.append({"group": grp, "type": label, "severity": sev, "current": round(row[cur_col], 1), "previous": round(row[prev_col], 1), "delta": round(val, 1), "explanation": f"{grp}: {label} ({val:+.1f}{unit}, from {row[prev_col]:.1f} to {row[cur_col]:.1f}) vs {prev_name}."})
        d_chg, p_dur = row.get("Duration Change", float("nan")), row.get("Prev Avg Duration (sec)", float("nan"))
        if pd.notna(d_chg) and pd.notna(p_dur) and p_dur > 0:
            d_rel = abs(d_chg) / p_dur
            if abs(d_chg) >= DURATION_RULE["abs"][0] and d_rel >= DURATION_RULE["rel"][0]:
                sev = "HIGH" if (abs(d_chg) >= DURATION_RULE["abs"][1] and d_rel >= DURATION_RULE["rel"][1]) else "MEDIUM"
                anomalies.append({"group": grp, "type": "Unusual Duration Change", "severity": sev, "current": round(row["Avg Duration (sec)"], 1), "previous": round(p_dur, 1), "delta": round(d_chg, 1), "explanation": f"{grp}: Avg duration changed by {d_chg:+.0f}s (from {p_dur:.0f}s to {row['Avg Duration (sec)']:.0f}s) vs {prev_name}."})
    anom_df = pd.DataFrame(anomalies)
    return {"status": "ok", "message": "OK" if not anom_df.empty else "No anomalies detected.", "anomalies": anomalies, "anomalies_df": anom_df, "timeline": res["timeline"]}


# ---------------------------------------------------------------
# Optional UI Explorer for Step 5A
# ---------------------------------------------------------------
def render_query_layer_explorer(base_df, timeline=None, qc_col="None", voip_col="None", date_col=None, health_rules=None):
    st.markdown("---")
    with st.expander("🧭 Step 5A — Complete Analytics Query Layer Explorer (Deterministic Engine)", expanded=False):
        st.caption(
            "Test multi-dimensional stats, rankings, semantic call-level searches (Insurance, "
            "Location, Treatment, Intent, Outcome), daily time-series, and deterministic anomaly detection."
        )
        q_preset = (timeline or {}).get("preset", "All time")
        q_start = (timeline or {}).get("start")
        q_end = (timeline or {}).get("end")

        q_tabs = st.tabs([
            "📊 Multi-Dimension Stats",
            "🏆 Rankings",
            "🔎 Semantic & Call-Level Search",
            "📅 Daily Time-Series",
            "🚨 Deterministic Anomalies",
        ])

        with q_tabs[0]:
            dim_presets = [
                "Network", "Buyer", "Publisher", "Campaign",
                "Buyer + Campaign", "Publisher + Campaign", "Buyer + Publisher",
                "Buyer + Publisher + Campaign", "Phone Company", "Line Type", "Hangup By", "Fake Number",
            ]
            sel_dim = st.selectbox("Dimension / Combination:", dim_presets, index=5, key="q5a_dim")
            dim_res = get_dimension_stats(
                base_df, dimensions=sel_dim, preset=q_preset, start_date=q_start, end_date=q_end,
                qc_override=qc_col, voip_override=voip_col, date_override=date_col, health_rules=health_rules,
            )
            if dim_res["status"] != "ok":
                st.info(dim_res["message"])
            else:
                st.dataframe(dim_res["stats_df"], use_container_width=True, hide_index=True)

        with q_tabs[1]:
            rk_c1, rk_c2, rk_c3 = st.columns(3)
            rk_dim = rk_c1.selectbox("Rank Dimension:", ["Publisher", "Buyer", "Campaign", "Phone Company", "Line Type", "Buyer + Campaign", "Publisher + Campaign"], key="q5a_rk_dim")
            rk_type = rk_c2.selectbox("Ranking Criterion:", ["best", "worst", "highest_spam", "highest_voip", "highest_qualification", "biggest_improvement", "biggest_decline", "highest_volume", "lowest_volume", "distribution"], key="q5a_rk_type")
            rk_min = rk_c3.number_input("Min Calls:", min_value=1, max_value=500, value=5, key="q5a_rk_min")
            rk_res = rank_groups(
                base_df, dimensions=rk_dim, ranking_type=rk_type, min_calls=rk_min,
                preset=q_preset, start_date=q_start, end_date=q_end,
                qc_override=qc_col, voip_override=voip_col, date_override=date_col, health_rules=health_rules,
            )
            if rk_res["status"] != "ok":
                st.info(rk_res["message"])
            else:
                st.dataframe(rk_res["stats_df"], use_container_width=True, hide_index=True)

        with q_tabs[2]:
            s_c1, s_c2, s_c3, s_c4 = st.columns(4)
            s_pub = s_c1.text_input("Publisher filter:", "", key="q5a_s_pub")
            s_buy = s_c2.text_input("Buyer filter:", "", key="q5a_s_buy")
            s_cmp = s_c3.text_input("Campaign filter:", "", key="q5a_s_cmp")
            s_dur = s_c4.text_input("Duration filter (e.g. > 120):", "", key="q5a_s_dur")
            s_c5, s_c6, s_c7 = st.columns(3)
            s_attr = s_c5.selectbox("Semantic / QC Attribute:", ["Any / Free-text", "insurance", "location", "treatment", "caller_intent", "why_called", "outcome", "qualification_reason", "qc_issue"], key="q5a_s_attr")
            s_term = s_c6.text_input("Search Term (e.g. state insurance, Medicaid, inpatient):", "", key="q5a_s_term")
            s_repeat = s_c7.checkbox("Repeat Caller IDs only (>=2 calls)", value=False, key="q5a_s_rep")
            s_res = search_calls(
                base_df, query=s_term if s_term else None,
                attribute=None if s_attr == "Any / Free-text" else s_attr,
                preset=q_preset, start_date=q_start, end_date=q_end,
                publisher=s_pub or None, buyer=s_buy or None, campaign=s_cmp or None,
                duration=s_dur or None, repeat_caller_only=s_repeat,
                qc_override=qc_col, voip_override=voip_col, date_override=date_col,
            )
            if s_res["status"] != "ok":
                st.info(s_res["message"])
            else:
                st.markdown(f"**Matching Calls:** `{s_res['count']:,}`")
                if s_res["source_breakdown"]:
                    st.caption("Matched Source Priority Breakdown: " + ", ".join(f"{k}: {v}" for k, v in s_res["source_breakdown"].items()))
                display_cols = [c for c in s_res["df"].columns if not str(c).startswith("_qc_")]
                st.dataframe(s_res["df"][display_cols], use_container_width=True, hide_index=True)

        with q_tabs[3]:
            d_res = get_daily_breakdown(
                base_df, preset=q_preset, start_date=q_start, end_date=q_end,
                qc_override=qc_col, voip_override=voip_col, date_override=date_col,
            )
            if d_res["status"] != "ok":
                st.info(d_res["message"])
            else:
                st.dataframe(d_res["daily_df"], use_container_width=True, hide_index=True)
                with st.expander("Formatted Daily Summary Lines"):
                    for line in d_res["formatted_lines"]:
                        st.text(line)

        with q_tabs[4]:
            an_dim = st.selectbox("Anomaly Detection Dimension:", ["Publisher", "Buyer", "Campaign", "Buyer + Campaign", "Publisher + Campaign", "Phone Company"], key="q5a_an_dim")
            an_res = detect_anomalies(
                base_df, dimensions=an_dim, preset=q_preset if q_preset != "All time" else "Last 7 days",
                start_date=q_start, end_date=q_end,
                qc_override=qc_col, voip_override=voip_col, date_override=date_col, health_rules=health_rules,
            )
            if an_res["status"] != "ok" or an_res["anomalies_df"].empty:
                st.info(an_res["message"])
            else:
                st.dataframe(an_res["anomalies_df"], use_container_width=True, hide_index=True)
