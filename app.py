import streamlit as st
import pandas as pd
import database
import streamlit.components.v1 as components
import os
from datetime import date, timedelta

# ---------------------------------------------------------------------
# 1. STREAMLIT CONFIG, STATE INIT, & CSS
# ---------------------------------------------------------------------
try:
    database.start_scheduler()      # automatic API sync timer (does nothing until switched on in the admin panel)
except Exception:
    pass

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
        /* the loading logo screen is an overlay: its holder must not add any space above the dashboard */
        [data-testid="stElementContainer"]:has(.rms-page-loader), .element-container:has(.rms-page-loader) {position: absolute !important; height: 0 !important; margin: 0 !important; padding: 0 !important;}
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
def _zpack(text):
    """gzip + base64: the data block shrinks about 6 times, the browser unzips it in a fraction of a second."""
    import gzip, base64
    return base64.b64encode(gzip.compress(text.encode('utf-8'), 6)).decode('ascii')


def pack_rows(d, text_cols=None):
    """Column-wise, dictionary-encoded JSON. The browser rebuilds exactly the same list of records from it."""
    import json
    d = d.fillna(0)
    text_cols = text_cols or {'Month', 'Week_Start', 'Week_Range', 'Date', 'Sales_Doc_Type', 'Branch', 'Product_Desc', 'Order_Status'}
    import numpy as np
    keys = list(d.columns)
    dumps = lambda x: json.dumps(x, separators=(',', ':'))
    dict_parts, col_parts = [], []
    for k in keys:
        if k in text_cols:
            codes, uniques = pd.factorize(d[k])               # codes in order of first appearance, like the old loop
            dict_parts.append(dumps(k) + ':' + dumps(uniques.tolist()))
            col_parts.append(dumps(k) + ':' + dumps(codes.tolist()))
        else:
            arr = d[k].to_numpy()
            if arr.dtype.kind == 'f' and np.isfinite(arr).all() and (arr == np.floor(arr)).all():
                arr = arr.astype('int64')                       # whole numbers are sent as integers (smaller)
            txt = dumps(arr.tolist())
            if arr.dtype.kind == 'f':
                txt = txt.replace('.0,', ',').replace('.0]', ']')   # 12.0 -> 12 (same number for the browser, smaller text)
            col_parts.append(dumps(k) + ':' + txt)
    out = '{"n":%d,"keys":%s,"dict":{%s},"cols":{%s}}' % (len(d), dumps(keys), ','.join(dict_parts), ','.join(col_parts))
    return out.replace('</', '<\\/')

# ---------------------------------------------------------------------
# 2. DATA PROCESSING (Cached for Instant Reloads)
# ---------------------------------------------------------------------
LARGE_THRESHOLD = 25000000   # RWF missed per single order item that counts as a "large order" to verify


