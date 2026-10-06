import streamlit as st
import pandas as pd
import gspread

st.set_page_config(page_title="Network Analytics Dashboard", layout="wide")

# Google Sheets Connection Function
def get_google_client():
    try:
        return gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
    except Exception as exc:
        try:
            return gspread.service_account(filename="service_account.json")
        except Exception:
            raise RuntimeError("Google Sheets credentials not found in secrets or local file.") from exc

def load_sheet_data(worksheet) -> pd.DataFrame:
    try:
        data = worksheet.get_all_records()
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Error loading sheet data: {e}")
        return pd.DataFrame()

def compute_dynamic_analytics(df: pd.DataFrame, group_by_col: str) -> pd.DataFrame:
    if df.empty or group_by_col not in df.columns:
        return pd.DataFrame()

    df['Quality Score'] = pd.to_numeric(df['Quality Score'], errors='coerce').fillna(0)
    df['Duration'] = pd.to_numeric(df['Duration'], errors='coerce').fillna(0)
    df[group_by_col] = df[group_by_col].fillna('Unknown').astype(str).str.strip()

    summary = df.groupby(group_by_col).agg(
        Total_Calls=('Call Date', 'count'),
        Avg_Score=('Quality Score', 'mean'),
        Avg_Duration=('Duration', 'mean'),
        Qualified_Calls=('AI QC Report', lambda x: x.str.contains('Qualified', case=False, na=False).sum()),
        Spam_Calls=('AI QC Report', lambda x: x.str.contains('Spam|Robo|Solicitation', case=False, na=False).sum()),
        Non_Qualified=('AI QC Report', lambda x: x.str.contains('Non-Qualified|Info Only', case=False, na=False).sum())
    ).reset_index()

    summary['Qualification_%'] = (summary['Qualified_Calls'] / summary['Total_Calls'] * 100).round(1)
    summary['Spam_%'] = (summary['Spam_Calls'] / summary['Total_Calls'] * 100).round(1)
    summary['Avg_Score'] = summary['Avg_Score'].round(1)
    summary['Avg_Duration'] = summary['Avg_Duration'].round(1)

    return summary.sort_values(by='Total_Calls', ascending=False)

# --- STREAMLIT UI ---
st.title("📊 Network & Campaign Intelligence Dashboard")
st.markdown("Independent Analytics Engine for Google Sheets Data")

# Input boxes with your exact sheet and tab names pre-filled
col1, col2 = st.columns(2)
with col1:
    target_sheet_name = st.text_input("Google Sheet Name:", "Ringba to Sheet QC")
with col2:
    target_tab_name = st.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")

if st.button("🔄 Fetch & Analyze Data"):
    try:
        with st.spinner("Connecting to Google Sheets..."):
            gc = get_google_client()
            sheet = gc.open(target_sheet_name).worksheet(target_tab_name)
            df = load_sheet_data(sheet)

        if not df.empty:
            st.success(f"Successfully loaded {len(df)} records from '{target_sheet_name}' ({target_tab_name})!")

            # Metric selector dropdown
            available_columns = [col for col in ['Publisher', 'Buyer', 'get_campaign_category', 'Line Type', 'Phone Company'] if col in df.columns]
            
            if available_columns:
                selected_dimension = st.selectbox("Group / Analyze By:", available_columns)
                
                result_df = compute_dynamic_analytics(df, selected_dimension)

                st.subheader(f"Performance Breakdown by {selected_dimension}")
                st.dataframe(result_df, use_container_width=True)
            else:
                st.warning("Expected columns (Publisher, Buyer, etc.) not found in the Sheet headers.")
        else:
            st.info("The sheet is empty.")
    except Exception as e:
        st.error(f"Failed to load analytics: {str(e)}")
