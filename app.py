# app.py
import os
import io
import pandas as pd
import streamlit as st
from core import (
    build_postal_lookup_from_df,
    process_dataframe,
    load_gazetteer_subset,  # <-- new: memory-light built-in loader
    normalize_postal,
)

st.set_page_config(page_title="Driving Distance Helper", layout="wide")
st.title("Driving Distance Helper")
st.markdown("Upload your study CSV, map the columns, set the origin, and download distances.")

# --- STEP 1: OpenRouteService API key (explicit paste-in) ---
st.markdown("### Step 1: Add your OpenRouteService (ORS) API key")
if "show_ors_help" not in st.session_state:
    st.session_state["show_ors_help"] = False

col_api, col_help = st.columns([0.7, 0.3])
ors_api_key = col_api.text_input(
    "Paste your ORS API key here",
    type="password",
    placeholder="sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    help="Required to calculate driving distances (routing).",
)
if col_help.button("I don't know what this is or how to get one"):
    st.session_state["show_ors_help"] = True

if not ors_api_key:
    st.error("Step 1 required: paste your ORS API key above to enable routing.")

if st.session_state["show_ors_help"]:
    st.markdown("#### How to get an OpenRouteService API key")
    st.markdown("""
1. Open **openrouteservice.org** and create a free account.
2. After logging in, go to **Dashboard → API Keys**.
3. Click **Create API Key**, copy the key.
4. Return here and paste it into the field above.

**Direct links:**
- Sign up: https://openrouteservice.org/sign-up/
- Developer dashboard: https://openrouteservice.org/dev/#/home
    """)
    if st.button("Close help"):
        st.session_state["show_ors_help"] = False

with st.expander("Advanced settings", expanded=False):
    sleep_s = st.slider("Delay between requests (seconds)", 0.0, 2.0, 1.0, 0.1)

# --- STEP 2: Upload study CSV ---
st.subheader("1) Upload STUDY CSV")
up = st.file_uploader("Choose your study CSV", type=["csv"])
if not up:
    st.stop()

try:
    df = pd.read_csv(up)
except Exception:
    up.seek(0)
    df = pd.read_csv(up, encoding="latin-1")

st.write("Preview")
st.dataframe(df.head(20), use_container_width=True)

# --- STEP 3: Map columns ---
st.subheader("2) Map columns")
study_id_col = st.selectbox("Study ID column", list(df.columns))
postal_col = st.selectbox("Postal code column", list(df.columns))

# --- STEP 4: Origin (postal or coordinates) ---
st.subheader("3) Origin")
origin_mode = st.radio(
    "How do you want to enter the origin?",
    options=["Postal code (recommended)", "Coordinates (advanced)"],
    index=0,
    horizontal=True,
)

origin_lon = None
origin_lat = None
origin_pc_raw = ""

if origin_mode == "Postal code (recommended)":
    origin_pc_raw = st.text_input("Origin postal code (e.g., V6T 1Z4)").strip()
    if origin_pc_raw:
        # We'll resolve it *after* we have a lookup (subset or uploaded)
        st.caption("Origin will be resolved after the gazetteer is prepared.")
    else:
        st.caption("Tip: enter a postal code to resolve origin automatically.")
else:
    c1, c2 = st.columns(2)
    origin_lon = c1.number_input("Origin longitude", value=-119.493700, format="%.6f")
    origin_lat = c2.number_input("Origin latitude", value=49.888000, format="%.6f")
    st.caption("Advanced: enter coordinates directly (order is longitude, then latitude).")

# --- STEP 5: Gazetteer selection ---
st.subheader("4) Gazetteer (Postal Code Map)")

if "postal_lookup" not in st.session_state:
    st.session_state["postal_lookup"] = {}
if "gaz_mode" not in st.session_state:
    st.session_state["gaz_mode"] = None  # "builtin" or "upload"

c1, c2 = st.columns(2)
use_builtin_clicked = c1.button("Use built-in postal codes", type="primary")
use_upload_clicked  = c2.button("Upload your own postal codes")

if use_builtin_clicked:
    st.session_state["gaz_mode"] = "builtin"
    st.session_state["postal_lookup"] = {}  # lazy-load on Compute
if use_upload_clicked:
    st.session_state["gaz_mode"] = "upload"
    st.session_state["postal_lookup"] = {}

# Built-in: do NOT load now (to save memory). We'll load a subset at Compute time.
if st.session_state["gaz_mode"] == "builtin":
    st.info("Built-in gazetteer selected. A memory-light subset will be prepared when you click **Compute**.")

# Upload mode: build small lookup from the user's gazetteer now
elif st.session_state["gaz_mode"] == "upload":
    gaz = st.file_uploader("Upload gazetteer CSV (columns like: postal, lat, lon)", type=["csv"], key="gaz")
    if gaz:
        gdf = pd.read_csv(gaz, engine="python", on_bad_lines="skip")
        st.write("Gazetteer preview")
        st.dataframe(gdf.head(10), use_container_width=True)

        guess_postal = next((c for c in gdf.columns if "postal" in c.lower()), gdf.columns[0])
        guess_lat    = next((c for c in gdf.columns if "lat" in c.lower()),    gdf.columns[1])
        guess_lon    = next((c for c in gdf.columns if "lon" in c.lower() or "lng" in c.lower()), gdf.columns[2])

        cc1, cc2, cc3 = st.columns(3)
        col_post = cc1.selectbox("Postal column", list(gdf.columns), index=list(gdf.columns).index(guess_postal))
        col_lat  = cc2.selectbox("Latitude column", list(gdf.columns), index=list(gdf.columns).index(guess_lat))
        col_lon  = cc3.selectbox("Longitude column", list(gdf.columns), index=list(gdf.columns).index(guess_lon))

        if st.button("Build gazetteer map (from upload)"):
            st.session_state["postal_lookup"] = build_postal_lookup_from_df(gdf, col_post, col_lat, col_lon)
            st.success(f"Loaded {len(st.session_state['postal_lookup'])//2} unique postal codes from uploaded file.")

