import streamlit as st
import pandas as pd
import database
import streamlit.components.v1 as components

# ---------------------------------------------------------------------
# 1. STREAMLIT CONFIG, STATE INIT, & CSS
# ---------------------------------------------------------------------
st.set_page_config(page_title="Unfulfilled Deliveries Dashboard", layout="wide", initial_sidebar_state="collapsed")

# Initialize session state immediately
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False

st.markdown("""
    <style>
        /* Hide Streamlit's native header, footer, and margins to allow edge-to-edge HTML */
        [data-testid="collapsedControl"] {display: none !important;}
        [data-testid="stSidebar"] {display: none !important;}
        #MainMenu, header, footer {visibility: hidden !important; height: 0px !important; display: none !important;}
        .block-container { padding: 0rem !important; margin: 0rem !important; max-width: 100% !important; overflow-x: hidden; background-color: #f4f7fb;}
        
        /* Style the Admin section at the bottom of the page */
        .admin-section { padding: 2rem; background-color: white; border-top: 2px solid #eaedf2; margin-top: 1rem; }
        
        div.stButton > button {
            background-color: #ffffff !important; color: #5a5ce0 !important; border: 1px solid #c2c9d6 !important;
            width: 100%; font-weight: 600; border-radius: 6px; padding: 8px; transition: 0.2s; font-family: 'Inter', sans-serif;
        }
        div.stButton > button:hover { background-color: #e8f0fe !important; border-color: #5a5ce0 !important; }
    </style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------
# 2a. COMPACT DATA PACKING (the dashboard data used to be sent as ~30 MB of JSON; this sends the same rows in ~5 MB)
# ---------------------------------------------------------------------
def pack_rows(d):
    """Column-wise, dictionary-encoded JSON. The browser rebuilds exactly the same list of records from it."""
    import json
    d = d.fillna(0)
    text_cols = {'Month', 'Week_Start', 'Week_Range', 'Date', 'Sales_Doc_Type', 'Branch', 'Product_Desc'}
    keys = list(d.columns)
    dicts, cols = {}, {}
    for k in keys:
        vals = d[k].tolist()
        if k in text_cols:
            index, uniques, codes = {}, [], []
            for v in vals:
                i = index.get(v)
                if i is None:
                    i = index[v] = len(uniques)
                    uniques.append(v)
                codes.append(i)
            dicts[k] = uniques
            cols[k] = codes
        else:
            cols[k] = [int(v) if isinstance(v, float) and v.is_integer() else v for v in vals]
    out = json.dumps({'n': len(d), 'keys': keys, 'dict': dicts, 'cols': cols}, separators=(',', ':'))
    return out.replace('</', '<\\/')

# ---------------------------------------------------------------------
# 2. DATA PROCESSING (Cached for Instant Reloads)
# ---------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_and_prep_data():
    df = database.get_data()
    db_stats = database.get_db_stats()
    
    if not df.empty:
        df['Document Date'] = pd.to_datetime(df['Document Date'])
        df['Date'] = df['Document Date'].dt.strftime('%Y-%m-%d')
        df['Month'] = df['Document Date'].dt.strftime('%b %Y')
        
        # Calculate Date Range for Weeks
        df['Week_Start_Dt'] = df['Document Date'] - pd.to_timedelta(df['Document Date'].dt.dayofweek, unit='d')
        df['Week_End_Dt'] = df['Week_Start_Dt'] + pd.to_timedelta(6, unit='d')
        df['Week Range'] = df['Week_Start_Dt'].dt.strftime('%b %d').str.replace(' 0', ' ') + " - " + df['Week_End_Dt'].dt.strftime('%b %d').str.replace(' 0', ' ')
        df['Week_Start'] = df['Week_Start_Dt'].dt.strftime('%Y-%m-%d')
        
        # VECTORIZED CALCULATION: Replaces the slow lambda function, speeding up aggregation dramatically
        df['Missed_Line_Flag'] = (df['Missed_Qty'] > 0).astype(int)

        # Ordered value (RWF). Older databases don't have it yet -> it stays empty until the Excel is re-uploaded once.
        if 'Ordered_Value' not in df.columns:
            df['Ordered_Value'] = float('nan')
        
        df_agg = df.groupby(['Month', 'Week_Start', 'Week Range', 'Date', 'Sales Document Type', 'Branch', 'Material Description'], dropna=False, as_index=False).agg(
            Order_Qty=('Order_Qty', 'sum'),
            Confirmed_Qty=('Confirmed_Qty', 'sum'),
            Missed_Qty=('Missed_Qty', 'sum'),
            Lost_Value=('Lost_Value', 'sum'),
            Ordered_Value=('Ordered_Value', 'sum'),
            Total_Lines=('Sales Document', 'count'),
            Missed_Lines=('Missed_Line_Flag', 'sum')
        ).rename(columns={
            'Sales Document Type': 'Sales_Doc_Type', 
            'Week Range': 'Week_Range', 
            'Material Description': 'Product_Desc'
        })
        
        json_data = pack_rows(df_agg)
        return df, db_stats, json_data
    else:
        return df, db_stats, '{"n":0,"keys":[],"dict":{},"cols":{}}'

# Call the cached data loader
df, db_stats, json_data = load_and_prep_data()

# --- Extras for the added value-perspective cards, charts and tables (additive; nothing above is changed) ---
LARGE_THRESHOLD = 25000000   # RWF missed per single order item that counts as a "large order" to verify
ORDVAL_READY = bool((not df.empty) and ('Ordered_Value' in df.columns) and df['Ordered_Value'].notna().all())
ORDVAL_READY_JS = 'true' if ORDVAL_READY else 'false'
if not df.empty:
    _large = df[df['Lost_Value'] >= LARGE_THRESHOLD][['Sales Document', 'Sales Document Item', 'Date', 'Month', 'Sales Document Type', 'Branch', 'Material Description', 'Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value']].rename(columns={
        'Sales Document': 'Doc', 'Sales Document Item': 'Item', 'Sales Document Type': 'Sales_Doc_Type', 'Material Description': 'Product_Desc'})
    large_json = _large.fillna(0).to_json(orient='records')
else:
    large_json = "[]"

# ---------------------------------------------------------------------
# 3. EXACT HTML DASHBOARD INJECTION
# ---------------------------------------------------------------------
html_template = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unfulfilled Deliveries Dashboard</title>
    
    <!-- Plotly & DataTables -->
    <script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
    <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
    <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
    
    <!-- DataTables Buttons Extension for CSV/Excel Export -->
    <script src="https://cdn.datatables.net/buttons/2.4.1/js/dataTables.buttons.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
    <script src="https://cdn.datatables.net/buttons/2.4.1/js/buttons.html5.min.js"></script>

    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        :root {{
            --bg-color: #f4f7fb; --panel-bg: #ffffff; --sidebar-bg: #5a5ce0;
            --text-main: #2b3648; --text-muted: #8b95a5; --border-color: #eaedf2;
            --accent-purple: #5a5ce0; --accent-orange: #f48a6a; --accent-green: #34d399;
            --shadow: 0 4px 12px rgba(0, 0, 0, 0.05); --border-radius: 10px;
        }}
        
        body {{ background-color: var(--bg-color); color: var(--text-main); font-family: 'Inter', sans-serif; margin: 0; padding: 15px; display: flex; flex-direction: column; min-height: 100vh; box-sizing: border-box; font-size: 0.9rem; overflow-x: hidden; }}
        h1 {{ font-size: 1.4rem; margin: 0 0 15px 0; font-weight: 700; color: var(--text-main); }}
        
        .header-banner {{ background-color: var(--sidebar-bg); background-image: linear-gradient(to right, #4c4eb8, #5a5ce0); color: white; border-radius: 8px; padding: 12px 20px; display: flex; align-items: center; margin-bottom: 20px; box-shadow: var(--shadow); margin-top:0px; width: 100%; box-sizing: border-box; }}
        .header-banner h1 {{ margin: 0; font-size: 1.5rem; font-weight: 800; color: white;}}
        .header-logo {{ height: 28px; width: 45px; background: white; border-radius: 4px; display: inline-flex; align-items:center; justify-content:center; margin-right: 15px; color: var(--sidebar-bg); font-weight: bold; font-size: 0.6rem; text-align:center;}}

        .dashboard-container {{ display: flex; flex: 1; flex-wrap: wrap; gap: 15px; opacity: {1 if not df.empty else 0.2}; pointer-events: {'auto' if not df.empty else 'none'}; }}
        
        .selectBox {{ position: relative; }}
        .selectBox select {{ width: 100%; font-family: 'Inter', sans-serif; font-size: 0.85rem; font-weight: 600; cursor: pointer; background-color: var(--accent-purple); border: 1px solid rgba(255, 255, 255, 0.3); color: white; padding: 10px 14px; border-radius: 6px; outline: none; appearance: none; -webkit-appearance: none; box-shadow: inset 0 1px 2px rgba(255,255,255,0.1); }}
        .selectBox::after {{ content: "▼"; position: absolute; right: 12px; top: 12px; color: white; font-size: 0.7rem; pointer-events: none;}}
        .selectBox:hover select {{ background-color: #4c4eb8; }}
        
        .sidebar {{ flex: 0 0 240px; background-color: var(--sidebar-bg); border-radius: var(--border-radius); padding: 20px 15px; display: flex; flex-direction: column; gap: 12px; height: fit-content; color: white; box-shadow: var(--shadow); position: relative; }}
        .sidebar h2 {{ font-size: 1.05rem; margin: 0 0 5px 0; font-weight: 600; border-bottom: 1px solid rgba(255,255,255,0.15); padding-bottom: 10px;}}
        .filter-group {{ display: flex; flex-direction: column; gap: 5px; }}
        .filter-group label {{ font-size: 0.7rem; font-weight: 600; color: rgba(255, 255, 255, 0.8); text-transform: uppercase; letter-spacing: 0.5px;}}
        .filter-group input {{ background-color: rgba(255, 255, 255, 0.1); border: 1px solid rgba(255, 255, 255, 0.2); color: white; padding: 8px 10px; border-radius: 6px; outline: none; width: 100%; box-sizing: border-box; font-family: 'Inter', sans-serif; font-size: 0.85rem; transition: border-color 0.2s; }}
        .filter-group input::placeholder {{ color: rgba(255,255,255,0.6); }}
        
        .reset-btn {{ margin-top: 5px; padding: 10px; background: white; color: var(--sidebar-bg); border: none; cursor: pointer; border-radius: 6px; font-weight: 600; font-size: 0.85rem; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
        .reset-btn:hover {{ background: #f8f9fa; }}

        .kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 15px; width: 100%; }}
        .flip-card {{ background-color: transparent; height: 115px; perspective: 1000px; }}
        .flip-card-inner {{ position: relative; width: 100%; height: 100%; text-align: left; transition: transform 0.6s cubic-bezier(0.4, 0, 0.2, 1); transform-style: preserve-3d; }}
        .flip-card:hover .flip-card-inner {{ transform: rotateX(180deg); }}
        .flip-card-front, .flip-card-back {{ position: absolute; width: 100%; height: 100%; backface-visibility: hidden; border-radius: 12px; padding: 12px 15px; box-sizing: border-box; box-shadow: 0 4px 15px rgba(0,0,0,0.03); background: #fff; border: 1px solid #f0f0f0;}}
        
        /* Hides the plotly SVG when flipped to prevent bleeding */
        .flip-card:hover #donutChartContainer {{ opacity: 0 !important; transition: opacity 0.1s ease-out; }}
        #donutChartContainer {{ transition: opacity 0.3s ease-in; }}

        .flip-card-back {{ transform: rotateX(180deg); }}
        .flip-card-back::before {{ content: ""; position: absolute; left: 6px; top: 15%; bottom: 15%; width: 4px; border-radius: 10px; background: linear-gradient(to bottom, #34d399, #059669); box-shadow: 0 0 8px rgba(16, 185, 129, 0.6); }}
        .flip-card-back .kpi-title-back {{ color: #10b981; font-weight: 800; font-size: 0.75rem; text-transform: uppercase; }}
        .kpi-formula {{ background: #f8fafc; color: #1e3a8a; font-family: monospace; font-size: 0.7rem; padding: 6px; margin-top: 8px; border-radius: 4px; border-left: 3px solid #1e3a8a; }}

        .card-purple {{ background: linear-gradient(135deg, #f3e8ff 0%, #d8b4fe 100%); border: none; }}
        .card-purple .kpi-title {{ color: #6b21a8; }} .card-purple .kpi-value {{ color: #4c1d95; }}
        .card-yellow {{ background: linear-gradient(135deg, #fef08a 0%, #f59e0b 100%); border: none; }}
        .card-yellow .kpi-title {{ color: #92400e; }} .card-yellow .kpi-value {{ color: #78350f; }}
        .card-blue {{ background: linear-gradient(135deg, #dbeafe 0%, #60a5fa 100%); border: none; }}
        .card-blue .kpi-title {{ color: #1e40af; }} .card-blue .kpi-value {{ color: #1e3a8a; }}
        .card-teal {{ background: linear-gradient(135deg, #ccfbf1 0%, #2dd4bf 100%); border: none; color: #134e4a; }}
        .card-teal .kpi-title {{ color: #0f766e; }} .card-teal .kpi-value {{ color: #134e4a; }}
        .card-red {{ background: linear-gradient(135deg, #fee2e2 0%, #f87171 100%); border: none; }}
        .card-red .kpi-title {{ color: #991b1b; }} .card-red .kpi-value {{ color: #7f1d1d; }}

        .kpi-header {{ display: flex; justify-content: space-between; align-items: flex-start; }}
        .kpi-title {{ font-size: 0.7rem; font-weight: 700; color: #4b5563; text-transform: uppercase; letter-spacing: 0.5px; }}
        .info-icon {{ position: relative; display: inline-flex; justify-content: center; align-items: center; width: 18px; height: 18px; border-radius: 50%; background-color: rgba(107, 114, 128, 0.5); color: white; font-size: 0.65rem; font-weight: bold; cursor: help; }}
        .kpi-value {{ font-size: 2rem; font-weight: 800; margin-top: 4px; line-height: 1; }}
        .badge-row {{ display: flex; gap: 6px; margin-top: 8px; align-items: center; }}
        .badge {{ font-size: 0.6rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; }}
        .badge-red {{ background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }}
        .badge-teal {{ background: #134e4a; color: #ccfbf1; border: 1px solid #0d9488; }}

        .main-content {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 15px; }}
        .grid-row-middle {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px;}}
        .grid-row-bottom {{ display: grid; grid-template-columns: 1fr 2fr; gap: 15px; }}

        .panel {{ background-color: var(--panel-bg); border: 1px solid var(--border-color); border-radius: var(--border-radius); padding: 15px; display: flex; flex-direction: column; box-shadow: var(--shadow); position: relative; }}
        .panel-header {{ margin: 0 0 10px 0; font-size: 0.95rem; font-weight: 600; color: var(--text-main); display: flex; justify-content: space-between; align-items: center; }}
        
        .table-container {{ flex: 1; overflow-y: auto; overflow-x: auto; width: 100%; }}
        .chart-container {{ flex: 1; min-height: 280px; width: 100%; }}
        
        /* DataTables Custom Styling */
        table.light-table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
        table.light-table th, table.light-table td {{ padding: 10px; text-align: left; white-space: nowrap; border-bottom: 1px solid var(--border-color); }}
        table.light-table th {{ background-color: var(--sidebar-bg); color: white; font-weight: 800; position: sticky; top: 0; z-index: 10; font-size: 0.75rem; text-transform: uppercase; border-bottom: 2px solid #4c4eb8; cursor: pointer; }}
        table.light-table tbody tr:hover {{ background-color: #f0f4f9; }}
        
        .dataTables_wrapper .dataTables_length, .dataTables_wrapper .dataTables_filter {{ font-size: 0.8rem; margin-bottom: 10px; }}
        .dataTables_wrapper .dataTables_filter input {{ border: 1px solid var(--border-color); border-radius: 4px; padding: 4px 8px; outline: none; }}
        .dataTables_wrapper .dataTables_filter input:focus {{ border-color: var(--accent-purple); }}
        table.dataTable.no-footer {{ border-bottom: 1px solid var(--border-color) !important; }}
        
        /* Export Buttons Custom Styling */
        .dt-buttons {{ float: left; margin-bottom: 10px; }}
        .dataTables_filter {{ float: right; margin-bottom: 10px; }}
        .dt-btn {{ background-color: transparent !important; border: 1px solid var(--accent-purple) !important; color: var(--accent-purple) !important; border-radius: 4px !important; padding: 4px 8px !important; font-size: 0.75rem !important; font-weight: 600 !important; cursor: pointer; margin-right: 5px; }}
        .dt-btn:hover {{ background-color: var(--accent-purple) !important; color: white !important; }}

        /* ---- Added: value perspective, made-vs-missed charts & extra tables (same look & feel) ---- */
        .section-label {{ font-size: 0.7rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; margin: 2px 0 -6px 2px; }}
        .howto-box {{ background: #fffbeb; border: 1px solid #fde68a; border-left: 5px solid #f59e0b; border-radius: var(--border-radius); padding: 10px 15px; font-size: 0.78rem; line-height: 1.55; color: #4b5563; }}
        .howto-box b {{ color: var(--text-main); }}
        .kpi-value.kpi-value-sm {{ font-size: 1.9rem; }}


        /* ---- Added: 6-card KPI rows, donuts, fulfilment cards, branch drill-down ---- */
        .kpi-row {{ grid-template-columns: repeat(6, 1fr); }}
        .kpi-row .kpi-title {{ font-size: 0.64rem; letter-spacing: 0.3px; padding-right: 22px; line-height: 1.25; }}
        @media (min-width: 1850px) {{ .kpi-row .kpi-title {{ font-size: 0.7rem; letter-spacing: 0.5px; }} }}
        .card-green {{ background: linear-gradient(135deg, #d1fae5 0%, #86efac 100%); border: none; }}
        .card-green .kpi-title {{ color: #166534; }} .card-green .kpi-value {{ color: #14532d; }}

        @media (max-width: 1849px) {{ #donutChartContainer, #valRateDonutContainer {{ height: 54px !important; }} #donutChart, #valRateDonut {{ width: 52px !important; height: 52px !important; }} #kpiRateText, #kpiRateVal {{ font-size: 0.9rem !important; }} }}

        /* value-rate donut (same behaviour as the Missed Rate donut) */
        #valRateDonutContainer {{ transition: opacity 0.3s ease-in; }}
        .flip-card:hover #valRateDonutContainer {{ opacity: 0 !important; transition: opacity 0.1s ease-out; }}

        /* items-short donut */
        #itemsDonutContainer {{ position: absolute; right: 12px; top: 28px; width: 52px; height: 52px; display: flex; align-items: center; justify-content: center; transition: opacity 0.3s ease-in; }}
        .flip-card:hover #itemsDonutContainer {{ opacity: 0 !important; transition: opacity 0.1s ease-out; }}
        #itemsRateText {{ position: absolute; font-size: 0.72rem; font-weight: 800; color: #78350f; z-index: 2; }}
        #itemsDonut {{ width: 52px; height: 52px; position: absolute; z-index: 1; }}
        #kpiItems {{ font-size: 1.8rem; }}
        #kpiItemsSub {{ white-space: nowrap; }}
        .items-full {{ display: none; }}
        @media (min-width: 1850px) {{ #itemsDonutContainer, #itemsDonut {{ width: 58px; height: 58px; }} #itemsRateText {{ font-size: 0.85rem; }} #kpiItems {{ font-size: 2rem; }} .items-full {{ display: inline; }} }}
        @media (max-width: 1299px) {{ #itemsDonutContainer, #itemsDonut {{ width: 48px; height: 48px; }} #itemsRateText {{ font-size: 0.64rem; }} #kpiItems {{ font-size: 1.5rem; }} .items-word {{ display: none; }} }}
        @media (max-width: 1150px) {{ .kpi-row {{ grid-template-columns: repeat(3, 1fr); }} #donutChartContainer, #valRateDonutContainer {{ height: 60px !important; }} #donutChart, #valRateDonut {{ width: 58px !important; height: 58px !important; }} #kpiRateText, #kpiRateVal {{ font-size: 1.05rem !important; }} #itemsDonutContainer, #itemsDonut {{ width: 52px; height: 52px; }} #itemsDonutContainer {{ right: 12px; }} #itemsRateText {{ font-size: 0.72rem; }} #kpiItems {{ font-size: 1.8rem; }} }}

        /* figures scale with the card width, so they never overflow at any window size or browser zoom */
        .flip-card {{ container-type: inline-size; }}
        @supports (font-size: 1cqw) {{
            .kpi-row .kpi-value {{ font-size: clamp(1.05rem, 16cqw, 2rem); }}
            #kpiItems {{ font-size: clamp(1rem, 13cqw, 2rem); }}
            #itemsDonutContainer, #itemsDonut {{ width: clamp(36px, 24cqw, 58px); height: clamp(36px, 24cqw, 58px); }}
            #itemsRateText {{ font-size: clamp(0.55rem, 5cqw, 0.85rem); }}
            #valRateDonutContainer, #donutChartContainer {{ height: clamp(44px, 26cqw, 60px) !important; }}
            #valRateDonut, #donutChart {{ width: clamp(44px, 26cqw, 60px) !important; height: clamp(44px, 26cqw, 60px) !important; }}
            #kpiRateVal, #kpiRateText {{ font-size: clamp(0.72rem, 6.4cqw, 1.05rem) !important; }}
            @container (min-width: 190px) {{ .items-word {{ display: inline; }} }}
            @container (min-width: 250px) {{ .items-full {{ display: inline; }} }}
        }}


        /* icons stay perfectly round; cards a little shorter so they look more rectangular */
        .kpi-row .kpi-header .info-icon {{ flex: 0 0 18px; width: 18px; height: 18px; min-width: 18px; aspect-ratio: 1 / 1; }}
        .kpi-row .flip-card {{ height: 100px; }}
        .kpi-row .flip-card-front, .kpi-row .flip-card-back {{ padding: 10px 14px; }}
        .kpi-row .kpi-value {{ margin-top: 2px; }}
        .kpi-row .badge-row {{ margin-top: 6px; }}
        .kpi-row .kpi-formula {{ margin-top: 6px; padding: 4px 6px; font-size: clamp(0.56rem, 5.4cqw, 0.7rem); }}
        .kpi-row .flip-card-back .kpi-title-back {{ font-size: 0.68rem; }}
        @media (min-width: 1700px) {{ .kpi-row .flip-card {{ height: 106px; }} }}
        .kpi-hint {{ font-size: 0.54rem; margin-top: 4px; color: #4b5563; font-weight: 600; }}
        .card-clickable {{ cursor: pointer; }}
        .card-clickable:focus {{ outline: none; }}
        .card-clickable:focus-visible .flip-card-front {{ box-shadow: 0 0 0 2px #7c3aed; }}
        .lg-arrow {{ display: none; }}
        @supports (font-size: 1cqw) {{ @container (min-width: 170px) {{ .lg-arrow {{ display: inline; }} }} }}
        .badge-purple {{ background: #4c1d95; color: #f3e8ff; border: 1px solid #7c3aed; white-space: nowrap; }}
        @media (max-width: 780px) {{ .kpi-row {{ grid-template-columns: repeat(2, 1fr); }} }}

        /* click-to-drill on the branch bar charts */
        .drill-hint {{ font-size: 0.68rem; font-weight: 500; color: var(--text-muted); margin-left: 8px; }}
        #branchValueChart .nsewdrag, #branchQtyChart .nsewdrag, #madeMissValChart .nsewdrag, #madeMissQtyChart .nsewdrag {{ cursor: pointer !important; }}
        #drillOverlay {{ display: none; position: absolute; left: 0; top: 0; width: 100%; z-index: 1000; background: rgba(43, 54, 72, 0.45); }}
        #drillBox {{ position: absolute; left: 50%; transform: translateX(-50%); width: min(1040px, 94%); background: var(--bg-color); border-radius: 12px; box-shadow: 0 12px 40px rgba(0,0,0,0.3); padding: 0 0 16px 0; overflow: hidden; }}
        .drill-head {{ background-image: linear-gradient(to right, #4c4eb8, #5a5ce0); color: #fff; padding: 12px 18px; display: flex; justify-content: space-between; align-items: center; }}
        #drillTitle {{ font-size: 1.15rem; font-weight: 800; }} #drillSub {{ font-size: 0.72rem; opacity: 0.85; margin-top: 2px; }}
        #drillBox:focus {{ outline: none; }}
        #drillClose {{ background: rgba(255,255,255,0.18); border: 1px solid rgba(255,255,255,0.4); color: #fff; width: 32px; height: 32px; border-radius: 6px; font-size: 1.2rem; cursor: pointer; line-height: 1; }}
        #drillClose:hover {{ background: rgba(255,255,255,0.32); }}
        #drillBody {{ padding: 14px 18px 0 18px; display: flex; flex-direction: column; gap: 14px; }}
        .drill-tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
        .drill-tile {{ background: #fff; border: 1px solid var(--border-color); border-radius: 10px; padding: 10px 12px; box-shadow: var(--shadow); }}
        .drill-tile .t-label {{ font-size: 0.65rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; }}
        .drill-tile .t-value {{ font-size: 1.45rem; font-weight: 800; margin-top: 3px; color: var(--text-main); }}
        .drill-tile .t-ord {{ color: #1e3a8a; }} .drill-tile .t-made {{ color: #059669; }} .drill-tile .t-miss {{ color: #e0623f; }}
        .drill-strip {{ background: #fff; border-left: 5px solid var(--accent-purple); border-radius: 10px; padding: 10px 14px; font-size: 0.82rem; color: var(--text-main); line-height: 1.6; box-shadow: var(--shadow); }}
        .drill-grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
        .drill-panel {{ background: #fff; border: 1px solid var(--border-color); border-radius: 10px; padding: 12px; box-shadow: var(--shadow); }}
        .drill-panel h4 {{ margin: 0 0 8px 0; font-size: 0.9rem; font-weight: 600; color: var(--text-main); }}
        .drill-panel .table-container {{ max-height: 330px; }}
        .drill-panel td.num, .drill-panel th.num {{ text-align: right; }}
        .drill-panel .light-table th, .drill-panel .light-table td {{ padding: 8px 6px; font-size: 0.76rem; }}
        .drill-panel .light-table td:nth-child(5) {{ white-space: normal; }}
    </style>
</head>
<body>
    
    <!-- BANNER AT VERY TOP -->
    <div class="header-banner">
        <div class="header-logo">RMS</div>
        <h1>RMS Missed Deliveries & Sales Process KPIs</h1>
    </div>

    <!-- Alert for empty database -->
    <div style="display: { 'none' if not df.empty else 'block' }; background: #e0e7ff; color: #1e3a8a; padding: 15px; border-radius: 8px; border-left: 5px solid #4f46e5; margin-bottom: 20px; font-weight: 500; font-size: 0.95rem; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        ⚠️ The database is currently empty. Please scroll to the Data Management panel at the bottom to upload your data.
    </div>

    <div class="dashboard-container">
        <!-- EXACT PURPLE SIDEBAR -->
        <div class="sidebar">
            <h2>Filters</h2>
            <div class="filter-group">
                <label>Month</label>
                <div class="selectBox">
                    <select id="filterMonth"><option value="All">All Months</option></select>
                </div>
            </div>
            <div class="filter-group">
                <label >Sales Document Type</label>
                <div class="selectBox">
                    <select id="filterType"><option value="All">All Types</option></select>
                </div>
            </div>
            <div class="filter-group">
                <label>Branch Name</label>
                <div class="selectBox">
                    <select id="filterBranch"><option value="All">All Branches</option></select>
                </div>
            </div>
            <div class="filter-group">
                <label>Product Search</label>
                <input type="text" id="filterSearch" placeholder="Type to search...">
            </div>
            <button class="reset-btn" onclick="resetFilters()">Reset Filters</button>
        </div>

        <!-- MAIN CONTENT DASHBOARD -->
        <div class="main-content">
            <!-- TOP ROW -->
            <!-- VALUE PERSPECTIVE (first line) -->
            <div class="section-label">Value perspective (RWF)</div>
            <div class="kpi-row">
                <div class="flip-card">
                    <div class="flip-card-inner" id="card6-inner">
                        <div class="flip-card-front card-blue">
                            <div class="kpi-header"><span class="kpi-title">TOTAL SALES ORDERED</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiOrdVal">0</div>
                            
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">ORDERED VALUE FORMULA</span></div>
                            <div class="kpi-formula">Sum(Order Qty x Net Price)</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card7-inner">
                        <div class="flip-card-front card-teal">
                            <div class="kpi-header"><span class="kpi-title">SALES MADE</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiMadeVal">0</div>
                            <div class="badge-row"><span class="badge badge-teal">Confirmed Value</span></div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">SALES MADE FORMULA</span></div>
                            <div class="kpi-formula">Ordered Value - Missed Value</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card2-inner">
                        <div class="flip-card-front card-yellow">
                            <div class="kpi-header"><span class="kpi-title">MISSED VALUE</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiVal">0</div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">VALUE FORMULA</span></div>
                            <div class="kpi-formula">Sum(Net Value Missed)</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card8-inner">
                        <div class="flip-card-front card-red">
                            <div class="kpi-header"><span class="kpi-title">MISSED RATE (VALUE)</span><span class="info-icon">i</span></div>
                            <div id="valRateDonutContainer" style="display:flex; align-items:center; justify-content: center; height: 75px; width: 100%; padding-top: 5px; position:relative;">
                                <div id="kpiRateVal" style="position: absolute; font-size: 1.05rem; font-weight: 800; color: #7f1d1d; z-index: 2;">0%</div>
                                <div id="valRateDonut" style="width: 80px; height: 80px; position: absolute; z-index: 1;"></div>
                            </div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">VALUE RATE FORMULA</span></div>
                            <div class="kpi-formula">(Missed Value / Ordered Value) * 100</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card9-inner">
                        <div class="flip-card-front card-green">
                            <div class="kpi-header"><span class="kpi-title">FULFILMENT RATE (VALUE)</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiFulfVal">0</div>
                            
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">FULFILMENT FORMULA</span></div>
                            <div class="kpi-formula">(Sales Made / Ordered Value) * 100</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card card-clickable" id="largeCard" role="button" tabindex="0" title="Click to see the large orders">
                    <div class="flip-card-inner" id="card11-inner">
                        <div class="flip-card-front card-purple">
                            <div class="kpi-header"><span class="kpi-title">LARGE ORDERS SHARE</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiLargeShare">0</div>
                            <div class="badge-row"><span class="badge badge-purple" id="kpiLargeSub">-</span></div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">LARGE ORDERS</span></div>
                            <div class="kpi-formula" id="kpiLargeFormula">Missed of large items / Total missed</div>
                            <div class="kpi-hint">Click for the list</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- QUANTITY PERSPECTIVE (second line) -->
            <div class="section-label">Quantity perspective (units)</div>
            <div class="kpi-row">
                <div class="flip-card">
                    <div class="flip-card-inner" id="card3-inner">
                        <div class="flip-card-front card-blue">
                            <div class="kpi-header"><span class="kpi-title">ORDERED QTY</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiOrd">0</div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">ORDERED FORMULA</span></div>
                            <div class="kpi-formula">Sum(Order Qty Item)</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card4-inner">
                        <div class="flip-card-front card-teal">
                            <div class="kpi-header"><span class="kpi-title">DELIVERED QTY</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiDel">0</div>
                            <div class="badge-row"><span class="badge badge-teal">Fulfilled Volume</span></div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">DELIVERED FORMULA</span></div>
                            <div class="kpi-formula">Sum(Conf. Qty Item)</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card5-inner">
                        <div class="flip-card-front card-red">
                            <div class="kpi-header"><span class="kpi-title">MISSED QTY</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiMiss">0</div>
                            <div class="badge-row"><span class="badge badge-red">Unfulfilled Gap</span></div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">MISSED FORMULA</span></div>
                            <div class="kpi-formula">Ord Qty - Del Qty</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card1-inner">
                        <div class="flip-card-front card-purple">
                            <div class="kpi-header"><span class="kpi-title">MISSED RATE (QTY)</span><span class="info-icon">i</span></div>
                            <!-- FIXED PIE CHART OVERLAY (Reduced Font & Handled Flip) -->
                            <div id="donutChartContainer" style="display:flex; align-items:center; justify-content: center; height: 75px; width: 100%; padding-top: 5px; position:relative;">
                                <div id="kpiRateText" style="position: absolute; font-size: 1.05rem; font-weight: 800; color: #4c1d95; z-index: 2;">0%</div>
                                <div id="donutChart" style="width: 80px; height: 80px; position: absolute; z-index: 1;"></div>
                            </div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">MISSED RATE FORMULA</span></div>
                            <div class="kpi-formula">(Total Missed Vol / Total Ordered Vol) * 100</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card12-inner">
                        <div class="flip-card-front card-green">
                            <div class="kpi-header"><span class="kpi-title">FULFILMENT RATE (QTY)</span><span class="info-icon">i</span></div>
                            <div class="kpi-value" id="kpiFulfQty">0</div>
                            
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">FULFILMENT FORMULA</span></div>
                            <div class="kpi-formula">((Ordered Qty - Missed Qty) / Ordered Qty) * 100</div>
                        </div>
                    </div>
                </div>
                <div class="flip-card">
                    <div class="flip-card-inner" id="card10-inner">
                        <div class="flip-card-front card-yellow">
                            <div class="kpi-header"><span class="kpi-title">ORDER ITEMS SHORT</span><span class="info-icon">i</span></div>
                            <div id="itemsDonutContainer"><div id="itemsRateText">0%</div><div id="itemsDonut"></div></div>
                            <div class="kpi-value" id="kpiItems">0</div>
                            <div class="badge-row"><span class="badge badge-red" id="kpiItemsSub">-</span></div>
                        </div>
                        <div class="flip-card-back">
                            <div class="kpi-header"><span class="kpi-title-back">ITEMS SHORT FORMULA</span></div>
                            <div class="kpi-formula">(Items with Missed Qty > 0 / Total items) * 100</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- DYNAMIC EXECUTIVE INSIGHTS BANNER -->
            <div class="panel col-12" style="background: #ffffff; border-left: 5px solid var(--accent-purple);">
                <div class="panel-header" style="margin-bottom: 12px; color: var(--text-main); font-size: 16px;">
                    Dynamic Executive Insights
                </div>
                <ul id="dynamicInsights" style="list-style: none; padding-left: 0; display: flex; flex-direction: column; gap: 10px; font-size: 13px; color: var(--text-muted); line-height: 1.5; margin: 0;">
                </ul>
            </div>
            
            <!-- ADDED: HOW TO READ -->
            <div class="howto-box">
                <b>How to read:</b> <b>Ordered</b> = what customers asked for. <b>Made</b> = ordered minus missed (confirmed). <b>Missed</b> = ordered but not confirmed (mostly no stock).
                <b>Missed rate</b> = missed &divide; ordered of the <i>same</i> branch or month (its own failure rate). <b>Share of all missed</b> = that branch's missed &divide; missed of <i>all</i> branches.
                Value rates (RWF) and unit rates differ because the missed items are often expensive. <b>Large orders share</b> = part of the missed value that comes from single items of RWF {LARGE_THRESHOLD // 1000000}M+ (click that card to list them).
                <span id="ordValNote" style="color:#b45309; font-weight:600;"></span>
            </div>

            <!-- BAR CHARTS ROW -->
            <div class="grid-row-middle">
                <div class="panel">
                    <div class="panel-header">Lost Value by Branch <span class="info-icon">i</span></div>
                    <div class="chart-container" id="branchValueChart"></div>
                </div>
                <div class="panel">
                    <div class="panel-header">Top Branches by Missed Quantity <span class="info-icon">i</span></div>
                    <div class="chart-container" id="branchQtyChart"></div>
                </div>
            </div>

            <!-- TRENDS & DOC TYPES ROW -->
            <div class="grid-row-middle">
                <div class="panel">
                    <div class="panel-header">Missed Deliveries Trends <span class="info-icon">i</span></div>
                    <div class="chart-container" id="trendChart"></div>
                </div>
                <div class="panel">
                    <div class="panel-header">Lost Value by Doc Type <span class="info-icon">i</span></div>
                    <div class="chart-container" id="docTypeChart"></div>
                </div>
            </div>

            <!-- ADDED: MADE vs MISSED BY BRANCH -->
            <div class="grid-row-middle">
                <div class="panel">
                    <div class="panel-header">Value: Made vs Missed by Branch (% of own orders) <span class="info-icon">i</span></div>
                    <div class="chart-container" id="madeMissValChart"></div>
                </div>
                <div class="panel">
                    <div class="panel-header">Units: Made vs Missed by Branch (% of own orders) <span class="info-icon">i</span></div>
                    <div class="chart-container" id="madeMissQtyChart"></div>
                </div>
            </div>

            <!-- ADDED: MONTHLY MADE vs MISSED -->
            <div class="grid-row-middle">
                <div class="panel">
                    <div class="panel-header">Sales Made vs Missed by Month (RWF) <span class="info-icon">i</span></div>
                    <div class="chart-container" id="monthValueChart"></div>
                </div>
                <div class="panel">
                    <div class="panel-header">Missed Rate by Month: Value % vs Units % <span class="info-icon">i</span></div>
                    <div class="chart-container" id="monthRateChart"></div>
                </div>
            </div>
        </div>
    </div> <!-- END OF DASHBOARD CONTAINER -->

    <!-- FULL WIDTH TABLES AT BOTTOM -->
    <div class="grid-row-middle" style="margin-top: 15px;">
        <div class="panel">
            <div class="panel-header">Branch-wise Lost Sales Summary</div>
            <div class="table-container">
                <table class="light-table display nowrap" id="locationTable" style="width:100%">
                    <thead>
                        <tr><th>Branch</th><th># Products</th><th>Lost Sales (RWF)</th><th>Contribution %</th></tr>
                    </thead>
                </table>
            </div>
        </div>
        <div class="panel">
            <div class="panel-header">Top Missed Items</div>
            <div class="table-container">
                <table class="light-table display nowrap" id="topItemsTable" style="width:100%">
                    <thead>
                        <tr><th>Product</th><th>Lines</th><th>Lost RWF</th></tr>
                    </thead>
                </table>
            </div>
        </div>
    </div>

    <!-- ADDED: BRANCH PERFORMANCE / MONTHLY SUMMARY / LARGE ORDERS -->
    <div class="panel" style="margin-top: 15px;">
        <div class="panel-header">Branch Performance: Ordered vs Made vs Missed</div>
        <div class="table-container">
            <table class="light-table display nowrap" id="branchPerfTable" style="width:100%">
                <thead>
                    <tr><th>Branch</th><th>Sales Ordered (RWF)</th><th>Sales Made (RWF)</th><th>Sales Missed (RWF)</th><th>Missed % (Value)</th><th>Share of All Missed</th><th>Units Ordered</th><th>Units Made</th><th>Units Missed</th><th>Missed % (Units)</th></tr>
                </thead>
            </table>
        </div>
    </div>
    <div class="panel" style="margin-top: 15px;">
        <div class="panel-header">Monthly Summary</div>
        <div class="table-container">
            <table class="light-table display nowrap" id="monthSummaryTable" style="width:100%">
                <thead>
                    <tr><th>Month</th><th>Sales Ordered (RWF)</th><th>Sales Made (RWF)</th><th>Sales Missed (RWF)</th><th>Missed % (Value)</th><th>Units Ordered</th><th>Units Made</th><th>Units Missed</th><th>Missed % (Units)</th><th>Share of Missed Value</th></tr>
                </thead>
            </table>
        </div>
    </div>
    <div class="panel" style="margin-top: 15px;">
        <div class="panel-header">Large Orders to Verify (single items with RWF 25M+ missed)</div>
        <div class="table-container">
            <table class="light-table display nowrap" id="largeTable" style="width:100%">
                <thead>
                    <tr><th>Sales Doc</th><th>Item</th><th>Doc Date</th><th>Branch</th><th>Product</th><th>Ord Qty</th><th>Del Qty</th><th>Miss Qty</th><th>Lost Value (RWF)</th></tr>
                </thead>
            </table>
        </div>
    </div>

    <div class="panel" style="margin-top: 15px;">
        <div class="panel-header">Unfulfilled Details Log</div>
        <div class="table-container">
            <table id="detailsTable" class="light-table display nowrap" style="width:100%">
                <thead>
                    <tr>
                        <th>Month</th><th>Doc Date</th><th>Sales Doc Type</th>
                        <th>Branch</th><th>Product Desc.</th><th>Week Range</th>
                        <th>Ord Qty</th><th>Del Qty</th><th>Miss Qty</th><th>Lost Value (RWF)</th>
                    </tr>
                </thead>
            </table>
        </div>
    </div>

    <!-- ADDED: BRANCH DRILL-DOWN (opens when a branch bar is clicked) -->
    <div id="drillOverlay">
        <div id="drillBox">
            <div class="drill-head">
                <div><div id="drillTitle"></div><div id="drillSub"></div></div>
                <button id="drillClose" title="Close (Esc)">&times;</button>
            </div>
            <div id="drillBody"></div>
        </div>
    </div>

    <!-- JAVASCRIPT: INSTANT FILTERING & RENDERING -->
    <script>
        const rawPacked = {json_data};
        const rawData = (function (p) {{
            const out = new Array(p.n);
            for (let i = 0; i < p.n; i++) {{
                const o = {{}};
                for (let k = 0; k < p.keys.length; k++) {{
                    const key = p.keys[k], d = p.dict[key];
                    o[key] = d ? d[p.cols[key][i]] : p.cols[key][i];
                }}
                out[i] = o;
            }}
            return out;
        }})(rawPacked);
        let tableDetails, tableLoc, tableItems;
        const largeData = {large_json};
        const ordValReady = {ORDVAL_READY_JS};
        const LARGE_T = {LARGE_THRESHOLD};
        let tableBranchPerf, tableMonthSum, tableLarge;

        const btnConfig = [
            {{ extend: 'excelHtml5', text: '📥 Excel', className: 'dt-btn' }},
            {{ extend: 'csvHtml5', text: '📥 CSV', className: 'dt-btn' }}
        ];

        function formatNum(num) {{
            if (num >= 1000000000) return (num/1000000000).toFixed(2) + 'B';
            if (num >= 1000000) return (num/1000000).toFixed(2) + 'M';
            if (num >= 1000) return (num/1000).toFixed(1) + 'k';
            return Math.round(num);
        }}

        function initApp() {{
            if (!rawData || rawData.length === 0) return;
            
            // Populate Dropdowns
            const months = [...new Set(rawData.map(d => d.Month))];
            const types = [...new Set(rawData.map(d => d.Sales_Doc_Type))];
            const branches = [...new Set(rawData.map(d => d.Branch))];

            months.forEach(m => document.getElementById('filterMonth').insertAdjacentHTML('beforeend', `<option value="${{m}}">${{m}}</option>`));
            types.forEach(t => document.getElementById('filterType').insertAdjacentHTML('beforeend', `<option value="${{t}}">${{t}}</option>`));
            branches.forEach(b => document.getElementById('filterBranch').insertAdjacentHTML('beforeend', `<option value="${{b}}">${{b}}</option>`));

            // Init DataTables with Buttons and Sorting
            tableDetails = $('#detailsTable').DataTable({{
                dom: 'Bfrtip', buttons: btnConfig,
                columns: [
                    {{data: 'Month'}}, {{data: 'Date'}}, {{data: 'Sales_Doc_Type'}}, 
                    {{data: 'Branch'}}, {{data: 'Product_Desc'}}, {{data: 'Week_Range'}},
                    {{data: 'Order_Qty', render: function(d) {{ return (d === null || d === undefined || d === '') ? '' : Number(d).toLocaleString('en-US', {{maximumFractionDigits: 3}}); }}}}, {{data: 'Confirmed_Qty', render: function(d) {{ return (d === null || d === undefined || d === '') ? '' : Number(d).toLocaleString('en-US', {{maximumFractionDigits: 3}}); }}}}, 
                    {{data: 'Missed_Qty', render: function(data){{ return `<span style="color:#f48a6a; font-weight:bold;">${{Number(data).toLocaleString('en-US', {{maximumFractionDigits: 3}})}}</span>`; }}}},
                    {{data: 'Lost_Value', render: $.fn.dataTable.render.number(',', '.', 0)}}
                ],
                order: [[9, 'desc']], paging: false, scrollY: "400px", scrollCollapse: true, info: false, deferRender: true
            }});

            tableLoc = $('#locationTable').DataTable({{
                dom: 'Brtip', buttons: btnConfig,
                columns: [
                    {{data: 'Branch'}}, {{data: 'Products', render: function(d) {{ return (d === null || d === undefined || d === '') ? '' : Number(d).toLocaleString('en-US', {{maximumFractionDigits: 3}}); }}}},
                    {{data: 'Lost', render: $.fn.dataTable.render.number(',', '.', 0)}},
                    {{data: 'Pct', render: function(d){{ return d + '%'; }}}}
                ],
                order: [[2, 'desc']], paging: false, scrollY: "300px", scrollCollapse: true, info: false, bFilter: false
            }});

            tableItems = $('#topItemsTable').DataTable({{
                dom: 'Brtip', buttons: btnConfig,
                columns: [
                    {{data: 'Product'}}, {{data: 'Lines', render: function(d) {{ return (d === null || d === undefined || d === '') ? '' : Number(d).toLocaleString('en-US', {{maximumFractionDigits: 3}}); }}}},
                    {{data: 'Lost', render: $.fn.dataTable.render.number(',', '.', 0)}}
                ],
                order: [[2, 'desc']], paging: false, scrollY: "300px", scrollCollapse: true, info: false, bFilter: false
            }});

            // ADDED: init of the extra tables (guarded so the original tables are never affected)
            try {{
                const numR = function(d) {{ return (d === null || d === undefined) ? '&mdash;' : Number(d).toLocaleString('en-US', {{maximumFractionDigits: 0}}); }};
                const pctR = function(d) {{ return (d === null || d === undefined) ? '&mdash;' : Number(d).toFixed(1) + '%'; }};
                tableBranchPerf = $('#branchPerfTable').DataTable({{
                    dom: 'Brtip', buttons: btnConfig,
                    columns: [ {{data: 'Branch'}}, {{data: 'OrdVal', render: numR}}, {{data: 'MadeVal', render: numR}}, {{data: 'MissVal', render: numR}}, {{data: 'RateVal', render: pctR}}, {{data: 'Share', render: pctR}},
                               {{data: 'OrdQty', render: numR}}, {{data: 'MadeQty', render: numR}}, {{data: 'MissQty', render: numR}}, {{data: 'RateQty', render: pctR}} ],
                    order: [[3, 'desc']], paging: false, scrollY: "320px", scrollCollapse: true, info: false, bFilter: false
                }});
                tableMonthSum = $('#monthSummaryTable').DataTable({{
                    dom: 'Brtip', buttons: btnConfig,
                    columns: [ {{data: 'Month'}}, {{data: 'OrdVal', render: numR}}, {{data: 'MadeVal', render: numR}}, {{data: 'MissVal', render: numR}}, {{data: 'RateVal', render: pctR}},
                               {{data: 'OrdQty', render: numR}}, {{data: 'MadeQty', render: numR}}, {{data: 'MissQty', render: numR}}, {{data: 'RateQty', render: pctR}}, {{data: 'Share', render: pctR}} ],
                    ordering: false, paging: false, info: false, bFilter: false
                }});
                tableLarge = $('#largeTable').DataTable({{
                    dom: 'Brtip', buttons: btnConfig,
                    columns: [ {{data: 'Doc'}}, {{data: 'Item'}}, {{data: 'Date'}}, {{data: 'Branch'}}, {{data: 'Product_Desc'}}, {{data: 'Order_Qty', render: numR}}, {{data: 'Confirmed_Qty', render: numR}},
                               {{data: 'Missed_Qty', render: function(d){{ return '<span style="color:#f48a6a; font-weight:bold;">' + numR(d) + '</span>'; }}}}, {{data: 'Lost_Value', render: numR}} ],
                    order: [[8, 'desc']], paging: false, scrollY: "300px", scrollCollapse: true, info: false, bFilter: false
                }});
            }} catch (e) {{ console.error('Extra tables init error:', e); }}

            // Listeners
            document.getElementById('filterMonth').addEventListener('change', updateDashboard);
            document.getElementById('filterType').addEventListener('change', updateDashboard);
            document.getElementById('filterBranch').addEventListener('change', updateDashboard);
            document.getElementById('filterSearch').addEventListener('keyup', updateDashboard);

            updateDashboard();
        }}

        function resetFilters() {{
            document.getElementById('filterMonth').value = 'All';
            document.getElementById('filterType').value = 'All';
            document.getElementById('filterBranch').value = 'All';
            document.getElementById('filterSearch').value = '';
            updateDashboard();
        }}

        function updateDashboard() {{
            const fMonth = document.getElementById('filterMonth').value;
            const fType = document.getElementById('filterType').value;
            const fBranch = document.getElementById('filterBranch').value;
            const fSearch = document.getElementById('filterSearch').value.toLowerCase();

            // JS Instant Filter with strict String safety for searches
            // Automatically excludes Missed_Qty <= 0 for KPI accuracy
            const filtered = rawData.filter(r => {{
                if (r.Missed_Qty <= 0) return false;
                if (fMonth !== 'All' && r.Month !== fMonth) return false;
                if (fType !== 'All' && r.Sales_Doc_Type !== fType) return false;
                if (fBranch !== 'All' && r.Branch !== fBranch) return false;
                if (fSearch && !String(r.Product_Desc || "").toLowerCase().includes(fSearch)) return false;
                return true;
            }});

            let totalVal = 0, ordQty = 0, delQty = 0, missQty = 0, missedLines = 0, totalLines = 0;
            let monthAgg = {{}};
            const branchAggr = {{}};
            const typeAggr = {{}};
            const itemAggr = {{}};
            const weekAggr = {{}};
            const weekLabels = {{}};
            
            // Re-calculate totals using ALL data (including Missed_Qty=0) so the KPIs are accurate
            const rawFiltered = rawData.filter(r => {{
                if (fMonth !== 'All' && r.Month !== fMonth) return false;
                if (fType !== 'All' && r.Sales_Doc_Type !== fType) return false;
                if (fBranch !== 'All' && r.Branch !== fBranch) return false;
                if (fSearch && !String(r.Product_Desc || "").toLowerCase().includes(fSearch)) return false;
                return true;
            }});

            rawFiltered.forEach(r => {{
                totalVal += r.Lost_Value || 0;
                ordQty += r.Order_Qty || 0;
                delQty += r.Confirmed_Qty || 0;
                missQty += r.Missed_Qty || 0;
                totalLines += r.Total_Lines || 1;
                missedLines += r.Missed_Lines || 0;
            }});
            
            // Generate charts/tables using only missed data (>0)
            filtered.forEach(r => {{
                if(!monthAgg[r.Month]) monthAgg[r.Month] = 0;
                monthAgg[r.Month] += r.Lost_Value || 0;

                if(!branchAggr[r.Branch]) branchAggr[r.Branch] = {{val:0, qty:0, prods:new Set()}};
                branchAggr[r.Branch].val += r.Lost_Value;
                branchAggr[r.Branch].qty += r.Missed_Qty;
                branchAggr[r.Branch].prods.add(r.Product_Desc);

                if(!typeAggr[r.Sales_Doc_Type]) typeAggr[r.Sales_Doc_Type] = 0;
                typeAggr[r.Sales_Doc_Type] += r.Lost_Value;

                if(!itemAggr[r.Product_Desc]) itemAggr[r.Product_Desc] = {{lines:0, lost:0}};
                itemAggr[r.Product_Desc].lines += r.Total_Lines;
                itemAggr[r.Product_Desc].lost += r.Lost_Value;

                if(!weekAggr[r.Week_Start]) {{
                    weekAggr[r.Week_Start] = 0;
                    weekLabels[r.Week_Start] = r.Week_Range;
                }}
                weekAggr[r.Week_Start] += r.Lost_Value;
            }});

            // Volume-based missed rate
            const rate = ordQty > 0 ? ((missQty / ordQty) * 100).toFixed(1) : 0;

            document.getElementById('kpiRateText').innerText = rate + '%';
            document.getElementById('kpiVal').innerText = formatNum(totalVal);
            document.getElementById('kpiOrd').innerText = formatNum(ordQty);
            document.getElementById('kpiDel').innerText = formatNum(delQty);
            document.getElementById('kpiMiss').innerText = formatNum(missQty);

            Plotly.react('donutChart', [{{
                values: [missQty, delQty], labels: ['Missed Vol', 'Delivered Vol'],
                type: 'pie', hole: 0.7, textinfo: 'none', hoverinfo: 'label+percent', marker: {{colors: ['#4c1d95', '#e9d5ff']}}
            }}], {{ margin: {{t:0, b:0, l:0, r:0}}, showlegend: false, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)' }});

            // Update Tables
            const detailsRows = filtered.sort((a,b)=>b.Lost_Value - a.Lost_Value).slice(0, 1000).map(r => ({{ Month: r.Month, Date: r.Date, Sales_Doc_Type: r.Sales_Doc_Type, Branch: r.Branch, Product_Desc: r.Product_Desc, Week_Range: r.Week_Range, Order_Qty: r.Order_Qty, Confirmed_Qty: r.Confirmed_Qty, Missed_Qty: r.Missed_Qty, Lost_Value: r.Lost_Value }}));
            tableDetails.clear().rows.add(detailsRows).draw();

            const locTableData = Object.keys(branchAggr).map(k => ({{Branch: k, Products: branchAggr[k].prods.size, Lost: branchAggr[k].val, Pct: totalVal > 0 ? ((branchAggr[k].val / totalVal)*100).toFixed(1) : 0}}));
            tableLoc.clear().rows.add(locTableData).draw();

            const itemsTableData = Object.keys(itemAggr).map(k => ({{Product: k, Lines: itemAggr[k].lines, Lost: itemAggr[k].lost}}));
            tableItems.clear().rows.add(itemsTableData).draw();

            // Update Charts (Added Numbers inside/outside bars)
            const topBranchVal = Object.keys(branchAggr).sort((a,b)=>branchAggr[b].val - branchAggr[a].val).slice(0,10).reverse();
            Plotly.react('branchValueChart', [{{ 
                x: topBranchVal.map(b=>branchAggr[b].val), y: topBranchVal, type: 'bar', orientation: 'h', marker: {{color: '#f48a6a'}},
                text: topBranchVal.map(b=>formatNum(branchAggr[b].val)), textposition: 'auto'
            }}], {{margin: {{l: 120, r: 35, t: 10, b: 30}}}});

            const topBranchQty = Object.keys(branchAggr).sort((a,b)=>branchAggr[b].qty - branchAggr[a].qty).slice(0,10).reverse();
            Plotly.react('branchQtyChart', [{{ 
                x: topBranchQty.map(b=>branchAggr[b].qty), y: topBranchQty, type: 'bar', orientation: 'h', marker: {{color: '#5a5ce0'}},
                text: topBranchQty.map(b=>formatNum(branchAggr[b].qty)), textposition: 'auto'
            }}], {{margin: {{l: 120, r: 35, t: 10, b: 30}}}});

            const dTypes = Object.keys(typeAggr);
            Plotly.react('docTypeChart', [{{ labels: dTypes, values: dTypes.map(d=>typeAggr[d]), type: 'pie', hole:0.4, marker: {{colors: ['#5a5ce0', '#f48a6a', '#34d399', '#fef08a']}} }}], {{margin: {{l: 20, r: 20, t: 10, b: 30}}, showlegend: true}});

            // Trend Chart
            const sortedWeekStarts = Object.keys(weekAggr).sort();
            const xLabels = sortedWeekStarts.map(ws => weekLabels[ws]);
            const yVals = sortedWeekStarts.map(ws => weekAggr[ws]);
            Plotly.react('trendChart', [{{ x: xLabels, y: yVals, type: 'scatter', mode: 'lines+markers', line: {{color: '#10b981', width: 3}} }}], {{margin: {{l: 50, r: 20, t: 10, b: 60}}, xaxis: {{tickangle: -45, automargin: true}}}});

            // Dynamic Text Update
            let maxMonth = "", maxMonthVal = 0;
            for(const [m, v] of Object.entries(monthAgg)) {{ if(v > maxMonthVal) {{maxMonthVal = v; maxMonth = m;}} }}
            const pct = totalVal > 0 ? ((maxMonthVal / totalVal) * 100).toFixed(1) : 0;
            
            document.getElementById('dynamicInsights').innerHTML = `
                <li style="display: flex; align-items: flex-start; gap: 8px;">
                    <span style="color: var(--accent-orange); font-weight: bold; min-width: 160px;">• Peak Month Analysis:</span>
                    <span style="color: var(--text-main);"><b>${{maxMonth || 'N/A'}}</b> accounted for <b>${{pct}}%</b> of all lost sales value (RWF ${{formatNum(maxMonthVal)}}) within the selected filters.</span>
                </li>
                <li style="display: flex; align-items: flex-start; gap: 8px;">
                    <span style="color: var(--accent-green); font-weight: bold; min-width: 160px;">• Volume Overview:</span>
                    <span style="color: var(--text-main);">Across your selection, a total of <b>${{formatNum(missQty)}} units</b> were unfulfilled out of <b>${{formatNum(ordQty)}} ordered</b>.</span>
                </li>
            `;

            try {{ updateExtras(); }} catch (e) {{ console.error('Extras error:', e); }}
        }}

        // ADDED: value-perspective cards, made-vs-missed charts, branch/month/large-order tables and extra insights.
        // Runs after the original updateDashboard() and only reads rawData/largeData, so the original visuals are never altered.
        function updateExtras() {{
            const fMonth = document.getElementById('filterMonth').value;
            const fType = document.getElementById('filterType').value;
            const fBranch = document.getElementById('filterBranch').value;
            const fSearch = document.getElementById('filterSearch').value.toLowerCase();
            const hasV = ordValReady;
            const MADE_C = '#34d399', MISS_C = '#f48a6a', PURPLE_C = '#5a5ce0';
            const NA = '\u2014';
            const fmtInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
            const pctOf = (a, b) => b > 0 ? (a / b) * 100 : null;
            const pctTxt = (x) => x === null ? NA : x.toFixed(1) + '%';

            const pass = (r, useBranch) => {{
                if (fMonth !== 'All' && r.Month !== fMonth) return false;
                if (fType !== 'All' && r.Sales_Doc_Type !== fType) return false;
                if (useBranch && fBranch !== 'All' && r.Branch !== fBranch) return false;
                if (fSearch && !String(r.Product_Desc || "").toLowerCase().includes(fSearch)) return false;
                return true;
            }};
            const rows = rawData.filter(r => pass(r, true));
            const baseRows = fBranch === 'All' ? rows : rawData.filter(r => pass(r, false));
            const blank = () => ({{ordQty: 0, missQty: 0, ordVal: 0, missVal: 0, items: 0, short: 0}});
            const add = (a, r) => {{ a.ordQty += r.Order_Qty || 0; a.missQty += r.Missed_Qty || 0; a.ordVal += r.Ordered_Value || 0; a.missVal += r.Lost_Value || 0; a.items += r.Total_Lines || 1; a.short += r.Missed_Lines || 0; return a; }};
            const T = rows.reduce(add, blank());
            const B = baseRows.reduce(add, blank());
            const madeVal = T.ordVal - T.missVal;

            // ---- KPI cards
            document.getElementById('ordValNote').innerHTML = hasV ? '' : '<br>\u26A0 Ordered / Made value needs one re-upload of the Excel file (Data Management panel) to fill the new Ordered_Value column.';
            document.getElementById('kpiOrdVal').innerText = hasV ? formatNum(T.ordVal) : NA;
            document.getElementById('kpiMadeVal').innerText = hasV ? formatNum(madeVal) : NA;
            document.getElementById('kpiRateVal').innerText = hasV ? pctTxt(pctOf(T.missVal, T.ordVal)) : NA;
            document.getElementById('kpiFulfVal').innerText = hasV ? pctTxt(pctOf(madeVal, T.ordVal)) : NA;
            document.getElementById('kpiFulfQty').innerText = pctTxt(pctOf(T.ordQty - T.missQty, T.ordQty));
            Plotly.react('valRateDonut', [{{
                values: hasV ? [T.missVal, Math.max(T.ordVal - T.missVal, 0)] : [0, 1], labels: ['Missed Value', 'Made Value'],
                type: 'pie', hole: 0.7, textinfo: 'none', hoverinfo: 'label+percent', marker: {{colors: ['#7f1d1d', '#fecaca']}}
            }}], {{margin: {{t: 0, b: 0, l: 0, r: 0}}, showlegend: false, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)'}}, {{displayModeBar: false}});
            document.getElementById('kpiItems').innerText = fmtInt(T.short);
            document.getElementById('kpiItemsSub').innerHTML = '<span class="items-full">' + fmtInt(T.short) + ' </span>of ' + fmtInt(T.items) + '<span class="items-word"> items</span>';
            document.getElementById('itemsRateText').innerText = pctTxt(pctOf(T.short, T.items));
            Plotly.react('itemsDonut', [{{
                values: [T.short, Math.max(T.items - T.short, 0)], labels: ['Items short', 'Items fully served'],
                type: 'pie', hole: 0.7, textinfo: 'none', hoverinfo: 'label+percent', marker: {{colors: ['#92400e', '#fef3c7']}}
            }}], {{margin: {{t: 0, b: 0, l: 0, r: 0}}, showlegend: false, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)'}}, {{staticPlot: false, displayModeBar: false}});

            // ---- Branch aggregation
            const br = {{}};
            rows.forEach(r => {{ if (!br[r.Branch]) br[r.Branch] = blank(); add(br[r.Branch], r); }});
            const brNames = Object.keys(br);

            const stackedBranch = (chartId, names, getMade, getMiss, getAbs, valueMode) => {{
                if (valueMode && !hasV) {{
                    Plotly.react(chartId, [], {{margin: {{l: 20, r: 20, t: 10, b: 30}}, xaxis: {{visible: false}}, yaxis: {{visible: false}},
                        annotations: [{{text: 'Re-upload the Excel once to enable value ordered / made', showarrow: false, font: {{size: 13, color: '#8b95a5'}}}}]}});
                    return;
                }}
                const ys = names.slice().reverse();
                const made = ys.map(getMade), miss = ys.map(getMiss);
                Plotly.react(chartId, [
                    {{x: made, y: ys, type: 'bar', orientation: 'h', name: 'Made', marker: {{color: MADE_C}}, text: made.map(v => v.toFixed(0) + '%'), textposition: 'inside', hovertext: ys.map((b, i) => b + '<br>Made: ' + getAbs(b, 'made') + ' (' + made[i].toFixed(1) + '%)'), hoverinfo: 'text'}},
                    {{x: miss, y: ys, type: 'bar', orientation: 'h', name: 'Missed', marker: {{color: MISS_C}}, text: miss.map(v => v.toFixed(0) + '%'), textposition: 'inside', hovertext: ys.map((b, i) => b + '<br>Missed: ' + getAbs(b, 'miss') + ' (' + miss[i].toFixed(1) + '%)'), hoverinfo: 'text'}}
                ], {{barmode: 'stack', margin: {{l: 120, r: 35, t: 10, b: 50}}, xaxis: {{range: [0, 100], ticksuffix: '%'}}, legend: {{orientation: 'h', y: -0.18, traceorder: 'normal'}}}});
            }};
            const topV = brNames.slice().sort((a, b) => br[b].missVal - br[a].missVal).slice(0, 10);
            stackedBranch('madeMissValChart', topV,
                b => br[b].ordVal > 0 ? ((br[b].ordVal - br[b].missVal) / br[b].ordVal) * 100 : 0,
                b => br[b].ordVal > 0 ? (br[b].missVal / br[b].ordVal) * 100 : 0,
                (b, k) => 'RWF ' + formatNum(k === 'made' ? br[b].ordVal - br[b].missVal : br[b].missVal) + ' of RWF ' + formatNum(br[b].ordVal), true);
            const topQ = brNames.slice().sort((a, b) => br[b].missQty - br[a].missQty).slice(0, 10);
            stackedBranch('madeMissQtyChart', topQ,
                b => br[b].ordQty > 0 ? ((br[b].ordQty - br[b].missQty) / br[b].ordQty) * 100 : 0,
                b => br[b].ordQty > 0 ? (br[b].missQty / br[b].ordQty) * 100 : 0,
                (b, k) => formatNum(k === 'made' ? br[b].ordQty - br[b].missQty : br[b].missQty) + ' of ' + formatNum(br[b].ordQty) + ' units', false);

            // ---- Branch performance table
            const branchRows = brNames.map(k => ({{
                Branch: k,
                OrdVal: hasV ? br[k].ordVal : null, MadeVal: hasV ? br[k].ordVal - br[k].missVal : null, MissVal: br[k].missVal,
                RateVal: hasV ? pctOf(br[k].missVal, br[k].ordVal) : null, Share: pctOf(br[k].missVal, B.missVal),
                OrdQty: br[k].ordQty, MadeQty: br[k].ordQty - br[k].missQty, MissQty: br[k].missQty, RateQty: pctOf(br[k].missQty, br[k].ordQty)}}));
            tableBranchPerf.clear().rows.add(branchRows).draw();

            // ---- Month aggregation (chronological)
            const mo = {{}};
            rows.forEach(r => {{
                const key = String(r.Date || '').slice(0, 7) || r.Month;
                if (!mo[key]) mo[key] = Object.assign(blank(), {{label: r.Month}});
                add(mo[key], r);
            }});
            const moKeys = Object.keys(mo).sort();
            const moLabels = moKeys.map(k => mo[k].label);
            const moMiss = moKeys.reduce((a, k) => a + mo[k].missVal, 0);
            tableMonthSum.clear().rows.add(moKeys.map(k => ({{
                Month: mo[k].label,
                OrdVal: hasV ? mo[k].ordVal : null, MadeVal: hasV ? mo[k].ordVal - mo[k].missVal : null, MissVal: mo[k].missVal, RateVal: hasV ? pctOf(mo[k].missVal, mo[k].ordVal) : null,
                OrdQty: mo[k].ordQty, MadeQty: mo[k].ordQty - mo[k].missQty, MissQty: mo[k].missQty, RateQty: pctOf(mo[k].missQty, mo[k].ordQty), Share: pctOf(mo[k].missVal, moMiss)}}))).draw();

            if (hasV) {{
                Plotly.react('monthValueChart', [
                    {{x: moLabels, y: moKeys.map(k => mo[k].ordVal - mo[k].missVal), type: 'bar', name: 'Made', marker: {{color: MADE_C}}, text: moKeys.map(k => formatNum(mo[k].ordVal - mo[k].missVal)), textposition: 'auto'}},
                    {{x: moLabels, y: moKeys.map(k => mo[k].missVal), type: 'bar', name: 'Missed', marker: {{color: MISS_C}}, text: moKeys.map(k => formatNum(mo[k].missVal)), textposition: 'auto'}}
                ], {{barmode: 'stack', margin: {{l: 60, r: 20, t: 10, b: 50}}, yaxis: {{range: [0, Math.max(...moKeys.map(k => mo[k].ordVal)) * 1.12]}}, legend: {{orientation: 'h', y: -0.2, traceorder: 'normal'}}}});
                Plotly.react('monthRateChart', [
                    {{x: moLabels, y: moKeys.map(k => pctOf(mo[k].missVal, mo[k].ordVal) || 0), type: 'bar', name: 'Value %', marker: {{color: MISS_C}}, text: moKeys.map(k => pctTxt(pctOf(mo[k].missVal, mo[k].ordVal))), textposition: 'auto'}},
                    {{x: moLabels, y: moKeys.map(k => pctOf(mo[k].missQty, mo[k].ordQty) || 0), type: 'bar', name: 'Units %', marker: {{color: PURPLE_C}}, text: moKeys.map(k => pctTxt(pctOf(mo[k].missQty, mo[k].ordQty))), textposition: 'auto'}}
                ], {{barmode: 'group', margin: {{l: 50, r: 20, t: 10, b: 50}}, yaxis: {{ticksuffix: '%'}}, legend: {{orientation: 'h', y: -0.2, traceorder: 'normal'}}}});
            }} else {{
                const msg = {{margin: {{l: 20, r: 20, t: 10, b: 30}}, xaxis: {{visible: false}}, yaxis: {{visible: false}}, annotations: [{{text: 'Re-upload the Excel once to enable value ordered / made', showarrow: false, font: {{size: 13, color: '#8b95a5'}}}}]}};
                Plotly.react('monthValueChart', [], msg);
                Plotly.react('monthRateChart', [{{x: moLabels, y: moKeys.map(k => pctOf(mo[k].missQty, mo[k].ordQty) || 0), type: 'bar', name: 'Units %', marker: {{color: PURPLE_C}}, text: moKeys.map(k => pctTxt(pctOf(mo[k].missQty, mo[k].ordQty))), textposition: 'auto'}}],
                    {{margin: {{l: 50, r: 20, t: 10, b: 50}}, yaxis: {{ticksuffix: '%'}}}});
            }}

            // ---- Large orders (single items) with the same filters
            const lg = largeData.filter(r => pass(r, true)).sort((a, b) => b.Lost_Value - a.Lost_Value);
            tableLarge.clear().rows.add(lg.slice(0, 200)).draw();
            const lgSum = lg.reduce((a, r) => a + (r.Lost_Value || 0), 0);
            document.getElementById('kpiLargeShare').innerText = pctTxt(pctOf(lgSum, T.missVal));
            document.getElementById('kpiLargeSub').innerHTML = fmtInt(lg.length) + (lg.length === 1 ? ' item | ' : ' items | ') + formatNum(lgSum) + '<span class="lg-arrow"> ›</span>';
            document.getElementById('kpiLargeFormula').innerText = 'Missed of items >= RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M / Total missed';

            // ---- Extra insights (appended under the original two lines)
            const li = (color, label, html) => '<li style="display: flex; align-items: flex-start; gap: 8px;"><span style="color: ' + color + '; font-weight: bold; min-width: 160px;">&bull; ' + label + '</span><span style="color: var(--text-main);">' + html + '</span></li>';
            let extra = '';
            if (hasV) {{
                extra += li('var(--accent-purple)', 'Value Overview:', 'Of <b>RWF ' + formatNum(T.ordVal) + '</b> ordered, <b>RWF ' + formatNum(madeVal) + '</b> was made (' + pctTxt(pctOf(madeVal, T.ordVal)) + ') and <b>RWF ' + formatNum(T.missVal) + '</b> was missed (' + pctTxt(pctOf(T.missVal, T.ordVal)) + ') in the current selection.');
            }}
            if (topV.length) {{
                const v = topV[0], q = topQ[0];
                extra += li('var(--accent-orange)', 'Branch Focus:', '<b>' + v + '</b> has the largest value loss: <b>RWF ' + formatNum(br[v].missVal) + '</b>' +
                    (hasV ? ' = ' + pctTxt(pctOf(br[v].missVal, br[v].ordVal)) + ' of its own orders and ' : ' = ') + pctTxt(pctOf(br[v].missVal, B.missVal)) + ' of all missed value. Most missed units: <b>' + q + '</b> (' + formatNum(br[q].missQty) + ' units, ' + pctTxt(pctOf(br[q].missQty, br[q].ordQty)) + ' of its own orders).');
            }}
            if (T.missVal > 0) {{
                extra += li('#f59e0b', 'Concentration:', 'Items with <b>RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M+</b> missed each make up <b>' + pctTxt(pctOf(lgSum, T.missVal)) + '</b> of missed value (RWF ' + formatNum(lgSum) + ') &mdash; click the Large Orders Share card (or see the table below) to verify them.');
            }}
            document.getElementById('dynamicInsights').insertAdjacentHTML('beforeend', extra);
            try {{ bindDrill(); bindLargeCard(); }} catch (e) {{ console.error('Drill-down bind error:', e); }}
        }}

        // ADDED: click a branch bar -> branch drill-down window (reads rawData/largeData only; never touches the original charts)
        let drillBound = false;
        function bindDrill() {{
            if (drillBound) return;
            const ids = ['branchValueChart', 'branchQtyChart', 'madeMissValChart', 'madeMissQtyChart'];
            let ok = 0;
            ids.forEach(id => {{
                const el = document.getElementById(id);
                if (!el || typeof el.on !== 'function') return;
                el.on('plotly_click', function(d) {{
                    if (d && d.points && d.points.length) openDrill(String(d.points[0].y), d.event);
                }});
                const panel = el.closest('.panel');
                const hdr = panel ? panel.querySelector('.panel-header') : null;
                if (hdr && !hdr.querySelector('.drill-hint') && hdr.firstChild) {{
                    const w = document.createElement('span');
                    w.appendChild(hdr.firstChild);
                    const h = document.createElement('span');
                    h.className = 'drill-hint';
                    h.textContent = 'click a bar for branch details';
                    w.appendChild(h);
                    hdr.insertBefore(w, hdr.firstChild);
                }}
                ok++;
            }});
            if (ok > 0) {{
                document.getElementById('drillClose').addEventListener('click', closeDrill);
                document.getElementById('drillOverlay').addEventListener('click', function(e) {{ if (e.target.id === 'drillOverlay') closeDrill(); }});
                document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeDrill(); }});
                drillBound = true;
            }}
        }}

        function closeDrill() {{
            const o = document.getElementById('drillOverlay');
            if (!o || o.style.display === 'none') return;
            o.style.display = 'none';
            ['drillMonthChart', 'drillProdChart', 'lgBranchChart', 'lgMonthChart'].forEach(id => {{ const e = document.getElementById(id); if (e) {{ try {{ Plotly.purge(e); }} catch (x) {{}} }} }});
            document.getElementById('drillBody').innerHTML = '';
        }}

        function openDrill(branch, ev) {{
            const fMonth = document.getElementById('filterMonth').value;
            const fType = document.getElementById('filterType').value;
            const fSearch = document.getElementById('filterSearch').value.toLowerCase();
            const hasV = ordValReady;
            const NA = '\u2014';
            const fmtInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
            const pctOf = (a, b) => b > 0 ? (a / b) * 100 : null;
            const pctTxt = (x) => x === null ? NA : x.toFixed(1) + '%';
            const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const shorten = (s) => s.length > 34 ? s.slice(0, 33) + '\u2026' : s;
            const MADE_C = '#34d399', MISS_C = '#f48a6a';

            const base = rawData.filter(r => {{
                if (fMonth !== 'All' && r.Month !== fMonth) return false;
                if (fType !== 'All' && r.Sales_Doc_Type !== fType) return false;
                if (fSearch && !String(r.Product_Desc || "").toLowerCase().includes(fSearch)) return false;
                return true;
            }});
            const mine = base.filter(r => r.Branch === branch);
            const blank = () => ({{ordQty: 0, missQty: 0, ordVal: 0, missVal: 0, items: 0, short: 0}});
            const add = (a, r) => {{ a.ordQty += r.Order_Qty || 0; a.missQty += r.Missed_Qty || 0; a.ordVal += r.Ordered_Value || 0; a.missVal += r.Lost_Value || 0; a.items += r.Total_Lines || 1; a.short += r.Missed_Lines || 0; return a; }};
            const T = mine.reduce(add, blank());
            const A = base.reduce(add, blank());
            const madeVal = T.ordVal - T.missVal;

            const byB = {{}};
            base.forEach(r => {{ byB[r.Branch] = (byB[r.Branch] || 0) + (r.Lost_Value || 0); }});
            const ranked = Object.keys(byB).sort((a, b) => byB[b] - byB[a]);
            const rank = ranked.indexOf(branch) + 1;

            const mo = {{}};
            mine.forEach(r => {{ const k = String(r.Date || '').slice(0, 7) || r.Month; if (!mo[k]) mo[k] = Object.assign(blank(), {{label: r.Month}}); add(mo[k], r); }});
            const mk = Object.keys(mo).sort();

            const pr = {{}};
            mine.forEach(r => {{
                if ((r.Missed_Qty || 0) <= 0) return;
                const k = r.Product_Desc || '(blank)';
                if (!pr[k]) pr[k] = {{lost: 0, units: 0, items: 0}};
                pr[k].lost += r.Lost_Value || 0; pr[k].units += r.Missed_Qty || 0; pr[k].items += r.Missed_Lines || 0;
            }});
            const prods = Object.keys(pr).sort((a, b) => pr[b].lost - pr[a].lost);

            const lg = largeData.filter(r => r.Branch === branch && (fMonth === 'All' || r.Month === fMonth) && (fType === 'All' || r.Sales_Doc_Type === fType)
                && (!fSearch || String(r.Product_Desc || "").toLowerCase().includes(fSearch))).sort((a, b) => b.Lost_Value - a.Lost_Value);

            const tile = (label, value, cls) => '<div class="drill-tile"><div class="t-label">' + label + '</div><div class="t-value ' + (cls || '') + '">' + value + '</div></div>';
            let html = '<div class="drill-tiles">' +
                tile('Sales ordered (RWF)', hasV ? formatNum(T.ordVal) : NA, 't-ord') +
                tile('Sales made (RWF)', hasV ? formatNum(madeVal) : NA, 't-made') +
                tile('Sales missed (RWF)', formatNum(T.missVal), 't-miss') +
                tile('Missed rate (value)', hasV ? pctTxt(pctOf(T.missVal, T.ordVal)) : NA, 't-miss') +
                tile('Units ordered', formatNum(T.ordQty), 't-ord') +
                tile('Units made', formatNum(T.ordQty - T.missQty), 't-made') +
                tile('Units missed', formatNum(T.missQty), 't-miss') +
                tile('Missed rate (units)', pctTxt(pctOf(T.missQty, T.ordQty)), 't-miss') + '</div>';
            html += '<div class="drill-strip"><b>Share of all missed value:</b> ' + pctTxt(pctOf(T.missVal, A.missVal)) +
                ' <span style="color:var(--text-muted);">(rank #' + rank + ' of ' + ranked.length + ' branches)</span> &nbsp;&bull;&nbsp; ' +
                '<b>Order items short:</b> ' + fmtInt(T.short) + ' of ' + fmtInt(T.items) + ' (' + pctTxt(pctOf(T.short, T.items)) + ')</div>';
            html += '<div class="drill-grid2"><div class="drill-panel"><h4>Monthly: made vs missed (RWF)</h4><div id="drillMonthChart" style="height:280px;"></div></div>' +
                '<div class="drill-panel"><h4>Top 10 missed products (lost RWF)</h4><div id="drillProdChart" style="height:280px;"></div></div></div>';

            let rowsP = '';
            prods.slice(0, 15).forEach(k => {{
                rowsP += '<tr><td>' + esc(k) + '</td><td class="num">' + fmtInt(pr[k].items) + '</td><td class="num">' + fmtInt(pr[k].units) + '</td><td class="num">' + fmtInt(pr[k].lost) + '</td><td class="num">' + pctTxt(pctOf(pr[k].lost, T.missVal)) + '</td></tr>';
            }});
            html += '<div class="drill-panel"><h4>Top missed products in ' + esc(branch) + '</h4><div class="table-container"><table class="light-table"><thead><tr><th>Product</th><th class="num">Items short</th><th class="num">Missed units</th><th class="num">Lost value (RWF)</th><th class="num">% of branch missed</th></tr></thead><tbody>' +
                (rowsP || '<tr><td colspan="5">No missed items for the current filters.</td></tr>') + '</tbody></table></div></div>';
            if (lg.length) {{
                let rowsL = '';
                lg.slice(0, 15).forEach(r => {{
                    rowsL += '<tr><td>' + esc(r.Doc) + '</td><td>' + esc(r.Date) + '</td><td>' + esc(r.Product_Desc) + '</td><td class="num">' + fmtInt(r.Order_Qty) + '</td><td class="num">' + fmtInt(r.Missed_Qty) + '</td><td class="num">' + fmtInt(r.Lost_Value) + '</td></tr>';
                }});
                html += '<div class="drill-panel"><h4>Large orders to verify (RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M+ missed per item)</h4><div class="table-container"><table class="light-table"><thead><tr><th>Sales doc</th><th>Doc date</th><th>Product</th><th class="num">Ord qty</th><th class="num">Miss qty</th><th class="num">Lost value (RWF)</th></tr></thead><tbody>' + rowsL + '</tbody></table></div></div>';
            }}

            document.getElementById('drillTitle').textContent = branch + ' \u2014 branch drill-down';
            document.getElementById('drillSub').textContent = 'Filters applied \u2014 Month: ' + (fMonth === 'All' ? 'All months' : fMonth) + '  |  Doc type: ' + (fType === 'All' ? 'All types' : fType) + (fSearch ? '  |  Product search: "' + fSearch + '"' : '');
            document.getElementById('drillBody').innerHTML = html;
            const overlay = document.getElementById('drillOverlay'), box = document.getElementById('drillBox');
            const docH = document.documentElement.scrollHeight;
            overlay.style.height = docH + 'px';
            overlay.style.display = 'block';
            const y = (ev && ev.pageY) ? ev.pageY : (window.scrollY + 80);
            box.style.top = Math.max(10, Math.min(y - 90, docH - box.offsetHeight - 20)) + 'px';

            if (mk.length) {{
                const lbl = mk.map(k => mo[k].label);
                const traces = [];
                if (hasV) traces.push({{x: lbl, y: mk.map(k => mo[k].ordVal - mo[k].missVal), type: 'bar', name: 'Made', marker: {{color: MADE_C}}, text: mk.map(k => formatNum(mo[k].ordVal - mo[k].missVal)), textposition: 'auto'}});
                traces.push({{x: lbl, y: mk.map(k => mo[k].missVal), type: 'bar', name: 'Missed', marker: {{color: MISS_C}}, text: mk.map(k => formatNum(mo[k].missVal)), textposition: 'auto'}});
                Plotly.newPlot('drillMonthChart', traces, {{barmode: 'stack', margin: {{l: 60, r: 15, t: 10, b: 45}}, legend: {{orientation: 'h', y: -0.2, traceorder: 'normal'}}}}, {{displayModeBar: false}});
            }}
            const top10 = prods.slice(0, 10).reverse();
            if (top10.length) {{
                Plotly.newPlot('drillProdChart', [{{x: top10.map(k => pr[k].lost), y: top10.map(k => shorten(k)), type: 'bar', orientation: 'h', marker: {{color: MISS_C}},
                    text: top10.map(k => formatNum(pr[k].lost)), textposition: 'auto', hovertext: top10.map(k => k + '<br>RWF ' + fmtInt(pr[k].lost)), hoverinfo: 'text'}}],
                    {{margin: {{l: 10, r: 25, t: 10, b: 30}}, yaxis: {{automargin: true}}}}, {{displayModeBar: false}});
            }}
            try {{ box.setAttribute('tabindex', '-1'); box.focus({{preventScroll: true}}); }} catch (e) {{}}
            try {{ box.scrollIntoView({{block: 'start', behavior: 'smooth'}}); }} catch (e) {{}}
        }}
        // ADDED: click the "Large Orders Share" card -> list of the large orders (same window as the branch drill-down)
        let largeCardBound = false;
        function bindLargeCard() {{
            if (largeCardBound) return;
            const c = document.getElementById('largeCard');
            if (!c) return;
            c.addEventListener('click', function(e) {{ openLargeDrill(e); }});
            c.addEventListener('keydown', function(e) {{ if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); openLargeDrill(null); }} }});
            largeCardBound = true;
        }}

        function openLargeDrill(ev) {{
            const fMonth = document.getElementById('filterMonth').value;
            const fType = document.getElementById('filterType').value;
            const fBranch = document.getElementById('filterBranch').value;
            const fSearch = document.getElementById('filterSearch').value.toLowerCase();
            const NA = '\u2014';
            const fmtInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
            const pctOf = (a, b) => b > 0 ? (a / b) * 100 : null;
            const pctTxt = (x) => x === null ? NA : x.toFixed(1) + '%';
            const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const MISS_C = '#f48a6a', PURPLE_C = '#5a5ce0';
            const pass = (r) => {{
                if (fMonth !== 'All' && r.Month !== fMonth) return false;
                if (fType !== 'All' && r.Sales_Doc_Type !== fType) return false;
                if (fBranch !== 'All' && r.Branch !== fBranch) return false;
                if (fSearch && !String(r.Product_Desc || "").toLowerCase().includes(fSearch)) return false;
                return true;
            }};
            const totMiss = rawData.filter(pass).reduce((a, r) => a + (r.Lost_Value || 0), 0);
            const lg = largeData.filter(pass).sort((a, b) => b.Lost_Value - a.Lost_Value);
            const lgSum = lg.reduce((a, r) => a + (r.Lost_Value || 0), 0);
            const lgUnits = lg.reduce((a, r) => a + (r.Missed_Qty || 0), 0);
            const byBranch = {{}}, byMonth = {{}};
            lg.forEach(r => {{
                byBranch[r.Branch] = (byBranch[r.Branch] || 0) + (r.Lost_Value || 0);
                const k = String(r.Date || '').slice(0, 7) || r.Month;
                if (!byMonth[k]) byMonth[k] = {{label: r.Month, v: 0}};
                byMonth[k].v += r.Lost_Value || 0;
            }});
            const thr = 'RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M';
            const tile = (label, value, cls) => '<div class="drill-tile"><div class="t-label">' + label + '</div><div class="t-value ' + (cls || '') + '">' + value + '</div></div>';
            let html = '';
            if (!lg.length) {{
                html += '<div class="drill-strip">No single order item reaches ' + thr + ' of missed value under the current filters.</div>';
            }} else {{
                html += '<div class="drill-tiles">' +
                    tile('Large items', fmtInt(lg.length), 't-ord') +
                    tile('Missed value in them (RWF)', formatNum(lgSum), 't-miss') +
                    tile('Share of all missed value', pctTxt(pctOf(lgSum, totMiss)), 't-miss') +
                    tile('Total missed value (RWF)', formatNum(totMiss), 't-miss') +
                    tile('Missed units in them', formatNum(lgUnits), 't-miss') +
                    tile('Average per large item (RWF)', formatNum(lgSum / lg.length), 't-ord') +
                    tile('Largest single item (RWF)', formatNum(lg[0].Lost_Value), 't-miss') +
                    tile('Branches affected', fmtInt(Object.keys(byBranch).length), 't-ord') + '</div>';
                html += '<div class="drill-strip"><b>Large order</b> = a single order item with ' + thr + '+ of missed value. These items make up <b>' + pctTxt(pctOf(lgSum, totMiss)) +
                    '</b> of the missed value, so they are worth verifying (quantity, price, duplicate entry) with the branch before treating them as real lost sales.</div>';
                html += '<div class="drill-grid2"><div class="drill-panel"><h4>Large-order missed value by branch (RWF)</h4><div id="lgBranchChart" style="height:260px;"></div></div>' +
                    '<div class="drill-panel"><h4>Large-order missed value by month (RWF)</h4><div id="lgMonthChart" style="height:260px;"></div></div></div>';
                let rows = '';
                lg.slice(0, 200).forEach(r => {{
                    rows += '<tr><td>' + esc(r.Doc) + '</td><td>' + esc(r.Item) + '</td><td>' + esc(r.Date) + '</td><td>' + esc(r.Branch) + '</td><td>' + esc(r.Product_Desc) + '</td>' +
                        '<td class="num">' + fmtInt(r.Order_Qty) + '</td><td class="num">' + fmtInt(r.Confirmed_Qty) + '</td><td class="num">' + fmtInt(r.Missed_Qty) + '</td>' +
                        '<td class="num">' + fmtInt(r.Lost_Value) + '</td><td class="num">' + pctTxt(pctOf(r.Lost_Value, totMiss)) + '</td></tr>';
                }});
                html += '<div class="drill-panel"><h4>All large orders in the current selection (' + fmtInt(lg.length) + ')</h4><div class="table-container"><table class="light-table"><thead><tr>' +
                    '<th>Sales doc</th><th>Item</th><th>Doc date</th><th>Branch</th><th>Product</th><th class="num">Ord qty</th><th class="num">Del qty</th><th class="num">Miss qty</th><th class="num">Lost value (RWF)</th><th class="num">% of missed value</th>' +
                    '</tr></thead><tbody>' + rows + '</tbody></table></div></div>';
            }}
            document.getElementById('drillTitle').textContent = 'Large orders \u2014 ' + thr + '+ missed per item';
            document.getElementById('drillSub').textContent = 'Filters applied \u2014 Month: ' + (fMonth === 'All' ? 'All months' : fMonth) + '  |  Doc type: ' + (fType === 'All' ? 'All types' : fType) + '  |  Branch: ' + (fBranch === 'All' ? 'All branches' : fBranch) + (fSearch ? '  |  Product search: "' + fSearch + '"' : '');
            document.getElementById('drillBody').innerHTML = html;
            const overlay = document.getElementById('drillOverlay'), box = document.getElementById('drillBox');
            const docH = document.documentElement.scrollHeight;
            overlay.style.height = docH + 'px';
            overlay.style.display = 'block';
            const y = (ev && ev.pageY) ? ev.pageY : (window.scrollY + 80);
            box.style.top = Math.max(10, Math.min(y - 90, docH - box.offsetHeight - 20)) + 'px';
            if (lg.length) {{
                const bk = Object.keys(byBranch).sort((a, b) => byBranch[a] - byBranch[b]).slice(-10);
                Plotly.newPlot('lgBranchChart', [{{x: bk.map(k => byBranch[k]), y: bk, type: 'bar', orientation: 'h', marker: {{color: MISS_C}}, text: bk.map(k => formatNum(byBranch[k])), textposition: 'auto'}}],
                    {{margin: {{l: 10, r: 25, t: 10, b: 30}}, yaxis: {{automargin: true}}}}, {{displayModeBar: false}});
                const mk = Object.keys(byMonth).sort();
                Plotly.newPlot('lgMonthChart', [{{x: mk.map(k => byMonth[k].label), y: mk.map(k => byMonth[k].v), type: 'bar', marker: {{color: PURPLE_C}}, text: mk.map(k => formatNum(byMonth[k].v)), textposition: 'auto'}}],
                    {{margin: {{l: 60, r: 15, t: 10, b: 40}}}}, {{displayModeBar: false}});
            }}
            try {{ box.setAttribute('tabindex', '-1'); box.focus({{preventScroll: true}}); }} catch (e) {{}}
            try {{ box.scrollIntoView({{block: 'start', behavior: 'smooth'}}); }} catch (e) {{}}
        }}
        window.onload = initApp;
    </script>
</body>
</html>
"""

# Render the HTML
components.html(html_template, height=4300, scrolling=True)

# ---------------------------------------------------------------------
# 5. BOTTOM ADMIN PANEL
# ---------------------------------------------------------------------
st.markdown("<div class='admin-section'>", unsafe_allow_html=True)

if not st.session_state.logged_in:
    with st.expander("⚙️ Data Management & Settings (Admin Only)"):
        pwd = st.text_input("Admin Password", type="password", placeholder="Enter password...")
        if st.button("Login"):
            if pwd == "admin123":
                st.session_state.logged_in = True
                st.rerun()
            else:
                st.error("Incorrect Password")
else:
    st.markdown("<h3 style='margin: 0 0 10px 0; color: #1e293b; font-family: Inter;'>Data Management & Settings</h3>", unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total DB Records", f"{db_stats['rows']:,}")
    col2.metric("Earliest Date", db_stats['min_date'])
    col3.metric("Latest Date", db_stats['max_date'])
    with col4:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🚨 Clear Entire Database", help="Wipes old/duplicated data"):
            database.clear_database()
            st.cache_data.clear()
            st.rerun()
    
    st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
    sales_file = st.file_uploader("Upload New Sales Data (Excel)", type=['xlsx', 'xls'])
    if sales_file:
        with st.spinner("Processing..."):
            database.upload_sales(sales_file)
            st.cache_data.clear()
            st.success("✅ Sales Saved! Click Refresh below.")
    
    if st.button("🔄 Refresh Dashboard Data"):
        st.rerun()

st.markdown("</div>", unsafe_allow_html=True)