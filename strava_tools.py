# Utility functions for interacting with the Strava API, managing tokens, and
# processing activity data into a shoe-league table.
#
# Activity / gear / league files all live under <script_dir>/data/.

import os
import csv
import glob
import json
import time
import sqlite3
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

import pandas as pd
import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strava_tokens.db")


def _data_dir(script_dir: str) -> str:
    """Return (and create) the data directory used for all per-athlete files."""
    d = os.path.join(script_dir, "data")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Token storage (SQLite)
# --------------------------------------------------------------------------- #

def init_db(db_path):
    """Initialize the SQLite database for storing Strava tokens."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                athlete_id INTEGER PRIMARY KEY,
                raw_json TEXT NOT NULL
            )
        """)
    conn.close()


def save_token(token_json):
    """Save the Strava token for the athlete (keyed by athlete id)."""
    athlete_id = token_json.get("athlete", {}).get("id")
    if athlete_id is None:
        raise ValueError("Invalid token JSON: missing athlete ID.")

    conn = sqlite3.connect(DB_PATH)
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO tokens (athlete_id, raw_json)
            VALUES (?, ?)
        """, (athlete_id, json.dumps(token_json)))
    conn.close()


def load_token_for_athlete(athlete_id):
    """Load the full saved Strava token dict for the specified athlete."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT raw_json FROM tokens WHERE athlete_id = ?", (athlete_id,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def refresh_token_if_needed(token_info):
    """Refresh the Strava token if it is expired (placeholder — returns as-is)."""
    return token_info


# --------------------------------------------------------------------------- #
# Activity / gear file helpers
# --------------------------------------------------------------------------- #

def latest_activities_path(script_dir: str, athlete_id: Optional[int] = None) -> Optional[str]:
    """
    If athlete_id is given, return data/activities_{athlete_id}.json (may not exist).
    Otherwise return the most recent data/activities_*.json, or None.
    """
    data_dir = _data_dir(script_dir)
    if athlete_id is not None:
        return os.path.join(data_dir, f"activities_{athlete_id}.json")

    files = glob.glob(os.path.join(data_dir, "activities_*.json"))
    if files:
        files.sort(key=os.path.getmtime, reverse=True)
        return files[0]
    return None


def save_activities_for_athlete(athlete_id: int, activities: List[Dict[str, Any]], script_dir: str) -> str:
    """Save activities to data/activities_{athlete_id}.json and return the path."""
    data_dir = _data_dir(script_dir)
    path = os.path.join(data_dir, f"activities_{athlete_id}.json")
    safe_acts = []
    for a in activities:
        a = dict(a)  # copy so we don't mutate the caller's data
        a["athlete_id"] = athlete_id
        safe_acts.append(a)
    pd.DataFrame(safe_acts).to_json(path, orient="records", date_format="iso")
    return path


def extract_gear_ids_from_activities(activities_path, gear_ids_path):
    """
    Read activities JSON and write a JSON list of gear ids.
    Defensive: activities can be dicts missing expected keys or nested gear objects.
    """
    with open(activities_path, "r") as f:
        activities = json.load(f)

    gear_ids = set()
    for idx, activity in enumerate(activities):
        try:
            gear_id = activity.get("gear_id") if isinstance(activity, dict) else None

            if not gear_id:
                gear = activity.get("gear") if isinstance(activity, dict) else None
                if isinstance(gear, dict):
                    gear_id = gear.get("id") or gear.get("uid") or gear.get("gear_id")

            if not gear_id and isinstance(activity, dict):
                for v in activity.values():
                    if isinstance(v, dict) and ("id" in v and "name" in v):
                        gear_id = v.get("id")
                        break

            if gear_id:
                gear_ids.add(str(gear_id))
        except Exception as e:
            print(f"[extract_gear_ids] Warning: failed for activity index {idx}: {e}")

    with open(gear_ids_path, "w") as f:
        json.dump(sorted(list(gear_ids)), f, indent=2)
    print(f"Extracted {len(gear_ids)} gear IDs from activities. Written to {gear_ids_path}")


def fetch_gear_details(gear_ids_path, all_gear_path, access_token: Optional[str] = None):
    """
    Fetch gear details from Strava for each id in gear_ids_path.
    Defensive per-id; writes whatever successful responses we get.
    """
    with open(gear_ids_path, "r") as f:
        gear_ids = json.load(f)

    all_gear = []
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    if not access_token:
        print("[fetch_gear_details] Warning: no access token; gear endpoint may fail.")

    for idx, raw_gid in enumerate(gear_ids):
        try:
            gid = raw_gid
            if isinstance(gid, dict):
                gid = gid.get("id") or gid.get("uid") or str(gid)
            gid = str(gid)
            if not gid:
                continue

            try:
                r = requests.get(f"https://www.strava.com/api/v3/gear/{gid}", headers=headers, timeout=15)
            except Exception as e:
                print(f"[fetch_gear_details] Network error for gear id {gid}: {e}")
                continue

            if r.ok:
                try:
                    gear_obj = r.json()
                    if not isinstance(gear_obj, dict) or "id" not in gear_obj:
                        if isinstance(gear_obj, dict):
                            gear_obj["id"] = gid
                        else:
                            gear_obj = {"id": gid, "raw": gear_obj}
                    all_gear.append(gear_obj)
                except Exception as e:
                    print(f"[fetch_gear_details] Failed to decode JSON for gear {gid}: {e}")
            else:
                print(f"[fetch_gear_details] Strava returned {r.status_code} for gear {gid}")
        except Exception as e:
            print(f"[fetch_gear_details] Unexpected error at index {idx}: {e}")

    with open(all_gear_path, "w") as f:
        json.dump(all_gear, f, indent=2)
    print(f"Wrote details for {len(all_gear)} gear items to {all_gear_path}")


def combine_shoes(all_gear_path, activities_path, output_csv):
    """
    Build the shoe-league CSV from fetched gear details + activities.
    Produces real shoe names, retired status, first-use date and per-shoe stats.
    """
    with open(all_gear_path, "r") as f:
        all_gear = json.load(f)
    with open(activities_path, "r") as f:
        activities = json.load(f)

    # Only shoes from all_gear (gear ids start with 'g'; bikes start with 'b')
    shoes = {g["id"]: g for g in all_gear if str(g.get("id", "")).startswith("g")}

    shoe_stats = {}
    for shoe_id, shoe in shoes.items():
        shoe_stats[shoe_id] = {
            "name": shoe.get("name", "Unknown"),
            "retired": shoe.get("retired", False),
            "longest_run": 0,
            "total_distance": 0,
            "total_elevation_gain": 0,
            "activity_count": 0,
            "average_run_length": 0,
            "total_time": 0,
            "average_pace": 0,
            "first_use": None,
        }

    for activity in activities:
        gear_id = activity.get("gear_id")
        if gear_id in shoe_stats:
            dist = activity.get("distance", 0)
            elev = activity.get("total_elevation_gain", 0)
            moving = activity.get("moving_time", 0)
            date_str = activity.get("start_date_local", activity.get("start_date"))
            try:
                date_obj = datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                date_obj = None
            s = shoe_stats[gear_id]
            s["total_distance"] += dist
            s["total_elevation_gain"] += elev
            s["longest_run"] = max(s["longest_run"], dist)
            s["activity_count"] += 1
            s["total_time"] += moving
            if date_obj and (s["first_use"] is None or date_obj < s["first_use"]):
                s["first_use"] = date_obj

    for stats in shoe_stats.values():
        if stats["activity_count"] > 0:
            stats["average_run_length"] = stats["total_distance"] / stats["activity_count"]
        if stats["total_distance"] > 0:
            stats["average_pace"] = (stats["total_time"] / 60) / (stats["total_distance"] / 1000)
        stats["first_use_str"] = stats["first_use"].strftime("%b %Y") if stats["first_use"] else "-"

    with open(output_csv, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            "Shoe", "gear_id", "Retired", "Runs", "First Use", "Longest Run (km)",
            "Total Distance (km)", "Total Elevation Gain (km)",
            "Average Run Length (km)", "Total Time (h)", "Average Pace (min/km)"
        ])
        for shoe_id, stats in shoe_stats.items():
            avg_pace_str = (
                f"{int(stats['average_pace']):02d}:{int((stats['average_pace'] % 1) * 60):02d}"
                if stats["average_pace"] > 0 else "-"
            )
            retired_str = "Yes" if stats.get("retired") else "No"
            writer.writerow([
                stats["name"],
                shoe_id,
                retired_str,
                stats["activity_count"],
                stats["first_use_str"],
                round(stats["longest_run"] / 1000, 1),
                round(stats["total_distance"] / 1000, 1),
                round(stats["total_elevation_gain"] / 1000, 1),
                round(stats["average_run_length"] / 1000, 1),
                round(stats["total_time"] / 3600),
                avg_pace_str,
            ])
    print(f"League table saved to {output_csv}")


