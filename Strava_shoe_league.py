import os
import re
import glob
import time
import uuid
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import strava_tools
import requests
from streamlit.errors import StreamlitSecretNotFoundError

script_dir = os.path.dirname(os.path.abspath(__file__))
data_dir = os.path.join(script_dir, "data")
os.makedirs(data_dir, exist_ok=True)

# init DB if helper exposes it (no-op otherwise)
try:
    strava_tools.init_db(os.path.join(script_dir, "strava_tokens.db"))
except Exception:
    pass

st.set_page_config(page_title="Strava Shoe League", layout="wide")

# helpers
def get_query_params():
    """
    Return URL query params in a version-compatible way.
    Tries st.get_query_params -> st.experimental_get_query_params -> st.query_params -> {}.
    """
    for name in ("get_query_params", "experimental_get_query_params"):
        fn = getattr(st, name, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    try:
        return st.query_params if isinstance(st.query_params, dict) else dict(st.query_params)
    except Exception:
        return {}
    
def find_athlete_ids():
    ids = set()
    # look for activities and league CSVs produced previously
    for p in glob.glob(os.path.join(data_dir, "activities_*.json")) + glob.glob(os.path.join(data_dir, "shoe_league_table_*.csv")):
        m = re.search(r"_(\d+)\.", os.path.basename(p))
        if m:
            try:
                ids.add(int(m.group(1)))
            except Exception:
                pass
    return sorted(ids)

# safe secret accessor: prefer st.secrets when available, otherwise fall back to env
def _get_secret(key: str):
    try:
        # st.secrets may raise StreamlitSecretNotFoundError in some environments
        return st.secrets.get(key)
    except Exception:
        return os.environ.get(key)

def build_auth_url():
    client_id = _get_secret("STRAVA_CLIENT_ID") or st.session_state.get("_tmp_client_id")
    redirect = _get_secret("STRAVA_REDIRECT_URI") or st.session_state.get("_tmp_redirect")
    if not client_id or not redirect:
        return None
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": "activity:read_all,profile:read_all",
        "approval_prompt": "auto",
    }
    return "https://www.strava.com/oauth/authorize?" + "&".join(f"{k}={str(v)}" for k, v in params.items())

def build_auth_url_from_params(client_id: str, redirect: str):
    if not client_id or not redirect:
        return None
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": "activity:read_all,profile:read_all",
        "approval_prompt": "auto",
    }
    return "https://www.strava.com/oauth/authorize?" + "&".join(f"{k}={str(v)}" for k, v in params.items())

# Exchange authorization code for tokens and persist via helpers if available.
def _exchange_code_for_token(code: str):
    # prefer temporary session inputs, then secrets, then environment
    client_id = st.session_state.get("_tmp_client_id") or _get_secret("STRAVA_CLIENT_ID")
    client_secret = st.session_state.get("_tmp_client_secret") or _get_secret("STRAVA_CLIENT_SECRET")
    redirect = st.session_state.get("_tmp_redirect") or _get_secret("STRAVA_REDIRECT_URI")
    st.write("Client ID:", client_id)  # DEBUG: show client ID (but not secret) to confirm we're picking it up
    if not client_id or not client_secret or not redirect:
        # do not crash — instruct the user to fill the inputs
        st.error("Missing client credentials. Enter Client ID / Client Secret / Redirect URI in the Connect panel and retry.")
        return None
    try:
        resp = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": str(client_id),
                "client_secret": str(client_secret),
                "code": str(code),
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        resp.raise_for_status()
        token_resp = resp.json()
    except Exception as e:
        st.error(f"Token exchange failed: {e}")
        return None
    st.write("Token response:", token_resp)
    # try to persist token using known helper names (best-effort)
    try:
        saver = getattr(strava_tools, "save_token_for_token_response", None) or getattr(strava_tools, "save_token", None) or getattr(strava_tools, "save_token_for_athlete", None)
        if callable(saver):
            try:
                saver(token_resp)
            except Exception:
                pass
    except Exception:
        pass

    # set current athlete id in session if available
    try:
        aid = None
        if isinstance(token_resp, dict) and "athlete" in token_resp and isinstance(token_resp["athlete"], dict):
            aid = token_resp["athlete"].get("id")
        if aid:
            st.session_state["current_athlete_id"] = int(aid)
    except Exception:
        pass

    return token_resp

def add_download_button(path, label=None):
    if not path or not os.path.exists(path):
        return
    key = f"dl_{os.path.basename(path)}_{uuid.uuid4().hex[:8]}"
    with open(path, "rb") as f:
        st.download_button(label or f"Download {os.path.basename(path)}", f, file_name=os.path.basename(path), key=key)

# top UI
st.title("Strava Shoe League")

athlete_ids = find_athlete_ids()
col1, col2 = st.columns([2,1])
with col1:
    default_athlete = st.session_state.get("current_athlete_id")
    if default_athlete in athlete_ids:
        default_index = athlete_ids.index(default_athlete) + 1  # +1 for None at index 0
    else:
        default_index = 0
    athlete = st.selectbox("Select athlete (by id)", options=[None] + athlete_ids, index=default_index)
