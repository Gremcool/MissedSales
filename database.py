import sqlite3
import pandas as pd
import os

# Check if the Railway '/data' volume exists. If it does, use it. Otherwise, use local folder.
DATA_DIR = "/data" if os.path.exists("/data") else "."
DB_NAME = os.path.join(DATA_DIR, "rms_dashboard.db")

# Extract ONLY required columns to drastically reduce Excel parsing time and RAM footprint
NEEDED_SALES_COLS = [
    'Sales Document', 'Sales Document Item', 'Document Date', 
    'Sales Document Type', 'Plant', 'Material Description', 
    'Order Quantity (Item)', 'Confirmed Quantity (Item)', 'Net Price'
]

def get_connection():
    return sqlite3.connect(DB_NAME)

def init_db():
    """Creates the database schema with strict unique constraints and indexes."""
    conn = get_connection()
    # 1. Unique constraint ensures duplicates are physically blocked at the database level
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            "Sales Document" TEXT,
            "Sales Document Item" TEXT,
            "Document Date" TEXT,
            "Sales Document Type" TEXT,
            "Plant" TEXT,
            "Material Description" TEXT,
            "Order_Qty" REAL,
            "Confirmed_Qty" REAL,
            "Missed_Qty" REAL,
            "Lost_Value" REAL,
            UNIQUE("Sales Document", "Sales Document Item")
        )
    ''')
    # 2. Indexes make retrieving and aggregating the data exponentially faster
    conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON sales("Document Date")')
    conn.commit()
    conn.close()

def get_db_stats():
    """Uses a lightning-fast native SQL aggregation instead of loading the database into Pandas."""
    if not os.path.exists(DB_NAME):
        return {"rows": 0, "min_date": "N/A", "max_date": "N/A"}
    try:
        conn = get_connection()
        df = pd.read_sql("SELECT min([Document Date]) as min_d, max([Document Date]) as max_d, count(*) as count FROM sales", conn)
        conn.close()
        
        if df.empty or pd.isna(df['count'][0]) or df['count'][0] == 0:
            return {"rows": 0, "min_date": "N/A", "max_date": "N/A"}
            
        return {
            "rows": df['count'][0], 
            "min_date": str(df['min_d'][0])[:10], 
            "max_date": str(df['max_d'][0])[:10]
        }
    except:
        return {"rows": 0, "min_date": "N/A", "max_date": "N/A"}

def clear_database():
    """Physically deletes the database file for a true reset, then rebuilds the schema."""
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)
    init_db()

def get_data():
    """Reads pre-calculated SQL data and merges local Branch.csv instantly."""
    if not os.path.exists(DB_NAME): 
        return pd.DataFrame()
        
    try:
        conn = get_connection()
        # Data is already cleaned and calculated in SQL, so this read is near-instant
        df_sales = pd.read_sql("SELECT * FROM sales", conn)
        conn.close()
        
        if df_sales.empty:
            return pd.DataFrame()
            
        try:
            # Automatically grab and map your local VS Code branch mapping file
            df_branch = pd.read_csv("Branch.csv")
            df_branch['Plant'] = df_branch['Plant'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
            df_sales = df_sales.merge(df_branch, on='Plant', how='left')
        except:
            pass
            
        df_sales['Branch'] = df_sales.get('Branch', 'Unknown Branch').fillna('Unknown Branch')
        return df_sales
        
    except Exception as e:
        print(f"Error loading DB: {e}")
        return pd.DataFrame()

def upload_sales(file_buffer):
    """Processes upload in memory and delegates deduplication entirely to SQLite C-engine."""
    init_db()
    
    # 1. Load only what we need to save RAM
    header_df = pd.read_excel(file_buffer, nrows=0)
    valid_cols = [c for c in NEEDED_SALES_COLS if c in header_df.columns]
    df_new = pd.read_excel(file_buffer, usecols=valid_cols)
    
    # 2. Standardize keys to prevent phantom duplicates
    df_new['Sales Document'] = df_new['Sales Document'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    df_new['Sales Document Item'] = df_new['Sales Document Item'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    df_new['Plant'] = df_new.get('Plant', '').astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
    
    # 3. Pre-calculate KPIs BEFORE saving to database so get_data() does zero math on page load
    df_new['Order_Qty'] = pd.to_numeric(df_new.get('Order Quantity (Item)', 0), errors='coerce').fillna(0)
    df_new['Confirmed_Qty'] = pd.to_numeric(df_new.get('Confirmed Quantity (Item)', 0), errors='coerce').fillna(0)
    df_new['Missed_Qty'] = (df_new['Order_Qty'] - df_new['Confirmed_Qty']).clip(lower=0)
    net_price = pd.to_numeric(df_new.get('Net Price', 0), errors='coerce').fillna(0)
    df_new['Lost_Value'] = df_new['Missed_Qty'] * net_price
    
    # Drop raw columns we no longer need to save physical disk space
    cols_to_keep = ['Sales Document', 'Sales Document Item', 'Document Date', 'Sales Document Type', 'Plant', 'Material Description', 'Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value']
    df_new = df_new[[c for c in cols_to_keep if c in df_new.columns]]
    
    conn = get_connection()
    
    # 4. Push ONLY the new file to a temporary holding table
    df_new.to_sql("temp_sales", conn, if_exists="replace", index=False)
    
    # 5. Let SQLite handle merging and deduplication natively
    conn.execute('''
        INSERT OR IGNORE INTO sales (
            "Sales Document", "Sales Document Item", "Document Date", "Sales Document Type", 
            "Plant", "Material Description", "Order_Qty", "Confirmed_Qty", "Missed_Qty", "Lost_Value"
        )
        SELECT 
            "Sales Document", "Sales Document Item", "Document Date", "Sales Document Type", 
            "Plant", "Material Description", "Order_Qty", "Confirmed_Qty", "Missed_Qty", "Lost_Value"
        FROM temp_sales
    ''')
    
    conn.execute("DROP TABLE temp_sales")
    conn.commit()
    conn.close()