"""The flight catalog: one simulated year of scheduled flights.

The network is fifteen real US airports around five hubs. Schedules,
capacities and fares are synthesized from distance with a fixed seed, so every
team, the house airline and the grader see exactly the same catalog.
"""

from __future__ import annotations

import csv
import io
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

YEAR = 2027
SEED = 4340

# code, city, lat, lon, tz, demand weight, hub
AIRPORTS: list[tuple[str, str, float, float, str, int, bool]] = [
    ("ATL", "Atlanta", 33.6407, -84.4277, "America/New_York", 10, True),
    ("ORD", "Chicago", 41.9742, -87.9073, "America/Chicago", 9, True),
    ("DFW", "Dallas-Fort Worth", 32.8998, -97.0403, "America/Chicago", 8, True),
    ("DEN", "Denver", 39.8561, -104.6737, "America/Denver", 8, True),
    ("SLC", "Salt Lake City", 40.7899, -111.9791, "America/Denver", 5, True),
    ("LAX", "Los Angeles", 33.9416, -118.4085, "America/Los_Angeles", 9, False),
    ("SFO", "San Francisco", 37.6213, -122.3790, "America/Los_Angeles", 7, False),
    ("SEA", "Seattle", 47.4502, -122.3088, "America/Los_Angeles", 6, False),
    ("PDX", "Portland", 45.5898, -122.5951, "America/Los_Angeles", 4, False),
    ("BOI", "Boise", 43.5644, -116.2228, "America/Boise", 3, False),
    ("PHX", "Phoenix", 33.4342, -112.0116, "America/Phoenix", 6, False),
    ("LAS", "Las Vegas", 36.0840, -115.1537, "America/Los_Angeles", 6, False),
    ("MSP", "Minneapolis", 44.8848, -93.2223, "America/Chicago", 5, False),
    ("JFK", "New York", 40.6413, -73.7781, "America/New_York", 10, False),
    ("BOS", "Boston", 42.3656, -71.0096, "America/New_York", 6, False),
]

# Each spoke connects to these hubs. Hubs connect to every other hub.
SPOKE_HUBS = {
    "LAX": ["DEN", "SLC", "DFW", "ORD", "ATL"],
    "SFO": ["DEN", "SLC", "ORD", "DFW"],
    "SEA": ["SLC", "DEN", "ORD"],
    "PDX": ["SLC", "DEN"],
    "BOI": ["SLC", "DEN"],
    "PHX": ["DEN", "DFW", "SLC"],
    "LAS": ["DEN", "SLC", "DFW"],
    "MSP": ["ORD", "DEN", "ATL"],
    "JFK": ["ATL", "ORD", "DFW", "DEN"],
    "BOS": ["ATL", "ORD"],
}
# A few point-to-point routes that skip the hubs.
DIRECT = [("LAX", "SFO"), ("SEA", "PDX"), ("SEA", "LAX"), ("LAX", "JFK"),
          ("SFO", "JFK"), ("BOS", "JFK"), ("LAS", "LAX"), ("PHX", "LAX"),
          ("BOI", "SEA"), ("SEA", "SFO")]


@dataclass(frozen=True, slots=True)
class Airport:
    code: str
    city: str
    lat: float
    lon: float
    tz: str
    weight: int
    hub: bool

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)  # ZoneInfo caches instances itself


@dataclass(frozen=True, slots=True)
class Flight:
    flight_id: str          # unique per departure, e.g. FC1203-20270314
    flight_number: str      # FC1203, the same every day
    origin: str
    destination: str
    departure: datetime     # UTC
    arrival: datetime       # UTC
    origin_local_date: date
    capacity: int
    base_fare_cents: int
    distance_mi: int


AIRPORT_BY_CODE = {a[0]: Airport(*a) for a in AIRPORTS}


def miles(a: str, b: str) -> int:
    pa, pb = AIRPORT_BY_CODE[a], AIRPORT_BY_CODE[b]
    p1, p2 = math.radians(pa.lat), math.radians(pb.lat)
    dl = math.radians(pb.lon - pa.lon)
    c = math.acos(min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))
    return round(3958.8 * c)


def block_minutes(dist: int) -> int:
    """Gate-to-gate time: taxi plus cruise at roughly 480 mph."""
    return 5 * round((35 + dist / 8.0) / 5)


def capacity_for(dist: int) -> int:
    """Seats for sale on one flight.

    Deliberately about one eighth of a real aircraft. The tests send a few
    thousand travelers at one travel day, and at this size roughly one flight in
    five sells out while almost every traveler still finds a seat.
    """
    if dist < 450:
        return 10
    if dist < 1100:
        return 16
    if dist < 2000:
        return 20
    return 23


