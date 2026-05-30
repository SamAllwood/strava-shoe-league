import json
import os

import pandas as pd
import pytest

import strava_tools


# --- Fixtures -------------------------------------------------------------

def _sample_activities():
    """Two runs sharing one shoe (g1) plus a bike ride (b1) to be filtered out."""
    return pd.DataFrame([
        {"id": 1, "name": "Morning Run", "type": "Run", "distance": 5000,
         "total_elevation_gain": 50, "moving_time": 1500, "gear_id": "g1",
         "start_date_local": "2024-01-01T08:00:00Z"},
        {"id": 2, "name": "Evening Run", "type": "Run", "distance": 10000,
         "total_elevation_gain": 100, "moving_time": 3000, "gear_id": "g1",
         "start_date_local": "2024-01-02T18:00:00Z"},
        {"id": 3, "name": "Bike Ride", "type": "Ride", "distance": 20000,
         "total_elevation_gain": 200, "moving_time": 3600, "gear_id": "b1",
         "start_date_local": "2024-01-03T08:00:00Z"},
    ])


# --- secs_to_minsec_str ---------------------------------------------------

def test_secs_to_minsec_str_minutes():
    assert strava_tools.secs_to_minsec_str(330) == "5:30"


def test_secs_to_minsec_str_hours():
    assert strava_tools.secs_to_minsec_str(3661) == "1:01:01"


def test_secs_to_minsec_str_invalid():
    assert strava_tools.secs_to_minsec_str(0) == "-"
    assert strava_tools.secs_to_minsec_str("not a number") == "-"


# --- incremental fetch helpers --------------------------------------------

def test_newest_after_epoch_picks_latest():
    records = [
        {"id": 1, "start_date_local": "2024-01-01T08:00:00Z"},
        {"id": 2, "start_date_local": "2024-03-02T18:00:00Z"},  # newest
        {"id": 3, "start_date_local": "2024-02-15T07:00:00Z"},
    ]
    after = strava_tools._newest_after_epoch(records)
    # epoch for 2024-03-02T18:00:00Z
    assert after == 1709402400


def test_newest_after_epoch_empty_or_missing():
    assert strava_tools._newest_after_epoch([]) is None
    assert strava_tools._newest_after_epoch([{"id": 1}]) is None


# --- compute_shoe_league --------------------------------------------------

def test_compute_shoe_league_aggregates_per_shoe():
    league = strava_tools.compute_shoe_league(_sample_activities(), athlete_id=7)
    row = league.loc[league["gear_id"] == "g1"].iloc[0]
    assert row["Runs"] == 2
    assert row["Total Distance (km)"] == pytest.approx(15.0)
    assert row["Longest Run (km)"] == pytest.approx(10.0)
    # with no shoe lookup, the Shoe label falls back to the gear id
    assert row["Shoe"] == "g1"
    assert row["athlete_id"] == 7


def test_compute_shoe_league_empty_input():
    league = strava_tools.compute_shoe_league(pd.DataFrame())
    assert isinstance(league, pd.DataFrame)


# --- combine_shoes (real shoe names from fetched gear details) -------------

def test_combine_shoes_uses_gear_names_and_retired(tmp_path):
    activities = [
        {"gear_id": "g1", "distance": 5000, "total_elevation_gain": 50,
         "moving_time": 1500, "start_date_local": "2024-01-01T08:00:00Z"},
        {"gear_id": "g1", "distance": 10000, "total_elevation_gain": 100,
         "moving_time": 3000, "start_date_local": "2024-03-02T18:00:00Z"},
        {"gear_id": "b1", "distance": 20000, "total_elevation_gain": 200,
         "moving_time": 3600, "start_date_local": "2024-01-03T08:00:00Z"},
    ]
    all_gear = [
        {"id": "g1", "name": "Pegasus 40", "retired": False},
        {"id": "b1", "name": "Road Bike", "retired": False},  # bikes are ignored
    ]
    acts_path = tmp_path / "activities.json"
    gear_path = tmp_path / "all_gear.json"
    out_csv = tmp_path / "league.csv"
    acts_path.write_text(json.dumps(activities))
    gear_path.write_text(json.dumps(all_gear))

    strava_tools.combine_shoes(str(gear_path), str(acts_path), str(out_csv))
    out = pd.read_csv(out_csv)

    assert list(out["Shoe"]) == ["Pegasus 40"]  # only the shoe, not the bike
    row = out.iloc[0]
    assert row["Runs"] == 2
    assert row["Total Distance (km)"] == pytest.approx(15.0)
    assert row["Retired"] == "No"
    assert row["First Use"] == "Jan 2024"  # earliest activity date


# --- ensure_athlete_activities_and_league ---------------------------------

def test_ensure_builds_league_when_activities_present(tmp_path):
    athlete_id = 999
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    activities = _sample_activities().to_dict(orient="records")
    (data_dir / f"activities_{athlete_id}.json").write_text(json.dumps(activities))

    csv_path = str(data_dir / f"shoe_league_table_{athlete_id}.csv")
    league_df, activities_df, used_csv = strava_tools.ensure_athlete_activities_and_league(
        str(tmp_path), csv_path, athlete_id=athlete_id
    )

    assert not league_df.empty
    assert isinstance(activities_df, pd.DataFrame)
    assert used_csv == csv_path
    assert os.path.exists(csv_path)  # league table CSV is written out


def test_ensure_missing_activities_returns_empty(tmp_path):
    # No activities file present -> graceful empty frames, no exception.
    league_df, runs_df, _ = strava_tools.ensure_athlete_activities_and_league(
        str(tmp_path), str(tmp_path / "out.csv"), athlete_id=12345
    )
    assert league_df.empty
    assert runs_df.empty


# --- Token DB round-trip --------------------------------------------------

def test_token_db_roundtrip(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tokens.db")
    # save_token / load_token_for_athlete read the module-level DB_PATH at call
    # time, so patching it redirects them at the temp database.
    monkeypatch.setattr(strava_tools, "DB_PATH", db_path)
    strava_tools.init_db(db_path)

    token = {"access_token": "abc123", "athlete": {"id": 42}}
    strava_tools.save_token(token)

    loaded = strava_tools.load_token_for_athlete(42)
    assert isinstance(loaded, dict)
    assert loaded["access_token"] == "abc123"


def test_load_token_missing_returns_none(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tokens.db")
    monkeypatch.setattr(strava_tools, "DB_PATH", db_path)
    strava_tools.init_db(db_path)
    assert strava_tools.load_token_for_athlete(0) is None


def test_save_token_without_athlete_id_raises(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tokens.db")
    monkeypatch.setattr(strava_tools, "DB_PATH", db_path)
    strava_tools.init_db(db_path)
    with pytest.raises(ValueError):
        strava_tools.save_token({"access_token": "no_athlete_here"})