with col2:
    # Build URL from secrets/env/session if possible
    auth_url = build_auth_url()
    if auth_url:
        st.markdown(f"[Connect to Strava]({auth_url})")
        # capture code from redirect and exchange for token
        qp = get_query_params()
        st.write("Query params:", qp)
        code_val = None
        if isinstance(qp, dict):
            code_list = qp.get("code") or qp.get("Code") or qp.get("CODE")
            if isinstance(code_list, list) and code_list:
                code_val = code_list[0]
        if code_val:
            token = _exchange_code_for_token(code_val)
            aid = None
            if token and isinstance(token, dict):
                if "athlete" in token and isinstance(token["athlete"], dict):
                    aid = token["athlete"].get("id")
                    st.write("Token response:", token)  # DEBUG: show the token response
            if aid:
                st.session_state["current_athlete_id"] = int(aid)
                st.success(f"Connected to Strava as athlete {aid}. Fetching activities…")
                  # Immediately fetch activities for this athlete
                fetcher = getattr(strava_tools, "perform_fetch_and_build_for_athlete", None)
                if callable(fetcher):
                    try:
                        ok, out_csv = fetcher(script_dir, int(aid))
                        if ok:
                            st.success("Fetched activities and rebuilt league.")
                        else:
                            st.warning("Fetcher ran but reported no output.")
                    except Exception as e:
                        st.error(f"Fetch after connect failed: {e}")
            # Remove code from URL and reload so dropdown picks up new athlete
            components.html("<script>window.location.href = window.location.pathname;</script>", height=0)
            st.stop()
        else:
            st.error("No athlete ID found in token response. Please check your Strava app settings and try again.")

# Refresh button: fetch activities and rebuild league if helper exists
if st.button("Refresh activities (fetch)"):
    if not athlete:
        st.error("Select an athlete id first (or upload activities JSON into data/).")
    else:
        # try to use provided helper perform_fetch_and_build_for_athlete if available
        try:
            fetcher = getattr(strava_tools, "perform_fetch_and_build_for_athlete", None)
            if callable(fetcher):
                ok, out_csv = fetcher(script_dir, int(athlete))
                if ok:
                    st.success("Fetched activities and rebuilt league (helper).")
                else:
                    st.warning("Fetcher ran but reported no output.")
            else:
                # fallback: caller should have a saved activities JSON; just rebuild league from it
                st.info("No fetch helper available; will rebuild league from existing activities JSON if present.")
        except Exception as e:
            st.error(f"Refresh failed: {e}")

        # attempt to rebuild (best-effort) using ensure_athlete_activities_and_league
        try:
            df_new, runs_new, used_csv_new = strava_tools.ensure_athlete_activities_and_league(script_dir, os.path.join(data_dir, f"shoe_league_table_{athlete}.csv"), athlete_id=int(athlete))
            # cache JSON so immediate reload shows updated frames reliably
            try:
                st.session_state["_cached_df_json"] = df_new.to_json(date_format="iso", orient="split")
                st.session_state["_cached_runs_df_json"] = runs_new.to_json(date_format="iso", orient="split")
                st.session_state["_cached_used_csv"] = used_csv_new
            except Exception:
                pass
            st.success("Rebuilt league from activities.json.")
        except Exception as e:
            st.error(f"Failed to rebuild from activities.json: {e}")

        # reload to pick up cached frames
        components.html("<script>window.location.reload()</script>", height=0)
        st.stop()

# load frames preferring cached JSON set by refresh
df = pd.DataFrame()
runs_df = pd.DataFrame()
used_csv = None
if "_cached_df_json" in st.session_state:
    try:
        df = pd.read_json(st.session_state.pop("_cached_df_json"), orient="split")
        runs_df = pd.read_json(st.session_state.pop("_cached_runs_df_json", "[]"), orient="split")
        used_csv = st.session_state.pop("_cached_used_csv", None)
    except Exception:
        df = pd.DataFrame(); runs_df = pd.DataFrame(); used_csv = None
else:
    # normal load from activities.json (if present) via helper
    if athlete:
        try:
            df, runs_df, used_csv = strava_tools.ensure_athlete_activities_and_league(script_dir, os.path.join(data_dir, f"shoe_league_table_{athlete}.csv"), athlete_id=int(athlete))
        except Exception:
            df = pd.DataFrame(); runs_df = pd.DataFrame(); used_csv = None

# display
if athlete:
    st.header(f"Athlete {athlete} Strava Stats")
else:
    st.header("No athlete selected")

# show small metadata and downloads
if used_csv and os.path.exists(used_csv):
    st.write(f"Using league CSV: {os.path.basename(used_csv)}")
    add_download_button(used_csv, "Download league CSV")
activities_path = None
try:
    activities_path = strava_tools.latest_activities_path(script_dir, athlete_id=int(athlete)) if athlete else None
except Exception:
    activities_path = None
if activities_path and os.path.exists(activities_path):
    add_download_button(activities_path, "Download activities JSON")

# Shoe league table
st.subheader("Shoe League Table")
if isinstance(df, pd.DataFrame) and not df.empty:
    st.dataframe(df)
else:
    st.info("No shoe league data available. Click Refresh activities once you have connected or uploaded activities JSON.")

# Runs table (plain)
st.subheader("Runs")
if isinstance(runs_df, pd.DataFrame) and not runs_df.empty:
    # minimal display of runs
    display_runs = runs_df.copy()
    # format dates nicely if present
    if "start_date_local" in display_runs.columns:
        display_runs["start_date_local"] = pd.to_datetime(display_runs["start_date_local"], errors="coerce")
    st.dataframe(display_runs)
else:
    st.info("No runs data available.")

# small footer
st.markdown("---")
st.write("This app derives all tables from activities JSON; CSV outputs are produced as artifacts.")