# Status line
if st.session_state["postal_lookup"]:
    sample_keys = list(st.session_state["postal_lookup"].keys())[:5]
    mode = st.session_state["gaz_mode"] or "none"
    st.caption(f"Gazetteer mode: {mode}. Example keys: {sample_keys}")
else:
    st.info("Choose **Use built-in postal codes** or **Upload your own postal codes** to continue.")

# --- STEP 6: Compute distances ---
st.subheader("5) Compute distances")
run = st.button("Compute", type="primary")
if run:
    # 1) ORS key must be pasted (no env fallback to avoid burning your quota)
    key = (ors_api_key or "").strip()
    if not key:
        st.error("OpenRouteService key missing. Paste your own key above to enable routing.")
        st.stop()

    # 2) If using built-in and no lookup yet, build a subset just for the rows we need
    if not st.session_state.get("postal_lookup"):
        if st.session_state.get("gaz_mode") == "builtin":
            st.info("Preparing a memory-light gazetteer subset (only the postals in your CSV, plus origin if provided)...")

            # Normalize & de-duplicate needed postals from the study CSV
            def norm_any(s: str) -> str:
                s = "" if s is None else str(s).upper()
                m = re.search(r'([A-Z]\s*[\d]\s*[A-Z]\s*[\d]\s*[A-Z]\s*[\d])', s)
                if not m:
                    return ""
                alnum = "".join(ch for ch in m.group(1) if ch.isalnum())
                return (alnum[:3] + " " + alnum[3:]) if len(alnum) == 6 else ""

            needed = set(df[postal_col].astype(str).map(norm_any).tolist())

            # include origin postal if user typed one
            if origin_pc_raw:
                needed.add(origin_pc_raw)

            try:
                subset = load_gazetteer_subset(needed_postals=needed)
                if not subset:
                    st.error("Couldn’t find any of the needed postal codes in the built-in gazetteer.")
                    st.stop()
                st.session_state["postal_lookup"] = subset
                st.success(f"Prepared gazetteer subset with ~{len(subset)//2} unique postal codes.")
            except Exception as e:
                st.error(f"Failed to prepare gazetteer subset: {e}")
                st.stop()
        else:
            st.error("Postal lookup not built yet. Click **Use built-in postal codes** or **Upload your own postal codes**.")
            st.stop()

    # 3) Ensure origin coordinates; if user gave a postal, resolve it now using the (subset/uploaded) gazetteer
    if origin_lon is None or origin_lat is None:
        if origin_pc_raw:
            pc_norm = normalize_postal(origin_pc_raw)
            latlon = st.session_state["postal_lookup"].get(pc_norm) or st.session_state["postal_lookup"].get(pc_norm.replace(" ", ""))
            if latlon:
                origin_lat, origin_lon = float(latlon[0]), float(latlon[1])
                st.success(f"Origin resolved → lat={origin_lat:.6f}, lon={origin_lon:.6f}")
        if origin_lon is None or origin_lat is None:
            st.error("Origin is not set. Enter a postal code (recommended) or coordinates.")
            st.stop()

    # ---- Pre-flight diagnostics: how many study postals will match? ----
    def norm_any_pre(s: str) -> str:
        s = "" if s is None else str(s).upper()
        m = re.search(r'([A-Z]\s*[\d]\s*[A-Z]\s*[\d]\s*[A-Z]\s*[\d])', s)
        if not m:
            return ""
        alnum = "".join(ch for ch in m.group(1) if ch.isalnum())
        return (alnum[:3] + " " + alnum[3:]) if len(alnum) == 6 else ""

    lkp = st.session_state.get("postal_lookup", {})
    study_norm = df[postal_col].astype(str).map(norm_any_pre)
    missing_mask = ~study_norm.isin(lkp.keys())
    missing_count = int(missing_mask.sum())
    total_rows = len(df)
    st.info(f"Pre-flight: {total_rows - missing_count}/{total_rows} rows will match the gazetteer.")
    if missing_count:
        examples = df.loc[missing_mask, postal_col].astype(str).head(10).tolist()
        st.warning(f"First {min(10, missing_count)} unmatched examples: {examples}")

    # ---- Routing ----
    with st.spinner("Routing..."):
        out = process_dataframe(
            df=df,
            study_id_col=study_id_col,
            postal_col=postal_col,
            origin_lon=float(origin_lon),
            origin_lat=float(origin_lat),
            postal_lookup=st.session_state["postal_lookup"],
            api_key=key,
            sleep_s=sleep_s,
        )

    st.success("Done.")
    st.dataframe(out.head(30), use_container_width=True)

    buf = io.StringIO()
    out.to_csv(buf, index=False)
    st.download_button("Download results CSV", buf.getvalue(), file_name="distance_results.csv", mime="text/csv")

# --- Footer ---
st.markdown("---")
st.markdown(
    """
**Disclaimer:**  
This tool is provided for research purposes only. Accuracy depends on postal code centroids,
OpenStreetMap data, and the OpenRouteService API. While the developers do not intentionally collect
any data, third parties (e.g., API providers or network services) may collect usage data unbeknownst
to us. Please verify critical distances independently before making clinical, operational, or policy decisions.

**Citation:**  
*Kieran Chalmers, Adela Gottardi, David Gottardi, Murray Chalmers.*  
**CanMapDistance: A Canadian Postal Code Driving Distance Calculator.**  
Created August 2025.
"""
)
st.caption("Thanks for using the app!")
