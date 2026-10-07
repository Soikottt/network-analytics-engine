import pandas as pd
import re

# ---------------------------------------------------------------
# Constants & Regex Patterns for Columns A–O Data
# ---------------------------------------------------------------
QUAL_PAT = r"CALL TYPE:\s*QUAL"
SPAM_PAT = r"CALL TYPE:\s*SPAM|SPAM/ROBOT:\s*YES"
VOIP_PAT = r"VOIP"
QC_DONE_PAT = r"^\s*CALL TYPE:"


def find_col(columns, exact_names, keywords):
    """Finds a column name matching exact names or keywords case-insensitively."""
    lowered = {c.lower().strip(): c for c in columns}
    for name in exact_names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    for kw in keywords:
        for c in columns:
            if kw in c.lower():
                return c
    return None


def extract_qc_field(qc_series, field_name):
    """
    Extracts specific semantic attributes (e.g. 'Insurance', 'Caller Intent', 'Outcome', 
    'Treatment/Service Interest', 'Qualification Reason') from Column K AI QC Report.
    Strictly follows the reliability rule: missing info remains None (no guessing).
    """
    results = []
    pattern = re.compile(rf"{field_name}:\s*([^\|]+)", re.IGNORECASE)
    for val in qc_series:
        match = pattern.search(str(val))
        if match:
            res = match.group(1).strip()
            results.append(res if res.lower() not in ["none", "n/a", ""] else None)
        else:
            results.append(None)
    return pd.Series(results, index=qc_series.index)


def normalize_groups(frame, cols):
    """Ensures blank or missing group values are normalized to 'Unknown'."""
    out = frame.copy()
    for c in cols:
        if c in out.columns:
            out[c] = out[c].fillna("Unknown").astype(str).str.strip()
            out.loc[out[c] == "", c] = "Unknown"
    return out


# ---------------------------------------------------------------
# Core Dimension-Independent Query Engine
# ---------------------------------------------------------------
def query_calls(
    df,
    start_date=None,
    end_date=None,
    buyer=None,
    publisher=None,
    campaign=None,
    caller_id=None,
    min_duration=None,
    max_duration=None,
    hangup_by=None,
    min_score=None,
    max_score=None,
    line_type=None,
    phone_company=None,
    fake_number=None,
    insurance_query=None,
    semantic_keyword=None,
):
    """
    Dimension-independent query engine supporting combinations of all A-O fields:
    Call Date, Buyer, Publisher, Campaign, Caller ID, Duration, Note, ShortSummary,
    Recording, Hangup By, AI QC Report, Quality Score, Line Type, Phone Company, Fake Number.
    """
    filtered = df.copy()

    # 1. Date Filtering
    if start_date and "Parsed_Date" in filtered.columns:
        filtered = filtered[filtered["Parsed_Date"] >= pd.Timestamp(start_date)]
    if end_date and "Parsed_Date" in filtered.columns:
        filtered = filtered[filtered["Parsed_Date"] < pd.Timestamp(end_date) + pd.Timedelta(days=1)]

    def apply_match(series, val):
        if val is None or val == "":
            return series
        if isinstance(val, (list, tuple)):
            return series.astype(str).str.strip().isin([str(v).strip() for v in val])
        return series.astype(str).str.contains(str(val), case=False, na=False)

    # 2. Standard Field Filters (A-O Columns)
    if buyer and "Buyer" in filtered.columns:
        filtered = filtered[apply_match(filtered["Buyer"], buyer)]
    if publisher and "Publisher" in filtered.columns:
        filtered = filtered[apply_match(filtered["Publisher"], publisher)]
    if campaign and "Campaign" in filtered.columns:
        filtered = filtered[apply_match(filtered["Campaign"], campaign)]
    if caller_id and "Caller ID" in filtered.columns:
        filtered = filtered[apply_match(filtered["Caller ID"], caller_id)]
    if hangup_by and "Hangup By" in filtered.columns:
        filtered = filtered[apply_match(filtered["Hangup By"], hangup_by)]
    if line_type and "Line Type" in filtered.columns:
        filtered = filtered[apply_match(filtered["Line Type"], line_type)]
    if phone_company and "Phone Company" in filtered.columns:
        filtered = filtered[apply_match(filtered["Phone Company"], phone_company)]
    if fake_number and "Fake Number" in filtered.columns:
        filtered = filtered[apply_match(filtered["Fake Number"], fake_number)]

    # 3. Numeric Filters (Duration & Quality Score)
    if min_duration is not None and "Duration_Num" in filtered.columns:
        filtered = filtered[filtered["Duration_Num"] >= min_duration]
    if max_duration is not None and "Duration_Num" in filtered.columns:
        filtered = filtered[filtered["Duration_Num"] <= max_duration]
    if min_score is not None and "Quality_Score_Num" in filtered.columns:
        filtered = filtered[filtered["Quality_Score_Num"] >= min_score]
    if max_score is not None and "Quality_Score_Num" in filtered.columns:
        filtered = filtered[filtered["Quality_Score_Num"] <= max_score]

    # 4. Semantic Call-Level AI QC Filtering (Column K)
    qc_col_name = find_col(filtered.columns, ["AI QC Report"], ["ai qc", "qc report", "qc"])
    if insurance_query and qc_col_name and qc_col_name != "None":
        ins_series = extract_qc_field(filtered[qc_col_name], "Insurance")
        ins_mask = ins_series.astype(str).str.contains(str(insurance_query), case=False, na=False)
        filtered = filtered[ins_mask]

    if semantic_keyword and qc_col_name and qc_col_name != "None":
        sem_mask = filtered[qc_col_name].astype(str).str.contains(str(semantic_keyword), case=False, na=False)
        filtered = filtered[sem_mask]

    return filtered


