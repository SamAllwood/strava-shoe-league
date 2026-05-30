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


# --- build_league_table ---------------------------------------------------

def test_build_league_table_filters_to_runs_only():
    league = strava_tools.build_league_table(_sample_activities())
    gear_ids = set(league["gear_id"].values)
    assert "g1" in gear_ids
    assert "b1" not in gear_ids  # the bike ride must be excluded


def test_build_league_table_aggregates_per_shoe():
    league = strava_tools.build_league_table(_sample_activities())
    row = league.loc[league["gear_id"] == "g1"].iloc[0]
    assert row["Runs"] == 2
    assert row["Total Distance (km)"] == pytest.approx(15.0)
    assert row["Longest Run (km)"] == pytest.approx(10.0)


def test_build_league_table_empty_input():
    league = strava_tools.build_league_table(pd.DataFrame())
    assert isinstance(league, pd.DataFrame)


# --- ensure_athlete_activities_and_league ---------------------------------

def test_ensure_athlete_activities_and_league(tmp_path):
    athlete_id = 999
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    activities = _sample_activities().to_dict(orient="records")
    (data_dir / f"activities_{athlete_id}.json").write_text(json.dumps(activities))

    csv_path = str(data_dir / f"shoe_league_table_{athlete_id}.csv")
    league_df, activities_df, used_csv = strava_tools.ensure_athlete_activities_and_league(
        str(tmp_path), csv_path, athlete_id=athlete_id
    )

    assert isinstance(league_df, pd.DataFrame)
    assert isinstance(activities_df, pd.DataFrame)
    assert used_csv == csv_path
    assert os.path.exists(csv_path)  # league table CSV is written out


def test_ensure_missing_activities_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        strava_tools.ensure_athlete_activities_and_league(
            str(tmp_path), str(tmp_path / "out.csv"), athlete_id=12345
        )


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