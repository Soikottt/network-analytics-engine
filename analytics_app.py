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

# Refresh / Connect Data Button (Cache Management)
if st.sidebar.button("🔄 Connect & Load Fresh Data") or 'sheet_loaded' not in st.session_state:
    try:
        with st.spinner("Connecting to Google Sheets..."):
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
        date_cols = [c for c in work_df.columns if 'date' in c.lower() or 'time' in c.lower() or 'day' in c.lower()]
        
        if date_cols:
            date_col_name = date_cols[0]
            work_df['Parsed_Date'] = pd.to_datetime(work_df[date_col_name], errors='coerce')
            valid_dates = work_df['Parsed_Date'].dropna()
            
            if not valid_dates.empty:
                min_d, max_d = valid_dates.min().date(), valid_dates.max().date()
                date_range = st.sidebar.date_input("Select Date Range:", (min_d, max_d))
                
                if isinstance(date_range, tuple) and len(date_range) == 2:
                    start_d, end_d = date_range
                    work_df = work_df[(work_df['Parsed_Date'].dt.date >= start_d) & (work_df['Parsed_Date'].dt.date <= end_d)]
            else:
                st.sidebar.info("Date column found, but values could not be parsed.")
        else:
            st.sidebar.info("No date/time column detected for range filtering.")

        # --- FEATURE 3: Advanced Score & Duration Sliders ---
        st.sidebar.markdown("---")
        st.sidebar.subheader("🎛️ Advanced Score & Duration")
        
        if 'Quality Score' in work_df.columns:
            work_df['Quality Score Num'] = pd.to_numeric(work_df['Quality Score'], errors='coerce').fillna(0)
            min_score, max_score = int(work_df['Quality Score Num'].min()), int(work_df['Quality Score Num'].max())
            if min_score == max_score:
                max_score = min_score + 1
            selected_score_range = st.sidebar.slider("Quality Score Range:", min_score, max_score, (min_score, max_score))
            work_df = work_df[(work_df['Quality Score Num'] >= selected_score_range[0]) & (work_df['Quality Score Num'] <= selected_score_range[1])]

        if 'Duration' in work_df.columns:
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

            work_df['Duration Num'] = work_df['Duration'].apply(parse_duration)
            min_dur, max_dur = int(work_df['Duration Num'].min()), int(work_df['Duration Num'].max())
            if min_dur == max_dur:
                max_dur = min_dur + 1
            selected_dur_range = st.sidebar.slider("Duration Range (secs):", min_dur, max_dur, (min_dur, max_dur))
            work_df = work_df[(work_df['Duration Num'] >= selected_dur_range[0]) & (work_df['Duration Num'] <= selected_dur_range[1])]

        # --- TOP KPI METRICS (Including VoIP & VoIP Percentage) ---
        st.markdown("### 📈 Network Overview & Key Metrics")
        kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
        
        total_calls_count = len(work_df)
        kpi1.metric("Total Filtered Calls", f"{total_calls_count:,}")
        
        # Qualified & Spam Metrics
        if 'AI QC Report' in work_df.columns:
            qual_count = work_df['AI QC Report'].str.contains('Qualified', case=False, na=False).sum()
            spam_count = work_df['AI QC Report'].str.contains('Spam|Robo|Solicitation', case=False, na=False).sum()
            qual_pct = (qual_count / total_calls_count * 100) if total_calls_count > 0 else 0
            
            kpi2.metric("Qualified Calls", f"{qual_count:,} ({qual_pct:.1f}%)")
            kpi3.metric("Spam / Fake Calls", f"{spam_count:,}")
        else:
            kpi2.metric("Qualified Calls", "N/A")
            kpi3.metric("Spam / Fake Calls", "N/A")

        # VoIP & VoIP Percentage Tracking
        voip_cols = [c for c in work_df.columns if 'voip' in c.lower()]
        if voip_cols:
            voip_col_name = voip_cols[0]
            voip_count = work_df[voip_col_name].astype(str).str.contains('yes|true|voip', case=False, na=False).sum()
            voip_pct = (voip_count / total_calls_count * 100) if total_calls_count > 0 else 0
            kpi4.metric("VoIP Calls", f"{voip_count:,} ({voip_pct:.1f}%)")
        else:
            kpi4.metric("VoIP Calls", "N/A (Col missing)")

        if 'Quality Score Num' in work_df.columns:
            avg_scr = work_df['Quality Score Num'].mean()
            kpi5.metric("Avg Quality Score", f"{avg_scr:.1f}" if not pd.isna(avg_scr) else "0.0")
        else:
            kpi5.metric("Avg Quality Score", "N/A")

        st.markdown("---")

        # --- Dimension Grouping Section ---
        selected_dimension = st.selectbox("Group / Analyze By (All Sheet Headings):", available_columns)
        
        if selected_dimension:
            temp_df = work_df.copy()
            temp_df[selected_dimension] = temp_df[selected_dimension].fillna('Unknown').astype(str).str.strip()
            
            # Aggregation Dictionary setup
            agg_dict = {'Total_Calls': (temp_df.columns[0], 'count')}
            if 'Quality Score Num' in temp_df.columns:
                agg_dict['Avg_Score'] = ('Quality Score Num', 'mean')
            if 'Duration Num' in temp_df.columns:
                agg_dict['Avg_Duration'] = ('Duration Num', 'mean')
            if 'AI QC Report' in temp_df.columns:
                agg_dict.update({
                    'Qualified_Calls': ('AI QC Report', lambda x: x.str.contains('Qualified', case=False, na=False).sum()),
                    'Spam_Calls': ('AI QC Report', lambda x: x.str.contains('Spam|Robo|Solicitation', case=False, na=False).sum()),
                    'Non_Qualified': ('AI QC Report', lambda x: x.str.contains('Non-Qualified|Info Only', case=False, na=False).sum())
                })
            if voip_cols:
                agg_dict['VoIP_Calls'] = (voip_cols[0], lambda x: x.astype(str).str.contains('yes|true|voip', case=False, na=False).sum())

            summary = temp_df.groupby(selected_dimension).agg(**agg_dict).reset_index()

            # Percentage & Rounding calculations
            if 'Qualified_Calls' in summary.columns and 'Total_Calls' in summary.columns:
                summary['Qualification_%'] = (summary['Qualified_Calls'] / summary['Total_Calls'] * 100).round(1)
            if 'Spam_Calls' in summary.columns and 'Total_Calls' in summary.columns:
                summary['Spam_%'] = (summary['Spam_Calls'] / summary['Total_Calls'] * 100).round(1)
            if 'VoIP_Calls' in summary.columns and 'Total_Calls' in summary.columns:
                summary['VoIP_%'] = (summary['VoIP_Calls'] / summary['Total_Calls'] * 100).round(1)
            if 'Avg_Score' in summary.columns:
                summary['Avg_Score'] = summary['Avg_Score'].round(1)
            if 'Avg_Duration' in summary.columns:
                summary['Avg_Duration'] = summary['Avg_Duration'].round(1)

            summary = summary.sort_values(by='Total_Calls', ascending=False)
            
            st.subheader(f"Performance Breakdown by {selected_dimension}")
            
            # Fast Native Bar Chart
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
                
                # Raw Details Table
                st.dataframe(filtered_rows, use_container_width=True)
                
                # Fast CSV Download Button
                csv_data = filtered_rows.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Download Filtered Rows as CSV",
                    data=csv_data,
                    file_name="filtered_network_records.csv",
                    mime="text/csv",
                )

            # --- FEATURE 6: Publisher Side-by-Side Comparison Mode ---
            st.markdown("---")
            st.subheader("⚖️ Side-by-Side Comparison Mode")
            compare_vals = st.multiselect(f"Select multiple items from '{selected_dimension}' to compare directly:", summary[selected_dimension].tolist())
            
            if compare_vals:
                comparison_df = summary[summary[selected_dimension].isin(compare_vals)]
                st.dataframe(comparison_df, use_container_width=True)
                
    else:
        st.warning("No valid column headers found in the Sheet.")
