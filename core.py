# core.py
import os
import time
import pandas as pd
from typing import Dict, Tuple, Optional
import openrouteservice as ors
from openrouteservice import directions

def normalize_postal(pc: str) -> str:
    """
    Return a canonical Canadian postal code ('ABC 123') found anywhere in the string.
    - Finds the first A#A#A# pattern inside messy text (e.g., 'V6T 1Z4, Canada')
    - Uppercases, strips non-alphanumerics, and inserts the space
    """
    if not isinstance(pc, str):
        return ""
    s = pc.upper()

    # Keep only letters/digits for scanning, but remember original for regex
    import re
    # Look for a postal code pattern anywhere in the string (allow spaces/dashes in between)
    # e.g., V6T 1Z4, V6T-1Z4, V6T1Z4
    m = re.search(r'([A-Z]\s*[\d]\s*[A-Z]\s*[\d]\s*[A-Z]\s*[\d])', s)
    if not m:
        return ""

    # Collapse to alphanumerics and format as 'ABC 123'
    alnum = "".join(ch for ch in m.group(1) if ch.isalnum())
    if len(alnum) != 6:
        return ""
    return alnum[:3] + " " + alnum[3:]


def build_postal_lookup_from_df(df: pd.DataFrame,
                                postal_col: str,
                                lat_col: str,
                                lon_col: str) -> Dict[str, Tuple[float, float]]:
    import re
    m: Dict[str, Tuple[float, float]] = {}

    def parse_float(x) -> Optional[float]:
        if pd.isna(x):
            return None
        s = str(x).strip()
        # first signed float in the string
        mo = re.search(r'[-+]?\d+(?:\.\d+)?', s)
        return float(mo.group(0)) if mo else None

    for row in df.itertuples(index=False):
        pc_raw = getattr(row, postal_col, None)
        lat_raw = getattr(row, lat_col, None)
        lon_raw = getattr(row, lon_col, None)

        # Build robust keys
        raw = "" if pc_raw is None else str(pc_raw)
        alnum = "".join(ch for ch in raw.upper() if ch.isalnum())
        if not alnum:
            continue

        # Two interchangeable keys
        key_spaced = alnum[:3] + " " + alnum[3:] if len(alnum) == 6 else alnum
        key_nospace = alnum

        lat = parse_float(lat_raw)
        lon = parse_float(lon_raw)

        if lat is None or lon is None:
            continue

        # Store both variants → lookups succeed whether caller uses space or not
        m[key_spaced] = (lat, lon)
        m[key_nospace] = (lat, lon)

    return m


def get_ors_client(api_key: Optional[str] = None) -> ors.Client:
    api_key = api_key or os.environ.get("ORS_API_KEY", "")
    if not api_key:
        raise RuntimeError("OpenRouteService API key is missing.")
    return ors.Client(key=api_key)

def route_km_via_ors(client: ors.Client,
                     origin_lonlat: Tuple[float, float],
                     dest_lonlat: Tuple[float, float]) -> float:
    # ORS expects (lon, lat) pairs
    res = directions.directions(
        client=client,
        coordinates=[origin_lonlat, dest_lonlat],
        profile="driving-car",
        format="geojson",
        units="km",
    )
    return float(res["features"][0]["properties"]["segments"][0]["distance"])

def load_builtin_gazetteer(
    paths=(
        "data/ca_postals.csv.gz.csv",  # POSIX
        "data/ca_postals.csv.gz",
        "data/ca_postals.csv",
        r"data\ca_postals.csv.gz.csv", # Windows variants (in case you run locally)
        r"data\ca_postals.csv.gz",
        r"data\ca_postals.csv",
    )
) -> Dict[str, Tuple[float, float]]:

    """
    Load a built-in Canadian postal gazetteer from one of the given paths.
    Uses a tolerant CSV reader and then our robust builder.
    """
    import pandas as pd
    last_err = None
    for p in paths:
        try:
            # tolerant reader: infer compression, allow messy lines/encodings
            df = pd.read_csv(p, engine="python", on_bad_lines="skip")
            # If lat/lon are strings with junk, build_postal_lookup_from_df cleans them
            # Try common column names first
            cols = {c.lower(): c for c in df.columns}
            postal_col = cols.get("postal") or cols.get("postal_code") or list(df.columns)[0]
            lat_col    = cols.get("lat")    or cols.get("latitude")    or list(df.columns)[1]
            lon_col    = cols.get("lon")    or cols.get("lng") or cols.get("longitude") or list(df.columns)[2]
            return build_postal_lookup_from_df(df, postal_col, lat_col, lon_col)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not load built-in gazetteer from {paths}. Last error: {last_err}")



def process_dataframe(df: pd.DataFrame,
                      study_id_col: str,
                      postal_col: str,
                      origin_lon: float,
                      origin_lat: float,
                      postal_lookup: Dict[str, Tuple[float, float]],
                      api_key: str,
                      sleep_s: float = 1.0) -> pd.DataFrame:
    # Tolerant to weird header names (spaces, duplicates, etc.)
    study_id_col = str(study_id_col).strip()
    postal_col = str(postal_col).strip()

    client = get_ors_client(api_key)
    origin = (float(origin_lon), float(origin_lat))  # (lon, lat)

    out_rows = []
    for idx, row in df.iterrows():
        rec = row.to_dict()

        # Always fetch by the column LABEL (not tuple attribute names like _0/_1)
        postal_val = rec.get(postal_col, None)
        pc = normalize_postal("" if postal_val is None else str(postal_val))

        error, dist_km = "", None
        try:
            if pc and pc in postal_lookup:
                lat, lon = postal_lookup[pc]      # stored as (lat, lon)
                dest = (float(lon), float(lat))   # ORS wants (lon, lat)
                dist_km = route_km_via_ors(client, origin, dest)
            else:
                error = "Invalid Postal Code"
        except Exception as e:
            error = f"Routing error: {e}"

        rec["distance_km"] = dist_km
        rec["error"] = error
        out_rows.append(rec)

        time.sleep(max(0.0, sleep_s))  # polite pacing
    return pd.DataFrame(out_rows)


