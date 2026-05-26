# This file contains utility functions for interacting with the Strava API, managing tokens, and processing activity data.

import pandas as pd
import os
import json
import sqlite3
import requests
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strava_tokens.db")

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
    """Save the Strava token for the athlete."""
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
    """Load the Strava token for the specified athlete."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT raw_json FROM tokens WHERE athlete_id = ?", (athlete_id,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None

def refresh_token_if_needed(token_info):
    """Refresh the Strava token if it is expired."""
    # Implement token refresh logic here
    # This is a placeholder for the actual implementation
    return token_info

def latest_activities_path(script_dir, athlete_id):
    """Return the path to the latest activities JSON file for the athlete."""
    return os.path.join(script_dir, f"data/activities_{athlete_id}.json")

def ensure_athlete_activities_and_league(script_dir, csv_path, athlete_id):
    """Ensure athlete activities are loaded and the league table is built."""
    activities_path = latest_activities_path(script_dir, athlete_id)
    if not os.path.exists(activities_path):
        raise FileNotFoundError(f"No activities file found for athlete {athlete_id}.")

    # Load activities and process to create league table
    activities_df = pd.read_json(activities_path)
    league_df = build_league_table(activities_df)
    
    # Save league table to CSV
    league_df.to_csv(csv_path, index=False)
    return league_df, activities_df, csv_path


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

def secs_to_minsec_str(s):
    try:
        s = float(s)
        if s <= 0 or s is None:
            return "-"
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        if h > 0:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m}:{sec:02d}"
    except Exception:
        return "-"

def compute_shoe_league(runs_df: pd.DataFrame, shoe_lookup_df: Optional[pd.DataFrame] = None, athlete_id: Optional[int] = None) -> pd.DataFrame:
    out = _compute_shoe_league_internal(runs_df, shoe_lookup_df)
    out["athlete_id"] = athlete_id if athlete_id is not None else pd.NA
    return out

def build_league_table(activities_df):
    """Build the shoe league table and runs DataFrame from activities data."""
    if activities_df is None:
        return pd.DataFrame()

    # Ensure a DataFrame
    acts = activities_df.copy() if not isinstance(activities_df, pd.DataFrame) else activities_df.copy()

    # Normalize known fields into a runs-like DataFrame
    # Accept variations present in Strava exports: 'type' or 'sport_type', distances in metres
    cols = {}
    cols["id"] = acts.get("id", pd.Series([pd.NA]*len(acts)))
    cols["name"] = acts.get("name", pd.Series([""]*len(acts)))
    # distance may be in 'distance' (metres) or 'distance_km'
    if "distance" in acts.columns:
        cols["distance"] = acts["distance"]
    elif "distance_km" in acts.columns:
        cols["distance"] = acts["distance_km"] * 1000.0
    else:
        cols["distance"] = pd.Series([pd.NA]*len(acts))

    cols["total_elevation_gain"] = acts.get("total_elevation_gain", pd.Series([0]*len(acts)))
    cols["moving_time"] = acts.get("moving_time", pd.Series([0]*len(acts)))
    # start date
    if "start_date_local" in acts.columns:
        cols["start_date_local"] = acts["start_date_local"]
    elif "start_date" in acts.columns:
        cols["start_date_local"] = acts["start_date"]
    else:
        cols["start_date_local"] = pd.Series([pd.NaT]*len(acts))

    # workout type and gear id if present
    cols["workout_type"] = acts.get("workout_type", pd.Series([pd.NA]*len(acts)))
    cols["gear_id"] = acts.get("gear_id", acts.get("gear", pd.Series([""]*len(acts))))

    runs_df = pd.DataFrame(cols)

    # Filter to running activities if possible (many Strava exports include a 'type' column)
    if "type" in acts.columns:
        mask = acts["type"].astype(str).str.lower().eq("run")
        runs_df = runs_df.loc[mask.values].reset_index(drop=True)
    # If no 'type' column, keep all rows but downstream grouping will handle empty/irrelevant rows.

    # Convert numeric columns to numeric
    runs_df["distance"] = pd.to_numeric(runs_df["distance"], errors="coerce").fillna(0)
    runs_df["total_elevation_gain"] = pd.to_numeric(runs_df["total_elevation_gain"], errors="coerce").fillna(0)
    runs_df["moving_time"] = pd.to_numeric(runs_df["moving_time"], errors="coerce").fillna(0)

    # Compute league using existing internal helper
    league_df = compute_shoe_league(runs_df, shoe_lookup_df=None, athlete_id=None)

    # Ensure consistent final formatting (matching previous UI expectations)
    # Keep Average Pace as string formatted via secs_to_minsec_str
    try:
        # convert Average Pace if numeric (already a string from internal helper in many cases)
        if "Average Pace (min/km)" in league_df.columns:
            league_df["Average Pace (min/km)"] = league_df["Average Pace (min/km)"].astype(str).replace("nan", "-")
            # no-op if already formatted, else format numeric seconds-per-km -> M:SS
            # detect numeric-like values and format
            def _fmt(x):
                try:
                    v = float(x)
                    return secs_to_minsec_str(v)
                except Exception:
                    return x
            league_df["Average Pace (min/km)"] = league_df["Average Pace (min/km)"].apply(_fmt)
    except Exception:
        pass

    return league_df