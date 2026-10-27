from datetime import date, timedelta

import pytest

from flightcontrol import catalog, oracle, store
from flightcontrol.client import UnsafeURL, check_url


@pytest.fixture(scope="module")
def cat():
    return catalog.get()


def test_catalog_is_deterministic(cat):
    again = catalog.generate()
    assert len(again.flights) == len(cat.flights)
    assert again.to_csv() == cat.to_csv()


def test_catalog_shape(cat):
    assert 150_000 < len(cat.flights) < 200_000
    assert len(catalog.AIRPORT_BY_CODE) == 15
    ids = [f.flight_id for f in cat.flights]
    assert len(ids) == len(set(ids))
    for f in cat.flights[:2000]:
        assert f.arrival > f.departure
        assert f.capacity in (76, 126, 162, 186)


def test_origin_local_date_is_local(cat):
    # A 21:30 departure from LAX is the next day in UTC but the same local date.
    late = [f for f in cat.by_origin["LAX"] if f.departure.hour >= 4 and f.departure.hour < 8]
    assert late
    f = late[0]
    assert f.departure.date() == f.origin_local_date + timedelta(days=1)


def test_oracle_itineraries_are_valid(cat):
    day = date(2027, 5, 4)
    for a, b in [("BOI", "JFK"), ("LAX", "SFO"), ("SEA", "ATL"), ("BOS", "DEN")]:
        its = oracle.itineraries(cat, a, b, day)
        assert its, (a, b)
        for i in its:
            assert oracle.check_itinerary(cat, i.itinerary_id, a, b, day, None) is None


def test_check_itinerary_rejects_bad_connections(cat):
    day = date(2027, 5, 4)
    f = cat.by_origin_date[("BOI", day)][0]
    assert oracle.check_itinerary(cat, f.flight_id, "BOI", "JFK", day, None) == "wrong origin or destination"
    assert "unknown flight" in oracle.check_itinerary(cat, "FC1-20270101", "BOI", "JFK", day, None)
    # Two flights that do not connect.
    g = next(x for x in cat.by_origin["ATL"] if x.origin_local_date == day)
    why = oracle.check_itinerary(cat, f"{f.flight_id}+{g.flight_id}", "BOI", g.destination, day, None)
    assert why is not None


def test_url_guard():
    with pytest.raises(UnsafeURL):
        check_url("http://team.trycloudflare.com")
    with pytest.raises(UnsafeURL):
        check_url("https://example.org")
    with pytest.raises(UnsafeURL):
        check_url("https://user:pw@team.trycloudflare.com")
    assert check_url("http://localhost:8080/", allow_private=True) == "http://localhost:8080"


def test_leaderboard_takes_best_of_last_three(tmp_path, monkeypatch):
    monkeypatch.setenv("FC_DB", str(tmp_path / "fc.sqlite3"))
    store.init()
    t = store.create_team("Team A", "team-a")
    for score in (0.9, 0.2, 0.5, 0.4):
        rid = store.queue_run(t["id"], 2)
        store.finish_run(rid, "done", score, True, "", {})
    row = next(r for r in store.leaderboard() if r["slug"] == "team-a")
    assert row["tests"][2] == 0.5   # 0.9 is older than the last three
    assert row["total"] == 20.0
