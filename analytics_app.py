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
        rows = worksheet.get_all_values()
        if not rows or len(rows) < 2:
            return pd.DataFrame()
        
        headers = [str(h).strip() for h in rows[0]]
        data = rows[1:]
        
        # Handle empty header names
        cleaned_headers = [h if h != "" else f"Unnamed_{i}" for i, h in enumerate(headers)]
        
        df = pd.DataFrame(data, columns=cleaned_headers)
        return df
    except Exception as e:
        st.error(f"Error loading sheet data: {e}")
        return pd.DataFrame()

def compute_dynamic_analytics(df: pd.DataFrame, group_by_col: str) -> pd.DataFrame:
    if df.empty or group_by_col not in df.columns:
        return pd.DataFrame()

    # Safe data type casting
    if 'Quality Score' in df.columns:
        df['Quality Score'] = pd.to_numeric(df['Quality Score'], errors='coerce').fillna(0)
    else:
        df['Quality Score'] = 0

    if 'Duration' in df.columns:
        df['Duration'] = pd.to_numeric(df['Duration'], errors='coerce').fillna(0)
    else:
        df['Duration'] = 0

    df[group_by_col] = df[group_by_col].fillna('Unknown').astype(str).str.strip()

    # Dynamic aggregation based on available columns
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
st.markdown("Independent Analytics Engine for Google Sheets Data")

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

            # Show ALL columns of the sheet in the dropdown
            available_columns = [col for col in df.columns if not col.startswith("Unnamed_")]
            
            if available_columns:
                selected_dimension = st.selectbox("Group / Analyze By (All Sheet Headings):", available_columns)
                
                result_df = compute_dynamic_analytics(df, selected_dimension)

                st.subheader(f"Performance Breakdown by {selected_dimension}")
                st.dataframe(result_df, use_container_width=True)
            else:
                st.warning("No valid column headers found in the Sheet.")
        else:
            st.info("The sheet is empty.")
    except Exception as e:
        st.error(f"Failed to load analytics: {str(e)}")