# --------------------------------------------------------------------------- #
# League computation (fallback when gear details aren't available)
# --------------------------------------------------------------------------- #

def secs_to_minsec_str(s):
    try:
        s = float(s)
        if s <= 0:
            return "-"
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        if h > 0:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"
    except Exception:
        return "-"


def _compute_shoe_league_internal(runs_df: pd.DataFrame, shoe_lookup_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    cols = [
        "gear_id", "Runs", "Total Distance (km)", "Total Elevation Gain (km)",
        "Average Run Length (km)", "Longest Run (km)", "Total Time (h)", "Average Pace (min/km)"
    ]
    if runs_df is None or runs_df.empty:
        return pd.DataFrame(columns=cols + ["Shoe"])

    r = runs_df.copy()
    r["distance_km"] = pd.to_numeric(r.get("distance", r.get("distance_km", 0)), errors="coerce") / 1000.0
    r["elev_m"] = pd.to_numeric(r.get("total_elevation_gain", 0), errors="coerce").fillna(0)
    r["moving_time_s"] = pd.to_numeric(r.get("moving_time", 0), errors="coerce").fillna(0)
    if "gear_id" not in r.columns:
        r["gear_id"] = ""
    if "name" not in r.columns:
        r["name"] = ""
    agg = (
        r.groupby("gear_id")
        .agg(
            Runs=("name", "count"),
            Total_Distance_km=("distance_km", "sum"),
            Total_Elevation_m=("elev_m", "sum"),
            Longest_Run_km=("distance_km", "max"),
            Total_Time_s=("moving_time_s", "sum"),
        )
        .reset_index()
    )
    agg["Average Run Length (km)"] = (agg["Total_Distance_km"] / agg["Runs"]).fillna(0)
    agg["Total Distance (km)"] = agg["Total_Distance_km"].round(3)
    agg["Total Elevation Gain (km)"] = (agg["Total_Elevation_m"] / 1000.0).round(3)
    agg["Longest Run (km)"] = agg["Longest_Run_km"].round(3)
    agg["Total Time (h)"] = (agg["Total_Time_s"] / 3600.0).round(3)

    def _pace(row):
        if row["Total_Distance_km"] > 0:
            return secs_to_minsec_str(row["Total_Time_s"] / row["Total_Distance_km"])
        return "-"
    agg["Average Pace (min/km)"] = agg.apply(_pace, axis=1)

    out = agg[[
        "gear_id", "Runs", "Total Distance (km)", "Total Elevation Gain (km)",
        "Average Run Length (km)", "Longest Run (km)", "Total Time (h)", "Average Pace (min/km)"
    ]].copy()
    if shoe_lookup_df is not None and "gear_id" in shoe_lookup_df.columns and "Shoe" in shoe_lookup_df.columns:
        out = out.merge(shoe_lookup_df[["Shoe", "gear_id"]], on="gear_id", how="left")
        out["Shoe"] = out["Shoe"].fillna(out["gear_id"])
    else:
        out["Shoe"] = out["gear_id"]
    final_cols = [
        "Shoe", "Average Run Length (km)", "Runs", "Total Distance (km)",
        "Total Elevation Gain (km)", "Average Pace (min/km)", "Longest Run (km)",
        "Total Time (h)", "gear_id"
    ]
    for c in final_cols:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[final_cols]
    numeric_cols = ["Average Run Length (km)", "Runs", "Total Distance (km)",
                    "Total Elevation Gain (km)", "Longest Run (km)", "Total Time (h)"]
    for c in numeric_cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def compute_shoe_league(runs_df: pd.DataFrame, shoe_lookup_df: Optional[pd.DataFrame] = None, athlete_id: Optional[int] = None) -> pd.DataFrame:
    out = _compute_shoe_league_internal(runs_df, shoe_lookup_df)
    out["athlete_id"] = athlete_id if athlete_id is not None else pd.NA
    return out


def build_shoe_lookup_from_activities(runs_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Best-effort Shoe-name lookup from any gear info embedded in the activities."""
    if runs_df is None or runs_df.empty:
        return None
    gear_map = {}
    if "gear" in runs_df.columns:
        for g in runs_df["gear"].dropna().unique():
            if isinstance(g, dict):
                gid = g.get("id") or g.get("uid") or ""
                name = g.get("name") or ""
                if gid and name:
                    gear_map[str(gid)] = name
    if "gear_id" in runs_df.columns and "gear_name" in runs_df.columns:
        for gid, name in runs_df[["gear_id", "gear_name"]].dropna().itertuples(index=False, name=None):
            gear_map[str(gid)] = name
    if not gear_map:
        return None
    return pd.DataFrame({"Shoe": list(gear_map.values()), "gear_id": list(gear_map.keys())})


# --------------------------------------------------------------------------- #
# Top-level orchestration
# --------------------------------------------------------------------------- #

def ensure_athlete_activities_and_league(script_dir: str, csv_path: str, athlete_id: Optional[int] = None) -> Tuple[pd.DataFrame, pd.DataFrame, Optional[str]]:
    """
    Load existing athlete activities and the shoe-league table.

    Prefers an existing league CSV. If missing but activities exist, rebuilds the
    league: from fetched gear details (real shoe names) when available, otherwise
    via compute_shoe_league (gear_id used as the shoe name).

    Returns (league_df, runs_df, csv_used). runs_df is the raw activities frame.
    """
    data_dir = _data_dir(script_dir)
    athlete_activities_path = latest_activities_path(script_dir, athlete_id=athlete_id) if athlete_id is not None else None
    athlete_csv_path = os.path.join(data_dir, f"shoe_league_table_{athlete_id}.csv") if athlete_id is not None else None

    runs_df = pd.DataFrame()
    if athlete_activities_path and os.path.exists(athlete_activities_path):
        try:
            runs_df = pd.read_json(athlete_activities_path)
        except Exception:
            runs_df = pd.DataFrame()

    csv_to_use = None
    if athlete_csv_path and os.path.exists(athlete_csv_path) and os.path.getsize(athlete_csv_path) > 0:
        csv_to_use = athlete_csv_path
    elif csv_path and os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
        csv_to_use = csv_path

    df = pd.DataFrame()
    if csv_to_use:
        try:
            df = pd.read_csv(csv_to_use)
        except Exception:
            df = pd.DataFrame()

    # Build the league if we have no CSV yet but do have activities.
    if df.empty and not runs_df.empty:
        out_csv = athlete_csv_path or csv_path
        all_gear_path = os.path.join(data_dir, f"all_gear_{athlete_id}.json") if athlete_id is not None else None
        if all_gear_path and os.path.exists(all_gear_path) and os.path.getsize(all_gear_path) > 0:
            combine_shoes(all_gear_path, athlete_activities_path, out_csv)
            try:
                df = pd.read_csv(out_csv) if os.path.exists(out_csv) else pd.DataFrame()
                csv_to_use = out_csv
            except Exception:
                df = pd.DataFrame()
        else:
            shoe_lookup = build_shoe_lookup_from_activities(runs_df)
            df = compute_shoe_league(runs_df, shoe_lookup_df=shoe_lookup, athlete_id=athlete_id)
            try:
                df.to_csv(out_csv, index=False)
                csv_to_use = out_csv
            except Exception:
                pass

    return df, runs_df, csv_to_use or csv_path


def _load_existing_activities(activities_path: str) -> List[Dict[str, Any]]:
    """Return saved activities as a list of records, or [] if none/unreadable."""
    if not (activities_path and os.path.exists(activities_path)):
        return []
    try:
        existing = pd.read_json(activities_path)
        if isinstance(existing, pd.DataFrame) and not existing.empty:
            return existing.to_dict(orient="records")
    except Exception:
        pass
    return []


def _newest_after_epoch(records: List[Dict[str, Any]]) -> Optional[int]:
    """Epoch seconds of the newest start_date_local across records, or None."""
    dates = [r.get("start_date_local") or r.get("start_date") for r in records]
    dates = [d for d in dates if d is not None and not (isinstance(d, float) and pd.isna(d))]
    if not dates:
        return None
    try:
        newest = pd.to_datetime(pd.Series(dates), errors="coerce", utc=True).max()
        if pd.notna(newest):
            return int(newest.timestamp())
    except Exception:
        pass
    return None


def perform_fetch_and_build_for_athlete(script_dir: str, athlete_id: int, access_token: Optional[str] = None, incremental: bool = True) -> Tuple[bool, Optional[str]]:
    """
    Fetch activities from Strava, save them, fetch gear details and build the shoe
    league (with real shoe names). Returns (True, csv_path) or (False, None).

    When incremental=True and a saved activities file exists, only activities newer
    than the most recent saved one are fetched and merged into the file (deduped by
    id) — avoiding a full re-download. A full fetch is done when no file exists.
    """
    # Resolve an access token from the saved token if not supplied.
    if not access_token:
        token_info = load_token_for_athlete(athlete_id)
        if isinstance(token_info, dict):
            access_token = (
                token_info.get("access_token")
                or token_info.get("token")
                or token_info.get("access")
            )
    if not access_token:
        return False, None

    data_dir = _data_dir(script_dir)
    activities_path = os.path.join(data_dir, f"activities_{athlete_id}.json")
    gear_ids_path = os.path.join(data_dir, f"gear_ids_{athlete_id}.json")
    all_gear_path = os.path.join(data_dir, f"all_gear_{athlete_id}.json")
    out_csv = os.path.join(data_dir, f"shoe_league_table_{athlete_id}.csv")

    existing = _load_existing_activities(activities_path) if incremental else []
    after = _newest_after_epoch(existing) if existing else None

    # Fetch (paginated). With `after` set, Strava returns only newer activities.
    new_acts = []
    page = 1
    per_page = 200
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        while True:
            params = {"per_page": per_page, "page": page}
            if after:
                params["after"] = after
            resp = requests.get(
                "https://www.strava.com/api/v3/athlete/activities",
                headers=headers,
                params=params,
                timeout=30,
            )
            if resp.status_code == 401:
                return False, None
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            new_acts.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
            time.sleep(0.2)  # be nice to the API
    except Exception:
        if not new_acts and not existing:
            return False, None

    # Merge new + existing, dedupe by activity id, newest first.
    merged_map = {}
    for a in existing + new_acts:
        try:
            merged_map[int(a["id"])] = a
        except Exception:
            continue
    merged = sorted(merged_map.values(), key=lambda x: str(x.get("start_date_local") or ""), reverse=True)
    if not merged:
        return False, None

    activities_path = save_activities_for_athlete(athlete_id, merged, script_dir)

    # Rebuild gear/league when we got new activities, or if artifacts are missing.
    if new_acts or not (os.path.exists(all_gear_path) and os.path.exists(out_csv)):
        extract_gear_ids_from_activities(activities_path, gear_ids_path)
        fetch_gear_details(gear_ids_path, all_gear_path, access_token)
        combine_shoes(all_gear_path, activities_path, out_csv)

    return True, out_csv