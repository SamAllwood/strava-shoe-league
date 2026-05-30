import os
import re
import glob
import uuid
import streamlit as st
import pandas as pd
import strava_tools
import requests

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

def do_rerun():
    """Rerun the script in a version-compatible way (preserves session_state)."""
    for name in ("rerun", "experimental_rerun"):
        fn = getattr(st, name, None)
        if callable(fn):
            fn()
            return

def clear_query_params():
    """Remove all URL query params in a version-compatible way.

    Uses st.query_params (which edits the real page URL) rather than injecting
    JavaScript via components.html — a component script only runs inside its
    sandboxed iframe and cannot navigate the parent page.
    """
    try:
        st.query_params.clear()
        return
    except Exception:
        pass
    for name in ("experimental_set_query_params", "set_query_params"):
        fn = getattr(st, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass

def extract_code(qp):
    """Pull the OAuth 'code' out of query params, tolerating case/list forms."""
    if not isinstance(qp, dict):
        return None
    item = qp.get("code") or qp.get("Code") or qp.get("CODE")
    if isinstance(item, list):
        return item[0] if item else None
    return item if isinstance(item, str) else None

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

# Exchange authorization code for tokens and persist via helpers if available.
def _exchange_code_for_token(code: str):
    # prefer temporary session inputs, then secrets, then environment
    client_id = st.session_state.get("_tmp_client_id") or _get_secret("STRAVA_CLIENT_ID")
    client_secret = st.session_state.get("_tmp_client_secret") or _get_secret("STRAVA_CLIENT_SECRET")
    redirect = st.session_state.get("_tmp_redirect") or _get_secret("STRAVA_REDIRECT_URI")
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

# Handle the Strava OAuth redirect BEFORE any widgets render, so the selected
# athlete can be seeded into the selectbox below. The 'code' is single-use, so
# we remember the last one handled and never re-submit it (re-submitting a used
# code is what Strava rejects with a token-exchange error).
code_val = extract_code(get_query_params())
if code_val and code_val != st.session_state.get("_last_code"):
    st.session_state["_last_code"] = code_val
    token = _exchange_code_for_token(code_val)
    aid = None
    if token and isinstance(token, dict) and isinstance(token.get("athlete"), dict):
        aid = token["athlete"].get("id")
    if aid:
        aid = int(aid)
        st.session_state["current_athlete_id"] = aid
        st.session_state["athlete_select"] = aid  # seed the dropdown selection
        msg = (None, None)
        fetcher = getattr(strava_tools, "perform_fetch_and_build_for_athlete", None)
        if callable(fetcher):
            try:
                ok, _ = fetcher(script_dir, aid)
                if ok:
                    msg = ("success", f"Connected as athlete {aid}. Fetched activities and rebuilt league.")
                else:
                    msg = ("warning", f"Connected as athlete {aid}, but the fetcher reported no output.")
            except Exception as e:
                msg = ("error", f"Connected as athlete {aid}, but fetch failed: {e}")
        else:
            msg = ("success", f"Connected as athlete {aid}.")
        st.session_state["_flash"] = msg
    else:
        st.session_state["_flash"] = ("error", "Token exchange did not return an athlete. Check your Strava app settings and try connecting again.")
    # Strip the code (and any other params) from the real page URL, then rerun.
    clear_query_params()
    do_rerun()

# top UI
st.title("Strava Shoe League")

# show any one-shot message left by the connect flow
flash = st.session_state.pop("_flash", None)
if flash and flash[0]:
    getattr(st, flash[0], st.info)(flash[1])

athlete_ids = find_athlete_ids()
options = [None] + athlete_ids
col1, col2 = st.columns([2, 1])
with col1:
    # Seed the initial selection (first render only) from a connected athlete or
    # an ?athlete=<id> URL param; thereafter the keyed widget owns its own state.
    if "athlete_select" not in st.session_state:
        seed = st.session_state.get("current_athlete_id")
        if seed is None:
            qp_athlete = get_query_params().get("athlete")
            if isinstance(qp_athlete, list):
                qp_athlete = qp_athlete[0] if qp_athlete else None
            try:
                seed = int(qp_athlete) if qp_athlete else None
            except (TypeError, ValueError):
                seed = None
        st.session_state["athlete_select"] = seed
    # Guard against a stored selection that is no longer a valid option.
    if st.session_state.get("athlete_select") not in options:
        st.session_state["athlete_select"] = None
    athlete = st.selectbox("Select athlete (by id)", options=options, key="athlete_select")
with col2:
    # Build URL from secrets/env/session if possible
    auth_url = build_auth_url()
    if auth_url:
        st.markdown(f"[Connect to Strava]({auth_url})")

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
                    st.success("Fetched activities and rebuilt league.")
                else:
                    st.warning("Fetcher ran but reported no output.")
            else:
                # fallback: caller should have a saved activities JSON; rebuild from it below
                st.info("No fetch helper available; will rebuild league from existing activities JSON if present.")
        except Exception as e:
            st.error(f"Refresh failed: {e}")

        # Rerun so the freshly-fetched data is loaded from disk and displayed.
        # st.rerun keeps session_state (and the selected athlete) intact.
        do_rerun()

# load frames from activities.json (if present) via helper
df = pd.DataFrame()
runs_df = pd.DataFrame()
used_csv = None
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