def _branch_lookup():
    """plant code -> branch name, from Branch.csv (same rules as before; a missing file means every line is "Unknown Branch")."""
    try:
        b = pd.read_csv("Branch.csv")
        b['Plant'] = b['Plant'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
        return dict(zip(b['Plant'], b['Branch']))
    except Exception:
        return {}


def _date_fields(dates):
    """Month, week start and week label for each DISTINCT date (about 500 dates instead of a million lines)."""
    u = pd.Series(pd.unique(dates.dropna()))
    dt = pd.to_datetime(u, format='%Y-%m-%d', errors='coerce')
    ws = dt - pd.to_timedelta(dt.dt.dayofweek, unit='d')
    we = ws + pd.to_timedelta(6, unit='d')
    rng = ws.dt.strftime('%b %d').str.replace(' 0', ' ') + " - " + we.dt.strftime('%b %d').str.replace(' 0', ' ')
    return pd.DataFrame({'Date': u, 'Month': dt.dt.strftime('%b %Y'), 'Week_Start': ws.dt.strftime('%Y-%m-%d'), 'Week_Range': rng})


# ---------------------------------------------------------------------
# 2. DATA PROCESSING (Cached for Instant Reloads)
# The database does the heavy summarising (SQLite does it natively in a few seconds) and only the summarised rows come
# into memory. Loading every order line into pandas used to take about 1.3 GB of memory for a million lines.
# ---------------------------------------------------------------------
@st.cache_data(show_spinner=False, max_entries=2)
def load_and_prep_data(data_version=0):
    db_stats = database.get_db_stats()
    empty = _zpack('{"n":0,"keys":[],"dict":{},"cols":{}}')
    if not os.path.exists(database.DB_NAME):
        return True, db_stats, False, empty, empty, "[]"
    conn = database.get_connection()
    try:
        conn.execute('PRAGMA temp_store = MEMORY')           # SQLite sorts in memory (about 40% faster)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(sales)').fetchall()]
        if not cols or conn.execute('SELECT COUNT(*) FROM sales').fetchone()[0] == 0:
            return True, db_stats, False, empty, empty, "[]"
        has = lambda c: c in cols
        ov_sum = 'COALESCE(SUM("Ordered_Value"), 0)' if has('Ordered_Value') else '0'
        status_expr = "COALESCE(NULLIF(TRIM(\"Order Status\"), ''), 'Unknown')" if has('Order Status') else "'Unknown'"
        # order value is "ready" only when every line has it (older databases fill it in on the next sync / upload)
        ordval_ready = has('Ordered_Value') and conn.execute('SELECT COUNT(*) FROM sales WHERE "Ordered_Value" IS NULL').fetchone()[0] == 0
        agg = pd.read_sql(f'''SELECT substr("Document Date", 1, 10) AS Date, "Sales Document Type" AS Sales_Doc_Type, Plant, "Material Description" AS Product_Desc, {status_expr} AS Order_Status,
                   SUM(Order_Qty) AS Order_Qty, SUM(Confirmed_Qty) AS Confirmed_Qty, SUM(Missed_Qty) AS Missed_Qty, SUM(Lost_Value) AS Lost_Value,
                   {ov_sum} AS Ordered_Value, COUNT(*) AS Total_Lines, SUM(CASE WHEN Missed_Qty > 0 THEN 1 ELSE 0 END) AS Missed_Lines
                FROM sales GROUP BY 1, 2, 3, 4, 5''', conn)
        miss = pd.read_sql(f'''SELECT "Sales Document" AS Doc, "Sales Document Item" AS Item, substr("Document Date", 1, 10) AS Date, "Sales Document Type" AS Sales_Doc_Type, Plant,
                   "Material Description" AS Product_Desc, Order_Qty, Confirmed_Qty, Missed_Qty, Lost_Value, {status_expr} AS Order_Status,
                   {'"Customer Name"' if has('Customer Name') else 'NULL'} AS CName, {'"Customer Code"' if has('Customer Code') else 'NULL'} AS CCode
                FROM sales WHERE Missed_Qty > 0''', conn)
    finally:
        conn.close()

    bl = _branch_lookup()
    wf = _date_fields(pd.concat([agg['Date'], miss['Date']]))
    for frame in (agg, miss):
        frame['Branch'] = frame['Plant'].map(bl).fillna('Unknown Branch')
    agg = agg.merge(wf, on='Date', how='left')
    keys = ['Month', 'Week_Start', 'Week_Range', 'Date', 'Sales_Doc_Type', 'Branch', 'Product_Desc', 'Order_Status']
    sums = ['Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value', 'Ordered_Value', 'Total_Lines', 'Missed_Lines']
    df_agg = agg.groupby(keys, dropna=False, as_index=False)[sums].sum()       # sorted by the keys, exactly as before
    raw_z = _zpack(pack_rows(df_agg))

    # The missed order lines only (about 8% of all lines), with the facility (customer) and the order / item numbers.
    # They feed the Top Facilities table and every click-through window ("what exactly was missed").
    miss = miss.merge(wf[['Date', 'Month', 'Week_Start']], on='Date', how='left')
    name = miss['CName'].astype('string').str.strip()
    code = miss['CCode'].astype('string').str.strip()
    facility = name.mask(name.isna() | (name == ''), code).fillna('Unknown facility')
    miss['Facility'] = facility.mask(facility == '', 'Unknown facility').astype(str)
    miss['Doc'] = miss['Doc'].astype(str)
    miss['Item'] = miss['Item'].astype(str)
    df_miss = miss[['Doc', 'Item', 'Date', 'Month', 'Week_Start', 'Sales_Doc_Type', 'Branch', 'Facility', 'Product_Desc', 'Order_Status', 'Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value']]
    miss_z = _zpack(pack_rows(df_miss, {'Month', 'Week_Start', 'Date', 'Sales_Doc_Type', 'Branch', 'Facility', 'Product_Desc', 'Order_Status'}))
    large = df_miss[df_miss['Lost_Value'] >= LARGE_THRESHOLD][['Doc', 'Item', 'Date', 'Month', 'Week_Start', 'Sales_Doc_Type', 'Branch', 'Product_Desc', 'Order_Status', 'Order_Qty', 'Confirmed_Qty', 'Missed_Qty', 'Lost_Value']]
    return False, db_stats, bool(ordval_ready), raw_z, miss_z, large.fillna(0).to_json(orient='records')

# ---------------------------------------------------------------------
# LOADING ANIMATION: the RMS logo with its ring turning. It is shown while the server prepares the data, and again while the browser unpacks it.
# (Everything is inside this file: nothing to upload or configure.)
# ---------------------------------------------------------------------
RMS_STILL_B64 = "iVBORw0KGgoAAAANSUhEUgAAAU0AAAB6CAMAAAAMJvFXAAAAflBMVEX///8ActMA+f4AXrAAAAAAAAAAAAD89QAAm/0AAAAAXK79BADtrwAAkvsAbMwAcdrboAAC/AD9ogAAB/393AAAYLIAZfjxsQD9XQCj/ABUAACZBQAAAJQAiPEA6pz4wwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADy8y1WAAAAIHRSTlMA+wzjFtWeDSxfpAHdV6FlpQIHAk9tF2kFBQQDA5MJjRRQCMMAABSkSURBVHja7Z2LcuMos4AJEEkWKzuWEsWZy877v+VPN3doJOQkc7bqmK1NHFvC6FPTNxoNY3ut79mjfUN7YH20R3u0R3u0R3u0R3u0R3u0R3u0R/vb7Xr9rjA/zZ/0Pef87s5mBT85d33AC8nmG1P5lxanSvPrNtvzuTkdjhz0y6iD/ub+4KGVPbFe/f3blNPkAzTzs9bsFazZ8D3N/GjiS6mhLPBjXfUJA/4PDd4aCuyLGcAwTBdskx9T/CWfkt6P+K/bXTcGRjiK/fZ0Pl+mTIj9H+sl7eISjqsPSsFnix5Aeu448Lj/n+aX/uZpOo/poefzZJAuXy9ySpkBzLsAM5qTZtXQ8FIvpFbgl6SLCGe/l5UdsnOfhKfpKKFMGpDEiMYpSKisz4JNlRrr1Gs6i+d5VxzTv4YmmHb8wHNR8VyHPiaRHTbZ77huXJuRufxeipX7Ycq5Z7/gmFFU7zjIaDJp5v4zBiqe73rsN6UO0WRHaFrBc7PhZqdDTvNJjPuGjfNfkpUTQwwBdj9LzQnUSMM95lZ/HKGpSk308fHhJOAeP+AgTUAFk1EL54wmaKBoivMuTjhg0UrzqaCp7OXM/U8N6byriFAHgHwOX+NL9UELrttW9Ato4kRGH0Y7QuRMj3DWmS4kTNCb0gwdum9V6nCPp4OO3r5jGGAypRr7PExTD31KfJOSpjlk97svxHkg+L2dh9QBG/N9GA7S5IXLSnix3jdZNt3O+2kanFLar6RoPqElkhsDWHJzntn09RBMfSZ7axbO/pp5HFee+6vx3N6f5y5wuI+mGPxMVoqmuWOJFnoWxx7SIA7RhC+UrGm+Xw3LQAncMXxj7n1Tys1vOG67X9fTXTSfQC+qXiNZtGknae7g5JxQmhnNURycLyZoapnkjmawOPrv19dX+GE+eO1nDB4M+yvnV7Yx2z9H80noaTUbK0PLpj7kUr2yYamx0jTtiI/CvGjTqBhrplkAeWcvcWO3iJTxluQ3yaZxZRTeqxrNDUuk9URFJwof5a+i6g8JIjCCmaBao2sCi+rfX/6J24t2ABGi0YlyV3WYK7uT5pk7ya/RfPIxUeYbrVWYfqaTsouhpG0ZUDhPKiMm87Zc0l753L//U9Asqe3lqQqa57w9CSqyE17w6jQrqnPlazVS9DQF4cROISM3TOezH5Yw91bGNOszvidft9EsMlx7NHnZJiK6CyFmlSaFE/KfkLfao1nGV5c4u2mIuqiTum1NNIMduhY0+/6mSjFfj8nmmRNuLh/OokqqTvPJxUQytgBb1trTzL4P4ln0gLJ04gRdieAJtNCMo/BINl89SKZZvls/6aqJX9ujrF2ay4o884BZiBaaIMG3FMG65ZV7milwhLnEd+V2Y8vAbIbpcnz14CPXnv2fWDC1t/SuDfs7+ktXL/nSnMrvp+nEc6zl3TZomqOk/i9ylLdCHEczG5XmJW2IZyenyTxqK8752/gVCY9opr9omH/g18s/yNMYr/hG8DtpSisOsnClmmha14XJPkqzNNBMzZSeB0Mt770MBxa2NmLEmObrq/aWXv68ouOZnIPRtGSfkk2TgxgpH2mPJuB0QUVDiEPTRAdC31cpK96jbMlmzpvezfwew3yxivMdXtx+UQtad9PEkBWhifRIs36ySTONifbixQ2aVUe80Wm/bfuKXjZRMgGmvKJPr1/uLQgdlE0SGtC87dNMnPi9vFCN5kaCTzXR5ERWXlI0HUxlVQPMdaa+zKaHI/NL1zTnBpo+O/dz2T+UtkJP1RSKun8RvadkE6f5P+9OWfZaAWiaQUn028LZSnNYqCNVC00zUXvZcqTzkAhjtgZpcgyV+iKa197QdJLptLGlGSzRx8eXyCYn5KVihQThxA968EQM0Oq9m6UKntmAHwcW1LIgwqTkZhUSmn8MzHcDk5kFlY8eaEZ1EOD1b3gGzTRzlSeqNGmckrBAZTDuaJb6VYxvlyEUzizyiByqwir3WUMP00jmHIusfqM3XztIG6p/C82qh/T7d0lpwnCqpFyP0wVZIHG+2AqPcnFnuy2ZKPdzms8EVx39zPfY9vdai74yHW6Nb+GbbAAxfIrmKOi0B2HsicXIiQCkxftIRs6nN8dxfFuJ+q6m+hbnL4G1SfMc79qA//PnVcfmyp7Wg6z2+voERiE/wsIS/6RsTkQsxMlaj5Ga0+epVAA6+j7XaG4F874QyS157yyD/Utlh19zmK/9/GI8TWflIIOstaZorLggfZGaaBJitNZoTi05aJj+FZqM0gvEqu8IqnRg8+24Rc9oapicYwL+xS0YzeBsvhrJaCi4oPILo/Ui47VNMiVXzcgBzQacmPohaFrD3bRkiSV5+gbMc48utlLNcUtG8+UVBHjtbRzUS+u5v17fBDVnVStNnpSd2rxsqfOq2WKkSa6WFwt1jFfXhRrlG++KqeUBmrc7aer5PKJuvJrZ3Vtz/u5gkjT5fu69QAmlk0JsrNUSNPcXcO1tq8nm/KPhhiS+KOZ2flDeyz5NLZlvsBwysYmDG48pDzDyGqZIPRjUudWOB1FdFozLYQkPcnGmj6a5kywyiqJOs2cHcIJW47WKQzr7GdN8eWcXhCkgH2syHalkBu8a/YFjNFtWgDnborlTq4HVIlqaijSfn+ngKg/D2MrT4STsEd+zQtqjfINpji6c1mCA0+jP4FjENFW7995cDOA1MU1z2V7hsEmhOk3k2VC+GX3pUMuCUGHT7GlqEbwATHv3xYXhRwlMTzMMDzIh/EtoJsUANM3N9BvkPBdwhouQIN2DwHkrT+Nec9acPAs0Xy1MOxQ9bVBugfHTBs3b7Ytk0ywSzmyHJqv5jAJXcn7A+sg2TWlK8y+j2FU/wkcTLZIZZrr2MxFmWMDXf+i5/n5NY7fEpnNap9xJM1kk3KBZuPz5MvsOTS+gw2XcIyqoc+tZckvz5f0DdWY8ELy8j+yqihiRXBi5rxr21x5Na6CGKkzVTNO5v8N0wfoOUcFaKSPboamj8CIUFgP7N88qnJt20hyuey+KouuySe0TiJaOt6xQiMcwIRwFEhoq1iGVQxuHVprS0YQovMwrQC16tvPG+ZvqS2kKW3XaRJNOfrq18W2afbRYcbsWgkpFFHSVAq+tUUIW7v2quylqIc4AM2vj0JKdOkQTd5MMmWbfpMnyIhG4ZBXR3JjpIdHYR+sHq0sZF769OB+gyfp3KH/VA/hdwgSTNCXtsrKvpul2OslkjatK0yytZCoJcjFzI02Pk4wXy2hrPELzA6JHHUsKNolsToN6EnGaAvOotyxO55+jeWZ5enaed2Uzi9izNGGbFSLLFqEqO6+NOkIT1iRAwMc0ukUtCr9XdxO5Sd2nBXPNNP3u0AInXOkKCVq3ZNhCM/k8r2bb9d63vMe1+OahsS4baho0qgUCjHHKBy+ikapKPYlSbTOd2/3VRCQzuj23/S0K4vZoBsMuiioDwgo1rH3b2c9z4RTrEdlENaTNzUSuGyQ012LFpJmmL06v1mwqyNQol67ZoanCskTpEVZz78a/wltnd8lmIY3Ud/Rumj8YN/voYT8dY4lWdwFmNEt+8UHzlOq4FRK8ruXTSknFWmXTLkugBcKb2m/T7LG6EkJ0t0WDL0u+BKluvZ2Ud8gmRP9os/ibSL0/d82epsSjsZ8fRXVJM81/qX1ugFMyzpr9TSdjoIsxRkq3fG/JJigI3MK9EEhQebfpTdLLHoQVDT7GAXqYj2JwzsuCT0R4c7GQuks2addbDAOTPO6m35VNlLO3ywUETaV7eqo0e7PO5rafyyLlu0hiUXogdCyvrWe7PKzb/p7GRPE+MO3Z1hT6EZqE6hT57t59mqD4pHWuFGuj6fJ5wuTV/Qb0qAqzHN2ZE74pry5o654lPr9hdE/QOKd3RvVQKIOHwjWt+0Vk2zSJhXHMpkUKqkE21W3uey1eZfqxpOkQhAjfbW1J6rk4UTtiYiFV0CRTcnBlZuccLn1NE5TQ5yOR1w8zA/Au8/tpagsmybpqGPMS6Y+Wma5mXEuRRYEvRdMmOcbEPEQ1SNVdN0YRls/ikGSptJG4xYf+2bYbsaK67nGONK6mb8tmpbzqgnkP95SbJr3pE9ZzugxG0exn2CWaxqOuBkk3Bj/O1LM+TGB5a92ZP4ZCXYnGPfe2YD70Roinxqz+Dk0SJ/rgqoFmMTFUtqaY03waoNTv+i+9S3trEdUvNrRVdGLW5MnvgVNrsUNpMPodRxJlZLd18h5NRqrOGOcGTbN2srWzmpDNn1jocceKwFDRkBWag7DKAa6l0MIom5aPWVO03oja6nmfJhc0zgaa9rk0vJ2mqWwaxjtWBOK86S5OblathBjso5dgpidtDcZK+NwETx6wdpBmX6mlBK28soY4XRUS0WLTeRTsHVpGbaVpnDwUO22I8Ol3fECtfLnA//BjYOZxGV672gcWWSFSR2j2qfkTG2swG+tC++qLornoro8/CafuXnN694FJH0T1VFmLgJcxltk/0Uoz9mTIquCLTxuTNGF9tMGrIKwQ+zhQM5Nvo+GNNDHDZrSzoGpqlWV7FrUFEnXEps/bxZvOElVpmidB3kGTXz+upffXAvPn9lN2yoye3VB2nuCJg4PbrKyw1k7CMv70W2yoEEpLD8VuFPBZ+yJJQOG8tuWQtq6njCyvLjgemx8tdfzZUqZ63SWGhd2wDRtCNU3JkvB9qj6epHxromSziCcqloj87LM0r94hbOMZVcht177L2s6I6DmXRn9hqOWvZm2+Q4VnV1nppDYGwABmRclm+9ePNZr4NJ4WniCYv5p2EpRRUkiQ2oySvEICMH72IimavSRFk5cohqUZ58S99rlHNolKpXRdCNfNn0S9ZEbYSX7beU6m8USpasS4pnCwT1SKH22AG8domhQkYhcBp9IueZ7bZueuV05WarN7PaTo1IFvlSBh6P42YFXxftMi8pN4NmmS0dSTDVNKQ/xeTWuS05+vRU3DQj8BFitvk0hZCDZcbTI16eIATZNaDCH4mmy/Xp2ETqECyX/LGdNKbc/J5LUAfhnSci7ZR2kzLDWu1B/RV7i+Xd58g5eMdDTQ2TWHwmOYza83rWf0TF/NH2/2w7e3A3rTdYjnQY/El/uH9lzCV0+maGAZ4Em5awvOTGuq8CA9kQQPmbTWvC7yGm/F43oYufdGf/lQPtpnYR/9VZZdtJakKioAib4zXdGijwRd02J1g9bEXkPaOV7un/QtEkl5dvseY9gFG3xsO8q95yNu7sbj/ODj43lmddi3NYglo6gwLLnGlujMo0dEbOSIJUlT3SgvsDogn53sr1fVFmv9ldb41U6Eb1VLdI7YYugv/8rQYt2vPvvPGLqnhH/bP8BgNWT53i9TyBHWfePXk4Mpv1saEpoq18n9YZrXb4dZTRvwaBdiRNPlUb5P+fDa0Dj7PwHRNNSGQXpLJPyuZK00V6Zu5B3m/5Fr/GuNHNts84zEswLPIqNpLBAqMfU3/kHW//Y/+so3bEK5ZubrzhxXZ8+xamr+/uHSeze/598rVurrUYc+o1yWiKU0uaYZHyzcz/M3mSTigWryS+yf/LaDU3dvbqWJXtU843OIfn7RwGT1TdnSS/S+lHvfJu8ba9Pz5KijhsJDytcvYPl1P8MiTx20k4RXhg38Yf4yP/Q7+An8pY8+4dHYTv4VfHiyr7rOdgoHml8nAHhyvXS2P9OxtN+YDukU3j65bszA4pcn+31h3A0ayuTTElM0CIJmudK0a3fls20ndnrucPD46/T8LOFHZ46BAetfsnNHI44ODnIM3Wt4cbKHdf6FhA9O2G/o4ARfazouR3Ryb+sBJaPMBiyxXzfu/fZR6PYQAEU0R86PWwE9zpNuHY4emZnBwU8gZDHCe4ZmZ49m4X1HMyA+4WG6SXe8BtMZFlqEThZ96D/QxK+AI+x4LM3OjlJGLy0/c3p6S7Z5fidNaSSqMxylIYQ0nzv3FlyYoYkYUXLhMu0FG4hWtMN1wqmdRdwBTSdH7GTuhu5f+oOsaYO38FmbKU2jEvSttwjDSyf0XfNFp5SiGrKIpriLZmdownhPdip29gJPMGBDE0gEmngtIGzubEPTyZujGYE6mXNPgRu+6frvEtm0+i+naV46aulL6e/TcV+ZpBn/czLHZdPQ7CwNvNOAyggFDjymaT53N8BNUE8eekJT4YnAiU6aPLnQ/3PC+NkamVhvGhG3I8loxnJ6X+BUjdMXrPpSrf+IoB0yTkWJagmVoZ3oeMWIxEzThObpOdAyNFGeu85boa6keYq/2vefWCFUs1HfJ69CHTYzBwLBk1VJd7q1YXEspnnxD0aup9XmcqZrrd4Zu2JUf2fNKdx9S7mzRGOa9sxn48UbXGhqOm+FnO6TJM2TOf+UznTkaa3ic5MVYt5cHk2EwSk/4vqKaFeGwCcJq93sZfqwB+/z+Hss7RzqnHMjjUXuDE1rtE4yOtHoXytEXWmFnAVL5mMXBPg5DwBkoixPkYdUunTB1TqMM0shJTRH3vS01JQmekid9zUMODtsVH7Phqb5zFmhZySDH/upLFGuwXZH6jRoOdYFcTMmGzt4tv1nE9DelkDTKmKjVo1iDffsgHtEATkTNKHqRd0OptW9s3YKM+bkDJH0UiiteULbDpqtc86S8wk6exukoaklGpt5cXK6GE4E96dDmxf1j80GWHCEVSV4bNCbVmhtisB/C/sEzSVe1U/3Xx1fpMArYQ6dcY9OyDSEGJ44eu8oT50Mjqr3mcKJVoC6zgjVs9PK5n6ZDowLYGyzO9oqgM7ocemP9QrD2q7I+kP7DE2ohI3XhfysF5e2VT8VeaVWtoy1SV138wHaD4faatnOGG9vj1w/hpChaXWuPV73Z7i7HmTSfwg/mTQ23Uzl7tkFDt7IBJc1xK/e2N3RYGU+qpOJVtlgC9FRmtIpMpPZ8T9l/Il71zXyRBlOzFv8VRqXOzTqPxxm32HRsSz9kLHszPjzo02M599x0U+g+fRbCPZoh9sTtQL89ODyWabnEUoTHiwe7dEeTT0QPNqjPdqjPdqj/Uda3z8Y/FccpPn/lYv0P6zpCIQTLXWvAAAAAElFTkSuQmCC"
RMS_RING_SVG = '<svg class="rms-ring" viewBox="-64.2 -64.2 128.4 128.4" width="128" height="128" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><circle r="52.41" fill="none" stroke="#0972ca" stroke-width="5.6" stroke-linecap="round" stroke-dasharray="307.69 21.61" transform="rotate(243.56)"/><circle cx="-30.38" cy="-43.06" r="4.6" fill="#f1bc20"/></svg>'
RMS_LOADER_CSS = ("@keyframes rmsSpin{to{transform:rotate(360deg);}} @keyframes rmsBlink{0%,80%,100%{opacity:.15;}40%{opacity:1;}} "
                  ".rms-loader{display:flex;flex-direction:column;align-items:center;gap:14px;padding:46px 0 40px 0;font-family:'Inter',-apple-system,'Segoe UI',sans-serif;} "
                  ".rms-lockup{position:relative;width:333px;height:122px;} .rms-still{display:block;width:333px;height:122px;} "
                  ".rms-ring{position:absolute;left:-4.7px;top:-3.1px;width:128px;height:128px;animation:rmsSpin 1.4s linear infinite;will-change:transform;} "
                  ".rms-load-text{font-size:16px;font-weight:700;color:#1e293b;} .rms-dots span{animation:rmsBlink 1.2s infinite both;} "
                  ".rms-dots span:nth-child(2){animation-delay:.2s;} .rms-dots span:nth-child(3){animation-delay:.4s;} .rms-load-sub{font-size:12.5px;color:#64748b;text-align:center;max-width:420px;} "
                  "@media (prefers-reduced-motion: reduce){.rms-ring{animation-duration:6s;}}")

RMS_PAGE_OVERLAY_CSS = ("@keyframes rmsAutoHide{to{visibility:hidden;opacity:0;pointer-events:none;}} "
                        ".rms-page-loader{position:fixed;top:0;left:0;right:0;bottom:0;z-index:999999;background:#f4f7fb;display:flex;justify-content:center;align-items:flex-start;padding-top:126px;animation:rmsAutoHide .01s linear 45s forwards;}")
RMS_LOADER_GONE = '<div class="rms-page-loader" style="display:none"></div>'
_RMS_PREP = database.__dict__.setdefault('_rms_prep_state', {})      # which data version has already been prepared (survives page reruns)


def rms_loader(sub):
    return ('<div class="rms-loader"><div class="rms-lockup"><img class="rms-still" alt="RMS" src="data:image/png;base64,' + RMS_STILL_B64 + '">' + RMS_RING_SVG + '</div>'
            '<div class="rms-load-text">Loading the dashboard<span class="rms-dots"><span>.</span><span>.</span><span>.</span></span></div><div class="rms-load-sub">' + sub + '</div></div>')


RMS_LOADER_IFRAME = rms_loader("Unpacking your data. This only takes a moment.")

# Call the cached data loader
_rms_ver = database.data_version()
_rms_first = not st.session_state.get('rms_seen_dashboard', False)         # first time this visitor opens the page
_rms_slow = _rms_first or (_RMS_PREP.get('v') != _rms_ver)                 # ... or the data has to be prepared again (after a sync / upload)
_rms_wait = st.empty()                                                    # always created, so every element keeps the same position on every run (the dashboard frame is not rebuilt)
if not _rms_slow:
    _rms_wait.markdown(RMS_LOADER_GONE, unsafe_allow_html=True)
else:
    _rms_wait.markdown("<style>" + RMS_LOADER_CSS + RMS_PAGE_OVERLAY_CSS + "</style><div class=\"rms-page-loader\">" + rms_loader("Preparing your numbers. The first visit after an update can take a little longer.") + "</div>", unsafe_allow_html=True)
try:
    IS_EMPTY, db_stats, ORDVAL_READY, raw_z, miss_z, large_json = load_and_prep_data(_rms_ver)
except Exception:
    _rms_wait.markdown(RMS_LOADER_GONE, unsafe_allow_html=True)
    raise
_RMS_PREP['v'] = _rms_ver
ORDVAL_READY_JS = 'true' if ORDVAL_READY else 'false'

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

        .dashboard-container {{ display: flex; flex: 1; flex-wrap: wrap; gap: 15px; opacity: {0.2 if IS_EMPTY else 1}; pointer-events: {'none' if IS_EMPTY else 'auto'}; }}
        
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

        /* ---- Added: checkbox dropdowns (multi-select), week dropdown and calendar. The closed buttons look exactly like the old selects. ---- */
        .msel {{ position: relative; }}
        .msel-btn {{ display: block; width: 100%; box-sizing: border-box; text-align: left; font-family: 'Inter', sans-serif; font-size: 0.85rem; font-weight: 600; line-height: normal; cursor: pointer; background-color: var(--accent-purple); border: 1px solid rgba(255, 255, 255, 0.3); color: white; padding: 10px 34px 10px 14px; border-radius: 6px; outline: none; box-shadow: inset 0 1px 2px rgba(255,255,255,0.1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .selectBox:hover .msel-btn {{ background-color: #4c4eb8; }}
        .msel.open .msel-btn {{ background-color: #4c4eb8; }}
        .msel-panel {{ display: none; position: absolute; left: 0; top: calc(100% + 4px); width: 100%; min-width: 210px; box-sizing: border-box; background: #fff; color: var(--text-main); border: 1px solid var(--border-color); border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.20); z-index: 500; padding: 6px; }}
        .msel.open .msel-panel {{ display: block; }}
        .filter-group .msel-panel .msel-search {{ display: block; width: 100%; box-sizing: border-box; padding: 7px 9px; margin: 0 0 4px 0; border: 1px solid var(--border-color); border-radius: 6px; background: #fff; color: var(--text-main); font-family: 'Inter', sans-serif; font-size: 0.8rem; }}
        .filter-group .msel-panel .msel-search::placeholder {{ color: var(--text-muted); }}
        .msel-list {{ max-height: 230px; overflow-y: auto; }}
        .filter-group .msel-panel label.msel-row {{ display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 0.82rem; font-weight: 500; color: var(--text-main); text-transform: none; letter-spacing: 0; }}
        .filter-group .msel-panel label.msel-row:hover {{ background: #f1f2fb; }}
        .filter-group .msel-panel label.msel-all {{ font-weight: 700; border-bottom: 1px solid var(--border-color); border-radius: 6px 6px 0 0; margin-bottom: 2px; }}
        .filter-group .msel-panel input[type=checkbox] {{ width: 15px; height: 15px; margin: 0; padding: 0; flex: 0 0 15px; accent-color: var(--accent-purple); cursor: pointer; background: transparent; }}
        .msel-empty {{ padding: 8px; color: var(--text-muted); font-size: 0.8rem; }}
        .cal-panel {{ display: none; position: absolute; left: 0; top: calc(100% + 4px); width: 300px; box-sizing: border-box; background: #fff; color: var(--text-main); border: 1px solid var(--border-color); border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,0.22); z-index: 600; padding: 10px; }}
        .msel.open .cal-panel {{ display: block; }}
        .cal-tabs {{ display: flex; gap: 4px; background: #f1f2fb; border-radius: 8px; padding: 3px; margin-bottom: 8px; }}
        .cal-tab {{ flex: 1; text-align: center; padding: 5px 0; border-radius: 6px; cursor: pointer; font-size: 0.75rem; font-weight: 700; color: var(--text-muted); user-select: none; }}
        .cal-tab.active {{ background: var(--accent-purple); color: #fff; }}
        .cal-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }}
        .cal-nav {{ cursor: pointer; padding: 4px 12px; border-radius: 6px; font-weight: 800; font-size: 1rem; color: var(--accent-purple); user-select: none; }}
        .cal-nav:hover, .cal-title:hover {{ background: #f1f2fb; }}
        .cal-title {{ cursor: pointer; font-weight: 700; font-size: 0.88rem; padding: 4px 8px; border-radius: 6px; user-select: none; }}
        .cal-grid {{ display: flex; flex-direction: column; gap: 2px; }}
        .cal-row {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; border-radius: 6px; }}
        .cal-dow {{ text-align: center; font-size: 0.65rem; font-weight: 700; color: var(--text-muted); padding: 3px 0; }}
        .cal-day {{ text-align: center; padding: 6px 0; border-radius: 6px; font-size: 0.78rem; font-weight: 600; cursor: pointer; color: var(--text-main); user-select: none; }}
        .cal-day.out {{ color: #aab3c0; font-weight: 500; }}
        .cal-day.nodata {{ color: #d3d8e0; cursor: not-allowed; font-weight: 500; }}
        .cal-day.hasdata:hover {{ background: #e9eafd; }}
        .cal-day.sel {{ background: var(--accent-purple); color: #fff; }}
        .cal-grid.weekmode .cal-row.hasdata:hover .cal-day {{ background: #e9eafd; }}
        .cal-day.sel, .cal-day.sel:hover, .cal-day.hasdata.sel:hover {{ background: var(--accent-purple); color: #fff; }}
        .cal-grid.weekmode .cal-row.wsel .cal-day, .cal-grid.weekmode .cal-row.wsel:hover .cal-day {{ background: var(--accent-purple); color: #fff; }}
        .cal-months {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }}
        .cal-m {{ text-align: center; padding: 10px 0; border-radius: 8px; border: 1px solid var(--border-color); font-size: 0.82rem; font-weight: 700; cursor: pointer; user-select: none; }}
        .cal-m:hover {{ background: #e9eafd; }}
        .cal-m.sel {{ background: var(--accent-purple); color: #fff; border-color: var(--accent-purple); }}
        .cal-m.nodata {{ color: #c5ccd6; cursor: not-allowed; background: #fafbfd; }}
        .cal-foot {{ display: flex; justify-content: space-between; gap: 6px; margin-top: 8px; }}
        .cal-btn {{ cursor: pointer; font-size: 0.72rem; font-weight: 700; color: var(--accent-purple); padding: 4px 8px; border-radius: 6px; user-select: none; }}
        .cal-btn:hover {{ background: #f1f2fb; }}
        .cal-hint {{ margin-top: 6px; font-size: 0.7rem; color: var(--text-muted); }}

        /* ---- Added: "Ask the data" panel ---- */
        .ask-top {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 8px; }}
        .ask-groups {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .ask-chip {{ cursor: pointer; border: 1px solid var(--border-color); background: #fff; color: var(--text-main); border-radius: 20px; padding: 7px 14px; font-family: 'Inter', sans-serif; font-size: 0.8rem; font-weight: 600; }}
        .ask-chip:hover {{ background: #f1f2fb; }}
        .ask-chip.active {{ background: var(--accent-purple); color: #fff; border-color: var(--accent-purple); }}
        .ask-search {{ width: 260px; max-width: 100%; box-sizing: border-box; padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 20px; font-family: 'Inter', sans-serif; font-size: 0.8rem; color: var(--text-main); background: #fff; }}
        .ask-questions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 4px; }}
        .ask-q {{ cursor: pointer; text-align: left; border: 1px solid var(--border-color); background: #fafbff; color: var(--text-main); border-radius: 8px; padding: 9px 12px; font-family: 'Inter', sans-serif; font-size: 0.82rem; font-weight: 500; }}
        .ask-q:hover {{ background: #eef0ff; border-color: var(--accent-purple); }}
        .ask-q.active {{ background: #eef0ff; border-color: var(--accent-purple); font-weight: 700; }}
        .ask-q small {{ display: block; color: var(--text-muted); font-size: 0.68rem; font-weight: 600; margin-top: 2px; text-transform: uppercase; letter-spacing: 0.3px; }}
        .ask-hint {{ color: var(--text-muted); font-size: 0.8rem; padding: 4px 2px; }}
        .ask-answer {{ margin-top: 10px; }}
        .ask-answer:empty {{ display: none; }}
        .ask-card {{ border-left: 5px solid var(--accent-purple); background: #fff; border-radius: 10px; padding: 12px 16px; box-shadow: var(--shadow); }}
        .ask-title {{ font-size: 0.72rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 4px; }}
        .ask-head {{ font-size: 0.95rem; font-weight: 600; color: var(--text-main); line-height: 1.5; margin-bottom: 10px; }}
        .ask-table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
        .ask-table th {{ text-align: left; font-size: 0.7rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.3px; padding: 6px 10px; border-bottom: 2px solid var(--border-color); white-space: nowrap; }}
        .ask-table td {{ padding: 7px 10px; border-bottom: 1px solid var(--border-color); color: var(--text-main); }}
        .ask-table .num, .ask-table th.num {{ text-align: right; white-space: nowrap; }}
        .ask-table td.bar {{ background-repeat: no-repeat; background-size: 100% 100%; }}
        .ask-note {{ margin-top: 8px; font-size: 0.78rem; color: #92400e; background: #fffbeb; border-radius: 6px; padding: 6px 10px; }}
        .ask-scope {{ margin-top: 8px; font-size: 0.7rem; color: var(--text-muted); }}
        @media (max-width: 900px) {{ .ask-questions {{ grid-template-columns: 1fr; }} .ask-search {{ width: 100%; }} }}

        /* ---- Added: fiscal year, Compare KPIs, Top Facilities and the click-through windows ---- */
        .more-panel {{ margin-top: 15px; }}
        .cmp-controls {{ display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-end; margin-bottom: 8px; }}
        .cmp-controls > div > label.cap {{ display: block; font-size: 0.68rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 4px; }}
        .cmp-select {{ padding: 8px 12px; border: 1px solid var(--border-color); border-radius: 8px; font-family: 'Inter', sans-serif; font-size: 0.82rem; font-weight: 600; color: var(--text-main); background: #fff; min-width: 270px; }}
        .cmp-radios {{ display: flex; gap: 14px; padding: 6px 0; }}
        .cmp-radios label {{ display: flex; align-items: center; gap: 5px; font-size: 0.8rem; font-weight: 600; color: var(--text-main); cursor: pointer; }}
        .cmp-radios input {{ accent-color: var(--accent-purple); margin: 0; }}
        .cmp-fys {{ display: flex; flex-wrap: wrap; gap: 6px; }}
        .cmp-note {{ margin-top: 6px; font-size: 0.75rem; color: var(--text-muted); }}
        .fac-grid {{ display: grid; grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr); gap: 14px; align-items: start; }}
        table.fac-table th, table.fac-table td {{ white-space: nowrap; padding: 9px 7px; font-size: 0.78rem; }}
        table.fac-table td.name {{ white-space: normal; min-width: 130px; }}
        .fac-table tbody tr, table.row-click tbody tr {{ cursor: pointer; }}
        .fac-table td.num, .fac-table th.num {{ text-align: right; }}
        .fac-empty {{ background: #fffbeb; color: #92400e; border-radius: 8px; padding: 10px 14px; font-size: 0.82rem; line-height: 1.5; }}
        .drill-tile.hl {{ border-color: var(--accent-purple); box-shadow: 0 0 0 2px rgba(90, 92, 224, 0.18); }}
        .drill-panel table.light-table td.name {{ white-space: normal; }}
        .drill-panel .dataTables_wrapper {{ font-size: 0.78rem; }}
        .drill-panel .dt-buttons {{ margin-bottom: 6px; }}
        @media (max-width: 900px) {{ .fac-grid {{ grid-template-columns: 1fr; }} }}

        /* ---- Added: "Showing ..." strip, period tags, collapsible filters ---- */
        .show-strip {{ display: flex; align-items: center; gap: 12px; background: #fff; border: 1px solid var(--border-color); border-left: 5px solid var(--accent-purple); border-radius: 10px; padding: 8px 14px; box-shadow: var(--shadow); font-size: 0.85rem; color: var(--text-main); }}
        .sb-toggle {{ cursor: pointer; border: 1px solid var(--border-color); background: #f1f2fb; color: var(--accent-purple); border-radius: 8px; padding: 6px 12px; font-family: 'Inter', sans-serif; font-size: 0.75rem; font-weight: 700; white-space: nowrap; }}
        .sb-toggle:hover {{ background: #e4e6fb; }}
        .show-text {{ flex: 1; min-width: 0; line-height: 1.5; }}
        .show-text b {{ color: var(--text-muted); text-transform: uppercase; font-size: 0.68rem; letter-spacing: 0.4px; margin-right: 2px; }}
        .show-period {{ font-weight: 800; color: var(--accent-purple); font-size: 0.98rem; }}
        .show-sep {{ color: var(--text-muted); margin: 0 2px; }}
        .show-right {{ font-size: 0.72rem; color: var(--text-muted); white-space: nowrap; }}
        .period-chip {{ display: inline-block; margin-left: 8px; padding: 2px 9px; border-radius: 20px; background: #eef0ff; color: var(--accent-purple); font-size: 0.66rem; font-weight: 700; text-transform: none; letter-spacing: 0; vertical-align: middle; white-space: nowrap; }}
        .dashboard-container.sb-collapsed .sidebar {{ display: none; }}
        @media (max-width: 700px) {{ .show-strip {{ flex-wrap: wrap; }} .show-right {{ width: 100%; }} }}
        .panel {{ min-width: 0; }}      /* lets charts and tables shrink back when the filter side is shown again (a grid box otherwise keeps its widest size) */

        /* ---- Added: prompt box of the Ask panel ---- */
        .ask-prompt {{ display: flex; gap: 6px; align-items: center; flex: 1; justify-content: flex-end; min-width: 280px; }}
        .ask-prompt .ask-search {{ flex: 1; max-width: 480px; width: auto; }}
        .ask-understood {{ margin: 2px 0 8px 0; padding: 9px 14px; background: #eef0ff; border-left: 4px solid var(--accent-purple); border-radius: 8px; font-size: 0.82rem; color: var(--text-main); line-height: 1.55; }}
        .ask-understood:empty {{ display: none; }}
        .ask-understood .ask-chip {{ padding: 3px 10px; font-size: 0.72rem; margin-left: 8px; }}
        .ask-understood .ask-miss {{ color: #92400e; }}

        /* ---- Ask the data: the main thing on the page (green frame, like the KPI card) ---- */
        .ask-hero {{ border: 3px solid #1b8a3c; border-radius: 18px; background: #f0f8f1; box-shadow: 0 6px 18px rgba(27, 138, 60, 0.14); padding: 20px 24px 18px 24px; }}
        .ask-kicker {{ font-size: 0.74rem; font-weight: 800; letter-spacing: 0.9px; color: #5b6b78; text-transform: uppercase; }}
        .ask-title-main {{ font-size: 1.6rem; font-weight: 800; color: #0f172a; margin: 2px 0 2px 0; line-height: 1.25; }}
        .ask-sub {{ font-size: 0.85rem; color: #5b6b78; margin-bottom: 14px; }}
        .ask-prompt-row {{ display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }}
        .ask-hero .ask-search {{ flex: 1; width: auto; max-width: none; height: 52px; box-sizing: border-box; padding: 0 22px; font-size: 1rem; border: 2px solid #b9dcc2; border-radius: 28px; background: #fff; }}
        .ask-hero .ask-search:focus {{ outline: none; border-color: #1b8a3c; box-shadow: 0 0 0 3px rgba(27, 138, 60, 0.18); }}
        .ask-hero #askGo {{ height: 52px; padding: 0 32px; font-size: 1rem; background: #1b8a3c; border-color: #1b8a3c; color: #fff; border-radius: 28px; }}
        .ask-hero #askGo:hover {{ background: #16742f; }}
        .ask-try {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; font-size: 0.8rem; color: #5b6b78; font-weight: 600; }}
        .ask-eg {{ cursor: pointer; border: 1px dashed #8fcca0; background: transparent; color: #166534; border-radius: 20px; padding: 5px 12px; font-family: 'Inter', sans-serif; font-size: 0.78rem; font-weight: 600; }}
        .ask-eg:hover {{ background: #e3f3e6; }}
        .ask-hero .ask-chip {{ border-color: #b9dcc2; }}
        .ask-hero .ask-chip:hover {{ background: #e3f3e6; }}
        .ask-hero .ask-chip.active {{ background: #1b8a3c; border-color: #1b8a3c; color: #fff; }}
        .ask-hero .ask-q {{ background: #fff; }}
        .ask-hero .ask-q:hover, .ask-hero .ask-q.active {{ background: #e3f3e6; border-color: #1b8a3c; }}
        .ask-hero .ask-card {{ border-left-color: #1b8a3c; }}
        .ask-hero .ask-understood {{ background: #e3f3e6; border-left-color: #1b8a3c; }}
        @media (max-width: 700px) {{ .ask-prompt-row {{ flex-wrap: wrap; }} .ask-title-main {{ font-size: 1.3rem; }} }}
        {RMS_LOADER_CSS}
        #rmsLoader {{ position: absolute; top: 0; left: 0; width: 100%; min-height: 100%; z-index: 99999; background: var(--bg-color); display: flex; justify-content: center; align-items: flex-start; padding-top: 110px; box-sizing: border-box; transition: opacity 0.4s ease; }}
        #rmsLoader.done {{ opacity: 0; pointer-events: none; }}
    </style>
</head>
<body>
<div id="rmsLoader">{RMS_LOADER_IFRAME}</div>
<script>requestAnimationFrame(function() {{ requestAnimationFrame(function() {{ try {{ var p = window.parent.document.querySelectorAll('.rms-page-loader'); for (var i = 0; i < p.length; i++) p[i].style.display = 'none'; }} catch (e) {{}} }}); }});</script>
    
    <!-- BANNER AT VERY TOP -->
    <div class="header-banner">
        <div class="header-logo">RMS</div>
        <h1>RMS Missed Deliveries & Sales Process KPIs</h1>
    </div>

    <!-- Alert for empty database -->
    <div style="display: { 'block' if IS_EMPTY else 'none' }; background: #e0e7ff; color: #1e3a8a; padding: 15px; border-radius: 8px; border-left: 5px solid #4f46e5; margin-bottom: 20px; font-weight: 500; font-size: 0.95rem; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        ⚠️ The database is currently empty. Please scroll to the Data Management panel at the bottom to upload your data.
    </div>

    <div class="dashboard-container">
        <!-- EXACT PURPLE SIDEBAR -->
        <div class="sidebar">
            <h2>Filters</h2>
            <div class="filter-group">
                <label>Fiscal Year</label>
                <div class="selectBox msel" id="mselFY">
                    <button type="button" class="msel-btn" id="mselFYBtn">All Years</button>
                    <div class="msel-panel" id="mselFYPanel"></div>
                </div>
            </div>
            <div class="filter-group">
                <label>Month</label>
                <div class="selectBox msel" id="mselMonth">
                    <button type="button" class="msel-btn" id="mselMonthBtn">All Months</button>
                    <div class="msel-panel" id="mselMonthPanel"></div>
                    <select id="filterMonth" style="display:none"><option value="All">All Months</option></select>
                </div>
            </div>
            <div class="filter-group">
                <label>Week</label>
                <div class="selectBox msel" id="mselWeek">
                    <button type="button" class="msel-btn" id="mselWeekBtn">All Weeks</button>
                    <div class="msel-panel" id="mselWeekPanel"></div>
                </div>
            </div>
            <div class="filter-group">
                <label>Calendar</label>
                <div class="selectBox msel" id="calBox">
                    <button type="button" class="msel-btn" id="calBtn">All dates</button>
                    <div class="cal-panel" id="calPanel"></div>
                </div>
            </div>
            <div class="filter-group">
                <label >Sales Document Type</label>
                <div class="selectBox msel" id="mselType">
                    <button type="button" class="msel-btn" id="mselTypeBtn">All Types</button>
                    <div class="msel-panel" id="mselTypePanel"></div>
                    <select id="filterType" style="display:none"><option value="All">All Types</option></select>
                </div>
            </div>
            <div class="filter-group">
                <label>Order Status</label>
                <div class="selectBox msel" id="mselStatus">
                    <button type="button" class="msel-btn" id="mselStatusBtn">All Statuses</button>
                    <div class="msel-panel" id="mselStatusPanel"></div>
                </div>
            </div>
            <div class="filter-group">
                <label>Branch Name</label>
                <div class="selectBox msel" id="mselBranch">
                    <button type="button" class="msel-btn" id="mselBranchBtn">All Branches</button>
                    <div class="msel-panel" id="mselBranchPanel"></div>
                    <select id="filterBranch" style="display:none"><option value="All">All Branches</option></select>
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
            <!-- ADDED: what the numbers below show, and the button that folds the filters away -->
            <div class="show-strip" id="showStrip">
                <button type="button" class="sb-toggle" id="sbToggle" aria-expanded="true" title="Hide the filters to give the dashboard more room">&laquo; Hide filters</button>
                <div class="show-text" id="showText"><b>Showing</b> <span class="show-period">&hellip;</span></div>
                <div class="show-right" id="showRight"></div>
            </div>
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

            
            <!-- ASK THE DATA: the main thing on the page -->
            <div class="panel ask-panel ask-hero" id="askPanel" style="display:none;">
                <div class="ask-kicker">Ask the data</div>
                <div class="ask-title-main">What do you want to know?</div>
                <div class="ask-sub">Pick a topic, then a question. Answers use the same numbers as the dashboard and follow your filters.</div>
                <div class="ask-try" id="askTry"></div>
                <div class="ask-groups" id="askGroups"></div>
                <div class="ask-questions" id="askQuestions"></div>
                <div class="ask-answer" id="askAnswer"></div>
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

    <!-- ADDED: COMPARE KPIs (fiscal years, month on month) AND TOP 10 FACILITIES -->
    <div class="panel more-panel" id="cmpPanel" style="display:none;">
        <div class="panel-header"><span>Compare KPIs <span class="drill-hint">pick a KPI and the fiscal years &mdash; month on month. Click a bar or point for the data behind it</span></span></div>
        <div class="cmp-controls">
            <div><label class="cap" for="cmpKpi">KPI</label><select id="cmpKpi" class="cmp-select"></select></div>
            <div><label class="cap">Fiscal years</label><div class="cmp-fys" id="cmpFys"></div></div>
            <div><label class="cap">Show as</label><div class="cmp-radios"><label><input type="radio" name="cmpType" value="bar" checked> Bars</label><label><input type="radio" name="cmpType" value="line"> Lines</label></div></div>
        </div>
        <div id="cmpChart" style="height:340px;"></div>
        <div class="cmp-note" id="cmpNote"></div>
    </div>
    <div class="panel more-panel" id="facPanel" style="display:none;">
        <div class="panel-header"><span><span id="facTitle">Top 10 Facilities by Missed Value</span> <span class="drill-hint">click a bar or a row to see exactly what was missed</span></span></div>
        <div class="cmp-controls">
            <div><label class="cap">Rank by</label><div class="cmp-radios"><label><input type="radio" name="facMetric" value="val" checked> Missed value</label><label><input type="radio" name="facMetric" value="qty"> Missed units</label><label><input type="radio" name="facMetric" value="lines"> Lines short</label></div></div>
        </div>
        <div id="facBody">
            <div class="fac-grid">
                <div id="facChart" style="height:390px;"></div>
                <div class="table-container" style="max-height:390px;"><table class="light-table fac-table" id="facTable"><thead><tr><th>#</th><th>Facility</th><th>Branch</th><th class="num">Missed RWF</th><th class="num">Share</th><th class="num">Units</th><th class="num">Lines</th></tr></thead><tbody></tbody></table></div>
            </div>
        </div>
        <div class="cmp-note" id="facNote"></div>
    </div>

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
        const rawPackedZ = "{raw_z}";
        const missPackedZ = "{miss_z}";
        let rawData = [];
        let missData = [];
        function unpackRows(p) {{
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
        }}
        async function gunzipB64(b64) {{
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
            return await new Response(stream).text();
        }}
        function hideRmsLoader() {{
            const L = document.getElementById('rmsLoader');
            if (!L) return;
            L.classList.add('done');
            setTimeout(function() {{ L.remove(); }}, 450);
        }}
        async function loadAndStart() {{
            try {{
                if (typeof DecompressionStream === 'undefined') throw new Error('this browser is too old to open the data. Please use a current Chrome, Edge, Firefox or Safari.');
                rawData = unpackRows(JSON.parse(await gunzipB64(rawPackedZ)));
                missData = unpackRows(JSON.parse(await gunzipB64(missPackedZ)));
            }} catch (e) {{
                console.error('Data load error:', e);
                hideRmsLoader();
                document.body.insertAdjacentHTML('afterbegin', '<div style="margin:12px;padding:12px 16px;background:#fef2f2;border-left:5px solid #dc2626;border-radius:8px;font-family:sans-serif;">Could not load the dashboard data: ' + String(e.message || e) + '</div>');
                return;
            }}
            initApp();
            window.dashReady = true;
            hideRmsLoader();
        }}
        let tableDetails, tableLoc, tableItems;
        const largeData = {large_json};
        const ordValReady = {ORDVAL_READY_JS};
        const LARGE_T = {LARGE_THRESHOLD};
        let tableBranchPerf, tableMonthSum, tableLarge;
        let defaultMonth = 'All';   // set to the latest month once the data is loaded

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

            initFilters();   // checkbox dropdowns, week dropdown and calendar
            try {{ initAsk(); }} catch (e) {{ console.error('Ask panel init error:', e); }}
            try {{ initMore(); }} catch (e) {{ console.error('Compare / facility init error:', e); }}       // 'Ask the data' panel (can never stop the dashboard)

            // Listeners
            document.getElementById('filterMonth').addEventListener('change', updateDashboard);
            document.getElementById('filterType').addEventListener('change', updateDashboard);
            document.getElementById('filterBranch').addEventListener('change', updateDashboard);
            document.getElementById('filterSearch').addEventListener('keyup', updateDashboard);

            updateDashboard();   // first draw with all months: keeps the table column widths exactly as they always were

            // ADDED: then switch to the latest month in the data ("All Months" is still one click away)
            try {{
                let latest = null, latestMonth = 'All';
                rawData.forEach(r => {{ if (r.Date && (latest === null || r.Date > latest)) {{ latest = r.Date; latestMonth = r.Month; }} }});
                defaultMonth = latestMonth;
                if (latestMonth !== 'All') {{
                    FILT.months = new Set([latestMonth]);
                    refreshAll();
                    updateDashboard();
                }}
            }} catch (e) {{ console.error('Default month error:', e); }}
        }}

        // ===================== ADDED: checkbox filters, week dropdown and calendar =====================
        // FILT holds the current choice. An EMPTY set means "All". Every chart, table and window reads the filters only through matchRow().
        const FILT = {{fys: new Set(), statuses: new Set(), months: new Set(), weeks: new Set(), types: new Set(), branches: new Set(), dayFrom: null, dayTo: null, search: ''}};
        const MON3 = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        let MONTH_ORDER = [], WEEK_INFO = {{}}, WEEKS_BY_MONTH = {{}}, WEEK_MONTHS = {{}}, DATE_MONTH = {{}}, MULTI_YEAR = false, FY_OF_MONTH = {{}}, FY_ORDER = [];
        const MS = {{}};
        const CAL = {{mode: 'day', view: 'days', year: 2026, month: 0, rangeStart: null}};
        let updateTimer = null;

        function matchRow(r, skip) {{
            skip = skip || {{}};
            if (!skip.fy && FILT.fys.size && !FILT.fys.has(FY_OF_MONTH[r.Month])) return false;
            if (!skip.month && FILT.months.size && !FILT.months.has(r.Month)) return false;
            if (!skip.week && FILT.weeks.size && !FILT.weeks.has(r.Week_Start)) return false;
            if (!skip.day && FILT.dayFrom !== null && (r.Date < FILT.dayFrom || r.Date > FILT.dayTo)) return false;
            if (!skip.type && FILT.types.size && !FILT.types.has(r.Sales_Doc_Type)) return false;
            if (!skip.status && FILT.statuses.size && !FILT.statuses.has(r.Order_Status)) return false;
            if (!skip.branch && FILT.branches.size && !FILT.branches.has(r.Branch)) return false;
            if (!skip.search && FILT.search && !String(r.Product_Desc || "").toLowerCase().includes(FILT.search)) return false;
            return true;
        }}
        function syncSearch() {{ FILT.search = document.getElementById('filterSearch').value.toLowerCase(); }}
        function escHtml(s) {{ return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }}
        function sortedMonths(set) {{ return MONTH_ORDER.filter(m => set.has(m)); }}
        function weekLabel(ws) {{ const base = WEEK_INFO[ws] || ws; return MULTI_YEAR ? base + ', ' + ws.slice(0, 4) : base; }}
        function fmtDay(d, withYear) {{ const p = d.split('-'); return MON3[parseInt(p[1], 10) - 1] + ' ' + parseInt(p[2], 10) + (withYear ? ', ' + p[0] : ''); }}
        function pad2(n) {{ return (n < 10 ? '0' : '') + n; }}
        function ymd(y, m, d) {{ return y + '-' + pad2(m + 1) + '-' + pad2(d); }}
        function utcDate(y, m, d) {{ return new Date(Date.UTC(y, m, d)); }}
        function weekStartOf(ds) {{
            const p = ds.split('-').map(Number);
            const dt = utcDate(p[0], p[1] - 1, p[2]);
            dt.setUTCDate(dt.getUTCDate() - ((dt.getUTCDay() + 6) % 7));
            return ymd(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate());
        }}
        function hasDataDay(ds) {{ return DATE_MONTH[ds] !== undefined; }}

        // text used in the windows that open from a click (branch drill-down, large orders)
        function describeFilters(withBranch) {{
            const parts = [];
            if (FILT.fys.size) parts.push('Fiscal year: ' + FY_ORDER.filter(f => FILT.fys.has(f)).join(', '));
            parts.push('Month: ' + (FILT.months.size ? sortedMonths(FILT.months).join(', ') : 'All months'));
            if (FILT.weeks.size) parts.push('Week: ' + [...FILT.weeks].sort().map(weekLabel).join('; '));
            if (FILT.dayFrom !== null) parts.push('Day: ' + (FILT.dayFrom === FILT.dayTo ? fmtDay(FILT.dayFrom, true) : fmtDay(FILT.dayFrom, true) + ' - ' + fmtDay(FILT.dayTo, true)));
            parts.push('Doc type: ' + (FILT.types.size ? [...FILT.types].sort().join(', ') : 'All types'));
            if (FILT.statuses.size) parts.push('Status: ' + [...FILT.statuses].sort().join(', '));
            if (withBranch) parts.push('Branch: ' + (FILT.branches.size ? [...FILT.branches].sort().join(', ') : 'All branches'));
            if (FILT.search) parts.push('Product search: "' + FILT.search + '"');
            return 'Filters applied \u2014 ' + parts.join('  |  ');
        }}

        // ---------- checkbox dropdowns ----------
        function monthOptionsList() {{ return FILT.fys.size ? MONTH_ORDER.filter(m => FILT.fys.has(FY_OF_MONTH[m])) : MONTH_ORDER; }}
        function weekOptions() {{
            let weeks;
            if (FILT.months.size || FILT.fys.size) {{ const s = new Set(); (FILT.months.size ? [...FILT.months] : monthOptionsList()).forEach(m => (WEEKS_BY_MONTH[m] || []).forEach(w => s.add(w))); weeks = [...s]; }}
            else weeks = Object.keys(WEEK_INFO);
            return weeks.sort().map(w => ({{value: w, label: weekLabel(w)}}));
        }}
        function buildMSel(key, boxId, allLabel, getOptions, getSet, searchable) {{
            const box = document.getElementById(boxId);
            const m = {{key: key, box: box, btn: box.querySelector('.msel-btn'), panel: box.querySelector('.msel-panel'), allLabel: allLabel, getOptions: getOptions, getSet: getSet, searchable: searchable, opts: []}};
            MS[key] = m;
            m.btn.addEventListener('click', function(e) {{ e.stopPropagation(); togglePanel(key); }});
            m.panel.addEventListener('click', function(e) {{ e.stopPropagation(); }});
            m.panel.addEventListener('change', function(e) {{ if (e.target.type === 'checkbox') onMSelChange(key, e.target); }});
            m.panel.addEventListener('input', function(e) {{
                if (!e.target.classList.contains('msel-search')) return;
                const q = e.target.value.toLowerCase();
                m.panel.querySelectorAll('.msel-list .msel-row').forEach(row => {{ row.style.display = (row.getAttribute('data-label') || '').indexOf(q) >= 0 ? '' : 'none'; }});
            }});
        }}
        function renderPanel(key) {{
            const m = MS[key];
            m.opts = m.getOptions();
            let h = m.searchable ? '<input class="msel-search" type="text" placeholder="Search...">' : '';
            h += '<label class="msel-row msel-all"><input type="checkbox" data-all="1"><span>' + escHtml(m.allLabel) + '</span></label><div class="msel-list">';
            m.opts.forEach(o => {{ h += '<label class="msel-row" data-label="' + escHtml(String(o.label).toLowerCase()) + '"><input type="checkbox" value="' + escHtml(o.value) + '"><span>' + escHtml(o.label) + '</span></label>'; }});
            if (!m.opts.length) h += '<div class="msel-empty">No options</div>';
            m.panel.innerHTML = h + '</div>';
            m.sig = m.opts.map(o => o.value).join(',');
            refreshMSel(key);
        }}
        // the Week list must always match the chosen months (also at start-up and after any change made by the calendar or Reset)
        function syncWeekOptions() {{
            const mm = MS.month;
            if (mm && monthOptionsList().join(',') !== mm.sig) renderPanel('month');
            const m = MS.week;
            if (m && weekOptions().map(o => o.value).join(',') !== m.sig) renderPanel('week');
        }}
        function refreshMSel(key) {{
            const m = MS[key], set = m.getSet();
            m.panel.querySelectorAll('input[type=checkbox]').forEach(cb => {{ cb.checked = cb.hasAttribute('data-all') ? set.size === 0 : set.has(cb.value); }});
            let txt;
            if (set.size === 0) txt = m.allLabel;
            else if (set.size === 1) {{ const v = [...set][0], o = m.opts.find(x => x.value === v); txt = o ? o.label : v; }}
            else txt = set.size + ' selected';
            m.btn.textContent = txt;
            m.btn.title = txt;
        }}
        function closePanels(except) {{
            Object.keys(MS).forEach(k => {{ if (k !== except) MS[k].box.classList.remove('open'); }});
            if (except !== 'cal') document.getElementById('calBox').classList.remove('open');
        }}
        function togglePanel(key) {{
            const m = MS[key], was = m.box.classList.contains('open');
            closePanels(was ? null : key);
            m.box.classList.toggle('open', !was);
            if (!was && m.searchable) {{ const s = m.panel.querySelector('.msel-search'); if (s) {{ s.value = ''; s.dispatchEvent(new Event('input', {{bubbles: true}})); s.focus(); }} }}
        }}
        function onMSelChange(key, cb) {{
            const set = MS[key].getSet();
            if (cb.hasAttribute('data-all')) set.clear(); else if (cb.checked) set.add(cb.value); else set.delete(cb.value);
            if (key === 'month') {{                         // weeks and days must still belong to the chosen months
                FILT.dayFrom = FILT.dayTo = null; CAL.rangeStart = null;
                if (FILT.months.size) FILT.weeks.forEach(w => {{ if (![...(WEEK_MONTHS[w] || [])].some(x => FILT.months.has(x))) FILT.weeks.delete(w); }});
                renderPanel('week');
            }} else if (key === 'week') {{ FILT.dayFrom = FILT.dayTo = null; CAL.rangeStart = null; }}
            else if (key === 'fy') {{                       // months, weeks and days must still belong to the chosen fiscal years
                FILT.dayFrom = FILT.dayTo = null; CAL.rangeStart = null;
                if (FILT.fys.size) {{
                    [...FILT.months].forEach(x => {{ if (!FILT.fys.has(FY_OF_MONTH[x])) FILT.months.delete(x); }});
                    [...FILT.weeks].forEach(w => {{ if (![...(WEEK_MONTHS[w] || [])].some(x => FILT.fys.has(FY_OF_MONTH[x]))) FILT.weeks.delete(w); }});
                }}
            }}
            refreshAll();
            scheduleUpdate();
        }}
        function scheduleUpdate() {{ clearTimeout(updateTimer); updateTimer = setTimeout(updateDashboard, 60); }}
        function refreshAll() {{ syncWeekOptions(); Object.keys(MS).forEach(refreshMSel); document.getElementById('calBtn').textContent = calText(); document.getElementById('calBtn').title = calText(); if (document.getElementById('calBox').classList.contains('open')) renderCal(); }}

        // ---------- calendar ----------
        function calText() {{
            const sameYear = (a, b) => a.slice(0, 4) === b.slice(0, 4);
            if (FILT.dayFrom !== null) return FILT.dayFrom === FILT.dayTo ? fmtDay(FILT.dayFrom, true) : fmtDay(FILT.dayFrom, !sameYear(FILT.dayFrom, FILT.dayTo)) + ' - ' + fmtDay(FILT.dayTo, true);
            if (FILT.weeks.size === 1) return 'Week ' + weekLabel([...FILT.weeks][0]);
            if (FILT.weeks.size > 1) return FILT.weeks.size + ' weeks';
            if (FILT.months.size === 1) return [...FILT.months][0];
            if (FILT.months.size > 1) return FILT.months.size + ' months';
            if (FILT.fys.size === 1) return [...FILT.fys][0];
            if (FILT.fys.size > 1) return FILT.fys.size + ' fiscal years';
            return 'All dates';
        }}
        function calGoToSelection() {{
            let label = FILT.months.size ? sortedMonths(FILT.months).pop() : MONTH_ORDER[MONTH_ORDER.length - 1];
            if (FILT.dayFrom !== null) label = DATE_MONTH[FILT.dayFrom] || label;
            const p = (label || '').split(' ');
            const mi = MON3.indexOf(p[0]);
            if (mi >= 0) {{ CAL.month = mi; CAL.year = parseInt(p[1], 10); }}
        }}
        function renderCal() {{
            const panel = document.getElementById('calPanel');
            let h = '<div class="cal-tabs">' + ['day', 'week', 'month'].map(t => '<div class="cal-tab' + (CAL.mode === t ? ' active' : '') + '" data-mode="' + t + '">' + t.charAt(0).toUpperCase() + t.slice(1) + '</div>').join('') + '</div>';
            if (CAL.view === 'months') {{
                h += '<div class="cal-head"><div class="cal-nav" data-nav="-12">&lsaquo;</div><div class="cal-title" data-title="1">' + CAL.year + '</div><div class="cal-nav" data-nav="12">&rsaquo;</div></div><div class="cal-months">';
                for (let m = 0; m < 12; m++) {{
                    const lab = MON3[m] + ' ' + CAL.year, ok = MONTH_ORDER.indexOf(lab) >= 0;
                    h += '<div class="cal-m' + (ok ? '' : ' nodata') + (ok && FILT.months.has(lab) && FILT.weeks.size === 0 && FILT.dayFrom === null ? ' sel' : '') + '" data-m="' + m + '">' + MON3[m] + '</div>';
                }}
                h += '</div>';
            }} else {{
                h += '<div class="cal-head"><div class="cal-nav" data-nav="-1">&lsaquo;</div><div class="cal-title" data-title="1">' + MON3[CAL.month] + ' ' + CAL.year + ' &#9662;</div><div class="cal-nav" data-nav="1">&rsaquo;</div></div>';
                h += '<div class="cal-grid' + (CAL.mode === 'week' ? ' weekmode' : '') + '"><div class="cal-row">' + ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].map(x => '<div class="cal-dow">' + x + '</div>').join('') + '</div>';
                const first = utcDate(CAL.year, CAL.month, 1), lead = (first.getUTCDay() + 6) % 7, start = new Date(first);
                start.setUTCDate(1 - lead);
                for (let w = 0; w < 6; w++) {{
                    const rs = new Date(start); rs.setUTCDate(start.getUTCDate() + w * 7);
                    const wsKey = ymd(rs.getUTCFullYear(), rs.getUTCMonth(), rs.getUTCDate());
                    let cells = '';
                    for (let d = 0; d < 7; d++) {{
                        const dt = new Date(rs); dt.setUTCDate(rs.getUTCDate() + d);
                        const ds = ymd(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate());
                        const sel = FILT.dayFrom !== null && ds >= FILT.dayFrom && ds <= FILT.dayTo;
                        cells += '<div class="cal-day' + (dt.getUTCMonth() !== CAL.month ? ' out' : '') + (hasDataDay(ds) ? ' hasdata' : ' nodata') + (sel ? ' sel' : '') + '" data-d="' + ds + '">' + dt.getUTCDate() + '</div>';
                    }}
                    h += '<div class="cal-row' + (WEEK_INFO[wsKey] !== undefined ? ' hasdata' : '') + (FILT.weeks.has(wsKey) ? ' wsel' : '') + '">' + cells + '</div>';
                }}
                h += '</div>';
                if (CAL.mode === 'day') h += '<div class="cal-hint">' + (CAL.rangeStart ? 'Now click a second day to make a range.' : 'Click a day. Click a second day to make a range.') + '</div>';
                if (CAL.mode === 'week') h += '<div class="cal-hint">Click any day to pick its whole week (Mon-Sun).</div>';
            }}
            h += '<div class="cal-foot"><span class="cal-btn" data-act="clear">Clear days / weeks</span><span class="cal-btn" data-act="latest">Latest month</span></div>';
            panel.innerHTML = h;
        }}
        function setDayRange(a, b) {{
            FILT.dayFrom = a; FILT.dayTo = b; FILT.weeks = new Set(); FILT.fys = new Set();
            FILT.months = new Set(Object.keys(DATE_MONTH).filter(d => d >= a && d <= b).map(d => DATE_MONTH[d]));
        }}
        function afterCalChange() {{ renderPanel('week'); refreshAll(); scheduleUpdate(); }}
        function onCalClick(e) {{
            e.stopPropagation();
            const t = e.target.closest('[data-mode],[data-nav],[data-title],[data-m],[data-d],[data-act]');
            if (!t) return;
            if (t.dataset.mode) {{ CAL.mode = t.dataset.mode; CAL.rangeStart = null; CAL.view = CAL.mode === 'month' ? 'months' : 'days'; calGoToSelection(); renderCal(); return; }}
            if (t.dataset.nav) {{
                const n = parseInt(t.dataset.nav, 10);
                if (CAL.view === 'months') CAL.year += n / 12;
                else {{ CAL.month += n; if (CAL.month < 0) {{ CAL.month = 11; CAL.year--; }} if (CAL.month > 11) {{ CAL.month = 0; CAL.year++; }} }}
                renderCal(); return;
            }}
            if (t.dataset.title) {{ CAL.view = CAL.view === 'days' ? 'months' : 'days'; renderCal(); return; }}
            if (t.dataset.m !== undefined) {{
                const m = parseInt(t.dataset.m, 10), lab = MON3[m] + ' ' + CAL.year;
                if (MONTH_ORDER.indexOf(lab) < 0) return;
                if (CAL.mode === 'month') {{
                    if (e.ctrlKey || e.metaKey || e.shiftKey) {{ if (FILT.months.has(lab)) FILT.months.delete(lab); else FILT.months.add(lab); }}
                    else FILT.months = new Set([lab]);
                    FILT.weeks = new Set(); FILT.dayFrom = FILT.dayTo = null; FILT.fys = new Set();
                    afterCalChange();
                }} else {{ CAL.view = 'days'; CAL.month = m; renderCal(); }}
                return;
            }}
            if (t.dataset.d) {{
                const d = t.dataset.d;
                if (CAL.mode === 'week') {{
                    const ws = weekStartOf(d);
                    if (WEEK_INFO[ws] === undefined) return;
                    FILT.weeks = new Set([ws]); FILT.months = new Set(WEEK_MONTHS[ws]); FILT.dayFrom = FILT.dayTo = null; FILT.fys = new Set();
                }} else {{
                    if (!hasDataDay(d)) return;
                    if (CAL.rangeStart === null) {{ CAL.rangeStart = d; setDayRange(d, d); }}
                    else {{ const a = CAL.rangeStart <= d ? CAL.rangeStart : d, b = CAL.rangeStart <= d ? d : CAL.rangeStart; CAL.rangeStart = null; setDayRange(a, b); }}
                }}
                afterCalChange(); return;
            }}
            if (t.dataset.act === 'clear') {{ FILT.weeks = new Set(); FILT.dayFrom = FILT.dayTo = null; CAL.rangeStart = null; afterCalChange(); }}
            if (t.dataset.act === 'latest') {{ FILT.months = defaultMonth !== 'All' ? new Set([defaultMonth]) : new Set(); FILT.fys = new Set(); FILT.weeks = new Set(); FILT.dayFrom = FILT.dayTo = null; CAL.rangeStart = null; calGoToSelection(); afterCalChange(); }}
        }}

        function initFilters() {{
            const monthMin = {{}};
            rawData.forEach(r => {{
                if (!(r.Month in monthMin) || r.Date < monthMin[r.Month]) monthMin[r.Month] = r.Date;
                if (!WEEK_INFO[r.Week_Start]) WEEK_INFO[r.Week_Start] = r.Week_Range;
                (WEEKS_BY_MONTH[r.Month] = WEEKS_BY_MONTH[r.Month] || new Set()).add(r.Week_Start);
                (WEEK_MONTHS[r.Week_Start] = WEEK_MONTHS[r.Week_Start] || new Set()).add(r.Month);
                DATE_MONTH[r.Date] = r.Month;
            }});
            MONTH_ORDER = Object.keys(monthMin).sort((a, b) => monthMin[a] < monthMin[b] ? -1 : 1);     // chronological, not alphabetical
            // Rwanda Government fiscal year: 1 July - 30 June (FY 2025/26 = Jul 2025 - Jun 2026)
            MONTH_ORDER.forEach(mo => {{ const p = mo.split(' '), mi = MON3.indexOf(p[0]), yr = parseInt(p[1], 10), s = mi >= 6 ? yr : yr - 1, fy = 'FY ' + s + '/' + String((s + 1) % 100).padStart(2, '0'); FY_OF_MONTH[mo] = fy; if (FY_ORDER.indexOf(fy) < 0) FY_ORDER.push(fy); }});
            MULTI_YEAR = new Set(Object.keys(WEEK_INFO).map(w => w.slice(0, 4))).size > 1;
            const alpha = (a, b) => String(a).localeCompare(String(b));
            const types = [...new Set(rawData.map(d => d.Sales_Doc_Type))].sort(alpha);
            const branches = [...new Set(rawData.map(d => d.Branch))].sort(alpha);
            buildMSel('fy', 'mselFY', 'All Years', () => FY_ORDER.map(f => ({{value: f, label: f}})), () => FILT.fys, false);
            buildMSel('month', 'mselMonth', 'All Months', () => monthOptionsList().map(m => ({{value: m, label: m}})), () => FILT.months, false);
            buildMSel('week', 'mselWeek', 'All Weeks', weekOptions, () => FILT.weeks, false);
            buildMSel('type', 'mselType', 'All Types', () => types.map(t => ({{value: t, label: t}})), () => FILT.types, types.length > 8);
            const stOrder = ['Open', 'Partial', 'Complete'];
            const statuses = [...new Set(rawData.map(d => d.Order_Status))].filter(s => s && s !== 'Unknown').sort((x, y) => {{ const ix = stOrder.indexOf(x), iy = stOrder.indexOf(y); return ((ix < 0 ? 9 : ix) - (iy < 0 ? 9 : iy)) || x.localeCompare(y); }});
            buildMSel('status', 'mselStatus', 'All Statuses', () => statuses.map(s => ({{value: s, label: s}})), () => FILT.statuses, false);
            document.getElementById('mselStatus').closest('.filter-group').style.display = statuses.length ? '' : 'none';      // shown only when the data has an order status
            buildMSel('branch', 'mselBranch', 'All Branches', () => branches.map(b => ({{value: b, label: b}})), () => FILT.branches, branches.length > 8);
            Object.keys(MS).forEach(renderPanel);
            const calBox = document.getElementById('calBox');
            document.getElementById('calBtn').addEventListener('click', function(e) {{
                e.stopPropagation();
                const was = calBox.classList.contains('open');
                closePanels(was ? null : 'cal');
                calBox.classList.toggle('open', !was);
                if (!was) {{ CAL.rangeStart = null; CAL.view = CAL.mode === 'month' ? 'months' : 'days'; calGoToSelection(); renderCal(); }}
            }});
            document.getElementById('calPanel').addEventListener('click', onCalClick);
            document.addEventListener('click', function() {{ closePanels(null); }});
            document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closePanels(null); }});
            refreshAll();
        }}

        // set every filter at once (used by Reset, by the calendar and by tests)
        function setFilters(st) {{
            st = st || {{}};
            FILT.fys = new Set(st.fys || []); FILT.months = new Set(st.months || []); FILT.weeks = new Set(st.weeks || []);
            FILT.types = new Set(st.types || []); FILT.statuses = new Set(st.statuses || []); FILT.branches = new Set(st.branches || []);
            FILT.dayFrom = st.dayFrom || null; FILT.dayTo = st.dayTo || st.dayFrom || null;
            document.getElementById('filterSearch').value = st.search || '';
            CAL.rangeStart = null;
            if (MS.week) {{ renderPanel('week'); refreshAll(); }}
            updateDashboard();
        }}
        function getFilters() {{
            return {{fys: FY_ORDER.filter(f => FILT.fys.has(f)), months: sortedMonths(FILT.months), weeks: [...FILT.weeks].sort(), types: [...FILT.types].sort(), statuses: [...FILT.statuses].sort(), branches: [...FILT.branches].sort(), dayFrom: FILT.dayFrom, dayTo: FILT.dayTo, search: FILT.search}};
        }}
        function resetFilters() {{ setFilters({{months: defaultMonth !== 'All' ? [defaultMonth] : []}}); }}

        // ===================== ADDED: "Ask the data" - ready-made questions grouped by topic (no AI service, no cost) =====================
        // Every answer is computed from the same rows as the cards and charts, so the numbers always agree with the dashboard.
        const ASK = {{group: null, current: null, query: ''}};
        const ASK_MIN_LINES = 30;       // branches / types with fewer order lines than this are left out of "rate" rankings (too small to be fair)

        function askRows(skip) {{
            skip = skip || {{}};
            return rawData.filter(r => matchRow(r, {{month: !!skip.time, week: !!skip.time, day: !!skip.time, fy: !!skip.time, type: !!skip.type, branch: !!skip.branch}}));
        }}
        function askAgg(rows, keyFn) {{
            const m = {{}};
            rows.forEach(r => {{
                const k = keyFn(r);
                let a = m[k];
                if (!a) a = m[k] = {{key: k, ordQty: 0, missQty: 0, ordVal: 0, missVal: 0, lines: 0, short: 0, days: new Set()}};
                a.ordQty += r.Order_Qty || 0; a.missQty += r.Missed_Qty || 0; a.ordVal += r.Ordered_Value || 0; a.missVal += r.Lost_Value || 0;
                a.lines += r.Total_Lines || 1; a.short += r.Missed_Lines || 0; a.days.add(r.Date);
            }});
            return Object.keys(m).map(k => m[k]);
        }}
        function askTot(rows) {{
            const t = {{ordQty: 0, missQty: 0, ordVal: 0, missVal: 0, lines: 0, short: 0, days: new Set()}};
            rows.forEach(r => {{ t.ordQty += r.Order_Qty || 0; t.missQty += r.Missed_Qty || 0; t.ordVal += r.Ordered_Value || 0; t.missVal += r.Lost_Value || 0; t.lines += r.Total_Lines || 1; t.short += r.Missed_Lines || 0; t.days.add(r.Date); }});
            return t;
        }}
        const aPct = (x) => (x === null || x === undefined || !isFinite(x)) ? '\u2014' : x.toFixed(1) + '%';
        const aRateV = (a) => (ordValReady && a.ordVal > 0) ? a.missVal / a.ordVal * 100 : null;
        const aRateQ = (a) => a.ordQty > 0 ? a.missQty / a.ordQty * 100 : null;
        const aShare = (part, whole) => whole > 0 ? part / whole * 100 : null;
        const aRwf = (n) => 'RWF ' + formatNum(n);
        const aInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
        const aB = (s) => '<b>' + escHtml(s) + '</b>';
        const aReal = (r) => !!r.Product_Desc && r.Product_Desc !== 0 && r.Product_Desc !== '0';     // order headers with no item have no product
        function aTop(arr, valFn, n, desc, nameFn) {{
            const nm = nameFn || (a => a.key);
            const out = arr.slice().sort((a, b) => {{ const d = valFn(a) - valFn(b); return d !== 0 ? (desc ? -d : d) : String(nm(a)).localeCompare(String(nm(b))); }});
            return out.slice(0, n);
        }}
        function askScope(skip) {{
            skip = skip || {{}};
            const p = [];
            if (skip.time) p.push('Months: all months compared (month, week and day filters not applied)');
            else {{
                if (FILT.fys.size) p.push('Fiscal year: ' + FY_ORDER.filter(f => FILT.fys.has(f)).join(', '));
                p.push('Month: ' + (FILT.months.size ? sortedMonths(FILT.months).join(', ') : 'All months'));
                if (FILT.weeks.size) p.push('Week: ' + [...FILT.weeks].sort().map(weekLabel).join('; '));
                if (FILT.dayFrom !== null) p.push('Day: ' + (FILT.dayFrom === FILT.dayTo ? fmtDay(FILT.dayFrom, true) : fmtDay(FILT.dayFrom, true) + ' - ' + fmtDay(FILT.dayTo, true)));
            }}
            p.push(skip.type ? 'Doc types: all shown' : 'Doc type: ' + (FILT.types.size ? [...FILT.types].sort().join(', ') : 'All types'));
            if (FILT.statuses.size) p.push('Status: ' + [...FILT.statuses].sort().join(', '));
            p.push(skip.branch ? 'Branches: all shown' : 'Branch: ' + (FILT.branches.size ? [...FILT.branches].sort().join(', ') : 'All branches'));
            if (FILT.search) p.push('Product search: "' + FILT.search + '"');
            return 'Based on \u2014 ' + p.join('  |  ');
        }}
        const askEmpty = (skip) => ({{headline: 'There is no data for the current filters. Change the filters above and try again.', cols: [], rows: [], scope: askScope(skip)}});
        function askNoValueNote() {{ return ordValReady ? '' : 'Order value is not in this database yet, so value rates are shown as a dash. Run an API sync (or re-upload the Excel) to fill it.'; }}

        // ---- the questions ----
        const ASK_GROUPS = [
            {{id: 'overview', title: 'Overview', questions: [
                {{id: 'o1', text: 'What is the overall picture?', kw: 'summary total headline status overall how are we doing missed rate fulfilment', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const t = askTot(rows);
                    const lv = ordValReady ? ', ' + aB(aPct(aRateV(t))) + ' of the order value' : '';
                    return {{headline: 'Customers ordered ' + aB(aRwf(t.ordVal)) + ' and ' + aB(aRwf(t.missVal)) + ' could not be supplied' + lv + '. In units, ' + aB(formatNum(t.missQty)) + ' of ' + aB(formatNum(t.ordQty)) + ' (' + aPct(aRateQ(t)) + ') were missed.',
                        cols: ['Measure', 'Value', 'Units'], barCol: -1,
                        rows: [['Ordered', ordValReady ? aRwf(t.ordVal) : '\u2014', formatNum(t.ordQty)], ['Made (confirmed)', ordValReady ? aRwf(t.ordVal - t.missVal) : '\u2014', formatNum(t.ordQty - t.missQty)], ['Missed', aRwf(t.missVal), formatNum(t.missQty)],
                               ['Missed rate', aPct(aRateV(t)), aPct(aRateQ(t))], ['Fulfilment rate', ordValReady ? aPct(100 - aRateV(t)) : '\u2014', aPct(100 - aRateQ(t))], ['Order items short', aInt(t.short) + ' of ' + aInt(t.lines), aPct(aShare(t.short, t.lines))]],
                        note: askNoValueNote(), scope: askScope()}}; }}}},
                {{id: 'o2', text: 'What data am I looking at?', kw: 'data period coverage lines dates how many records', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const t = askTot(rows); const dates = [...t.days].sort();
                    const b = new Set(rows.map(r => r.Branch)).size, p = new Set(rows.filter(aReal).map(r => r.Product_Desc)).size, ty = new Set(rows.map(r => r.Sales_Doc_Type)).size;
                    return {{headline: 'This selection covers ' + aB(aInt(t.lines)) + ' order lines from ' + aB(fmtDay(dates[0], true)) + ' to ' + aB(fmtDay(dates[dates.length - 1], true)) + '.', cols: ['Item', 'Count'], barCol: -1,
                        rows: [['Order lines', aInt(t.lines)], ['Days with orders', aInt(dates.length)], ['Branches', aInt(b)], ['Products', aInt(p)], ['Document types', aInt(ty)]], scope: askScope()}}; }}}},
                {{id: 'o3', text: 'How much of the loss comes from a few large orders?', kw: 'large orders concentration biggest few share', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const t = askTot(rows);
                    const lg = largeData.filter(r => matchRow(r)).sort((a, b) => b.Lost_Value - a.Lost_Value); const lv = lg.reduce((s, r) => s + (r.Lost_Value || 0), 0); const thr = 'RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M';
                    if (!lg.length) return {{headline: 'No single order item reaches ' + thr + ' of missed value, so the loss is spread over many smaller items.', cols: [], rows: [], scope: askScope()}};
                    return {{headline: aB(aInt(lg.length) + ' single items') + ' of ' + thr + '+ each add up to ' + aB(aRwf(lv)) + ', which is ' + aB(aPct(aShare(lv, t.missVal))) + ' of everything missed.', cols: ['Measure', 'Value'], barCol: -1,
                        rows: [['Large items', aInt(lg.length)], ['Missed value in them', aRwf(lv)], ['Share of all missed value', aPct(aShare(lv, t.missVal))], ['Average per large item', aRwf(lv / lg.length)], ['Largest single item', aRwf(lg[0].Lost_Value) + ' (' + lg[0].Product_Desc + ', ' + lg[0].Branch + ')']],
                        note: 'Worth verifying with the branch before treating them as real lost sales (click the Large Orders Share card for the list).', scope: askScope()}}; }}}}
            ]}},
            {{id: 'branches', title: 'Branches', questions: [
                {{id: 'b1', text: 'Which branches lost the most value?', kw: 'branch branches most lost value worst biggest loss rwf', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const t = askTot(rows); const ag = askAgg(rows, r => r.Branch); const top = aTop(ag, a => a.missVal, 10, true);
                    return {{headline: aB(top[0].key) + ' lost the most: ' + aB(aRwf(top[0].missVal)) + ' (' + aPct(aShare(top[0].missVal, t.missVal)) + ' of all missed value).', cols: ['Branch', 'Missed value', 'Share of total', 'Missed rate (value)'], barCol: 1, bars: top.map(a => top[0].missVal > 0 ? a.missVal / top[0].missVal : 0),
                        rows: top.map(a => [a.key, aRwf(a.missVal), aPct(aShare(a.missVal, t.missVal)), aPct(aRateV(a))]), note: askNoValueNote(), scope: askScope()}}; }}}},
                {{id: 'b2', text: 'Which branches miss the most units?', kw: 'branch branches most units quantity missed volume', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const t = askTot(rows); const ag = askAgg(rows, r => r.Branch); const top = aTop(ag, a => a.missQty, 10, true);
                    return {{headline: aB(top[0].key) + ' missed the most units: ' + aB(formatNum(top[0].missQty)) + ' (' + aPct(aShare(top[0].missQty, t.missQty)) + ' of all missed units).', cols: ['Branch', 'Missed units', 'Share of total', 'Missed rate (units)'], barCol: 1, bars: top.map(a => top[0].missQty > 0 ? a.missQty / top[0].missQty : 0),
                        rows: top.map(a => [a.key, formatNum(a.missQty), aPct(aShare(a.missQty, t.missQty)), aPct(aRateQ(a))]), scope: askScope()}}; }}}},
                {{id: 'b3', text: 'Which branches have the highest missed rate?', kw: 'branch branches highest missed rate worst failure percentage', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const ag = askAgg(rows, r => r.Branch).filter(a => a.lines >= ASK_MIN_LINES); if (!ag.length) return {{headline: 'No branch has enough order lines (at least ' + ASK_MIN_LINES + ') in this selection to rank fairly.', cols: [], rows: [], scope: askScope()}};
                    const rf = (a) => {{ const v = aRateV(a); return v === null ? (aRateQ(a) || 0) : v; }}; const top = aTop(ag, rf, 10, true);
                    return {{headline: aB(top[0].key) + ' fails the most often: ' + aB(aPct(rf(top[0]))) + ' of what it is asked for ' + (aRateV(top[0]) === null ? '(by units)' : '(by value)') + '.', cols: ['Branch', 'Missed rate (value)', 'Missed rate (units)', 'Order lines'], barCol: 1, bars: top.map(a => rf(top[0]) > 0 ? rf(a) / rf(top[0]) : 0),
                        rows: top.map(a => [a.key, aPct(aRateV(a)), aPct(aRateQ(a)), aInt(a.lines)]), note: 'Only branches with at least ' + ASK_MIN_LINES + ' order lines are ranked. ' + askNoValueNote(), scope: askScope()}}; }}}},
                {{id: 'b4', text: 'Which branches serve their customers best?', kw: 'branch branches best lowest missed rate good performing', run: function() {{
                    const rows = askRows(); if (!rows.length) return askEmpty(); const ag = askAgg(rows, r => r.Branch).filter(a => a.lines >= ASK_MIN_LINES); if (!ag.length) return {{headline: 'No branch has enough order lines (at least ' + ASK_MIN_LINES + ') in this selection to rank fairly.', cols: [], rows: [], scope: askScope()}};
                    const rf = (a) => {{ const v = aRateV(a); return v === null ? (aRateQ(a) || 0) : v; }}; const top = aTop(ag, rf, 10, false);
                    return {{headline: aB(top[0].key) + ' does best: only ' + aB(aPct(rf(top[0]))) + ' of what it is asked for is missed ' + (aRateV(top[0]) === null ? '(by units)' : '(by value)') + '.', cols: ['Branch', 'Missed rate (value)', 'Missed rate (units)', 'Order lines'], barCol: -1,
                        rows: top.map(a => [a.key, aPct(aRateV(a)), aPct(aRateQ(a)), aInt(a.lines)]), note: 'Only branches with at least ' + ASK_MIN_LINES + ' order lines are ranked. ' + askNoValueNote(), scope: askScope()}}; }}}}
            ]}},
            {{id: 'products', title: 'Products', questions: [
                {{id: 'p1', text: 'Which products lost the most value?', kw: 'product products most lost value worst biggest loss rwf', run: function() {{
                    const rows = askRows(); const pr = rows.filter(aReal); if (!pr.length) return askEmpty(); const t = askTot(rows); const top = aTop(askAgg(pr, r => r.Product_Desc), a => a.missVal, 10, true);
                    return {{headline: aB(top[0].key) + ' lost the most: ' + aB(aRwf(top[0].missVal)) + ' (' + aPct(aShare(top[0].missVal, t.missVal)) + ' of all missed value).', cols: ['Product', 'Missed value', 'Share of total', 'Missed units'], barCol: 1, bars: top.map(a => top[0].missVal > 0 ? a.missVal / top[0].missVal : 0),
                        rows: top.map(a => [a.key, aRwf(a.missVal), aPct(aShare(a.missVal, t.missVal)), formatNum(a.missQty)]), scope: askScope()}}; }}}},
                {{id: 'p2', text: 'Which products are missed most often?', kw: 'product products most often frequent lines repeat stock-out stockout', run: function() {{
                    const rows = askRows(); const pr = rows.filter(aReal); if (!pr.length) return askEmpty(); const top = aTop(askAgg(pr, r => r.Product_Desc), a => a.short, 10, true);
                    if (top[0].short === 0) return {{headline: 'No product line was short in this selection.', cols: [], rows: [], scope: askScope()}};
                    return {{headline: aB(top[0].key) + ' was short on ' + aB(aInt(top[0].short) + ' order lines') + ', more than any other product.', cols: ['Product', 'Lines short', 'Missed units', 'Missed value'], barCol: 1, bars: top.map(a => a.short / top[0].short),
                        rows: top.map(a => [a.key, aInt(a.short), formatNum(a.missQty), aRwf(a.missVal)]), note: 'Frequent small shortages matter to customers even when the value is low.', scope: askScope()}}; }}}},
                {{id: 'p3', text: 'Which products have the most missed units?', kw: 'product products most units quantity missed volume', run: function() {{
                    const rows = askRows(); const pr = rows.filter(aReal); if (!pr.length) return askEmpty(); const t = askTot(rows); const top = aTop(askAgg(pr, r => r.Product_Desc), a => a.missQty, 10, true);
                    return {{headline: aB(top[0].key) + ' has the most missed units: ' + aB(formatNum(top[0].missQty)) + ' (' + aPct(aShare(top[0].missQty, t.missQty)) + ' of all missed units).', cols: ['Product', 'Missed units', 'Share of total', 'Missed value'], barCol: 1, bars: top.map(a => top[0].missQty > 0 ? a.missQty / top[0].missQty : 0),
                        rows: top.map(a => [a.key, formatNum(a.missQty), aPct(aShare(a.missQty, t.missQty)), aRwf(a.missVal)]), scope: askScope()}}; }}}},
                {{id: 'p4', text: 'Which products are missed but show no value (free-issue)?', kw: 'product products free issue zero price no value 0 programme donor free', run: function() {{
                    const rows = askRows(); const pr = rows.filter(aReal); if (!pr.length) return askEmpty(); const t = askTot(rows); const free = askAgg(pr, r => r.Product_Desc).filter(a => a.missQty > 0 && a.missVal === 0);
                    if (!free.length) return {{headline: 'Every missed product in this selection carries a price, so none shows RWF 0.', cols: [], rows: [], scope: askScope()}};
                    const top = aTop(free, a => a.missQty, 10, true); const fu = free.reduce((s, a) => s + a.missQty, 0);
                    return {{headline: aB(aInt(free.length) + ' products') + ' were missed but show RWF 0 because their unit price is 0 (typically programme or free-issue supplies). They make up ' + aB(aPct(aShare(fu, t.missQty))) + ' of the missed units.', cols: ['Product', 'Missed units', 'Lines short'], barCol: 1, bars: top.map(a => a.missQty / top[0].missQty),
                        rows: top.map(a => [a.key, formatNum(a.missQty), aInt(a.short)]), note: 'Their value is 0, so look at units for these products.', scope: askScope()}}; }}}}
            ]}},
            {{id: 'trends', title: 'Time & trends', questions: [
                {{id: 't1', text: 'How does the missed rate change month by month?', kw: 'month monthly trend time compare over time rate change', run: function() {{
                    const rows = askRows({{time: true}}); if (!rows.length) return askEmpty({{time: true}}); const ag = askAgg(rows, r => r.Month); const byM = {{}}; ag.forEach(a => {{ byM[a.key] = a; }});
                    const ms = MONTH_ORDER.filter(m => byM[m]); const worst = aTop(ag, a => (aRateV(a) === null ? (aRateQ(a) || 0) : aRateV(a)), 1, true)[0];
                    return {{headline: 'The highest missed rate was in ' + aB(worst.key) + ' (' + aB(aPct(aRateV(worst) === null ? aRateQ(worst) : aRateV(worst))) + ').', cols: ['Month', 'Ordered value', 'Missed value', 'Missed rate (value)', 'Missed rate (units)', 'Days with orders'], barCol: 3, bars: ms.map(m => {{ const a = byM[m], v = aRateV(a); return Math.min((v === null ? 0 : v) / 100, 1); }}),
                        rows: ms.map(m => {{ const a = byM[m]; return [m, ordValReady ? aRwf(a.ordVal) : '\u2014', aRwf(a.missVal), aPct(aRateV(a)), aPct(aRateQ(a)), aInt(a.days.size)]; }}), note: askNoValueNote(), scope: askScope({{time: true}})}}; }}}},
                {{id: 't2', text: 'Is it getting better or worse?', kw: 'better worse improving trend latest previous month compare change', run: function() {{
                    const rows = askRows({{time: true}}); const ag = askAgg(rows, r => r.Month); const byM = {{}}; ag.forEach(a => {{ byM[a.key] = a; }}); const ms = MONTH_ORDER.filter(m => byM[m]);
                    if (ms.length < 2) return {{headline: 'There is only one month of data for this selection, so there is nothing to compare yet.', cols: [], rows: [], scope: askScope({{time: true}})}};
                    const cur = byM[ms[ms.length - 1]], prev = byM[ms[ms.length - 2]]; const useV = ordValReady && cur.ordVal > 0 && prev.ordVal > 0;
                    const c = useV ? aRateV(cur) : aRateQ(cur), p = useV ? aRateV(prev) : aRateQ(prev); const d = c - p;
                    const word = Math.abs(d) < 0.05 ? 'unchanged' : (d < 0 ? 'better' : 'worse');
                    const partial = cur.days.size < prev.days.size * 0.8 ? 'The latest month has only ' + cur.days.size + ' days of data so far (' + prev.days.size + ' in the month before), so compare the rates, not the totals.' : '';
                    return {{headline: aB(ms[ms.length - 1]) + ' is ' + aB(word) + ' than ' + aB(ms[ms.length - 2]) + ': the missed rate ' + (useV ? '(value) ' : '(units) ') + 'is ' + aB(aPct(c)) + ' against ' + aPct(p) + (word === 'unchanged' ? '.' : ' (' + (d < 0 ? 'down ' : 'up ') + Math.abs(d).toFixed(1) + ' points).'), cols: ['Measure', ms[ms.length - 2], ms[ms.length - 1]], barCol: -1,
                        rows: [['Missed rate (value)', aPct(aRateV(prev)), aPct(aRateV(cur))], ['Missed rate (units)', aPct(aRateQ(prev)), aPct(aRateQ(cur))], ['Missed value', aRwf(prev.missVal), aRwf(cur.missVal)], ['Days with orders', aInt(prev.days.size), aInt(cur.days.size)]], note: (partial + ' ' + askNoValueNote()).trim(), scope: askScope({{time: true}})}}; }}}},
                {{id: 't3', text: 'Which weeks were the worst?', kw: 'week weeks worst highest weekly time', run: function() {{
                    const rows = askRows().length ? rawData.filter(r => matchRow(r, {{week: true, day: true}})) : []; if (!rows.length) return askEmpty(); const ag = askAgg(rows, r => r.Week_Start); const top = aTop(ag, a => a.missVal, 8, true);
                    return {{headline: 'The worst week was ' + aB(weekLabel(top[0].key)) + ' with ' + aB(aRwf(top[0].missVal)) + ' missed.', cols: ['Week', 'Missed value', 'Missed rate (value)', 'Missed units'], barCol: 1, bars: top.map(a => top[0].missVal > 0 ? a.missVal / top[0].missVal : 0),
                        rows: top.map(a => [weekLabel(a.key), aRwf(a.missVal), aPct(aRateV(a)), formatNum(a.missQty)]), note: 'Weeks that run across two months only count the days inside the selected months.', scope: askScope()}}; }}}},
                {{id: 't4', text: 'Which days were the worst?', kw: 'day days worst highest daily date time', run: function() {{
                    const rows = rawData.filter(r => matchRow(r, {{week: true, day: true}})); if (!rows.length) return askEmpty(); const ag = askAgg(rows, r => r.Date); const top = aTop(ag, a => a.missVal, 10, true);
                    return {{headline: 'The worst day was ' + aB(fmtDay(top[0].key, true)) + ' with ' + aB(aRwf(top[0].missVal)) + ' missed.', cols: ['Day', 'Missed value', 'Missed units', 'Order lines'], barCol: 1, bars: top.map(a => top[0].missVal > 0 ? a.missVal / top[0].missVal : 0),
                        rows: top.map(a => [fmtDay(a.key, true), aRwf(a.missVal), formatNum(a.missQty), aInt(a.lines)]), note: 'One very large order can make a single day look bad.', scope: askScope()}}; }}}}
            ]}},
            {{id: 'large', title: 'Large orders', questions: [
                {{id: 'l1', text: 'What are the biggest single lost orders?', kw: 'large orders biggest single lost order top items verify', run: function() {{
                    const lg = largeData.filter(r => matchRow(r)).sort((a, b) => b.Lost_Value - a.Lost_Value || String(a.Doc).localeCompare(String(b.Doc))); const thr = 'RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M';
                    if (!lg.length) return {{headline: 'No single order item reaches ' + thr + ' of missed value under the current filters.', cols: [], rows: [], scope: askScope()}}; const top = lg.slice(0, 10);
                    return {{headline: 'The biggest single loss is ' + aB(aRwf(top[0].Lost_Value)) + ': ' + aB(top[0].Product_Desc) + ' at ' + aB(top[0].Branch) + '.', cols: ['Sales doc', 'Branch', 'Product', 'Date', 'Missed value'], barCol: 4, bars: top.map(r => r.Lost_Value / top[0].Lost_Value),
                        rows: top.map(r => [r.Doc + ' / ' + r.Item, r.Branch, r.Product_Desc, fmtDay(r.Date, true), aRwf(r.Lost_Value)]), note: 'Items of ' + thr + '+ missed value each. Worth verifying quantity and price with the branch.', scope: askScope()}}; }}}},
                {{id: 'l2', text: 'Which branches have the most large orders?', kw: 'large orders branch branches most count', run: function() {{
                    const lg = largeData.filter(r => matchRow(r)); if (!lg.length) return {{headline: 'No single order item reaches RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M of missed value under the current filters.', cols: [], rows: [], scope: askScope()}};
                    const ag = {{}}; lg.forEach(r => {{ const a = ag[r.Branch] || (ag[r.Branch] = {{key: r.Branch, n: 0, v: 0}}); a.n++; a.v += r.Lost_Value || 0; }}); const all = Object.keys(ag).map(k => ag[k]); const tv = all.reduce((s, a) => s + a.v, 0); const top = aTop(all, a => a.v, 10, true);
                    return {{headline: aB(top[0].key) + ' has the biggest large-order loss: ' + aB(aRwf(top[0].v)) + ' on ' + aB(aInt(top[0].n) + ' items') + '.', cols: ['Branch', 'Large items', 'Missed value', 'Share of large-order loss'], barCol: 2, bars: top.map(a => a.v / top[0].v),
                        rows: top.map(a => [a.key, aInt(a.n), aRwf(a.v), aPct(aShare(a.v, tv))]), scope: askScope()}}; }}}},
                {{id: 'l3', text: 'Which products make up the large orders?', kw: 'large orders product products most', run: function() {{
                    const lg = largeData.filter(r => matchRow(r)); if (!lg.length) return {{headline: 'No single order item reaches RWF ' + (LARGE_T / 1000000).toFixed(0) + 'M of missed value under the current filters.', cols: [], rows: [], scope: askScope()}};
                    const ag = {{}}; lg.forEach(r => {{ const a = ag[r.Product_Desc] || (ag[r.Product_Desc] = {{key: r.Product_Desc, n: 0, v: 0}}); a.n++; a.v += r.Lost_Value || 0; }}); const all = Object.keys(ag).map(k => ag[k]); const tv = all.reduce((s, a) => s + a.v, 0); const top = aTop(all, a => a.v, 10, true);
                    return {{headline: aB(top[0].key) + ' is the largest: ' + aB(aRwf(top[0].v)) + ' on ' + aB(aInt(top[0].n) + ' items') + '.', cols: ['Product', 'Large items', 'Missed value', 'Share of large-order loss'], barCol: 2, bars: top.map(a => a.v / top[0].v),
                        rows: top.map(a => [a.key, aInt(a.n), aRwf(a.v), aPct(aShare(a.v, tv))]), scope: askScope()}}; }}}}
            ]}},
            {{id: 'types', title: 'Document types', questions: [
                {{id: 'd1', text: 'How is the missed value split by document type?', kw: 'document type types program programme split share category', run: function() {{
                    const rows = askRows({{type: true}}); if (!rows.length) return askEmpty({{type: true}}); const t = askTot(rows); const top = aTop(askAgg(rows, r => r.Sales_Doc_Type), a => a.missVal, 20, true);
                    return {{headline: aB(top[0].key) + ' accounts for the most missed value: ' + aB(aRwf(top[0].missVal)) + ' (' + aPct(aShare(top[0].missVal, t.missVal)) + ').', cols: ['Document type', 'Missed value', 'Share of total', 'Missed rate (value)', 'Missed units'], barCol: 1, bars: top.map(a => top[0].missVal > 0 ? a.missVal / top[0].missVal : 0),
                        rows: top.map(a => [a.key, aRwf(a.missVal), aPct(aShare(a.missVal, t.missVal)), aPct(aRateV(a)), formatNum(a.missQty)]), note: askNoValueNote(), scope: askScope({{type: true}})}}; }}}},
                {{id: 'd2', text: 'Which document type has the highest missed rate?', kw: 'document type types program programme highest missed rate worst', run: function() {{
                    const rows = askRows({{type: true}}); if (!rows.length) return askEmpty({{type: true}}); const ag = askAgg(rows, r => r.Sales_Doc_Type).filter(a => a.lines >= ASK_MIN_LINES); if (!ag.length) return {{headline: 'No document type has enough order lines (at least ' + ASK_MIN_LINES + ') in this selection to rank fairly.', cols: [], rows: [], scope: askScope({{type: true}})}};
                    const rf = (a) => {{ const v = aRateV(a); return v === null ? (aRateQ(a) || 0) : v; }}; const top = aTop(ag, rf, 20, true);
                    return {{headline: aB(top[0].key) + ' has the highest missed rate: ' + aB(aPct(rf(top[0]))) + '.', cols: ['Document type', 'Missed rate (value)', 'Missed rate (units)', 'Order lines'], barCol: 1, bars: top.map(a => rf(top[0]) > 0 ? rf(a) / rf(top[0]) : 0),
                        rows: top.map(a => [a.key, aPct(aRateV(a)), aPct(aRateQ(a)), aInt(a.lines)]), note: 'Only types with at least ' + ASK_MIN_LINES + ' order lines are ranked. ' + askNoValueNote(), scope: askScope({{type: true}})}}; }}}}
            ]}}
        ];
        const ASK_BY_ID = {{}};
        ASK_GROUPS.forEach(g => g.questions.forEach(q => {{ q.group = g.title; ASK_BY_ID[q.id] = q; }}));

        function renderAskQuestions() {{
            const box = document.getElementById('askQuestions');
            let list = [], hint = '';
            if (ASK.query) {{
                const words = ASK.query.toLowerCase().split(' ').filter(Boolean);
                ASK_GROUPS.forEach(g => g.questions.forEach(q => {{ const hay = (q.text + ' ' + q.kw + ' ' + g.title).toLowerCase(); if (words.every(w => hay.indexOf(w) >= 0)) list.push(q); }}));
                if (!list.length) hint = 'No question matches those words. Try "branch", "product", "week", "large" or "trend", or pick a topic.';
            }} else if (ASK.group) {{
                const g = ASK_GROUPS.find(x => x.id === ASK.group); list = g ? g.questions : [];
            }} else hint = 'Pick a topic to see its questions.';
            box.innerHTML = hint ? '<div class="ask-hint">' + hint + '</div>' : list.map(q => '<button type="button" class="ask-q' + (ASK.current === q.id ? ' active' : '') + '" data-q="' + q.id + '">' + escHtml(q.text) + (ASK.query ? '<small>' + escHtml(q.group) + '</small>' : '') + '</button>').join('');
            document.querySelectorAll('#askGroups .ask-chip').forEach(c => c.classList.toggle('active', !ASK.query && c.getAttribute('data-g') === ASK.group));
        }}
        function renderAskAnswer() {{
            const el = document.getElementById('askAnswer'), q = ASK_BY_ID[ASK.current];
            if (!q) {{ el.innerHTML = ''; return; }}
            const a = q.run();
            let h = '<div class="ask-card"><div class="ask-title">' + escHtml(q.text) + '</div><div class="ask-head">' + a.headline + '</div>';
            if (a.rows && a.rows.length) {{
                h += '<div class="table-container" style="overflow-x:auto;"><table class="ask-table"><thead><tr>' + a.cols.map((c, i) => '<th' + (i > 0 ? ' class="num"' : '') + '>' + escHtml(c) + '</th>').join('') + '</tr></thead><tbody>';
                a.rows.forEach((r, ri) => {{
                    h += '<tr>' + r.map((c, i) => {{
                        const pct = (a.barCol === i && a.bars) ? Math.max(0, Math.min(100, (a.bars[ri] || 0) * 100)).toFixed(1) : null;
                        const cls = (i > 0 ? 'num' : '') + (pct !== null ? ' bar' : '');
                        const sty = pct !== null ? ' style="background-image: linear-gradient(to right, rgba(244,138,106,0.30) ' + pct + '%, transparent ' + pct + '%)"' : '';
                        return '<td class="' + cls + '"' + sty + '>' + escHtml(c) + '</td>';
                    }}).join('') + '</tr>';
                }});
                h += '</tbody></table></div>';
            }}
            if (a.note) h += '<div class="ask-note">' + escHtml(a.note) + '</div>';
            h += '<div class="ask-scope">' + escHtml(a.scope || askScope()) + '</div></div>';
            el.innerHTML = h;
        }}
        function refreshAsk() {{ if (ASK.current) renderAskAnswer(); }}
        function initAsk() {{
            const panel = document.getElementById('askPanel'); if (!panel) return;
            askExtendGroups();
            document.getElementById('askGroups').innerHTML = ASK_GROUPS.map(g => '<button type="button" class="ask-chip" data-g="' + g.id + '">' + escHtml(g.title) + '</button>').join('');
            panel.style.display = '';
            document.getElementById('askGroups').addEventListener('click', function(e) {{
                const c = e.target.closest('.ask-chip'); if (!c) return;
                ASK.group = (ASK.group === c.getAttribute('data-g') && !ASK.query) ? null : c.getAttribute('data-g'); ASK.query = ''; renderAskQuestions();
            }});
            document.getElementById('askQuestions').addEventListener('click', function(e) {{
                const b = e.target.closest('.ask-q'); if (!b) return;
                ASK.current = b.getAttribute('data-q'); renderAskQuestions(); renderAskAnswer();
            }});
            // quick buttons: they open fixed questions directly
            const quick = ['b1', 't2', 'f1', 'l1'];
            document.getElementById('askTry').innerHTML = '<span>Popular:</span>' + quick.map(id => '<button type="button" class="ask-eg" data-q="' + id + '">' + escHtml(ASK_BY_ID[id].text) + '</button>').join('');
            document.getElementById('askTry').addEventListener('click', function(e) {{
                const b = e.target.closest('.ask-eg'); if (!b) return;
                const id = b.getAttribute('data-q'), g = ASK_GROUPS.find(x => x.questions.some(y => y.id === id));
                ASK.current = id; ASK.group = g ? g.id : null; renderAskQuestions(); renderAskAnswer();
            }});
            renderAskQuestions();
        }}

        // ===================== ADDED: Compare KPIs (fiscal years), Top 10 Facilities and click-through windows =====================
        // Everything below only READS rawData / missData through matchRow(), so the original views are never altered.
        const FY_MONTHS = ['Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'];
        const FAC_UNKNOWN = 'Unknown facility';
        const CMP = {{kpi: null, fys: null, type: 'bar', fyList: []}};
        const FAC = {{metric: 'val'}};
        const NA2 = '\u2014';
        const fmtI = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
        const fmtQ = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 3}});
        const pctOf2 = (a, b) => b > 0 ? (a / b) * 100 : null;
        const pctT2 = (x) => (x === null || x === undefined || !isFinite(x)) ? NA2 : x.toFixed(1) + '%';
        const SKIP_TIME = {{month: true, week: true, day: true, fy: true}};

        const CMP_KPIS = [
            {{id: 'fulfVal', label: 'Fulfilment rate (value)', kind: 'pct', needsV: true, fn: a => a.ordVal > 0 ? (a.ordVal - a.missVal) / a.ordVal * 100 : null}},
            {{id: 'fulfQty', label: 'Fulfilment rate (units)', kind: 'pct', fn: a => a.ordQty > 0 ? (a.ordQty - a.missQty) / a.ordQty * 100 : null}},
            {{id: 'missRateVal', label: 'Missed rate (value)', kind: 'pct', needsV: true, fn: a => a.ordVal > 0 ? a.missVal / a.ordVal * 100 : null}},
            {{id: 'missRateQty', label: 'Missed rate (units)', kind: 'pct', fn: a => a.ordQty > 0 ? a.missQty / a.ordQty * 100 : null}},
            {{id: 'missVal', label: 'Missed value (RWF)', kind: 'rwf', fn: a => a.missVal}},
            {{id: 'missQty', label: 'Missed units', kind: 'num', fn: a => a.missQty}},
            {{id: 'ordVal', label: 'Total sales ordered (RWF)', kind: 'rwf', needsV: true, fn: a => a.ordVal}},
            {{id: 'madeVal', label: 'Sales made (RWF)', kind: 'rwf', needsV: true, fn: a => a.ordVal - a.missVal}},
            {{id: 'ordQty', label: 'Ordered units', kind: 'num', fn: a => a.ordQty}},
            {{id: 'delQty', label: 'Delivered units', kind: 'num', fn: a => a.delQty}},
            {{id: 'short', label: 'Order items short (lines)', kind: 'num', fn: a => a.short}},
            {{id: 'shortRate', label: 'Order items short (% of lines)', kind: 'pct', fn: a => a.lines > 0 ? a.short / a.lines * 100 : null}}
        ];
        const fyStart = (label) => parseInt(String(label).replace('FY ', '').split('/')[0], 10);
        const fyMonthLabel = (fy, i) => MON3[(6 + i) % 12] + ' ' + (fyStart(fy) + (i >= 6 ? 1 : 0));
        function fmtKpi(kind, v) {{
            if (v === null || v === undefined || !isFinite(v)) return NA2;
            if (kind === 'pct') return v.toFixed(1) + '%';
            if (kind === 'rwf') return 'RWF ' + formatNum(v);
            return String(formatNum(v));
        }}
        const sumBy = (rows, f) => rows.reduce((s, r) => s + (f(r) || 0), 0);

        // ---------- Compare KPIs ----------
        function cmpMonthly() {{
            const m = {{}};
            rawData.forEach(r => {{
                if (!matchRow(r, SKIP_TIME)) return;
                let a = m[r.Month];
                if (!a) a = m[r.Month] = {{ordQty: 0, delQty: 0, missQty: 0, ordVal: 0, missVal: 0, lines: 0, short: 0, days: new Set()}};
                a.ordQty += r.Order_Qty || 0; a.delQty += r.Confirmed_Qty || 0; a.missQty += r.Missed_Qty || 0; a.ordVal += r.Ordered_Value || 0;
                a.missVal += r.Lost_Value || 0; a.lines += r.Total_Lines || 1; a.short += r.Missed_Lines || 0; a.days.add(r.Date);
            }});
            return m;
        }}
        function initCompare() {{
            const sel = document.getElementById('cmpKpi');
            sel.innerHTML = CMP_KPIS.map(k => '<option value="' + k.id + '"' + ((k.needsV && !ordValReady) ? ' disabled' : '') + '>' + escHtml(k.label) + '</option>').join('');
            CMP.kpi = ordValReady ? 'fulfVal' : 'fulfQty';
            sel.value = CMP.kpi;
            CMP.fys = new Set(FY_ORDER.slice(-2));                       // default: last year and this year
            document.getElementById('cmpFys').innerHTML = FY_ORDER.map(f => '<button type="button" class="ask-chip" data-fy="' + escHtml(f) + '">' + escHtml(f) + '</button>').join('');
            sel.addEventListener('change', function() {{ CMP.kpi = sel.value; renderCompare(); }});
            document.getElementById('cmpFys').addEventListener('click', function(e) {{
                const b = e.target.closest('.ask-chip'); if (!b) return;
                const f = b.getAttribute('data-fy');
                if (CMP.fys.has(f)) {{ if (CMP.fys.size > 1) CMP.fys.delete(f); }} else CMP.fys.add(f);
                renderCompare();
            }});
            document.querySelectorAll('input[name="cmpType"]').forEach(r => r.addEventListener('change', function() {{ CMP.type = r.value; if (r.checked) renderCompare(); }}));
            document.getElementById('cmpPanel').style.display = '';
        }}
        function renderCompare() {{
            const el = document.getElementById('cmpChart');
            if (!el || !FY_ORDER.length || typeof Plotly === 'undefined') return;
            const k = CMP_KPIS.find(x => x.id === CMP.kpi) || CMP_KPIS[0];
            const fys = FY_ORDER.filter(f => CMP.fys.has(f));
            CMP.fyList = fys;
            document.querySelectorAll('#cmpFys .ask-chip').forEach(c => c.classList.toggle('active', CMP.fys.has(c.getAttribute('data-fy'))));
            const m = cmpMonthly();
            const pal = ['#5a5ce0', '#f48a6a', '#34d399', '#facc15'];
            const nF = FY_ORDER.length;
            const traces = fys.map((fy, ti) => {{
                const y = FY_MONTHS.map((_, i) => {{ const a = m[fyMonthLabel(fy, i)]; return (!a || (k.needsV && !ordValReady)) ? null : k.fn(a); }});
                const name = fy + (fy === FY_ORDER[nF - 1] ? ' (this year)' : (fy === FY_ORDER[nF - 2] ? ' (last year)' : ''));
                const tr = {{x: FY_MONTHS, y: y, name: name, hovertemplate: '<b>' + escHtml(name) + '</b><br>%{{x}}: %{{customdata}}<extra></extra>', customdata: y.map(v => fmtKpi(k.kind, v))}};
                if (CMP.type === 'line') {{ tr.type = 'scatter'; tr.mode = 'lines+markers'; tr.line = {{color: pal[ti % 4], width: 3}}; tr.marker = {{size: 8}}; tr.connectgaps = false; }}
                else {{ tr.type = 'bar'; tr.marker = {{color: pal[ti % 4]}}; if (fys.length <= 2) {{ tr.text = y.map(v => v === null ? '' : fmtKpi(k.kind, v)); tr.textposition = 'outside'; tr.cliponaxis = false; tr.textfont = {{size: 10}}; }} }}
                return tr;
            }});
            const finite = traces.reduce((a, t) => a.concat(t.y.filter(v => v !== null)), []);
            const mx = finite.length ? Math.max.apply(null, finite) : 1;
            const yaxis = {{rangemode: 'tozero', automargin: true}};
            if (k.kind === 'pct') {{ yaxis.ticksuffix = '%'; yaxis.range = [0, Math.max(5, Math.min(mx * 1.18, 108))]; }}
            else {{ const tv = [0, 1, 2, 3, 4, 5].map(i => mx * 1.15 * i / 5); yaxis.tickvals = tv; yaxis.ticktext = tv.map(v => String(formatNum(v))); yaxis.range = [0, mx * 1.15 || 1]; }}
            Plotly.react('cmpChart', traces, {{barmode: 'group', margin: {{l: 60, r: 15, t: 10, b: 40}}, legend: {{orientation: 'h', y: 1.14}}, yaxis: yaxis, xaxis: {{categoryorder: 'array', categoryarray: FY_MONTHS}}}}, {{displayModeBar: false}});
            if (!el._cmpBound && typeof el.on === 'function') {{
                el._cmpBound = true;
                el.on('plotly_click', function(d) {{
                    const p = d && d.points && d.points[0]; if (!p) return;
                    const fy = CMP.fyList[p.curveNumber]; if (!fy) return;
                    const label = fyMonthLabel(fy, FY_MONTHS.indexOf(p.x));
                    openSliceDetail({{title: fy + ' \u2014 ' + label + ': what was missed', pred: r => r.Month === label, skip: SKIP_TIME, ev: d.event, note: 'Month-on-month view: the month, week, day and fiscal-year filters are replaced by the month you clicked. Type, branch and product filters still apply.'}});
                }});
            }}
            let note = '';
            if (k.needsV && !ordValReady) note = 'Order value is not in this database yet, so value KPIs are not available. Run an API sync to fill it.';
            else if (FY_ORDER.length < 2) note = 'Only one fiscal year has data so far, so there is nothing to compare yet.';
            else if (k.kind !== 'pct') {{
                const lastDate = Object.keys(DATE_MONTH).sort().pop(); const dim = (y, mo) => new Date(y, mo + 1, 0).getDate();
                const lm = DATE_MONTH[lastDate], parts = lm.split(' '), mi = MON3.indexOf(parts[0]);
                if (parseInt(lastDate.slice(8, 10), 10) < dim(parseInt(parts[1], 10), mi)) note = lm + ' only has data up to ' + fmtDay(lastDate, true) + ', so its total is partial. Rates are not affected.';
            }}
            document.getElementById('cmpNote').textContent = note;
        }}

        // ---------- Top 10 Facilities ----------
        function facAggregate() {{
            const rows = missData.filter(r => matchRow(r));
            const m = {{}}; const tot = {{val: 0, qty: 0, lines: 0}}; const unk = {{val: 0, qty: 0, lines: 0}};
            rows.forEach(r => {{
                tot.val += r.Lost_Value || 0; tot.qty += r.Missed_Qty || 0; tot.lines++;
                if (r.Facility === FAC_UNKNOWN) {{ unk.val += r.Lost_Value || 0; unk.qty += r.Missed_Qty || 0; unk.lines++; return; }}
                let a = m[r.Facility];
                if (!a) a = m[r.Facility] = {{name: r.Facility, val: 0, qty: 0, lines: 0, br: {{}}}};
                a.val += r.Lost_Value || 0; a.qty += r.Missed_Qty || 0; a.lines++;
                const b = a.br[r.Branch] || (a.br[r.Branch] = {{val: 0, n: 0}}); b.val += r.Lost_Value || 0; b.n++;
            }});
            return {{list: Object.keys(m).map(k => m[k]), tot: tot, unk: unk}};
        }}
        function facMain(a) {{
            return Object.keys(a.br).sort((x, y) => (a.br[y].val - a.br[x].val) || (a.br[y].n - a.br[x].n) || x.localeCompare(y))[0];
        }}
        function initFacilities() {{
            document.querySelectorAll('input[name="facMetric"]').forEach(r => r.addEventListener('change', function() {{ if (r.checked) {{ FAC.metric = r.value; renderFacilities(); }} }}));
            document.querySelector('#facTable tbody').addEventListener('click', function(e) {{
                const tr = e.target.closest('tr'); if (!tr || !tr.getAttribute('data-fac')) return;
                openFacilityDetail(tr.getAttribute('data-fac'), e);
            }});
            document.getElementById('facPanel').style.display = '';
        }}
        function renderFacilities() {{
            const body = document.getElementById('facBody'); if (!body || typeof Plotly === 'undefined') return;
            const g = facAggregate();
            const key = {{val: 'val', qty: 'qty', lines: 'lines'}}[FAC.metric] || 'val';
            const title = {{val: 'Top 10 Facilities by Missed Value', qty: 'Top 10 Facilities by Missed Units', lines: 'Top 10 Facilities by Lines Short'}}[key];
            document.getElementById('facTitle').textContent = title;
            const top = g.list.slice().sort((a, b) => (b[key] - a[key]) || a.name.localeCompare(b.name)).slice(0, 10);
            const tb = document.querySelector('#facTable tbody');
            const note = document.getElementById('facNote');
            const hasNames = g.list.length > 0;
            if (!hasNames) {{
                Plotly.react('facChart', [], {{margin: {{l: 20, r: 20, t: 10, b: 30}}, xaxis: {{visible: false}}, yaxis: {{visible: false}}}}, {{displayModeBar: false}});
                tb.innerHTML = '';
                note.innerHTML = g.tot.lines === 0 ? 'No missed order lines under the current filters.' : '<div class="fac-empty"><b>No facility names in the database yet.</b> Facility names come from the API. Run a full API sync once (From the first day of your oldest month) and this table fills in.</div>';
                return;
            }}
            const disp = facDisplay(top.map(a => a.name));
            const lab = (a) => {{ const s = disp[a.name]; return s.length > 38 ? s.slice(0, 36) + '\u2026' : s; }};
            const rev = top.slice().reverse();
            const fmtMetric = (a) => key === 'val' ? formatNum(a.val) : (key === 'qty' ? formatNum(a.qty) : fmtI(a.lines));
            Plotly.react('facChart', [{{x: rev.map(a => a[key]), y: rev.map(a => disp[a.name]), customdata: rev.map(a => a.name), type: 'bar', orientation: 'h', marker: {{color: '#f48a6a'}},
                text: rev.map(fmtMetric), textposition: 'auto', hovertemplate: '%{{y}}<br>%{{text}}<extra></extra>'}}],
                {{margin: {{l: 200, r: 35, t: 10, b: 30}}, yaxis: {{automargin: true, tickmode: 'array', tickvals: rev.map(a => disp[a.name]), ticktext: rev.map(lab)}}}}, {{displayModeBar: false}});
            const el = document.getElementById('facChart');
            if (!el._facBound && typeof el.on === 'function') {{
                el._facBound = true;
                el.on('plotly_click', function(d) {{ const p = d && d.points && d.points[0]; if (p) openFacilityDetail(String(p.customdata || p.y), d.event); }});
            }}
            tb.innerHTML = top.map((a, i) => '<tr data-fac="' + escHtml(a.name) + '"><td>' + (i + 1) + '</td><td class="name">' + escHtml(disp[a.name]) + '</td><td>' + escHtml(facMain(a)) + '</td><td class="num">' + fmtI(a.val) + '</td><td class="num">' +
                pctT2(pctOf2(a.val, g.tot.val)) + '</td><td class="num">' + fmtI(a.qty) + '</td><td class="num">' + fmtI(a.lines) + '</td></tr>').join('');
            note.innerHTML = g.unk.lines > 0 ? fmtI(g.unk.lines) + ' missed lines (RWF ' + formatNum(g.unk.val) + ') have no facility name yet and are not ranked. They fill in after a full API sync.' : '';
        }}

        // ---------- click-through windows ----------
        function showDrill(title, sub, html, ev, after, cleanup) {{
            try {{ if (window.__drillCleanup) {{ window.__drillCleanup(); window.__drillCleanup = null; }} }} catch (e) {{}}
            document.getElementById('drillTitle').textContent = title;
            document.getElementById('drillSub').textContent = sub;
            document.getElementById('drillBody').innerHTML = html;
            const overlay = document.getElementById('drillOverlay'), box = document.getElementById('drillBox');
            const docH = document.documentElement.scrollHeight;
            overlay.style.height = docH + 'px';
            overlay.style.display = 'block';
            const y = (ev && ev.pageY) ? ev.pageY : (window.scrollY + 80);
            box.style.top = Math.max(10, Math.min(y - 90, docH - box.offsetHeight - 20)) + 'px';
            window.__drillCleanup = cleanup || null;
            if (after) {{ try {{ after(); }} catch (e) {{ console.error('Window error:', e); }} }}
            try {{ box.setAttribute('tabindex', '-1'); box.focus({{preventScroll: true}}); }} catch (e) {{}}
            try {{ box.scrollIntoView({{block: 'start', behavior: 'smooth'}}); }} catch (e) {{}}
        }}
        const dTile = (label, value, cls, hl) => '<div class="drill-tile' + (hl ? ' hl' : '') + '"><div class="t-label">' + label + '</div><div class="t-value ' + (cls || '') + '">' + value + '</div></div>';
        function totalsOf(rows) {{
            const t = {{ordQty: 0, delQty: 0, missQty: 0, ordVal: 0, missVal: 0, lines: 0, short: 0}};
            rows.forEach(r => {{ t.ordQty += r.Order_Qty || 0; t.delQty += r.Confirmed_Qty || 0; t.missQty += r.Missed_Qty || 0; t.ordVal += r.Ordered_Value || 0; t.missVal += r.Lost_Value || 0; t.lines += r.Total_Lines || 1; t.short += r.Missed_Lines || 0; }});
            return t;
        }}
        function topBars(id, rows, keyFn, n, color) {{
            const m = {{}}; rows.forEach(r => {{ const k = keyFn(r); m[k] = (m[k] || 0) + (r.Lost_Value || 0); }});
            const ks = Object.keys(m).sort((a, b) => (m[b] - m[a]) || a.localeCompare(b)).slice(0, n).reverse();
            Plotly.newPlot(id, [{{x: ks.map(k => m[k]), y: ks, type: 'bar', orientation: 'h', marker: {{color: color}}, text: ks.map(k => formatNum(m[k])), textposition: 'auto'}}],
                {{margin: {{l: 10, r: 25, t: 10, b: 30}}, yaxis: {{automargin: true, tickmode: 'array', tickvals: ks, ticktext: ks.map(k => k.length > 34 ? k.slice(0, 32) + '\u2026' : k)}}}}, {{displayModeBar: false}});
        }}
        function monthBars(id, rows, color) {{
            const m = {{}}; rows.forEach(r => {{ m[r.Month] = (m[r.Month] || 0) + (r.Lost_Value || 0); }});
            const ks = MONTH_ORDER.filter(x => m[x] !== undefined);
            Plotly.newPlot(id, [{{x: ks, y: ks.map(k => m[k]), type: 'bar', marker: {{color: color}}, text: ks.map(k => formatNum(m[k])), textposition: 'auto'}}], {{margin: {{l: 60, r: 15, t: 10, b: 40}}}}, {{displayModeBar: false}});
        }}
        function destroyLinesTable() {{
            try {{ if (window.$ && $.fn.dataTable.isDataTable('#mlTable')) $('#mlTable').DataTable().destroy(); }} catch (e) {{}}
            ['mlChartA', 'mlChartB', 'bkMonthChart'].forEach(id => {{ const e = document.getElementById(id); if (e) {{ try {{ Plotly.purge(e); }} catch (x) {{}} }} }});
        }}

        // The window with the missed order lines behind a number, a bar, a point or a row
        function openLines(o) {{
            syncSearch();
            const lines = o.lines.slice().sort((a, b) => (b.Lost_Value - a.Lost_Value) || String(a.Doc).localeCompare(String(b.Doc)) || String(a.Item).localeCompare(String(b.Item)));
            const selVal = sumBy(missData.filter(r => matchRow(r)), r => r.Lost_Value);
            const val = sumBy(lines, r => r.Lost_Value), units = sumBy(lines, r => r.Missed_Qty);
            const fac = new Set(lines.filter(r => r.Facility !== FAC_UNKNOWN).map(r => r.Facility));
            let html = o.extraTiles ? '<div class="drill-tiles">' + o.extraTiles + '</div>' : '';
            if (!lines.length) {{
                html += '<div class="drill-strip">No missed order lines for this selection.</div>';
                showDrill(o.title, o.sub || describeFilters(true), html, o.ev, null, destroyLinesTable);
                return;
            }}
            html += '<div class="drill-tiles">' + dTile('Missed order lines', fmtI(lines.length), 't-ord') + dTile('Missed value (RWF)', formatNum(val), 't-miss') + dTile('Missed units', formatNum(units), 't-miss') +
                dTile('Share of missed value in the selection', pctT2(pctOf2(val, selVal)), 't-miss') + dTile('Facilities affected', fmtI(fac.size), 't-ord') + dTile('Branches affected', fmtI(new Set(lines.map(r => r.Branch)).size), 't-ord') +
                dTile('Products affected', fmtI(new Set(lines.map(r => r.Product_Desc)).size), 't-ord') + dTile('Largest single line (RWF)', formatNum(lines[0].Lost_Value), 't-miss') + '</div>';
            if (o.note) html += '<div class="drill-strip">' + escHtml(o.note) + '</div>';
            const second = o.chartMode === 'facility' ? ['Missed value by month (RWF)', 'month'] : ['Top 10 products (missed RWF)', 'product'];
            const first = o.chartMode === 'facility' ? ['Top 10 missed products (RWF)', 'product'] : ['Top 10 branches (missed RWF)', 'branch'];
            html += '<div class="drill-grid2"><div class="drill-panel"><h4>' + first[0] + '</h4><div id="mlChartA" style="height:270px;"></div></div><div class="drill-panel"><h4>' + second[0] + '</h4><div id="mlChartB" style="height:270px;"></div></div></div>';
            html += '<div class="drill-panel"><h4>The ' + fmtI(lines.length) + ' missed order lines (search, sort and download below)</h4><table id="mlTable" class="light-table display nowrap" style="width:100%"></table></div>';
            showDrill(o.title, o.sub || describeFilters(true), html, o.ev, function() {{
                const charts = {{product: () => topBars('mlChartA', lines, r => r.Product_Desc, 10, '#f48a6a'), branch: () => topBars('mlChartA', lines, r => r.Branch, 10, '#f48a6a')}};
                (o.chartMode === 'facility' ? charts.product : charts.branch)();
                if (o.chartMode === 'facility') monthBars('mlChartB', o.monthLines || lines, '#5a5ce0'); else topBars('mlChartB', lines, r => r.Product_Desc, 10, '#5a5ce0');
                const data = lines.map(r => ({{Date: r.Date, Doc: r.Doc, Item: r.Item, Facility: r.Facility, Branch: r.Branch, Product: r.Product_Desc, Ord: r.Order_Qty, Del: r.Confirmed_Qty, Miss: r.Missed_Qty, Val: r.Lost_Value}}));
                const q = function(d) {{ return (d === null || d === undefined || d === '') ? '' : fmtQ(d); }};
                $('#mlTable').DataTable({{data: data, deferRender: true, dom: 'Bfrtip', buttons: btnConfig, pageLength: 10, lengthChange: false, order: [[9, 'desc']], scrollX: true, autoWidth: false,
                    columns: [{{title: 'Doc date', data: 'Date'}}, {{title: 'Sales doc', data: 'Doc'}}, {{title: 'Item', data: 'Item'}}, {{title: 'Facility', data: 'Facility', render: function(d) {{ return facLabel(d); }}}}, {{title: 'Branch', data: 'Branch'}}, {{title: 'Product', data: 'Product'}},
                        {{title: 'Ordered', data: 'Ord', className: 'dt-right', render: q}}, {{title: 'Delivered', data: 'Del', className: 'dt-right', render: q}},
                        {{title: 'Missed', data: 'Miss', className: 'dt-right', render: function(d) {{ return '<span style="color:#f48a6a; font-weight:bold;">' + fmtQ(d) + '</span>'; }}}},
                        {{title: 'Missed value (RWF)', data: 'Val', className: 'dt-right', render: $.fn.dataTable.render.number(',', '.', 0)}}]}});
            }}, destroyLinesTable);
        }}

        // A slice of the data (a week, a month, a type, a product ...) -> its totals plus the missed lines behind it
        function openSliceDetail(o) {{
            syncSearch();
            const skip = o.skip || {{}};
            const raw = rawData.filter(r => o.pred(r) && matchRow(r, skip));
            const lines = missData.filter(r => o.pred(r) && matchRow(r, skip));
            const t = totalsOf(raw), hasV = ordValReady;
            const extra = o.noTotals ? '' : dTile('Ordered value (RWF)', hasV ? formatNum(t.ordVal) : NA2, 't-ord') + dTile('Sales made (RWF)', hasV ? formatNum(t.ordVal - t.missVal) : NA2, 't-made') +
                dTile('Fulfilment rate (value)', hasV ? pctT2(pctOf2(t.ordVal - t.missVal, t.ordVal)) : NA2, 't-made') + dTile('Ordered units', formatNum(t.ordQty), 't-ord') +
                dTile('Delivered units', formatNum(t.delQty), 't-made') + dTile('Fulfilment rate (units)', pctT2(pctOf2(t.ordQty - t.missQty, t.ordQty)), 't-made') +
                dTile('Order lines', fmtI(t.lines), 't-ord') + dTile('Lines short', fmtI(t.short), 't-miss');
            openLines({{title: o.title, sub: describeFilters(true), lines: lines, extraTiles: extra, note: o.note, ev: o.ev, chartMode: o.chartMode, monthLines: o.monthLines}});
        }}
        function openFacilityDetail(name, ev) {{
            syncSearch();
            openSliceDetail({{title: 'Facility \u2014 ' + facLabel(name) + ': what was missed', pred: r => r.Facility === name, skip: {{}}, ev: ev, chartMode: 'facility', noTotals: true,
                monthLines: missData.filter(r => r.Facility === name && matchRow(r, SKIP_TIME))}});
        }}

        // The window behind a card that shows ordered / made / rates: the numbers per branch and per month
        function openBreakdown(o) {{
            syncSearch();
            const rows = rawData.filter(r => matchRow(r)), hasV = ordValReady;
            const t = totalsOf(rows);
            const byB = {{}}; rows.forEach(r => {{ const a = byB[r.Branch] || (byB[r.Branch] = {{ordQty: 0, delQty: 0, missQty: 0, ordVal: 0, missVal: 0, lines: 0, short: 0}}); a.ordQty += r.Order_Qty || 0; a.delQty += r.Confirmed_Qty || 0; a.missQty += r.Missed_Qty || 0; a.ordVal += r.Ordered_Value || 0; a.missVal += r.Lost_Value || 0; a.lines += r.Total_Lines || 1; }});
            const bs = Object.keys(byB).sort((a, b) => (byB[b].missVal - byB[a].missVal) || a.localeCompare(b));
            const hl = (k) => o.hl.indexOf(k) >= 0;
            const tiles = dTile('Total sales ordered (RWF)', hasV ? formatNum(t.ordVal) : NA2, 't-ord', hl('ordVal')) + dTile('Sales made (RWF)', hasV ? formatNum(t.ordVal - t.missVal) : NA2, 't-made', hl('madeVal')) +
                dTile('Missed value (RWF)', formatNum(t.missVal), 't-miss', hl('missVal')) + dTile('Fulfilment rate (value)', hasV ? pctT2(pctOf2(t.ordVal - t.missVal, t.ordVal)) : NA2, 't-made', hl('fulfVal')) +
                dTile('Ordered units', formatNum(t.ordQty), 't-ord', hl('ordQty')) + dTile('Delivered units', formatNum(t.delQty), 't-made', hl('delQty')) +
                dTile('Missed units', formatNum(t.missQty), 't-miss', hl('missQty')) + dTile('Fulfilment rate (units)', pctT2(pctOf2(t.ordQty - t.missQty, t.ordQty)), 't-made', hl('fulfQty'));
            let html = '<div class="drill-tiles">' + tiles + '</div>';
            html += '<div class="drill-strip">' + escHtml(o.explain) + ' <button type="button" class="ask-chip" id="bkLinesBtn" style="margin-left:6px;">Show the missed order lines</button></div>';
            html += '<div class="drill-panel"><h4>Ordered vs made by month (RWF) &mdash; all months, other filters applied</h4><div id="bkMonthChart" style="height:260px;"></div></div>';
            const brRows = bs.map(b => {{ const a = byB[b]; return '<tr><td>' + escHtml(b) + '</td><td class="num">' + (hasV ? fmtI(a.ordVal) : NA2) + '</td><td class="num">' + (hasV ? fmtI(a.ordVal - a.missVal) : NA2) + '</td><td class="num">' + fmtI(a.missVal) + '</td><td class="num">' +
                (hasV ? pctT2(pctOf2(a.ordVal - a.missVal, a.ordVal)) : NA2) + '</td><td class="num">' + fmtI(a.ordQty) + '</td><td class="num">' + fmtI(a.delQty) + '</td><td class="num">' + fmtI(a.missQty) + '</td><td class="num">' + pctT2(pctOf2(a.ordQty - a.missQty, a.ordQty)) + '</td></tr>'; }}).join('');
            html += '<div class="drill-panel"><h4>By branch (' + fmtI(bs.length) + ')</h4><div class="table-container"><table class="light-table"><thead><tr><th>Branch</th><th class="num">Ordered (RWF)</th><th class="num">Made (RWF)</th><th class="num">Missed (RWF)</th><th class="num">Fulfilment (value)</th><th class="num">Ordered units</th><th class="num">Delivered units</th><th class="num">Missed units</th><th class="num">Fulfilment (units)</th></tr></thead><tbody>' + brRows + '</tbody></table></div></div>';
            showDrill(o.title, describeFilters(true), html, o.ev, function() {{
                const mo = {{}}; rawData.filter(r => matchRow(r, SKIP_TIME)).forEach(r => {{ const a = mo[r.Month] || (mo[r.Month] = {{ordVal: 0, missVal: 0, ordQty: 0, missQty: 0}}); a.ordVal += r.Ordered_Value || 0; a.missVal += r.Lost_Value || 0; a.ordQty += r.Order_Qty || 0; a.missQty += r.Missed_Qty || 0; }});
                const ks = MONTH_ORDER.filter(x => mo[x]);
                const traces = hasV ? [{{x: ks, y: ks.map(k => mo[k].ordVal - mo[k].missVal), type: 'bar', name: 'Made', marker: {{color: '#34d399'}}}}, {{x: ks, y: ks.map(k => mo[k].missVal), type: 'bar', name: 'Missed', marker: {{color: '#f48a6a'}}}}]
                    : [{{x: ks, y: ks.map(k => mo[k].ordQty - mo[k].missQty), type: 'bar', name: 'Delivered units', marker: {{color: '#34d399'}}}}, {{x: ks, y: ks.map(k => mo[k].missQty), type: 'bar', name: 'Missed units', marker: {{color: '#f48a6a'}}}}];
                Plotly.newPlot('bkMonthChart', traces, {{barmode: 'stack', margin: {{l: 60, r: 15, t: 10, b: 40}}, legend: {{orientation: 'h', y: -0.2}}}}, {{displayModeBar: false}});
                document.getElementById('bkLinesBtn').addEventListener('click', function(e) {{ openSliceDetail({{title: o.title.split(' \u2014 ')[0] + ' \u2014 the missed order lines', pred: () => true, skip: {{}}, ev: e}}); }});
            }}, destroyLinesTable);
        }}

        // ---------- wiring: cards, charts and table rows become clickable ----------
        const CARD_DRILLS = [
            ['kpiVal', 'lines', 'Missed value'], ['kpiMiss', 'lines', 'Missed units'], ['kpiItems', 'lines', 'Order items short'],
            ['kpiOrdVal', 'break', 'Total sales ordered', ['ordVal']], ['kpiMadeVal', 'break', 'Sales made', ['madeVal']], ['valRateDonutContainer', 'break', 'Missed rate (value)', ['missVal', 'ordVal']], ['kpiFulfVal', 'break', 'Fulfilment rate (value)', ['fulfVal']],
            ['kpiOrd', 'break', 'Ordered units', ['ordQty']], ['kpiDel', 'break', 'Delivered units', ['delQty']], ['donutChartContainer', 'break', 'Missed rate (units)', ['missQty', 'ordQty']], ['kpiFulfQty', 'break', 'Fulfilment rate (units)', ['fulfQty']]
        ];
        const CARD_EXPLAIN = 'These are the ordered, made and missed numbers behind the card, per branch and per month, for the current filters.';
        function addHint(hdr, text) {{
            if (!hdr || hdr.querySelector('.drill-hint') || !hdr.firstChild) return;
            const w = document.createElement('span'); w.appendChild(hdr.firstChild);
            const h = document.createElement('span'); h.className = 'drill-hint'; h.textContent = text; w.appendChild(h);
            hdr.insertBefore(w, hdr.firstChild);
        }}
        function bindCards() {{
            CARD_DRILLS.forEach(function(c) {{
                const el = document.getElementById(c[0]); const card = el && el.closest('.flip-card');
                if (!card || card.getAttribute('data-more')) return;
                card.setAttribute('data-more', '1'); card.classList.add('card-clickable'); card.setAttribute('role', 'button'); card.setAttribute('tabindex', '0'); card.setAttribute('title', 'Click to see the data behind this number');
                const back = card.querySelector('.flip-card-back');
                if (back && !back.querySelector('.kpi-hint')) {{ const hnt = document.createElement('div'); hnt.className = 'kpi-hint'; hnt.textContent = 'Click for the data'; back.appendChild(hnt); }}
                const open = function(ev) {{
                    if (c[1] === 'lines') openSliceDetail({{title: c[2] + ' \u2014 the missed order lines behind it', pred: () => true, skip: {{}}, ev: ev, chartMode: 'branch'}});
                    else openBreakdown({{title: c[2] + ' \u2014 the numbers behind it', hl: c[3] || [], explain: CARD_EXPLAIN, ev: ev}});
                }};
                card.addEventListener('click', function(e) {{ open(e); }});
                card.addEventListener('keydown', function(e) {{ if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); open(null); }} }});
            }});
        }}
        function bindCharts() {{
            const specs = [
                ['trendChart', 'click a point for the order lines of that week', function(p, d) {{
                    const wsList = [...new Set(rawData.filter(r => r.Missed_Qty > 0 && matchRow(r)).map(r => r.Week_Start))].sort(); const ws = wsList[p.pointNumber]; if (!ws) return;
                    openSliceDetail({{title: 'Week ' + weekLabel(ws) + ': what was missed', pred: r => r.Week_Start === ws, skip: {{week: true, day: true}}, ev: d.event, chartMode: 'branch'}}); }}],
                ['docTypeChart', 'click a slice for the order lines of that type', function(p, d) {{
                    const t = String(p.label); openSliceDetail({{title: 'Document type ' + t + ': what was missed', pred: r => r.Sales_Doc_Type === t, skip: {{type: true}}, ev: d.event, chartMode: 'branch'}}); }}],
                ['monthValueChart', 'click a bar for the order lines of that month', function(p, d) {{ const mo = String(p.x); openSliceDetail({{title: mo + ': what was missed', pred: r => r.Month === mo, skip: SKIP_TIME, ev: d.event, chartMode: 'branch'}}); }}],
                ['monthRateChart', 'click a bar for the order lines of that month', function(p, d) {{ const mo = String(p.x); openSliceDetail({{title: mo + ': what was missed', pred: r => r.Month === mo, skip: SKIP_TIME, ev: d.event, chartMode: 'branch'}}); }}]
            ];
            specs.forEach(function(s) {{
                const el = document.getElementById(s[0]);
                if (!el || el._moreBound || typeof el.on !== 'function') return;
                el._moreBound = true;
                el.on('plotly_click', function(d) {{ const p = d && d.points && d.points[0]; if (p) s[2](p, d); }});
                const panel = el.closest('.panel'); addHint(panel ? panel.querySelector('.panel-header') : null, s[1]);
            }});
        }}
        function bindRows(tableId, dt, handler, hint) {{
            const t = document.getElementById(tableId);
            if (!t || !dt || t.getAttribute('data-more')) return;
            t.setAttribute('data-more', '1'); t.classList.add('row-click');
            $('#' + tableId + ' tbody').on('click', 'tr', function(e) {{ const data = dt.row(this).data(); if (data) handler(data, e); }});
            const panel = t.closest('.panel'); addHint(panel ? panel.querySelector('.panel-header') : null, hint);
        }}
        function bindTables() {{
            bindRows('topItemsTable', tableItems, function(d, e) {{ const p = d.Product; openSliceDetail({{title: p + ': what was missed', pred: r => r.Product_Desc === p, skip: {{}}, ev: e, chartMode: 'branch'}}); }}, 'click a row for the order lines of that product');
            bindRows('locationTable', tableLoc, function(d, e) {{ openDrill(d.Branch, e); }}, 'click a row for branch details');
            bindRows('branchPerfTable', tableBranchPerf, function(d, e) {{ openDrill(d.Branch, e); }}, 'click a row for branch details');
            bindRows('monthSummaryTable', tableMonthSum, function(d, e) {{ const mo = d.Month; openSliceDetail({{title: mo + ': what was missed', pred: r => r.Month === mo, skip: SKIP_TIME, ev: e, chartMode: 'branch'}}); }}, 'click a row for the order lines of that month');
        }}
        function initMore() {{ initCompare(); initFacilities(); initPeriod(); }}
        function refreshMore() {{ renderCompare(); renderFacilities(); bindCards(); bindCharts(); bindTables(); updateShowing(); }}

        // ===================== ADDED: "Showing ..." strip, period tags and the collapsible filter side =====================
        function periodText() {{
            const dash = ' \u2013 ';
            if (FILT.dayFrom !== null) {{
                if (FILT.dayFrom === FILT.dayTo) return fmtDay(FILT.dayFrom, true);
                return fmtDay(FILT.dayFrom, FILT.dayFrom.slice(0, 4) !== FILT.dayTo.slice(0, 4)) + dash + fmtDay(FILT.dayTo, true);
            }}
            if (FILT.weeks.size) {{
                const w = [...FILT.weeks].sort();
                if (w.length === 1) return 'Week ' + weekLabel(w[0]);
                const p = w[w.length - 1].split('-').map(Number), e = new Date(Date.UTC(p[0], p[1] - 1, p[2] + 6));
                const endStr = e.getUTCFullYear() + '-' + pad2(e.getUTCMonth() + 1) + '-' + pad2(e.getUTCDate());
                return w.length + ' weeks: ' + fmtDay(w[0], w[0].slice(0, 4) !== endStr.slice(0, 4)) + dash + fmtDay(endStr, true);
            }}
            const fyPrefix = FILT.fys.size ? FY_ORDER.filter(f => FILT.fys.has(f)).join(', ') : '';
            if (FILT.months.size) {{
                const ms = sortedMonths(FILT.months);
                const contiguous = ms.every((m, i) => i === 0 || MONTH_ORDER.indexOf(m) === MONTH_ORDER.indexOf(ms[i - 1]) + 1);
                const t = ms.length === 1 ? ms[0] : (contiguous ? ms[0] + dash + ms[ms.length - 1] : (ms.length <= 3 ? ms.join(', ') : ms.length + ' months'));
                return fyPrefix ? fyPrefix + ' \u00b7 ' + t : t;
            }}
            if (FILT.fys.size) {{
                const ms = MONTH_ORDER.filter(m => FILT.fys.has(FY_OF_MONTH[m]));
                return fyPrefix + (ms.length ? ' (' + ms[0] + dash + ms[ms.length - 1] + ')' : '');
            }}
            return 'All data (' + MONTH_ORDER[0] + dash + MONTH_ORDER[MONTH_ORDER.length - 1] + ')';
        }}
        function otherFiltersText() {{
            const p = [];
            if (FILT.types.size) p.push('Doc type: ' + [...FILT.types].sort().join(', '));
            if (FILT.statuses.size) p.push('Status: ' + [...FILT.statuses].sort().join(', '));
            if (FILT.branches.size) {{ const b = [...FILT.branches].sort(); p.push('Branch: ' + (b.length <= 3 ? b.join(', ') : b.length + ' branches')); }}
            if (FILT.search) p.push('Product: "' + FILT.search + '"');
            return p.join(' \u00b7 ');
        }}
        function updateShowing() {{
            const t = document.getElementById('showText'); if (!t) return;
            const extra = otherFiltersText();
            t.innerHTML = '<b>Showing</b> <span class="show-period">' + escHtml(periodText()) + '</span>' + (extra ? ' <span class="show-sep">\u00b7</span> ' + escHtml(extra) : '');
            const last = Object.keys(DATE_MONTH).sort().pop();
            document.getElementById('showRight').textContent = last ? 'Latest data: ' + fmtDay(last, true) : '';
            const pt = periodText();
            document.querySelectorAll('.period-chip').forEach(c => {{ c.textContent = c.getAttribute('data-kind') === 'all' ? 'All months compared' : pt; c.title = c.getAttribute('data-kind') === 'all' ? 'This view always compares every month; the month, week, day and fiscal-year filters do not change it' : 'Period shown: ' + pt; }});
        }}
        // which headings get a period tag: 'sel' = the selected period, 'all' = a month-by-month view that always compares every month
        const CHIP_HEADERS = [['Lost Value by Branch', 'sel'], ['Top Branches by Missed Quantity', 'sel'], ['Missed Deliveries Trends', 'sel'], ['Lost Value by Doc Type', 'sel'], ['Value: Made vs Missed by Branch', 'sel'], ['Units: Made vs Missed by Branch', 'sel'],
            ['Sales Made vs Missed by Month', 'all'], ['Missed Rate by Month', 'all'], ['Compare KPIs', 'all'], ['Top 10 Facilities', 'sel'], ['Branch-wise Lost Sales Summary', 'sel'], ['Top Missed Items', 'sel'],
            ['Branch Performance', 'sel'], ['Monthly Summary', 'all'], ['Large Orders to Verify', 'sel'], ['Unfulfilled Details Log', 'sel']];
        function addChip(host, kind, afterFirst) {{
            if (!host || host.querySelector(':scope > .period-chip, .period-chip')) return;
            const c = document.createElement('span'); c.className = 'period-chip'; c.setAttribute('data-kind', kind);
            if (afterFirst && host.firstChild && host.firstChild.nextSibling) host.insertBefore(c, host.firstChild.nextSibling); else host.appendChild(c);
        }}
        function addChips() {{
            document.querySelectorAll('.section-label').forEach(l => addChip(l, 'sel', false));
            document.querySelectorAll('.panel-header').forEach(h => {{
                const txt = (h.textContent || '').trim();
                const hit = CHIP_HEADERS.find(x => txt.indexOf(x[0]) === 0);
                if (!hit) return;
                let w = h.firstElementChild && h.firstElementChild.tagName === 'SPAN' && !h.firstElementChild.classList.contains('info-icon') ? h.firstElementChild : null;
                if (!w && h.firstChild) {{ w = document.createElement('span'); w.appendChild(h.firstChild); h.insertBefore(w, h.firstChild); }}
                addChip(w, hit[1], true);
            }});
        }}
        function resizeAfterLayout() {{
            window.dispatchEvent(new Event('resize'));
            setTimeout(function() {{
                try {{ $.fn.dataTable.tables({{visible: true, api: true}}).columns.adjust(); }} catch (e) {{}}
                document.querySelectorAll('.js-plotly-plot').forEach(el => {{ try {{ Plotly.Plots.resize(el); }} catch (e) {{}} }});
            }}, 80);
        }}
        function setSidebar(collapsed) {{
            const dc = document.querySelector('.dashboard-container'), b = document.getElementById('sbToggle');
            if (!dc || !b) return;
            dc.classList.toggle('sb-collapsed', collapsed);
            b.innerHTML = collapsed ? 'Show filters &raquo;' : '&laquo; Hide filters';
            b.setAttribute('aria-expanded', String(!collapsed));
            b.title = collapsed ? 'Show the filters again' : 'Hide the filters to give the dashboard more room';
            closePanels(null);
            try {{ localStorage.setItem('rmsFiltersCollapsed', collapsed ? '1' : '0'); }} catch (e) {{}}
            resizeAfterLayout();
        }}
        function initPeriod() {{
            const b = document.getElementById('sbToggle'); if (!b) return;
            b.addEventListener('click', function() {{ setSidebar(!document.querySelector('.dashboard-container').classList.contains('sb-collapsed')); }});
            addChips();
            let was = false; try {{ was = localStorage.getItem('rmsFiltersCollapsed') === '1'; }} catch (e) {{}}
            if (was) setSidebar(true);
            updateShowing();
        }}

        // ===================== ADDED: facility names without the "--154" tail, Facilities questions and the prompt box =====================
        // A facility is identified by its full name (so two different facilities with the same base name are never merged) and shown without the "--154" tail.
        function facLabel(raw) {{ return String(raw).replace(/\\s*--\\s*\\d+\\s*$/, '').trim() || String(raw); }}
        function facDisplay(names) {{
            const out = {{}}, count = {{}};
            names.forEach(n => {{ const l = facLabel(n); count[l] = (count[l] || 0) + 1; }});
            names.forEach(n => {{ const l = facLabel(n); out[n] = count[l] > 1 ? l + ' (' + ((String(n).match(/--\\s*(\\d+)\\s*$/) || [0, n])[1]) + ')' : l; }});
            return out;
        }}

        // ----- Facilities questions (added to the Ask panel) -----
        function askFacilities(metric) {{
            const g = facAggregate();
            if (g.tot.lines === 0) return askEmpty();
            if (!g.list.length) return {{headline: 'No facility names in the database yet. Run an API sync so the facility names are saved.', cols: [], rows: [], scope: askScope()}};
            const key = {{val: 'val', qty: 'qty', lines: 'lines'}}[metric];
            const top = g.list.slice().sort((a, b) => (b[key] - a[key]) || a.name.localeCompare(b.name)).slice(0, 10);
            const disp = facDisplay(top.map(a => a.name));
            const fm = (a) => key === 'val' ? aRwf(a.val) : (key === 'qty' ? formatNum(a.qty) + ' units' : aInt(a.lines) + ' lines');
            const note = g.unk.lines > 0 ? aInt(g.unk.lines) + ' missed lines (RWF ' + formatNum(g.unk.val) + ') have no facility name yet and are not ranked.' : '';
            return {{headline: aB(disp[top[0].name]) + ' is the facility with the most ' + {{val: 'missed value', qty: 'missed units', lines: 'short lines'}}[key] + ': ' + aB(fm(top[0])) + ' (' + aPct(aShare(top[0].val, g.tot.val)) + ' of all missed value).',
                cols: ['Facility', 'Branch', 'Missed value', 'Share of total', 'Missed units', 'Lines short'], barCol: key === 'val' ? 2 : (key === 'qty' ? 4 : 5), bars: top.map(a => top[0][key] > 0 ? a[key] / top[0][key] : 0),
                rows: top.map(a => [disp[a.name], facMain(a), aRwf(a.val), aPct(aShare(a.val, g.tot.val)), formatNum(a.qty), aInt(a.lines)]), note: note, scope: askScope()}};
        }}
        function askExtendGroups() {{
            if (ASK_BY_ID.f1) return;
            const g = {{id: 'facilities', title: 'Facilities', questions: [
                {{id: 'f1', text: 'Which facilities lost the most value?', kw: 'facility facilities customer hospital health centre clinic most lost value worst missed rwf', run: function() {{ return askFacilities('val'); }}}},
                {{id: 'f2', text: 'Which facilities were short on the most lines?', kw: 'facility facilities customer hospital health centre clinic lines short often frequent', run: function() {{ return askFacilities('lines'); }}}},
                {{id: 'f3', text: 'Which facilities missed the most units?', kw: 'facility facilities customer hospital health centre clinic units quantity volume', run: function() {{ return askFacilities('qty'); }}}}]}};
            g.questions.forEach(q => {{ q.group = g.title; ASK_BY_ID[q.id] = q; }});
            ASK_GROUPS.splice(2, 0, g);                       // after Branches
        }}

        function updateDashboard() {{
            syncSearch();

            // JS Instant Filter with strict String safety for searches
            // Automatically excludes Missed_Qty <= 0 for KPI accuracy
            const filtered = rawData.filter(r => {{
                if (r.Missed_Qty <= 0) return false;
                return matchRow(r);
            }});

            let totalVal = 0, ordQty = 0, delQty = 0, missQty = 0, missedLines = 0, totalLines = 0;
            let monthAgg = {{}};
            const branchAggr = {{}};
            const typeAggr = {{}};
            const itemAggr = {{}};
            const weekAggr = {{}};
            const weekLabels = {{}};
            
            // Re-calculate totals using ALL data (including Missed_Qty=0) so the KPIs are accurate
            const rawFiltered = rawData.filter(r => matchRow(r));

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

            try {{ updateExtras(); }} catch (e) {{ console.error('Extras error:', e); }}
        }}

        // ADDED: value-perspective cards, made-vs-missed charts, branch/month/large-order tables and extra insights.
        // Runs after the original updateDashboard() and only reads rawData/largeData, so the original visuals are never altered.
        function updateExtras() {{
            syncSearch();
            const hasV = ordValReady;
            const MADE_C = '#34d399', MISS_C = '#f48a6a', PURPLE_C = '#5a5ce0';
            const NA = '\u2014';
            const fmtInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
            const pctOf = (a, b) => b > 0 ? (a / b) * 100 : null;
            const pctTxt = (x) => x === null ? NA : x.toFixed(1) + '%';

            const pass = (r, useBranch) => matchRow(r, useBranch ? {{}} : {{branch: true}});
            const rows = rawData.filter(r => pass(r, true));
            const baseRows = FILT.branches.size === 0 ? rows : rawData.filter(r => pass(r, false));
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
            const monthRows = rawData.filter(r => matchRow(r, {{month: true, week: true, day: true, fy: true}}));
            const mo = {{}};
            monthRows.forEach(r => {{
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

            try {{ bindDrill(); bindLargeCard(); }} catch (e) {{ console.error('Drill-down bind error:', e); }}
            try {{ refreshAsk(); }} catch (e) {{ console.error('Ask panel error:', e); }}
            try {{ refreshMore(); }} catch (e) {{ console.error('Compare / facility error:', e); }}
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
            try {{ if (window.__drillCleanup) {{ window.__drillCleanup(); window.__drillCleanup = null; }} }} catch (x) {{}}
            ['drillMonthChart', 'drillProdChart', 'lgBranchChart', 'lgMonthChart'].forEach(id => {{ const e = document.getElementById(id); if (e) {{ try {{ Plotly.purge(e); }} catch (x) {{}} }} }});
            document.getElementById('drillBody').innerHTML = '';
        }}

        function openDrill(branch, ev) {{
            syncSearch();
            const hasV = ordValReady;
            const NA = '\u2014';
            const fmtInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
            const pctOf = (a, b) => b > 0 ? (a / b) * 100 : null;
            const pctTxt = (x) => x === null ? NA : x.toFixed(1) + '%';
            const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const shorten = (s) => s.length > 34 ? s.slice(0, 33) + '\u2026' : s;
            const MADE_C = '#34d399', MISS_C = '#f48a6a';

            const base = rawData.filter(r => matchRow(r, {{branch: true}}));
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
            rawData.filter(r => r.Branch === branch && matchRow(r, {{month: true, week: true, day: true, fy: true, branch: true}})).forEach(r => {{ const k = String(r.Date || '').slice(0, 7) || r.Month; if (!mo[k]) mo[k] = Object.assign(blank(), {{label: r.Month}}); add(mo[k], r); }});
            const mk = Object.keys(mo).sort();

            const pr = {{}};
            mine.forEach(r => {{
                if ((r.Missed_Qty || 0) <= 0) return;
                const k = r.Product_Desc || '(blank)';
                if (!pr[k]) pr[k] = {{lost: 0, units: 0, items: 0}};
                pr[k].lost += r.Lost_Value || 0; pr[k].units += r.Missed_Qty || 0; pr[k].items += r.Missed_Lines || 0;
            }});
            const prods = Object.keys(pr).sort((a, b) => pr[b].lost - pr[a].lost);

            const lg = largeData.filter(r => r.Branch === branch && matchRow(r, {{branch: true}})).sort((a, b) => b.Lost_Value - a.Lost_Value);

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
            document.getElementById('drillSub').textContent = describeFilters(false);
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
            syncSearch();
            const NA = '\u2014';
            const fmtInt = (n) => Number(n || 0).toLocaleString('en-US', {{maximumFractionDigits: 0}});
            const pctOf = (a, b) => b > 0 ? (a / b) * 100 : null;
            const pctTxt = (x) => x === null ? NA : x.toFixed(1) + '%';
            const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const MISS_C = '#f48a6a', PURPLE_C = '#5a5ce0';
            const pass = (r) => matchRow(r);
            const totMiss = rawData.filter(pass).reduce((a, r) => a + (r.Lost_Value || 0), 0);
            const lg = largeData.filter(pass).sort((a, b) => b.Lost_Value - a.Lost_Value);
            const lgSum = lg.reduce((a, r) => a + (r.Lost_Value || 0), 0);
            const lgUnits = lg.reduce((a, r) => a + (r.Missed_Qty || 0), 0);
            const byBranch = {{}}, byMonth = {{}};
            lg.forEach(r => {{
                byBranch[r.Branch] = (byBranch[r.Branch] || 0) + (r.Lost_Value || 0);
            }});
            largeData.filter(r => matchRow(r, {{month: true, week: true, day: true, fy: true}})).forEach(r => {{
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
            document.getElementById('drillSub').textContent = describeFilters(true);
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
        window.onload = loadAndStart;
    </script>
</body>
</html>
"""

# Render the HTML
components.html(html_template, height=4300, scrolling=True)
if _rms_first:
    st.session_state['rms_seen_dashboard'] = True      # the dashboard frame hides the logo screen itself the moment its own has painted (failsafe: it hides itself after 45 s)
else:
    _rms_wait.markdown(RMS_LOADER_GONE, unsafe_allow_html=True)

# ---------------------------------------------------------------------
# 5. BOTTOM ADMIN PANEL
# ---------------------------------------------------------------------
st.markdown("<div class='admin-section'>", unsafe_allow_html=True)
database.load_env_file()      # optional .env file next to app.py (for your own computer); real environment variables always win

if not st.session_state.logged_in:
    with st.expander("⚙️ Data Management & Settings (Admin Only)"):
        pwd = st.text_input("Admin Password", type="password", placeholder="Enter password...")
        if st.button("Login"):
            if pwd == os.environ.get("ADMIN_PASSWORD", "admin123"):
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
    

    # ---- Sync from the RMS Data Warehouse API (added) ----
    st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
    st.markdown("<h4 style='margin: 0 0 4px 0; color: #1e293b; font-family: Inter;'>Sync from RMS Data Warehouse (API)</h4>", unsafe_allow_html=True)
    _api = database.api_status()
    if not _api['configured']:
        st.warning("The API login is not set up yet. On Railway: open the Variables tab and add RMS_API_USER and RMS_API_PASSWORD. On your own computer: create a file named .env next to app.py with those two lines. Never put them in the code.")
    else:
        _last = database.last_sync()
        st.caption(f"Source: {_api['base']}  |  Login: {_api['user']} (from {_api['source']})  |  " + (f"Last sync: {_last}" if _last else "No sync yet"))
        # Normal use: refresh from the start of the latest month. If some older rows still have no order value, default to the
        # earliest date so ONE sync fills them all (the value cards need every row to have it).
        try:
            _from_default = pd.to_datetime(db_stats['max_date' if ORDVAL_READY else 'min_date']).date().replace(day=1)
        except Exception:
            _from_default = (date.today().replace(day=1) - timedelta(days=62)).replace(day=1)
        if not ORDVAL_READY and db_stats['rows']:
            st.info(f"Some older rows still have no order value, so the value cards show a dash. Run ONE sync that starts at {_from_default} (already filled in below) to fill them all.")
        # ---- Back-fill helper (added): ready-made ranges, put into From / To for you ----
        _pend = st.session_state.pop('bf_pending', None)
        if _pend:
            st.session_state['api_from'], st.session_state['api_to'] = _pend
        with st.expander("🧩 Back-fill helper: it fills the ranges in for you", expanded=bool(st.session_state.get('bf_open'))):
            _h1, _h2 = st.columns(2)
            _bf_start = _h1.date_input("Back-fill from", value=date(2025, 5, 1), key="bf_start")
            _bf_n = _h2.selectbox("Months per range", [1, 2, 3, 4, 6], index=2, key="bf_n")
            _plan = database.backfill_plan(_bf_start, _bf_n)
            _next_i = next((i for i, p in enumerate(_plan) if not p['done']), None)
            _lab = lambda i: f"{i + 1}. {_plan[i]['from']} to {_plan[i]['to']}   " + (f"✅ done ({_plan[i]['when']})" if _plan[i]['done'] else "⏳ not yet")
            if _plan:
                _pick = st.selectbox("Range", list(range(len(_plan))), index=_next_i if _next_i is not None else 0, format_func=_lab)
                def _bf_fill(a, b):
                    st.session_state['api_from'] = a
                    st.session_state['api_to'] = b
                st.button("➡️ Put this range into From / To", on_click=_bf_fill, args=(_plan[_pick]['from'], _plan[_pick]['to']),
                          help="Fills the From and To boxes below. Then click Sync from API.")
                st.checkbox("After one of these ranges is synced successfully, put the next one in automatically", value=True, key="bf_auto")
                _n_done = sum(1 for p in _plan if p['done'])
                st.caption(f"{_n_done} of {len(_plan)} ranges done." + (" All done." if _next_i is None else f" Next: {_plan[_next_i]['from']} to {_plan[_next_i]['to']}. Click the button, then Sync from API."))
            else:
                st.caption("Choose a start date in the past.")
        _c1, _c2 = st.columns(2)
        _d_from = _c1.date_input("From", value=_from_default, key="api_from")
        _d_to = _c2.date_input("To", value=date.today(), key="api_to")
        _b1, _b2, _b3 = st.columns(3)
        _go_test = _b1.button("🔌 Test connection")
        _go_preview = _b2.button("🔍 Preview (no changes)", help="Reads the API and compares it with the database. Nothing is saved.")
        _go_sync = _b3.button("⬇️ Sync from API", help="Saves the API data. Lines already in the database are updated, never duplicated.")
        if _go_test:
            with st.spinner("Contacting the API..."):
                st.session_state.api_out = {'kind': 'test', 'steps': database.api_test()}
        if _go_preview or _go_sync:
            _bar = st.progress(0.0, text="Starting...")
            def _cb(i, n, msg):
                _bar.progress(min(i / max(n, 1), 1.0), text=msg)
            try:
                _res = database.api_run(_d_from, _d_to, apply=_go_sync, progress=_cb)
                st.session_state.api_out = {'kind': 'sync' if _go_sync else 'preview', 'res': _res}
                if _go_sync and not _res['error'] and st.session_state.get('bf_auto', True):
                    # a back-fill range was just synced: put the next one that is not done into From / To (a normal sync of other dates changes nothing)
                    _pl = database.backfill_plan(st.session_state.get('bf_start', date(2025, 5, 1)), st.session_state.get('bf_n', 3))
                    if any(p['from'] == _d_from and p['to'] == _d_to for p in _pl):
                        _nx = next((p for p in _pl if not p['done']), None)
                        if _nx:
                            st.session_state['bf_pending'] = (_nx['from'], _nx['to'])
                        st.session_state['bf_open'] = True
            except database.ApiError as _e:
                st.session_state.api_out = {'kind': 'error', 'msg': str(_e)}
            except Exception as _e:
                st.session_state.api_out = {'kind': 'error', 'msg': f"Unexpected problem: {type(_e).__name__}"}
            _bar.empty()
            if _go_sync:
                st.cache_data.clear()
                st.rerun()
        _out = st.session_state.get('api_out')
        if _out:
            if _out['kind'] == 'error':
                st.error(_out['msg'])
            elif _out['kind'] == 'test':
                for _label, _ok, _detail in _out['steps']:
                    (st.success if _ok else st.error)(f"{_label}: {_detail}")
            else:
                _r = _out['res']
                _m = _r['months']
                _n = lambda v: f"{v:,.0f}"
                if _r['error']:
                    st.error(f"Stopped at {_r['error']}  (months before it were " + ("saved)" if _r['applied'] else "read only)"))
                if _m:
                    _tot = lambda k: sum(x[k] for x in _m)
                    if _r['applied']:
                        st.success(f"Synced {_n(_tot('api_lines'))} lines ({_n(_tot('inserted'))} new, {_n(_tot('updated'))} updated) for {_r['from']} to {_r['to']}.")
                    else:
                        st.info(f"PREVIEW, nothing was saved. API: {_n(_tot('api_lines'))} lines | database (same dates): {_n(_tot('db_lines'))} | already in database: {_n(_tot('matched'))} | new: {_n(_tot('new_in_api'))} | only in database: {_n(_tot('only_in_db'))} | confirmed qty differs on {_n(_tot('confirmed_differs'))} lines.")
                    _vals = _r.get('doctype_values') or []
                    _vtxt = (f" ({len(_vals)} values: " + ", ".join(_vals[:8]) + (" ..." if len(_vals) > 8 else "") + ")") if _vals else ""
                    st.caption(f"Document type taken from: {_r['doctype_source']}{_vtxt}  |  Confirmed quantity taken from API field: {_r['confirmed_field']}")
                    _ex = ", ".join(_r.get('extra_saved') or []) or "none"
                    _dy = _r.get('dyn_cols') or []
                    st.caption(("Also saved from the API: " if _r['applied'] else "Other fields the API sends (saved when you sync): ") + _ex + ((" | other columns, saved as API_...: " + ", ".join(_dy)) if _dy else "")
                               + f"  |  Lines with a facility name: {_n(_r.get('cust_named', 0))} of {_n(_r.get('cust_total', 0))}")
                    st.dataframe(pd.DataFrame([{
                        'Month': x['month'], 'API lines': _n(x['api_lines']), 'DB lines': _n(x['db_lines']), 'In both': _n(x['matched']),
                        'New in API': _n(x['new_in_api']), 'Only in DB': _n(x['only_in_db']), 'Confirmed qty differs': _n(x['confirmed_differs']),
                        'Missed qty (API)': _n(x['api_missed_qty']), 'Missed qty (DB)': _n(x['db_missed_qty']),
                        'Missed value (API)': _n(x['api_missed_value']), 'Missed value (DB)': _n(x['db_missed_value'])} for x in _m]), hide_index=True)
                for _w in _r['warnings']:
                    st.warning(_w)

    # ---- Automatic sync switch and status (added) ----
    if _api['configured']:
        _sch = database.scheduler_status()
        st.markdown("<hr style='margin:10px 0;'>", unsafe_allow_html=True)
        st.markdown("<h4 style='margin: 0 0 4px 0; color: #1e293b; font-family: Inter;'>Automatic sync</h4>", unsafe_allow_html=True)
        _hours = " and ".join(f"{h:02d}:00" for h in _sch['hours'])
        _box = st.checkbox(f"Run the API sync automatically every day at {_hours} (Rwanda time)", value=_sch['enabled'], disabled=_sch['forced'], key="auto_sync_box",
                           help="Tick this only after the Preview looks right. It re-reads the recent months from the API twice a day and updates the database.")
        if not _sch['forced'] and _box != _sch['enabled']:
            database.set_setting('auto_sync', 'on' if _box else 'off')
            st.rerun()
        if _sch['forced']:
            st.caption("This is set by the RMS_AUTO_SYNC variable, so the switch is locked.")
        if _sch['enabled']:
            st.caption(f"Next automatic run: {_sch['next_run']} (Kigali time)  |  Last automatic run: {_sch['last_auto'] or 'none yet'}")
            if not _sch['always_on']:
                st.caption("Timer note: it runs while the app is running. Start the app with  python run.py  (Railway start command) so it also runs when nobody has the page open; otherwise a missed run is caught up at the next visit.")
        else:
            st.caption("Automatic sync is OFF.")
    
    if st.button("🔄 Refresh Dashboard Data"):
        st.rerun()

st.markdown("</div>", unsafe_allow_html=True)