# ---------------------------------------------------------------
# Reusable Group & Dimension Statistics Engine
# ---------------------------------------------------------------
def get_group_stats(frame, group_cols, qc_col="AI QC Report", voip_col="Line Type"):
    """
    Computes standard analytics for any dimension or combination (e.g. Buyer + Campaign,
    Publisher + Buyer + Campaign, Line Type distribution, etc.).
    """
    if frame.empty:
        return pd.DataFrame()

    f = normalize_groups(frame, group_cols if isinstance(group_cols, list) else [group_cols])
    f["_row"] = 1
    
    f["_is_qual"] = (
        f[qc_col].astype(str).str.contains(QUAL_PAT, case=False, na=False, regex=True)
        if qc_col in f.columns and qc_col != "None" else False
    )
    f["_is_spam"] = (
        f[qc_col].astype(str).str.contains(SPAM_PAT, case=False, na=False, regex=True)
        if qc_col in f.columns and qc_col != "None" else False
    )
    f["_is_voip"] = (
        f[voip_col].astype(str).str.contains(VOIP_PAT, case=False, na=False, regex=True)
        if voip_col in f.columns and voip_col != "None" else False
    )
    f["_qc_done"] = (
        f[qc_col].astype(str).str.contains(QC_DONE_PAT, case=False, na=False, regex=True)
        if qc_col in f.columns and qc_col != "None" else False
    )

    grouped = f.groupby(group_cols).agg(
        Calls=("_row", "sum"),
        Qualified=("_is_qual", "sum"),
        Spam=("_is_spam", "sum"),
        VoIP=("_is_voip", "sum"),
        Avg_Score=("Quality_Score_Num", "mean"),
        Avg_Duration=("Duration_Num", "mean"),
        QC_Done=("_qc_done", "sum"),
    ).reset_index()

    # Calculate percentages safely (avoiding division by zero)
    grouped["Qualification_%"] = (grouped["Qualified"] / grouped["Calls"].where(grouped["Calls"] > 0) * 100).round(1)
    grouped["Spam_%"] = (grouped["Spam"] / grouped["Calls"].where(grouped["Calls"] > 0) * 100).round(1)
    grouped["VoIP_%"] = (grouped["VoIP"] / grouped["Calls"].where(grouped["Calls"] > 0) * 100).round(1)
    grouped["QC_Completion_%"] = (grouped["QC_Done"] / grouped["Calls"].where(grouped["Calls"] > 0) * 100).round(1)

    return grouped