import streamlit as st
import pandas as pd
import gspread

st.set_page_config(page_title="Network Analytics Dashboard", layout="wide")

st.title("📊 Network & Campaign Intelligence Dashboard")
st.markdown("Independent Analytics Engine for Google Sheets Data")

col1, col2 = st.columns(2)
with col1:
    target_sheet_name = st.text_input("Google Sheet Name:", "Ringba to Sheet QC")
with col2:
    target_tab_name = st.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")

# Button to control loading explicitly and prevent infinite startup spin
if st.button("🔄 Connect & Load Data"):
    try:
        with st.spinner("Connecting to Google Sheets..."):
            try:
                gc = gspread.service_account_from_dict(dict(st.secrets["gcp_service_account"]))
            except Exception:
                gc = gspread.service_account(filename="service_account.json")
            
            sheet = gc.open(target_sheet_name).worksheet(target_tab_name)
            rows = sheet.get_all_values()

        if not rows or len(rows) < 2:
            st.warning("The sheet is empty or contains no data rows.")
            st.session_state['sheet_loaded'] = False
        else:
            headers = [str(h).strip() for h in rows[0]]
            data = rows[1:]
            cleaned_headers = [h if h != "" else f"Unnamed_{i}" for i, h in enumerate(headers)]
            
            df = pd.DataFrame(data, columns=cleaned_headers)
            
            # Save into session state
            st.session_state['df'] = df
            st.session_state['sheet_loaded'] = True
            st.success(f"Successfully loaded {len(df)} records!")

    except Exception as e:
        st.error(f"Failed to load data: {str(e)}")
        st.session_state['sheet_loaded'] = False

# Render analytics if data is successfully loaded in session
if st.session_state.get('sheet_loaded', False) and 'df' in st.session_state:
    df = st.session_state['df']
    
    available_columns = [col for col in df.columns if not col.startswith("Unnamed_")]
    
    if available_columns:
        selected_dimension = st.selectbox("Group / Analyze By (All Sheet Headings):", available_columns)
        
        if selected_dimension:
            # Safe aggregation
            temp_df = df.copy()
            temp_df[selected_dimension] = temp_df[selected_dimension].fillna('Unknown').astype(str).str.strip()
            
            summary = temp_df.groupby(selected_dimension).size().reset_index(name='Total_Calls')
            summary = summary.sort_values(by='Total_Calls', ascending=False)
            
            st.subheader(f"Performance Breakdown by {selected_dimension}")
            st.dataframe(summary, use_container_width=True)
    else:
        st.warning("No valid column headers found in the Sheet.")
