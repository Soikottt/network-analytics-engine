import streamlit as st
import pandas as pd
import gspread

st.set_page_config(page_title="Network Analytics Dashboard", layout="wide")

@st.cache_resource
def get_google_client():
    try:
        return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
    except Exception as exc:
        try:
            return gspread.service_account(filename="service_account.json")
        except Exception:
            raise RuntimeError("Google Sheets credentials not found.") from exc

@st.cache_data(ttl=600)
def load_sheet_data(sheet_name, tab_name) -> pd.DataFrame:
    try:
        gc = get_google_client()
        sheet = gc.open(sheet_name).worksheet(tab_name)
        rows = sheet.get_all_values()
        if not rows or len(rows) < 2:
            return pd.DataFrame()
        
        headers = [str(h).strip() for h in rows[0]]
        data = rows[1:]
        cleaned_headers = [h if h != "" else f"Unnamed_{i}" for i, h in enumerate(headers)]
        
        return pd.DataFrame(data, columns=cleaned_headers)
    except Exception as e:
        st.error(f"Error loading sheet data: {e}")
        return pd.DataFrame()

def compute_dynamic_analytics(df: pd.DataFrame, group_by_col: str) -> pd.DataFrame:
    if df.empty or group_by_col not in df.columns:
        return pd.DataFrame()

    if 'Quality Score' in df.columns:
        df['Quality Score'] = pd.to_numeric(df['Quality Score'], errors='coerce').fillna(0)
    else:
        df['Quality Score'] = 0

    if 'Duration' in df.columns:
        df['Duration'] = pd.to_numeric(df['Duration'], errors='coerce').fillna(0)
    else:
        df['Duration'] = 0

    df[group_by_col] = df[group_by_col].fillna('Unknown').astype(str).str.strip()

    agg_dict = {
        'Total_Calls': ('Call Date', 'count') if 'Call Date' in df.columns else (df.columns[0], 'count'),
        'Avg_Score': ('Quality Score', 'mean'),
        'Avg_Duration': ('Duration', 'mean')
    }

    if 'AI QC Report' in df.columns:
        agg_dict.update({
            'Qualified_Calls': ('AI QC Report', lambda x: x.str.contains('Qualified', case=False, na=False).sum()),
            'Spam_Calls': ('AI QC Report', lambda x: x.str.contains('Spam|Robo|Solicitation', case=False, na=False).sum()),
            'Non_Qualified': ('AI QC Report', lambda x: x.str.contains('Non-Qualified|Info Only', case=False, na=False).sum())
        })

    summary = df.groupby(group_by_col).agg(**agg_dict).reset_index()

    if 'Qualified_Calls' in summary.columns and 'Total_Calls' in summary.columns:
        summary['Qualification_%'] = (summary['Qualified_Calls'] / summary['Total_Calls'] * 100).round(1)
    if 'Spam_Calls' in summary.columns and 'Total_Calls' in summary.columns:
        summary['Spam_%'] = (summary['Spam_Calls'] / summary['Total_Calls'] * 100).round(1)
        
    summary['Avg_Score'] = summary['Avg_Score'].round(1)
    summary['Avg_Duration'] = summary['Avg_Duration'].round(1)

    return summary.sort_values(by=summary.columns[1], ascending=False)

# --- STREAMLIT UI ---
st.title("📊 Network & Campaign Intelligence Dashboard")

col1, col2 = st.columns(2)
with col1:
    target_sheet_name = st.text_input("Google Sheet Name:", "Ringba to Sheet QC")
with col2:
    target_tab_name = st.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")

# Load data with caching so it doesn't freeze
with st.spinner("Loading data from Google Sheets..."):
    df = load_sheet_data(target_sheet_name, target_tab_name)

if not df.empty:
    st.success(f"Successfully loaded {len(df)} records!")

    available_columns = [col for col in df.columns if not col.startswith("Unnamed_")]
    
    if available_columns:
        selected_dimension = st.selectbox("Group / Analyze By (All Sheet Headings):", available_columns)
        
        result_df = compute_dynamic_analytics(df, selected_dimension)

        st.subheader(f"Performance Breakdown by {selected_dimension}")
        st.dataframe(result_df, use_container_width=True)
    else:
        st.warning("No valid column headers found.")
else:
    st.info("Please check sheet name, tab name, or credentials.")
