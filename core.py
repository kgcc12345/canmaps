# core.py
import os
import time
import pandas as pd
from typing import Dict, Tuple, Optional
import openrouteservice as ors
from openrouteservice import directions
import re


def normalize_postal(pc: str) -> str:
    """
    Return a canonical Canadian postal code 'ABC 123' found anywhere in the string.
    - Finds the first A#A#A# pattern inside messy text (e.g., 'V6T 1Z4, Canada')
    - Uppercases, strips non-alphanumerics, inserts the space.
    """
    if not isinstance(pc, str):
        return ""
    s = pc.upper()
    m = re.search(r'([A-Z]\s*[\d]\s*[A-Z]\s*[\d]\s*[A-Z]\s*[\d])', s)
    if not m:
        return ""
    alnum = "".join(ch for ch in m.group(1) if ch.isalnum())
    if len(alnum) != 6:
        return ""
    return alnum[:3] + " " + alnum[3:]


def build_postal_lookup_from_df(df: pd.DataFrame,
                                postal_col: str,
                                lat_col: str,
                                lon_col: str) -> Dict[str, Tuple[float, float]]:
    """
    Build a dict { 'ABC 123': (lat, lon), 'ABC123': (lat, lon) } from an uploaded gazetteer.
    """
    m: Dict[str, Tuple[float, float]] = {}

    def parse_float(x) -> Optional[float]:
        if pd.isna(x):
            return None
        s = str(x).strip()
        mo = re.search(r'[-+]?\d+(?:\.\d+)?', s)
        return float(mo.group(0)) if mo else None

    for row in df.itertuples(index=False):
        pc_raw = getattr(row, postal_col, None)
        lat_raw = getattr(row, lat_col, None)
        lon_raw = getattr(row, lon_col, None)

        raw = "" if pc_raw is None else str(pc_raw)
        alnum = "".join(ch for ch in raw.upper() if ch.isalnum())
        if not alnum:
            continue

        key_spaced = alnum[:3] + " " + alnum[3:] if len(alnum) == 6 else alnum
        key_nospace = alnum

        lat = parse_float(lat_raw)
        lon = parse_float(lon_raw)
        if lat is None or lon is None:
            continue

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
    """
    Call ORS driving-car, return distance in km.
    ORS expects (lon, lat) pairs.
    """
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
        # repo root
        "ca_postals.csv.gz", "ca_postals.csv",
        r"ca_postals.csv.gz", r"ca_postals.csv",
        # data/ subfolder
        "data/ca_postals.csv.gz.csv",
        "data/ca_postals.csv.gz",
        "data/ca_postals.csv",
        r"data\ca_postals.csv.gz.csv",
        r"data\ca_postals.csv.gz",
        r"data\ca_postals.csv",
    )
) -> Dict[str, Tuple[float, float]]:
    """
    FULL in-memory gazetteer loader (kept for uploads/local use).
    Avoid calling this on Render free tier (may exceed memory).
    """
    last_err = None
    for p in paths:
        try:
            df = pd.read_csv(p, engine="python", on_bad_lines="skip")
            cols = {c.lower(): c for c in df.columns}
            postal_col = cols.get("postal") or cols.get("postal_code") or list(df.columns)[0]
            lat_col    = cols.get("lat")    or cols.get("latitude")    or list(df.columns)[1]
            lon_col    = cols.get("lon")    or cols.get("lng") or cols.get("longitude") or list(df.columns)[2]
            return build_postal_lookup_from_df(df, postal_col, lat_col, lon_col)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not load built-in gazetteer from {paths}. Last error: {last_err}")


def load_gazetteer_subset(
    needed_postals: set,
    paths=(
        # repo root
        "ca_postals.csv.gz", "ca_postals.csv",
        r"ca_postals.csv.gz", r"ca_postals.csv",
        # data/ subfolder
        "data/ca_postals.csv.gz.csv",
        "data/ca_postals.csv.gz",
        "data/ca_postals.csv",
        r"data\ca_postals.csv.gz.csv",
        r"data\ca_postals.csv.gz",
        r"data\ca_postals.csv",
    ),
    chunksize: int = 200_000,
) -> Dict[str, Tuple[float, float]]:
    """
    Memory-light loader: scan the large gazetteer in chunks and only keep rows
    whose normalized postal is in `needed_postals`. Returns a small dict mapping
    both 'ABC 123' and 'ABC123' to (lat, lon).
    """
    # Normalize the target set once
    needed_norm = set()
    for pc in needed_postals:
        pc_n = normalize_postal(str(pc))
        if pc_n:
            needed_norm.add(pc_n)
            needed_norm.add(pc_n.replace(" ", ""))  # also accept nospace variant

    if not needed_norm:
        return {}

    last_err = None
    for p in paths:
        try:
            # Read header to detect columns once
            hdr = pd.read_csv(p, nrows=0)
            cols = {c.lower(): c for c in hdr.columns}
            postal_col = cols.get("postal") or cols.get("postal_code") or list(hdr.columns)[0]
            lat_col    = cols.get("lat")    or cols.get("latitude")    or list(hdr.columns)[1]
            lon_col    = cols.get("lon")    or cols.get("lng") or cols.get("longitude") or list(hdr.columns)[2]

            m: Dict[str, Tuple[float, float]] = {}
            # Stream the file in chunks to keep memory small
            for chunk in pd.read_csv(
                p,
                usecols=[postal_col, lat_col, lon_col],
                engine="python",
                on_bad_lines="skip",
                chunksize=chunksize,
            ):
                for row in chunk.itertuples(index=False):
                    raw = "" if getattr(row, postal_col) is None else str(getattr(row, postal_col))
                    key = normalize_postal(raw)
                    if not key:
                        continue
                    if (key not in needed_norm) and (key.replace(" ", "") not in needed_norm):
                        continue
                    # robust float parsing
                    lat_s = str(getattr(row, lat_col)).strip()
                    lon_s = str(getattr(row, lon_col)).strip()
                    try:
                        latf = float(re.search(r'[-+]?\d+(?:\.\d+)?', lat_s).group(0))
                        lonf = float(re.search(r'[-+]?\d+(?:\.\d+)?', lon_s).group(0))
                    except Exception:
                        continue
                    m[key] = (latf, lonf)
                    m[key.replace(" ", "")] = (latf, lonf)

                # Early exit if we've found everything
                if all((k in m) or (k.replace(" ", "") in m) for k in needed_norm):
                    break
            return m
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not build subset gazetteer from {paths}. Last error: {last_err}")


def process_dataframe(df: pd.DataFrame,
                      study_id_col: str,
                      postal_col: str,
                      origin_lon: float,
                      origin_lat: float,
                      postal_lookup: Dict[str, Tuple[float, float]],
                      api_key: str,
                      sleep_s: float = 1.0) -> pd.DataFrame:
    """
    For each row, normalize the destination postal, look up lat/lon from the
    (subset) gazetteer, route via ORS, and return a new DataFrame.
    """
    study_id_col = str(study_id_col).strip()
    postal_col = str(postal_col).strip()

    client = get_ors_client(api_key)
    origin = (float(origin_lon), float(origin_lat))  # (lon, lat)

    out_rows = []
    for _, row in df.iterrows():
        rec = row.to_dict()
        postal_val = rec.get(postal_col, None)
        pc = normalize_postal("" if postal_val is None else str(postal_val))

        error, dist_km = "", None
        try:
            if pc and pc in postal_lookup:
                lat, lon = postal_lookup[pc]      # (lat, lon) stored
                dest = (float(lon), float(lat))   # ORS expects (lon, lat)
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
