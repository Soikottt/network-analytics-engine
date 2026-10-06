import streamlit as st
import pandas as pd
import gspread
from datetime import datetime

# Page Config
st.set_page_config(page_title="Network & Campaign Intelligence Dashboard", layout="wide", initial_sidebar_state="expanded")

st.title("📊 Network & Campaign Intelligence Dashboard")
st.markdown("Advanced Publisher & Campaign Intelligence Engine")

# --- SIDEBAR: Controls, Connection & All Filters ---
st.sidebar.header("⚙️ Configuration & Filters")

target_sheet_name = st.sidebar.text_input("Google Sheet Name:", "Ringba to Sheet QC")
target_tab_name = st.sidebar.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")

# Refresh / Connect Data Button
if st.sidebar.button("🔄 Connect & Load Fresh Data") or 'sheet_loaded' not in st.session_state:
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
            st.session_state['sheet_loaded'] = False
        else:
            headers = [str(h).strip() for h in rows[0]]
            data = rows[1:]
            cleaned_headers = [h if h != "" else f"Unnamed_{i}" for i, h in enumerate(headers)]
            
            df = pd.DataFrame(data, columns=cleaned_headers)
            st.session_state['df'] = df
            st.session_state['sheet_loaded'] = True
            st.sidebar.success(f"Loaded {len(df):,} records successfully!")

    except Exception as e:
        st.sidebar.error(f"Failed: {str(e)}")
        st.session_state['sheet_loaded'] = False