def fare_for(dist: int) -> int:
    dollars = 59 + 0.115 * dist
    return int(round(dollars)) * 100


def routes() -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    hubs = [a[0] for a in AIRPORTS if a[6]]
    for i, a in enumerate(hubs):
        for b in hubs[i + 1:]:
            pairs.add((a, b))
    for spoke, hs in SPOKE_HUBS.items():
        for h in hs:
            pairs.add((spoke, h))
    pairs.update(DIRECT)
    out = []
    for a, b in sorted(pairs):
        out += [(a, b), (b, a)]
    return out


def daily_frequency(a: str, b: str) -> int:
    wa, wb = AIRPORT_BY_CODE[a].weight, AIRPORT_BY_CODE[b].weight
    f = round((wa * wb) ** 0.5 / 1.6)
    if AIRPORT_BY_CODE[a].hub and AIRPORT_BY_CODE[b].hub:
        f += 2
    return max(2, min(f, 8))


@dataclass
class Catalog:
    year: int
    flights: list[Flight]

    def __post_init__(self) -> None:
        self.by_id: dict[str, Flight] = {f.flight_id: f for f in self.flights}
        self.by_origin_date: dict[tuple[str, date], list[Flight]] = {}
        for f in self.flights:
            self.by_origin_date.setdefault((f.origin, f.origin_local_date), []).append(f)
        for v in self.by_origin_date.values():
            v.sort(key=lambda f: f.departure)
        # Departures out of an airport across all dates, sorted, for connections
        # that cross local midnight.
        self.by_origin: dict[str, list[Flight]] = {}
        for f in self.flights:
            self.by_origin.setdefault(f.origin, []).append(f)
        for v in self.by_origin.values():
            v.sort(key=lambda f: f.departure)
        self._dep_times = {k: [f.departure for f in v] for k, v in self.by_origin.items()}

    def departures(self, airport: str) -> list[datetime]:
        return self._dep_times.get(airport, [])

    @property
    def start(self) -> datetime:
        return datetime(self.year, 1, 1, tzinfo=timezone.utc)

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(FIELDS)
        for f in self.flights:
            w.writerow(row(f))
        return buf.getvalue()


FIELDS = ["flight_id", "flight_number", "origin", "destination",
          "scheduled_departure", "scheduled_arrival", "origin_local_date",
          "capacity", "base_fare_cents", "distance_mi"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(f: Flight) -> list:
    return [f.flight_id, f.flight_number, f.origin, f.destination, iso(f.departure),
            iso(f.arrival), f.origin_local_date.isoformat(), f.capacity,
            f.base_fare_cents, f.distance_mi]


def airports_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["code", "city", "lat", "lon", "tz", "hub"])
    for a in AIRPORT_BY_CODE.values():
        w.writerow([a.code, a.city, a.lat, a.lon, a.tz, int(a.hub)])
    return buf.getvalue()


def generate(year: int = YEAR, seed: int = SEED) -> Catalog:
    rng = random.Random(seed)
    # Fix each route's daily pattern once: departure local times and numbers.
    patterns = []
    number = 100
    for a, b in routes():
        dist = miles(a, b)
        freq = daily_frequency(a, b)
        first, last = 6 * 60, 21 * 60 + 30
        span = last - first
        times = sorted({first + 5 * round((span * (i + rng.random() * 0.8) / freq) / 5)
                        for i in range(freq)})
        for t in times:
            patterns.append((a, b, dist, t, f"FC{number}"))
            number += 1
    flights: list[Flight] = []
    day = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    while day < end:
        dow = day.weekday()
        for a, b, dist, t, num in patterns:
            # Saturday runs a thinner schedule.
            if dow == 5 and int(num[2:]) % 3 == 0:
                continue
            zone = AIRPORT_BY_CODE[a].zone
            local = datetime(day.year, day.month, day.day, t // 60, t % 60, tzinfo=zone)
            dep = local.astimezone(timezone.utc)
            arr = dep + timedelta(minutes=block_minutes(dist))
            flights.append(Flight(
                flight_id=f"{num}-{day:%Y%m%d}", flight_number=num, origin=a,
                destination=b, departure=dep, arrival=arr, origin_local_date=day,
                capacity=capacity_for(dist), base_fare_cents=fare_for(dist),
                distance_mi=dist))
        day += timedelta(days=1)
    flights.sort(key=lambda f: (f.departure, f.flight_id))
    return Catalog(year, flights)


_CACHE: dict[tuple[int, int], Catalog] = {}


def get(year: int = YEAR, seed: int = SEED) -> Catalog:
    key = (year, seed)
    if key not in _CACHE:
        _CACHE[key] = generate(year, seed)
    return _CACHE[key]
