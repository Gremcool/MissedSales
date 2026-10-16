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
    return sqlite3.connect(DB_NAME, timeout=60)

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
            "Ordered_Value" REAL,
            UNIQUE("Sales Document", "Sales Document Item")
        )
    ''')
    # 2. Indexes make retrieving and aggregating the data exponentially faster
    # Migration: databases created before the Ordered_Value column existed get it added (existing data is untouched)
    existing_cols = [r[1] for r in conn.execute('PRAGMA table_info(sales)').fetchall()]
    if 'Ordered_Value' not in existing_cols:
        conn.execute('ALTER TABLE sales ADD COLUMN "Ordered_Value" REAL')
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
    df_new['Ordered_Value'] = df_new['Order_Qty'] * net_price   # RWF value of what was ordered (used for Sales Made / rates)
    
    # Drop raw columns we no longer need to save physical disk space
    cols_to_keep = ['Sales Document', 'Sales Document Item', 'Document Date', 'Sales Document Type', 'Plant', 'Material Description', 'Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value', 'Ordered_Value']
    df_new = df_new[[c for c in cols_to_keep if c in df_new.columns]]
    
    conn = get_connection()
    
    # 4. Push ONLY the new file to a temporary holding table
    df_new.to_sql("temp_sales", conn, if_exists="replace", index=False)
    
    # 5. Let SQLite handle merging and deduplication natively
    conn.execute('''
        INSERT OR IGNORE INTO sales (
            "Sales Document", "Sales Document Item", "Document Date", "Sales Document Type", 
            "Plant", "Material Description", "Order_Qty", "Confirmed_Qty", "Missed_Qty", "Lost_Value", "Ordered_Value"
        )
        SELECT 
            "Sales Document", "Sales Document Item", "Document Date", "Sales Document Type", 
            "Plant", "Material Description", "Order_Qty", "Confirmed_Qty", "Missed_Qty", "Lost_Value", "Ordered_Value"
        FROM temp_sales
    ''')
    
    # 5b. Backfill Ordered_Value for rows saved before this column existed (only fills empty cells, never overwrites)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_temp_key ON temp_sales("Sales Document", "Sales Document Item")')
    conn.execute('''
        UPDATE sales SET "Ordered_Value" = (
            SELECT t."Ordered_Value" FROM temp_sales t
            WHERE t."Sales Document" = sales."Sales Document" AND t."Sales Document Item" = sales."Sales Document Item"
        )
        WHERE "Ordered_Value" IS NULL
          AND EXISTS (
            SELECT 1 FROM temp_sales t
            WHERE t."Sales Document" = sales."Sales Document" AND t."Sales Document Item" = sales."Sales Document Item"
          )
    ''')

    conn.execute("DROP TABLE temp_sales")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# RMS DATA WAREHOUSE API SYNC
# Pulls the flat line-level export (one request per calendar month), maps it to the same columns the Excel
# upload produces, and UPSERTS it (the API is the source of truth for the dates it covers).
# Login details come ONLY from environment variables (Railway "Variables"), never from the code.
# ---------------------------------------------------------------------
import time
import threading
from datetime import date, datetime, timedelta, timezone


class ApiError(Exception):
    """A problem with the API. The message is safe to show on screen (it never contains passwords or tokens)."""


DOC_PREFIX_TYPES = {'41': 'ZRMS', '61': 'ZSPO'}   # observed in the data: 41xxxxxxxx = ZRMS, 61xxxxxxxx = ZSPO
ALLOWED_DOC_TYPES = {'ZRMS', 'ZSPO'}
API_DB_COLS = ['Sales Document', 'Sales Document Item', 'Document Date', 'Sales Document Type', 'Plant',
               'Material Description', 'Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value', 'Ordered_Value']


_ENV_LOADED = {}      # settings currently taken from a .env file (key -> value)


def _find_env_file():
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (os.getcwd(), here):
        p = os.path.join(folder, '.env')
        if os.path.isfile(p):
            return p
    return None


def load_env_file():
    """Optional convenience for your own computer: read KEY=VALUE lines from a file named .env next to app.py.
    A real environment variable (e.g. a Railway Variable) ALWAYS wins over the file. Edits to the file are picked up without a restart.
    Passwords are never printed or logged. Keep .env out of git (see .gitignore)."""
    values = {}
    p = _find_env_file()
    if p:
        try:
            with open(p, encoding='utf-8-sig') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip()
                    if k.lower().startswith('export '):
                        k = k[7:].strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    if k:
                        values[k] = v
        except OSError:
            values = {}
    for k in list(_ENV_LOADED):                      # a setting removed from the file is forgotten
        if k not in values:
            os.environ.pop(k, None)
            _ENV_LOADED.pop(k, None)
    for k, v in values.items():
        if k not in os.environ or k in _ENV_LOADED:  # never override a real environment variable
            os.environ[k] = v
            _ENV_LOADED[k] = v
    return p


def _api_cfg():
    """Read the settings at call time so Railway variables (or the .env file) are always picked up."""
    load_env_file()
    g = os.environ.get
    return {
        'base': g('RMS_API_BASE_URL', 'https://api.rms.rw/datawarehouse/api').rstrip('/'),
        'user': g('RMS_API_USER', ''),
        'password': g('RMS_API_PASSWORD', ''),
        'login_path': g('RMS_API_LOGIN_PATH', '/auth/validate'),
        'list_path': g('RMS_API_LIST_PATH', '/dataservices/sales-orders'),
        'export_path': g('RMS_API_EXPORT_PATH', '/dataservices/sales-orders/export.csv'),
        'doctype_field': g('RMS_API_DOCTYPE_FIELD', 'program').strip().lower(),    # program (default, as given) | auto | prefix
        'confirmed_field': g('RMS_API_CONFIRMED_FIELD', 'qty_delivered').strip().lower(),
    }


def api_status():
    cfg = _api_cfg()
    return {'configured': bool(cfg['user'] and cfg['password']), 'base': cfg['base'], 'user': cfg['user'],
            'source': '.env file' if 'RMS_API_PASSWORD' in _ENV_LOADED else 'environment variable'}


class _Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.token_time = 0

    def login(self):
        import requests
        cfg = self.cfg
        try:
            r = requests.post(cfg['base'] + cfg['login_path'],
                              json={'username': cfg['user'], 'password': cfg['password']},
                              headers={'Accept': 'application/json'}, timeout=(15, 60))
        except requests.RequestException as e:
            raise ApiError("Cannot reach the API (%s). Check RMS_API_BASE_URL and that this server is allowed to reach it." % type(e).__name__)
        if r.status_code in (400, 401, 403):
            raise ApiError("The API rejected the login (HTTP %d). Check RMS_API_USER and RMS_API_PASSWORD." % r.status_code)
        if r.status_code != 200:
            raise ApiError("The API login failed (HTTP %d)." % r.status_code)
        try:
            token = r.json().get('token')
        except ValueError:
            token = None
        if not token:
            raise ApiError("The API login answered but did not return a token.")
        self.token, self.token_time = token, time.time()

    def get(self, path, params=None, stream=False):
        import requests
        if self.token is None or time.time() - self.token_time > 45 * 60:      # tokens last 60 minutes
            self.login()
        for attempt in (1, 2):
            try:
                r = requests.get(self.cfg['base'] + path, params=params, stream=stream, timeout=(15, 300),
                                 headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'text/csv, application/json'})
            except requests.RequestException as e:
                raise ApiError("Lost the connection to the API (%s)." % type(e).__name__)
            if r.status_code == 401 and attempt == 1:     # token expired: log in again once and retry
                r.close()
                self.login()
                continue
            break
        if r.status_code == 401:
            r.close()
            raise ApiError("The API rejected the access token twice.")
        if r.status_code != 200:
            r.close()
            raise ApiError("The API answered HTTP %d for %s." % (r.status_code, path))
        return r


def _month_chunks(d_from, d_to):
    out, cur = [], d_from
    while cur <= d_to:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((cur, min(nxt - timedelta(days=1), d_to)))
        cur = nxt
    return out


def _iter_export(api, d_from, d_to):
    r = api.get(api.cfg['export_path'], {'dateFrom': d_from.isoformat(), 'dateTo': d_to.isoformat()}, stream=True)
    try:
        r.raw.decode_content = True
        try:
            for chunk in pd.read_csv(r.raw, chunksize=50000, dtype=str, encoding='utf-8-sig'):
                yield chunk
        except pd.errors.EmptyDataError:
            return
        except (pd.errors.ParserError, UnicodeDecodeError):
            raise ApiError("The export did not return a readable CSV file.")
    finally:
        r.close()


def _map_api_chunk(df, cfg, state):
    """API csv columns -> the same columns the Excel upload produces. Returns (rows, skipped_count)."""
    cols = {str(c).strip().lower(): c for c in df.columns}
    conf = cfg['confirmed_field']
    need = ['order_no', 'line_no', 'order_date', 'plant', 'description', 'qty_ordered', 'unit_price', conf]
    if cfg['doctype_field'] == 'program':
        need.append('program')
    missing = [c for c in need if c not in cols]
    if missing:
        raise ApiError("The export is missing column(s): %s." % ", ".join(missing))

    def text(name):
        return df[cols[name]].astype('string').str.strip()

    out = pd.DataFrame(index=df.index)
    out['Sales Document'] = text('order_no').str.replace(r'\.0$', '', regex=True)
    out['Sales Document Item'] = text('line_no').str.replace(r'\.0$', '', regex=True)
    dt = pd.to_datetime(df[cols['order_date']], errors='coerce')
    out['Document Date'] = dt.dt.strftime('%Y-%m-%d 00:00:00')           # same text format the Excel upload stores
    plant = text('plant').str.replace(r'\.0$', '', regex=True)
    out['Plant'] = plant.mask(plant == '')
    out['Material Description'] = df[cols['description']].astype('string')
    q_ord = pd.to_numeric(df[cols['qty_ordered']], errors='coerce').fillna(0)
    q_conf = pd.to_numeric(df[cols[conf]], errors='coerce').fillna(0)
    price = pd.to_numeric(df[cols['unit_price']], errors='coerce').fillna(0)
    out['Order_Qty'] = q_ord
    out['Confirmed_Qty'] = q_conf
    out['Missed_Qty'] = (q_ord - q_conf).clip(lower=0)
    out['Lost_Value'] = out['Missed_Qty'] * price
    out['Ordered_Value'] = q_ord * price

    # Document type. DEFAULT: whatever the API gives in 'program', exactly as it is (a blank becomes "Unknown").
    # Optional modes (RMS_API_DOCTYPE_FIELD=auto|prefix) keep the older rule that only accepts ZRMS / ZSPO.
    if cfg['doctype_field'] == 'program':
        raw = text('program')
        blank = raw.isna() | (raw == '')
        out['Sales Document Type'] = raw.mask(blank, 'Unknown')
        state['doctype_source'] = 'program'
        state['doctype_blank'] = state.get('doctype_blank', 0) + int(blank.sum())
        state.setdefault('doctype_values', set()).update(out['Sales Document Type'].dropna().unique().tolist())
    else:
        # Document type (ZRMS / ZSPO): from the 'program' field when it holds exactly those values, otherwise from the order-number prefix
        prefix = out['Sales Document'].str[:2].map(DOC_PREFIX_TYPES)
        prog = text('program').str.upper() if 'program' in cols else None
        source = state.get('doctype_source')
        if source is None:
            mode = cfg['doctype_field']
            if mode in ('program', 'prefix'):
                source = mode
            elif prog is not None and prog.notna().all() and prog.isin(ALLOWED_DOC_TYPES).all():
                source = 'program'
            elif prefix.notna().all():
                source = 'prefix'
            else:
                raise ApiError("Cannot work out the document type (ZRMS/ZSPO): field 'program' holds other values and the order numbers do not all start with 41 or 61.")
            state['doctype_source'] = source
        types = prog if source == 'program' else prefix
        if types is None or types.isna().any() or not types.isin(ALLOWED_DOC_TYPES).all():
            raise ApiError("Some lines have a document type other than ZRMS/ZSPO (source: %s). Nothing was saved." % source)
        out['Sales Document Type'] = types

    valid = out['Sales Document'].notna() & out['Sales Document Item'].notna() & dt.notna()
    skipped = int((~valid).sum())
    out = out[valid]
    return out[API_DB_COLS], skipped


_TEMP_API_SQL = '''CREATE TABLE temp_api (
    "Sales Document" TEXT, "Sales Document Item" TEXT, "Document Date" TEXT, "Sales Document Type" TEXT, "Plant" TEXT,
    "Material Description" TEXT, "Order_Qty" REAL, "Confirmed_Qty" REAL, "Missed_Qty" REAL, "Lost_Value" REAL, "Ordered_Value" REAL)'''


def _load_month(conn, api, d_from, d_to, state):
    conn.execute('DROP TABLE IF EXISTS temp_api')
    conn.execute(_TEMP_API_SQL)
    skipped = 0
    for chunk in _iter_export(api, d_from, d_to):
        mapped, bad = _map_api_chunk(chunk, api.cfg, state)
        skipped += bad
        mapped.to_sql('temp_api', conn, if_exists='append', index=False)
    # the same order line twice in the export -> keep the last one
    conn.execute('DELETE FROM temp_api WHERE rowid NOT IN (SELECT MAX(rowid) FROM temp_api GROUP BY "Sales Document", "Sales Document Item")')
    conn.execute('CREATE INDEX idx_temp_api ON temp_api("Sales Document", "Sales Document Item")')
    return skipped


def _api_compare(conn, lo, hi):
    """Compare what the API returned for this month with what the database already holds for the same dates."""
    one = lambda sql, *a: conn.execute(sql, a).fetchone()
    has_ov = 'Ordered_Value' in [r[1] for r in conn.execute('PRAGMA table_info(sales)')]     # old databases do not have it yet
    sums = 'COUNT(*), COALESCE(SUM(Order_Qty),0), COALESCE(SUM(Confirmed_Qty),0), COALESCE(SUM(Missed_Qty),0), COALESCE(SUM(Ordered_Value),0), COALESCE(SUM(Lost_Value),0)'
    sums_db = sums if has_ov else sums.replace('COALESCE(SUM(Ordered_Value),0)', '0')
    a = one('SELECT %s FROM temp_api' % sums)
    d = one('SELECT %s FROM sales WHERE "Document Date" >= ? AND "Document Date" < ?' % sums_db, lo, hi)
    m = one('''SELECT COUNT(*),
                      COALESCE(SUM(CASE WHEN ABS(t.Confirmed_Qty - s.Confirmed_Qty) > 0.0005 THEN 1 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN ABS(t.Order_Qty - s.Order_Qty) > 0.0005 THEN 1 ELSE 0 END),0),
                      %s
               FROM temp_api t JOIN sales s ON s."Sales Document" = t."Sales Document" AND s."Sales Document Item" = t."Sales Document Item"''' %
               ('COALESCE(SUM(CASE WHEN s.Ordered_Value IS NOT NULL AND ABS(t.Ordered_Value - s.Ordered_Value) > 1 THEN 1 ELSE 0 END),0)' if has_ov else '0'))
    only_db = one('''SELECT COUNT(*) FROM sales s WHERE s."Document Date" >= ? AND s."Document Date" < ?
                     AND NOT EXISTS (SELECT 1 FROM temp_api t WHERE t."Sales Document" = s."Sales Document" AND t."Sales Document Item" = s."Sales Document Item")''', lo, hi)[0]
    return {'api_lines': a[0], 'api_ordered_qty': a[1], 'api_confirmed_qty': a[2], 'api_missed_qty': a[3], 'api_ordered_value': a[4], 'api_missed_value': a[5],
            'db_lines': d[0], 'db_ordered_qty': d[1], 'db_confirmed_qty': d[2], 'db_missed_qty': d[3], 'db_ordered_value': d[4], 'db_missed_value': d[5],
            'matched': m[0], 'confirmed_differs': m[1], 'order_qty_differs': m[2], 'value_differs': m[3],
            'new_in_api': a[0] - m[0], 'only_in_db': only_db}


def _api_apply(conn):
    cols = ', '.join('"%s"' % c for c in API_DB_COLS)
    updates = ', '.join('"%s" = excluded."%s"' % (c, c) for c in API_DB_COLS if c not in ('Sales Document', 'Sales Document Item'))
    conn.execute('INSERT INTO sales (%s) SELECT %s FROM temp_api WHERE 1 ON CONFLICT("Sales Document", "Sales Document Item") DO UPDATE SET %s' % (cols, cols, updates))


def _log_sync(conn, d_from, d_to, lines, inserted, updated, status, source='API'):
    conn.execute('CREATE TABLE IF NOT EXISTS sync_log (ts TEXT, source TEXT, from_date TEXT, to_date TEXT, lines INTEGER, inserted INTEGER, updated INTEGER, status TEXT)')
    conn.execute('INSERT INTO sync_log VALUES (?,?,?,?,?,?,?,?)',
                 (datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), source, d_from.isoformat(), d_to.isoformat(), lines, inserted, updated, status))


def last_sync():
    """Text such as '2026-10-17 09:12 UTC - 55,033 lines (Sep-Oct)', or None."""
    if not os.path.exists(DB_NAME):
        return None
    try:
        conn = get_connection()
        r = conn.execute('SELECT ts, from_date, to_date, lines, status FROM sync_log ORDER BY rowid DESC LIMIT 1').fetchone()
        conn.close()
        if not r:
            return None
        try:
            when = datetime.strptime(r[0], '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=2))).strftime('%Y-%m-%d %H:%M') + ' Kigali time'
        except ValueError:
            when = r[0]
        return '%s - %s lines, %s to %s (%s)' % (when, format(r[3], ','), r[1], r[2], r[4])
    except Exception:
        return None


def _api_run_impl(d_from, d_to, apply=False, progress=None, source='API'):
    """Fetch month by month. apply=False is a PREVIEW (nothing is written); apply=True upserts into the database.
    Each month is saved on its own, so a failure part-way never leaves a half-written month."""
    cfg = _api_cfg()
    if not (cfg['user'] and cfg['password']):
        raise ApiError("The API login is not set up (variables RMS_API_USER / RMS_API_PASSWORD).")
    if d_from > d_to:
        raise ApiError("The 'From' date is after the 'To' date.")
    if apply:
        init_db()                                # also upgrades old databases (adds Ordered_Value)
    else:                                        # a preview must not change anything: only create the tables if the database is brand new
        has_table = False
        if os.path.exists(DB_NAME):
            _c = get_connection()
            has_table = _c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sales'").fetchone() is not None
            _c.close()
        if not has_table:
            init_db()
    api = _Api(cfg)
    api.login()
    chunks = _month_chunks(d_from, d_to)
    conn = get_connection()
    state, months, error = {}, [], None
    plants, skipped_total = set(), 0
    try:
        for i, (a, b) in enumerate(chunks):
            if progress:
                progress(i, len(chunks), '%s %s ...' % ('Syncing' if apply else 'Reading', a.strftime('%b %Y')))
            try:
                skipped_total += _load_month(conn, api, a, b, state)
                lo = a.strftime('%Y-%m-%d 00:00:00')
                hi = (b + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
                st_ = _api_compare(conn, lo, hi)
                st_['month'] = a.strftime('%Y-%m') if (a.day == 1 and (b + timedelta(days=1)).day == 1) else '%s to %s' % (a, b)
                plants |= {r[0] for r in conn.execute('SELECT DISTINCT Plant FROM temp_api WHERE Plant IS NOT NULL')}
                st_['blank_plant'] = conn.execute('SELECT COUNT(*) FROM temp_api WHERE Plant IS NULL').fetchone()[0]
                st_['confirmed_gt_ordered'] = conn.execute('SELECT COUNT(*) FROM temp_api WHERE Confirmed_Qty > Order_Qty').fetchone()[0]
                if apply:
                    _api_apply(conn)
                    conn.commit()
                    st_['updated'], st_['inserted'] = st_['matched'], st_['new_in_api']
                months.append(st_)
            except ApiError as e:
                error = '%s: %s' % (a.strftime('%b %Y'), e)
                conn.rollback()
                break
            finally:
                conn.execute('DROP TABLE IF EXISTS temp_api')
                conn.commit()                    # make sure the scratch table is really gone, even after a preview or an error
        if apply and months:
            _log_sync(conn, d_from, d_to, sum(m['api_lines'] for m in months), sum(m.get('inserted', 0) for m in months),
                      sum(m.get('updated', 0) for m in months), 'complete' if error is None else 'partial', source)
            conn.commit()
    finally:
        conn.close()
    if progress:
        progress(len(chunks), len(chunks), 'Done')
    known = set()
    try:
        b = pd.read_csv('Branch.csv')
        known = set(b['Plant'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True))
    except Exception:
        pass
    warnings = []
    if state.get('doctype_blank'):
        warnings.append('Lines with no program (stored as "Unknown"): %d' % state['doctype_blank'])
    if skipped_total:
        warnings.append('Lines skipped (no order number, line number or valid date): %d' % skipped_total)
    unknown = sorted(p for p in plants if known and p not in known)
    if unknown:
        warnings.append('Plant(s) not in Branch.csv (they will show as "Unknown Branch"): %s' % ', '.join(unknown[:10]))
    if any(m['blank_plant'] for m in months):
        warnings.append('Lines with no plant (shown as "Unknown Branch"): %d' % sum(m['blank_plant'] for m in months))
    if any(m['confirmed_gt_ordered'] for m in months):
        warnings.append('Lines where the confirmed/delivered quantity is above the ordered quantity: %d' % sum(m['confirmed_gt_ordered'] for m in months))
    return {'applied': apply, 'months': months, 'error': error, 'warnings': warnings, 'from': d_from, 'to': d_to,
            'doctype_source': state.get('doctype_source'), 'doctype_values': sorted(state.get('doctype_values', [])), 'confirmed_field': cfg['confirmed_field']}


_SYNC_LOCK = threading.Lock()


def api_run(d_from, d_to, apply=False, progress=None, source='API'):
    """Public entry point: only one sync/preview can run at a time (they share a scratch table)."""
    if not _SYNC_LOCK.acquire(blocking=False):
        raise ApiError("Another sync is already running. Please wait a few minutes and try again.")
    try:
        return _api_run_impl(d_from, d_to, apply, progress, source)
    finally:
        _SYNC_LOCK.release()


def api_test():
    """Checks login, the list endpoint and the export columns without writing anything. Returns [(label, ok, detail)]."""
    steps = []
    cfg = _api_cfg()
    if not (cfg['user'] and cfg['password']):
        return [('Settings', False, 'RMS_API_USER / RMS_API_PASSWORD are not set.')]
    api = _Api(cfg)
    try:
        api.login()
        steps.append(('Login', True, 'accepted, token received'))
    except ApiError as e:
        return [('Login', False, str(e))]
    try:
        r = api.get(cfg['list_path'], {'page': 0, 'size': 1})
        total = r.json().get('totalElements')
        steps.append(('Sales orders list', True, '%s orders available' % (format(total, ',') if isinstance(total, int) else 'n/a')))
    except (ApiError, ValueError) as e:
        steps.append(('Sales orders list', False, str(e)))
    try:
        today = date.today()
        got = None
        for chunk in _iter_export(api, today - timedelta(days=14), today):
            got = chunk
            break
        if got is None or len(got) == 0:
            steps.append(('Export file', True, 'reachable, but no lines in the last 14 days'))
        else:
            _map_api_chunk(got.head(500), cfg, {})
            note = 'columns found: %s' % ', '.join(map(str, got.columns[:8])) + (' ...' if len(got.columns) > 8 else '')
            pcol = {str(c).strip().lower(): c for c in got.columns}.get('program')
            if pcol is not None and cfg['doctype_field'] == 'program':
                vals = got[pcol].dropna().astype(str).str.strip().value_counts().head(6).index.tolist()
                note += ' | program values seen: %s' % (', '.join(vals) if vals else '(all blank)')
            steps.append(('Export file', True, note))
    except ApiError as e:
        steps.append(('Export file', False, str(e)))
    return steps


# ---------------------------------------------------------------------
# AUTOMATIC SYNC - runs by itself twice a day (default 00:00 and 12:00, Rwanda time)
# A background thread checks every minute whether a scheduled time has passed since the last complete sync and, if so,
# runs the API sync for a recent window. It is OFF until you tick the switch in the admin panel (or set RMS_AUTO_SYNC=on),
# so nothing is overwritten before you have checked the Preview. If the app was down at a scheduled time, it catches up.
# ---------------------------------------------------------------------
import threading

KIGALI = timezone(timedelta(hours=2))          # Rwanda (CAT) is UTC+2 all year round, there is no daylight saving
_AUTO_STATE = {'fails': 0, 'next_retry': None}


def _now_utc():
    return datetime.now(timezone.utc)


def _sync_hours():
    raw = os.environ.get('RMS_SYNC_HOURS', '0,12')
    try:
        hours = sorted({int(x) for x in raw.split(',') if x.strip() != '' and 0 <= int(x) <= 23})
    except ValueError:
        hours = []
    return hours or [0, 12]


def _slot_times(now_utc):
    """Scheduled times (as UTC datetimes) from two days back to one day ahead of now."""
    k = now_utc.astimezone(KIGALI)
    out = []
    for dd in range(-2, 2):
        day = (k + timedelta(days=dd)).date()
        for h in _sync_hours():
            out.append(datetime(day.year, day.month, day.day, h, 0, tzinfo=KIGALI).astimezone(timezone.utc))
    return sorted(out)


def latest_slot(now_utc):
    return max(s for s in _slot_times(now_utc) if s <= now_utc)


def next_slot(now_utc):
    return min(s for s in _slot_times(now_utc) if s > now_utc)


def kigali_text(dt_utc):
    return dt_utc.astimezone(KIGALI).strftime('%a %d %b %H:%M')


def get_setting(key, default=None):
    if not os.path.exists(DB_NAME):
        return default
    try:
        conn = get_connection()
        conn.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)')
        r = conn.execute('SELECT value FROM app_settings WHERE key = ?', (key,)).fetchone()
        conn.close()
        return default if r is None else r[0]
    except Exception:
        return default


def set_setting(key, value):
    init_db()
    conn = get_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))
    conn.commit()
    conn.close()


def auto_sync_setting():
    """(enabled, forced_by_environment). The RMS_AUTO_SYNC variable, when set, overrides the admin switch."""
    load_env_file()                                       # settings may live in the .env file (the timer runs before anyone opens the page)
    env = os.environ.get('RMS_AUTO_SYNC', '').strip().lower()
    if env in ('on', '1', 'true', 'yes'):
        return True, True
    if env in ('off', '0', 'false', 'no'):
        return False, True
    return get_setting('auto_sync', 'off') == 'on', False


def _last_complete_sync():
    """(completion time in UTC, to_date text) of the latest complete sync, or None."""
    if not os.path.exists(DB_NAME):
        return None
    try:
        conn = get_connection()
        r = conn.execute("SELECT ts, to_date FROM sync_log WHERE status = 'complete' ORDER BY rowid DESC LIMIT 1").fetchone()
        conn.close()
        if not r:
            return None
        return datetime.strptime(r[0], '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc), r[1]
    except Exception:
        return None


def auto_sync_due(now_utc=None):
    now_utc = now_utc or _now_utc()
    last = _last_complete_sync()
    return last is None or last[0] < latest_slot(now_utc)


def _auto_window(now_utc):
    """Dates an automatic run should read: a rolling recent window, or a full back-fill the first time."""
    today = now_utc.astimezone(KIGALI).date()
    last = _last_complete_sync()
    if last is None:                                       # never synced from the API: back-fill so all rows come from one source
        try:
            return date.fromisoformat(os.environ.get('RMS_SYNC_FROM', '').strip()), today
        except ValueError:
            pass
        try:
            conn = get_connection()
            first = conn.execute('SELECT MIN(substr("Document Date", 1, 10)) FROM sales').fetchone()[0]
            conn.close()
            if first:
                return date.fromisoformat(first).replace(day=1), today
        except Exception:
            pass
        return (today.replace(day=1) - timedelta(days=62)).replace(day=1), today
    recent = min(today - timedelta(days=35), last[0].astimezone(KIGALI).date() - timedelta(days=3))
    return recent.replace(day=1), today


def run_auto_sync_once(now_utc=None):
    """One scheduler tick. Returns a short text when it tried to sync, otherwise None."""
    now_utc = now_utc or _now_utc()
    if not auto_sync_setting()[0]:
        return None
    cfg = _api_cfg()
    if not (cfg['user'] and cfg['password']):
        return None
    if not auto_sync_due(now_utc):
        return None
    retry = _AUTO_STATE.get('next_retry')
    if retry and now_utc < retry:
        return None
    if _SYNC_LOCK.locked():                                # a manual sync is running right now: try again next minute
        return None
    a, b = _auto_window(now_utc)
    stamp = kigali_text(now_utc)
    try:
        print('[auto-sync] %s Kigali: syncing %s to %s' % (stamp, a, b), flush=True)
        res = api_run(a, b, apply=True, progress=None, source='API-auto')
        if res['error']:
            raise ApiError(res['error'])
    except ApiError as e:
        if 'already running' in str(e):
            return None                                    # a manual sync is running: try again next minute
        _AUTO_STATE['fails'] += 1
        _AUTO_STATE['next_retry'] = now_utc + timedelta(minutes=min(15 * 2 ** (_AUTO_STATE['fails'] - 1), 120))
        msg = '%s Kigali: FAILED - %s (will retry)' % (stamp, e)
        set_setting('auto_last', msg)
        print('[auto-sync] ' + msg, flush=True)
        return msg
    _AUTO_STATE.update({'fails': 0, 'next_retry': None})
    lines = sum(m['api_lines'] for m in res['months'])
    ins = sum(m.get('inserted', 0) for m in res['months'])
    upd = sum(m.get('updated', 0) for m in res['months'])
    msg = '%s Kigali: OK - %s lines (%s new, %s updated) for %s to %s' % (stamp, format(lines, ','), format(ins, ','), format(upd, ','), a, b)
    set_setting('auto_last', msg)
    print('[auto-sync] ' + msg, flush=True)
    return msg


def start_scheduler():
    """Start the background timer (once per process; safe to call many times). Returns True if it was started now."""
    load_env_file()
    for t in threading.enumerate():
        if t.name == 'rms-api-scheduler' and t.is_alive():
            return False

    def loop():
        time.sleep(float(os.environ.get('RMS_SCHED_START_DELAY', '20')))
        while True:
            try:
                run_auto_sync_once()
            except Exception as e:                         # the timer must never die
                print('[auto-sync] unexpected problem: %s' % type(e).__name__, flush=True)
            time.sleep(float(os.environ.get('RMS_SCHED_TICK', '60')))

    threading.Thread(target=loop, name='rms-api-scheduler', daemon=True).start()
    return True


def scheduler_status(now_utc=None):
    now_utc = now_utc or _now_utc()
    enabled, forced = auto_sync_setting()
    return {'enabled': enabled, 'forced': forced,
            'thread_alive': any(t.name == 'rms-api-scheduler' and t.is_alive() for t in threading.enumerate()),
            'always_on': os.environ.get('RMS_RUN_PY') == '1',
            'hours': _sync_hours(), 'next_run': kigali_text(next_slot(now_utc)),
            'last_auto': get_setting('auto_last', None)}


def data_version():
    """Changes whenever the database file changes, so the dashboard reloads after an automatic sync."""
    try:
        return int(os.path.getmtime(DB_NAME) * 1000)
    except OSError:
        return 0