# Main Execution Flow
if st.session_state.get('sheet_loaded', False) and 'df' in st.session_state:
    df = st.session_state['df']
    available_columns = [col for col in df.columns if not col.startswith("Unnamed_")]
    
    if available_columns:
        work_df = df.copy()

        # --- PRE-PROCESS NUMERIC COLUMNS SAFELY ---
        score_col_name = 'Quality Score' if 'Quality Score' in available_columns else next((c for c in available_columns if 'score' in c.lower()), None)
        if score_col_name:
            work_df['Quality_Score_Num'] = pd.to_numeric(work_df[score_col_name], errors='coerce').fillna(0)
        else:
            work_df['Quality_Score_Num'] = 0.0

        dur_col_name = 'Duration' if 'Duration' in available_columns else next((c for c in available_columns if 'duration' in c.lower()), None)
        if dur_col_name:
            def parse_duration(val):
                try:
                    val_str = str(val).strip()
                    if ':' in val_str:
                        parts = list(map(float, val_str.split(':')))
                        if len(parts) == 3:
                            return parts[0] * 3600 + parts[1] * 60 + parts[2]
                        elif len(parts) == 2:
                            return parts[0] * 60 + parts[1]
                    return float(val_str)
                except:
                    return 0.0
            work_df['Duration_Num'] = work_df[dur_col_name].apply(parse_duration)
        else:
            work_df['Duration_Num'] = 0.0

        # --- MANUAL COLUMN MAPPING SETTINGS ---
        st.sidebar.markdown("---")
        st.sidebar.subheader("📌 Column Mapping Settings")
        
        default_voip = 'Line Type' if 'Line Type' in available_columns else available_columns[0]
        default_qc = 'AI QC Report' if 'AI QC Report' in available_columns else available_columns[0]

        selected_voip_col = st.sidebar.selectbox("Select VoIP / Line Type Column:", available_columns, index=available_columns.index(default_voip) if default_voip in available_columns else 0)
        selected_qc_col = st.sidebar.selectbox("Select AI QC / Status Column:", available_columns, index=available_columns.index(default_qc) if default_qc in available_columns else 0)

        # --- FEATURE 1: Global Search Box ---
        st.sidebar.markdown("---")
        st.sidebar.subheader("🔍 Global Search")
        search_query = st.sidebar.text_input("Search Phone, Caller ID, Note, etc.:", "").strip()
        
        if search_query:
            mask = work_df.apply(lambda row: row.astype(str).str.contains(search_query, case=False, na=False).any(), axis=1)
            work_df = work_df[mask].copy()

        # --- FEATURE 2: Date Range Filter ---
        st.sidebar.markdown("---")
        st.sidebar.subheader("📅 Date Range Filter")
        date_cols = [c for c in available_columns if 'date' in c.lower() or 'time' in c.lower() or 'day' in c.lower()]
        
        if date_cols:
            date_col_name = st.sidebar.selectbox("Select Date Column:", date_cols)
            work_df['Parsed_Date'] = pd.to_datetime(work_df[date_col_name], errors='coerce')
            valid_dates = work_df['Parsed_Date'].dropna()
            
            if not valid_dates.empty:
                min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
                date_range = st.sidebar.date_input("Select Date Range:", (min_d, max_d))
                
                if isinstance(date_range, tuple) and len(date_range) == 2:
                    start_d, end_d = date_range
                    work_df = work_df[(work_df['Parsed_Date'].dt.date >= start_d) & (work_df['Parsed_Date'].dt.date <= end_d)]
            else:
                st.sidebar.info("Date values could not be parsed.")
        else:
            st.sidebar.info("No date/time column detected.")

        # --- TOP KPI METRICS ---
        st.markdown("### 📈 Network Overview & Key Metrics")
        kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
        
        total_calls_count = len(work_df)
        kpi1.metric("Total Filtered Calls", f"{total_calls_count:,}")
        
        # Qualified & Spam Metrics
        if selected_qc_col:
            qual_count = work_df[selected_qc_col].astype(str).str.contains('QUAL', case=False, na=False).sum()
            spam_count = work_df[selected_qc_col].astype(str).str.contains('SPAM|ROBOT: YES', case=False, na=False).sum()
            qual_pct = (qual_count / total_calls_count * 100) if total_calls_count > 0 else 0
            
            kpi2.metric("Qualified Calls", f"{qual_count:,} ({qual_pct:.1f}%)")
            kpi3.metric("Spam / Fake Calls", f"{spam_count:,}")
        else:
            kpi2.metric("Qualified Calls", "0 (0.0%)")
            kpi3.metric("Spam / Fake Calls", "0")

        # VoIP Metrics
        if selected_voip_col:
            voip_count = work_df[selected_voip_col].astype(str).str.contains('VOIP', case=False, na=False).sum()
            voip_pct = (voip_count / total_calls_count * 100) if total_calls_count > 0 else 0
            kpi4.metric("VoIP Calls", f"{voip_count:,} ({voip_pct:.1f}%)")
        else:
            kpi4.metric("VoIP Calls", "0 (0.0%)")

        avg_scr = work_df['Quality_Score_Num'].mean()
        kpi5.metric("Avg Quality Score", f"{avg_scr:.1f}" if not pd.isna(avg_scr) else "0.0")

        st.markdown("---")

        # --- Dimension Grouping Section ---
        selected_dimension = st.selectbox("Group / Analyze By (All Sheet Headings):", available_columns, index=available_columns.index('Publisher') if 'Publisher' in available_columns else 0)
        
        if selected_dimension:
            temp_df = work_df.copy()
            temp_df[selected_dimension] = temp_df[selected_dimension].fillna('Unknown').astype(str).str.strip()
            
            # Safe Aggregation
            summary = temp_df.groupby(selected_dimension).agg(
                Total_Calls=('Quality_Score_Num', 'count'),
                Avg_Score=('Quality_Score_Num', 'mean'),
                Avg_Duration=('Duration_Num', 'mean'),
                Qualified_Calls=(selected_qc_col, lambda x: x.astype(str).str.contains('QUAL', case=False, na=False).sum()) if selected_qc_col else ('Quality_Score_Num', lambda x: 0),
                Spam_Calls=(selected_qc_col, lambda x: x.astype(str).str.contains('SPAM|ROBOT: YES', case=False, na=False).sum()) if selected_qc_col else ('Quality_Score_Num', lambda x: 0),
                VoIP_Calls=(selected_voip_col, lambda x: x.astype(str).str.contains('VOIP', case=False, na=False).sum()) if selected_voip_col else ('Quality_Score_Num', lambda x: 0)
            ).reset_index()

            # Percentage & Rounding calculations
            summary['Qualification_%'] = (summary['Qualified_Calls'] / summary['Total_Calls'] * 100).round(1)
            summary['Spam_%'] = (summary['Spam_Calls'] / summary['Total_Calls'] * 100).round(1)
            summary['VoIP_%'] = (summary['VoIP_Calls'] / summary['Total_Calls'] * 100).round(1)
            summary['Avg_Score'] = summary['Avg_Score'].round(1)
            summary['Avg_Duration'] = summary['Avg_Duration'].round(1)

            summary = summary.sort_values(by='Total_Calls', ascending=False)
            
            st.subheader(f"Performance Breakdown by {selected_dimension}")
            
            if len(summary) > 0:
                chart_data = summary.set_index(selected_dimension)['Total_Calls'].head(15)
                st.bar_chart(chart_data)

            st.markdown("💡 *Select multiple rows in the table below to view and export their combined raw details:*")

            # --- Multi-Row Interactive Selection Table ---
            event = st.dataframe(
                summary, 
                use_container_width=True, 
                on_select="rerun", 
                selection_mode="multi-row"
            )

            # --- Drill-Down & CSV Export Option ---
            selected_rows = event.selection.rows if hasattr(event, 'selection') else []
            
            if selected_rows:
                selected_vals = summary.iloc[selected_rows][selected_dimension].tolist()
                
                st.markdown("---")
                st.subheader(f"🔍 Full Details for selected `{selected_dimension}`: {', '.join(map(str, selected_vals))}")
                
                filtered_rows = work_df[temp_df[selected_dimension].isin(selected_vals)]
                st.info(f"Total matching records found: {len(filtered_rows):,}")
                
                st.dataframe(filtered_rows, use_container_width=True)
                
                csv_data = filtered_rows.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Download Filtered Rows as CSV",
                    data=csv_data,
                    file_name="filtered_network_records.csv",
                    mime="text/csv",
                )

            # --- Publisher Side-by-Side Comparison Mode ---
            st.markdown("---")
            st.subheader("⚖ Side-by-Side Comparison Mode")
            compare_vals = st.multiselect(f"Select multiple items from '{selected_dimension}' to compare directly:", summary[selected_dimension].tolist())
            
            if compare_vals:
                comparison_df = summary[summary[selected_dimension].isin(compare_vals)]
                st.dataframe(comparison_df, use_container_width=True)
                
    else:
        st.warning("No valid column headers found in the Sheet.")
