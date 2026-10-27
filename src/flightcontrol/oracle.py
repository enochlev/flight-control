"""Ground truth for search: what itineraries exist, and is a team's answer valid."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from bisect import bisect_left

from .catalog import Catalog, Flight

MIN_LAYOVER = timedelta(minutes=45)
MAX_LAYOVER = timedelta(minutes=240)
MAX_RESULTS = 20


@dataclass(frozen=True)
class Itinerary:
    legs: tuple[Flight, ...]

    @property
    def itinerary_id(self) -> str:
        return "+".join(f.flight_id for f in self.legs)

    @property
    def departure(self) -> datetime:
        return self.legs[0].departure

    @property
    def arrival(self) -> datetime:
        return self.legs[-1].arrival

    @property
    def stops(self) -> int:
        return len(self.legs) - 1

    @property
    def minutes(self) -> int:
        return int((self.arrival - self.departure).total_seconds() // 60)

    @property
    def base_fare_cents(self) -> int:
        return sum(f.base_fare_cents for f in self.legs)


def itineraries(cat: Catalog, origin: str, destination: str, day: date,
                after: datetime | None = None, max_stops: int = 1) -> list[Itinerary]:
    """Every valid nonstop and 1-stop itinerary leaving `origin` on local `day`."""
    out: list[Itinerary] = []
    for f in cat.by_origin_date.get((origin, day), []):
        if after is not None and f.departure <= after:
            continue
        if f.destination == destination:
            out.append(Itinerary((f,)))
        elif max_stops >= 1 and f.destination != origin:
            for g in connections(cat, f):
                if g.destination == destination:
                    out.append(Itinerary((f, g)))
    out.sort(key=lambda i: (i.stops, i.departure))
    return out


def connections(cat: Catalog, f: Flight) -> list[Flight]:
    """Flights leaving where `f` lands, inside the layover window."""
    deps = cat.by_origin.get(f.destination, [])
    times = cat.departures(f.destination)
    hi = f.arrival + MAX_LAYOVER
    i = bisect_left(times, f.arrival + MIN_LAYOVER)
    res = []
    while i < len(deps) and deps[i].departure <= hi:
        if deps[i].destination != f.origin:
            res.append(deps[i])
        i += 1
    return res


def parse_itinerary_id(cat: Catalog, itinerary_id: str) -> list[Flight] | None:
    legs = []
    for fid in itinerary_id.split("+"):
        f = cat.by_id.get(fid)
        if f is None:
            return None
        legs.append(f)
    return legs or None


def check_itinerary(cat: Catalog, itinerary_id: str, origin: str, destination: str,
                    day: date, after: datetime | None, max_stops: int = 1) -> str | None:
    """None if the itinerary is valid for this search, otherwise the reason."""
    legs = parse_itinerary_id(cat, itinerary_id)
    if legs is None:
        return f"unknown flight in {itinerary_id!r}"
    if len(legs) - 1 > max_stops:
        return f"{len(legs) - 1} stops, max is {max_stops}"
    if legs[0].origin != origin or legs[-1].destination != destination:
        return "wrong origin or destination"
    if legs[0].origin_local_date != day:
        return f"first leg leaves on {legs[0].origin_local_date}, searched {day}"
    if after is not None and legs[0].departure <= after:
        return "first leg already departed at the simulated time"
    seen = {legs[0].origin}
    for a, b in zip(legs, legs[1:]):
        if a.destination != b.origin:
            return f"{a.flight_id} lands at {a.destination} but {b.flight_id} leaves {b.origin}"
        lay = b.departure - a.arrival
        if not (MIN_LAYOVER <= lay <= MAX_LAYOVER):
            return f"layover {int(lay.total_seconds() // 60)} min at {a.destination}"
        if b.destination in seen:
            return "itinerary revisits an airport"
        seen.add(a.destination)
    return None
