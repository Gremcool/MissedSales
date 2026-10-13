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
        
        df_agg = df.groupby(['Month', 'Week_Start', 'Week Range', 'Date', 'Sales Document Type', 'Branch', 'Material Description'], dropna=False, as_index=False).agg(
            Order_Qty=('Order_Qty', 'sum'),
            Confirmed_Qty=('Confirmed_Qty', 'sum'),
            Missed_Qty=('Missed_Qty', 'sum'),
            Lost_Value=('Lost_Value', 'sum'),
            Total_Lines=('Sales Document', 'count'),
            Missed_Lines=('Missed_Line_Flag', 'sum')
        ).rename(columns={
            'Sales Document Type': 'Sales_Doc_Type', 
            'Week Range': 'Week_Range', 
            'Material Description': 'Product_Desc'
        })
        
        json_data = df_agg.fillna(0).to_json(orient='records')
        return df, db_stats, json_data
    else:
        return df, db_stats, "[]"

# Call the cached data loader
df, db_stats, json_data = load_and_prep_data()

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
            <div class="kpi-row">
                <div class="flip-card">
                    <div class="flip-card-inner" id="card1-inner">
                        <div class="flip-card-front card-purple">
                            <div class="kpi-header"><span class="kpi-title">MISSED RATE</span><span class="info-icon">i</span></div>
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
            </div>

            <!-- DYNAMIC EXECUTIVE INSIGHTS BANNER -->
            <div class="panel col-12" style="background: #ffffff; border-left: 5px solid var(--accent-purple);">
                <div class="panel-header" style="margin-bottom: 12px; color: var(--text-main); font-size: 16px;">
                    Dynamic Executive Insights
                </div>
                <ul id="dynamicInsights" style="list-style: none; padding-left: 0; display: flex; flex-direction: column; gap: 10px; font-size: 13px; color: var(--text-muted); line-height: 1.5; margin: 0;">
                </ul>
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

    <!-- JAVASCRIPT: INSTANT FILTERING & RENDERING -->
    <script>
        const rawData = {json_data};
        let tableDetails, tableLoc, tableItems;

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
                    {{data: 'Order_Qty'}}, {{data: 'Confirmed_Qty'}}, 
                    {{data: 'Missed_Qty', render: function(data){{ return `<span style="color:#f48a6a; font-weight:bold;">${{data}}</span>`; }}}},
                    {{data: 'Lost_Value', render: $.fn.dataTable.render.number(',', '.', 0)}}
                ],
                order: [[9, 'desc']], paging: false, scrollY: "400px", scrollCollapse: true, info: false, deferRender: true
            }});

            tableLoc = $('#locationTable').DataTable({{
                dom: 'Brtip', buttons: btnConfig,
                columns: [
                    {{data: 'Branch'}}, {{data: 'Products'}},
                    {{data: 'Lost', render: $.fn.dataTable.render.number(',', '.', 0)}},
                    {{data: 'Pct', render: function(d){{ return d + '%'; }}}}
                ],
                order: [[2, 'desc']], paging: false, scrollY: "300px", scrollCollapse: true, info: false, bFilter: false
            }});

            tableItems = $('#topItemsTable').DataTable({{
                dom: 'Brtip', buttons: btnConfig,
                columns: [
                    {{data: 'Product'}}, {{data: 'Lines'}},
                    {{data: 'Lost', render: $.fn.dataTable.render.number(',', '.', 0)}}
                ],
                order: [[2, 'desc']], paging: false, scrollY: "300px", scrollCollapse: true, info: false, bFilter: false
            }});

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
                    <span style="color: var(--text-main);"><b>${{maxMonth || 'N/A'}}</b> accounted for <b>${{pct}}%</b> of all lost sales value (RWF ${{(maxMonthVal/1000000).toFixed(2)}}M) within the selected filters.</span>
                </li>
                <li style="display: flex; align-items: flex-start; gap: 8px;">
                    <span style="color: var(--accent-green); font-weight: bold; min-width: 160px;">• Volume Overview:</span>
                    <span style="color: var(--text-main);">Across your selection, a total of <b>${{formatNum(missQty)}} units</b> were unfulfilled out of <b>${{formatNum(ordQty)}} ordered</b>.</span>
                </li>
            `;
        }}
        window.onload = initApp;
    </script>
</body>
</html>
"""

# Render the HTML
components.html(html_template, height=1950, scrolling=True)

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