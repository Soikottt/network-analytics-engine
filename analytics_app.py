import streamlit as st
import pandas as pd
import gspread

# Page Config (Wide layout for better data viewing)
st.set_page_config(page_title="Network Analytics Dashboard", layout="wide", initial_sidebar_state="collapsed")

st.title("📊 Network & Campaign Intelligence Dashboard")
st.markdown("Independent Analytics Engine for Google Sheets Data")

# Connection Inputs
col1, col2 = st.columns(2)
with col1:
    target_sheet_name = st.text_input("Google Sheet Name:", "Ringba to Sheet QC")
with col2:
    target_tab_name = st.text_input("Sheet Tab Name:", "ALL QC from 30 Sept 2026")

# Explicit Load Button to Prevent Startup Freezing
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
            
            st.session_state['df'] = df
            st.session_state['sheet_loaded'] = True
            st.success(f"Successfully loaded {len(df)} records!")

    except Exception as e:
        st.error(f"Failed to load data: {str(e)}")
        st.session_state['sheet_loaded'] = False

# Main Dashboard Execution (Only runs when data is loaded)
if st.session_state.get('sheet_loaded', False) and 'df' in st.session_state:
    df = st.session_state['df']
    
    available_columns = [col for col in df.columns if not col.startswith("Unnamed_")]
    
    if available_columns:
        st.markdown("---")
        
        # --- LIGHTWEIGHT FEATURE 1: Global Search Box ---
        search_query = st.text_input("🔍 Global Search (Filters entire dataset instantly by Phone, Caller ID, Note, etc.):", "").strip()
        if search_query:
            # Fast filtering across all string columns
            mask = df.apply(lambda row: row.astype(str).str.contains(search_query, case=False, na=False).any(), axis=1)
            work_df = df[mask].copy()
            st.info(f"Global search active: Found {len(work_df)} matching record(s).")
        else:
            work_df = df.copy()

        # --- LIGHTWEIGHT FEATURE 2: Top KPI Metric Cards ---
        st.markdown("### 📈 Network Overview")
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        
        total_calls_count = len(work_df)
        kpi1.metric("Total Calls", f"{total_calls_count:,}")
        
        # Safe metric calculations
        if 'AI QC Report' in work_df.columns:
            qual_count = work_df['AI QC Report'].str.contains('Qualified', case=False, na=False).sum()
            spam_count = work_df['AI QC Report'].str.contains('Spam|Robo|Solicitation', case=False, na=False).sum()
            qual_pct = (qual_count / total_calls_count * 100) if total_calls_count > 0 else 0
            
            kpi2.metric("Qualified Calls", f"{qual_count:,} ({qual_pct:.1f}%)")
            kpi3.metric("Spam / Fake Calls", f"{spam_count:,}")
        else:
            kpi2.metric("Qualified Calls", "N/A")
            kpi3.metric("Spam / Fake Calls", "N/A")

        if 'Quality Score' in work_df.columns:
            numeric_scores = pd.to_numeric(work_df['Quality Score'], errors='coerce')
            avg_scr = numeric_scores.mean()
            kpi4.metric("Avg Quality Score", f"{avg_scr:.1f}" if not pd.isna(avg_scr) else "0.0")
        else:
            kpi4.metric("Avg Quality Score", "N/A")

        st.markdown("---")

        # Dimension Grouping Selection
        selected_dimension = st.selectbox("Group / Analyze By (All Sheet Headings):", available_columns)
        
        if selected_dimension:
            temp_df = work_df.copy()
            temp_df[selected_dimension] = temp_df[selected_dimension].fillna('Unknown').astype(str).str.strip()
            
            # Safe data casting
            if 'Quality Score' in temp_df.columns:
                temp_df['Quality Score'] = pd.to_numeric(temp_df['Quality Score'], errors='coerce').fillna(0)
            if 'Duration' in temp_df.columns:
                temp_df['Duration'] = pd.to_numeric(temp_df['Duration'], errors='coerce').fillna(0)

            agg_dict = {'Total_Calls': (temp_df.columns[0], 'count')}
            if 'Quality Score' in temp_df.columns:
                agg_dict['Avg_Score'] = ('Quality Score', 'mean')
            if 'Duration' in temp_df.columns:
                agg_dict['Avg_Duration'] = ('Duration', 'mean')
            if 'AI QC Report' in temp_df.columns:
                agg_dict.update({
                    'Qualified_Calls': ('AI QC Report', lambda x: x.str.contains('Qualified', case=False, na=False).sum()),
                    'Spam_Calls': ('AI QC Report', lambda x: x.str.contains('Spam|Robo|Solicitation', case=False, na=False).sum()),
                    'Non_Qualified': ('AI QC Report', lambda x: x.str.contains('Non-Qualified|Info Only', case=False, na=False).sum())
                })

            summary = temp_df.groupby(selected_dimension).agg(**agg_dict).reset_index()

            if 'Qualified_Calls' in summary.columns and 'Total_Calls' in summary.columns:
                summary['Qualification_%'] = (summary['Qualified_Calls'] / summary['Total_Calls'] * 100).round(1)
            if 'Spam_Calls' in summary.columns and 'Total_Calls' in summary.columns:
                summary['Spam_%'] = (summary['Spam_Calls'] / summary['Total_Calls'] * 100).round(1)
            if 'Avg_Score' in summary.columns:
                summary['Avg_Score'] = summary['Avg_Score'].round(1)
            if 'Avg_Duration' in summary.columns:
                summary['Avg_Duration'] = summary['Avg_Duration'].round(1)

            summary = summary.sort_values(by='Total_Calls', ascending=False)
            
            st.subheader(f"Performance Breakdown by {selected_dimension}")
            
            # --- LIGHTWEIGHT FEATURE 3: Fast Native Bar Chart ---
            if len(summary) > 0:
                chart_data = summary.set_index(selected_dimension)['Total_Calls'].head(15)
                st.bar_chart(chart_data)

            st.markdown("💡 *Select multiple rows in the table below to view and export their combined raw details:*")

            # Interactive Table with multi-row selection enabled
            event = st.dataframe(
                summary, 
                use_container_width=True, 
                on_select="rerun", 
                selection_mode="multi-row"
            )

            # --- DRILL-DOWN & LIGHTWEIGHT FEATURE 4: CSV Export ---
            selected_rows = event.selection.rows if hasattr(event, 'selection') else []
            
            if selected_rows:
                selected_vals = summary.iloc[selected_rows][selected_dimension].tolist()
                
                st.markdown("---")
                st.subheader(f"🔍 Full Details for selected `{selected_dimension}`: {', '.join(map(str, selected_vals))}")
                
                filtered_rows = work_df[temp_df[selected_dimension].isin(selected_vals)]
                st.info(f"Total matching records found: {len(filtered_rows)}")
                
                # Show Raw Details Table
                st.dataframe(filtered_rows, use_container_width=True)
                
                # Fast CSV Download Button
                csv_data = filtered_rows.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Download Filtered Rows as CSV",
                    data=csv_data,
                    file_name="filtered_network_records.csv",
                    mime="text/csv",
                )
    else:
        st.warning("No valid column headers found in the Sheet.")
