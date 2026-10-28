"""Small public-geography helpers: a geohash encoder/decoder and a tiny neighborhood
gazetteer (approximate public centroids, not Qloo data) used to put human names on
heatmap grid cells returned by Qloo."""
from __future__ import annotations

import math

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lng: float, precision: int = 6) -> str:
    lat_rng, lng_rng = [-90.0, 90.0], [-180.0, 180.0]
    bits, bit, ch, even, out = [16, 8, 4, 2, 1], 0, 0, True, []
    while len(out) < precision:
        rng, val = (lng_rng, lng) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        if val > mid:
            ch |= bits[bit]
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_decode(gh: str) -> tuple[float, float]:
    lat_rng, lng_rng, even = [-90.0, 90.0], [-180.0, 180.0], True
    for c in gh:
        cd = _BASE32.index(c)
        for mask in (16, 8, 4, 2, 1):
            rng = lng_rng if even else lat_rng
            mid = (rng[0] + rng[1]) / 2
            if cd & mask:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return (lat_rng[0] + lat_rng[1]) / 2, (lng_rng[0] + lng_rng[1]) / 2


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


# city key -> (display, center lat, center lng, [(neighborhood, lat, lng), ...])
CITIES: dict[str, tuple[str, float, float, list[tuple[str, float, float]]]] = {
    "chicago": ("Chicago", 41.8781, -87.6298, [
        ("Logan Square", 41.9234, -87.7083), ("Wicker Park", 41.9088, -87.6796),
        ("Pilsen", 41.8565, -87.6564), ("Uptown", 41.9665, -87.6533),
        ("Lakeview", 41.9434, -87.6553), ("Hyde Park", 41.7943, -87.5907),
        ("Avondale", 41.9390, -87.7110), ("West Loop", 41.8827, -87.6470),
        ("Andersonville", 41.9800, -87.6680), ("Bridgeport", 41.8381, -87.6510)]),
    "new york": ("New York", 40.7128, -74.0060, [
        ("Williamsburg", 40.7081, -73.9571), ("Bushwick", 40.6958, -73.9171),
        ("Lower East Side", 40.7150, -73.9843), ("East Village", 40.7265, -73.9815),
        ("Greenpoint", 40.7304, -73.9515), ("Ridgewood", 40.7044, -73.9018),
        ("Harlem", 40.8116, -73.9465), ("Astoria", 40.7644, -73.9235),
        ("Park Slope", 40.6710, -73.9814), ("Crown Heights", 40.6694, -73.9422)]),
    "los angeles": ("Los Angeles", 34.0522, -118.2437, [
        ("Echo Park", 34.0782, -118.2606), ("Silver Lake", 34.0869, -118.2702),
        ("Highland Park", 34.1115, -118.1923), ("Koreatown", 34.0618, -118.3004),
        ("Arts District", 34.0410, -118.2330), ("Los Feliz", 34.1063, -118.2848),
        ("Venice", 33.9850, -118.4695), ("Boyle Heights", 34.0339, -118.2054)]),
    "san francisco": ("San Francisco", 37.7749, -122.4194, [
        ("Mission District", 37.7599, -122.4148), ("SoMa", 37.7785, -122.4056),
        ("Haight-Ashbury", 37.7692, -122.4481), ("North Beach", 37.8061, -122.4103),
        ("Inner Richmond", 37.7802, -122.4645), ("Outer Sunset", 37.7550, -122.4940),
        ("Lower Haight", 37.7720, -122.4310), ("Tenderloin", 37.7847, -122.4145)]),
    "austin": ("Austin", 30.2672, -97.7431, [
        ("East Austin", 30.2620, -97.7220), ("South Congress", 30.2500, -97.7490),
        ("Hyde Park", 30.3060, -97.7280), ("Downtown / Red River", 30.2680, -97.7360),
        ("Zilker", 30.2610, -97.7720), ("North Loop", 30.3190, -97.7230)]),
    "seattle": ("Seattle", 47.6062, -122.3321, [
        ("Capitol Hill", 47.6253, -122.3222), ("Ballard", 47.6687, -122.3847),
        ("Fremont", 47.6510, -122.3500), ("University District", 47.6615, -122.3133),
        ("Georgetown", 47.5480, -122.3220), ("Columbia City", 47.5596, -122.2870)]),
    "portland": ("Portland", 45.5152, -122.6784, [
        ("Alberta Arts", 45.5590, -122.6450), ("Hawthorne", 45.5120, -122.6250),
        ("Pearl District", 45.5290, -122.6830), ("Mississippi", 45.5500, -122.6760),
        ("Division", 45.5050, -122.6300), ("St. Johns", 45.5900, -122.7540)]),
    "nashville": ("Nashville", 36.1627, -86.7816, [
        ("East Nashville", 36.1820, -86.7480), ("The Gulch", 36.1520, -86.7890),
        ("12 South", 36.1250, -86.7900), ("Germantown", 36.1800, -86.7880),
        ("Midtown", 36.1510, -86.8000), ("Wedgewood-Houston", 36.1350, -86.7700)]),
    "atlanta": ("Atlanta", 33.7490, -84.3880, [
        ("Little Five Points", 33.7640, -84.3490), ("East Atlanta", 33.7400, -84.3440),
        ("Old Fourth Ward", 33.7630, -84.3700), ("West End", 33.7360, -84.4130),
        ("Midtown", 33.7840, -84.3830), ("Decatur", 33.7748, -84.2963)]),
    "boston": ("Boston", 42.3601, -71.0589, [
        ("Allston", 42.3539, -71.1337), ("Jamaica Plain", 42.3097, -71.1151),
        ("Cambridge / Central Sq", 42.3651, -71.1032), ("Somerville / Union Sq", 42.3794, -71.0935),
        ("South End", 42.3388, -71.0765), ("Fenway", 42.3467, -71.0972)]),
    "philadelphia": ("Philadelphia", 39.9526, -75.1652, [
        ("Fishtown", 39.9727, -75.1340), ("Northern Liberties", 39.9640, -75.1420),
        ("West Philly", 39.9550, -75.2100), ("South Philly", 39.9250, -75.1700),
        ("Kensington", 39.9900, -75.1240), ("Center City", 39.9520, -75.1640)]),
    "denver": ("Denver", 39.7392, -104.9903, [
        ("RiNo", 39.7680, -104.9800), ("Capitol Hill", 39.7330, -104.9790),
        ("Baker", 39.7130, -104.9880), ("Highlands", 39.7620, -105.0110),
        ("Five Points", 39.7550, -104.9770), ("South Broadway", 39.7000, -104.9870)]),
    "minneapolis": ("Minneapolis", 44.9778, -93.2650, [
        ("Uptown", 44.9490, -93.2980), ("Northeast", 45.0010, -93.2540),
        ("Powderhorn", 44.9390, -93.2490), ("Dinkytown", 44.9810, -93.2360),
        ("North Loop", 44.9890, -93.2770), ("Longfellow", 44.9420, -93.2160)]),
    "toronto": ("Toronto", 43.6532, -79.3832, [
        ("Queen West", 43.6476, -79.4100), ("Kensington Market", 43.6547, -79.4005),
        ("Leslieville", 43.6626, -79.3360), ("The Annex", 43.6700, -79.4070),
        ("Little Portugal", 43.6470, -79.4290), ("Parkdale", 43.6400, -79.4370)]),
    "london": ("London", 51.5072, -0.1276, [
        ("Hackney", 51.5450, -0.0553), ("Camden", 51.5390, -0.1426),
        ("Peckham", 51.4740, -0.0690), ("Brixton", 51.4613, -0.1156),
        ("Dalston", 51.5460, -0.0750), ("Shoreditch", 51.5260, -0.0780),
        ("Walthamstow", 51.5830, -0.0200)]),
    "tokyo": ("Tokyo", 35.6762, 139.6503, [
        ("Shimokitazawa", 35.6615, 139.6680), ("Koenji", 35.7050, 139.6496),
        ("Shibuya", 35.6580, 139.7016), ("Shinjuku", 35.6938, 139.7034),
        ("Kichijoji", 35.7030, 139.5795), ("Nakameguro", 35.6440, 139.6990),
        ("Ikebukuro", 35.7295, 139.7109)]),
}

ALIASES = {"nyc": "new york", "new york city": "new york", "brooklyn": "new york", "la": "los angeles",
           "sf": "san francisco", "philly": "philadelphia", "atl": "atlanta", "pdx": "portland"}


def city_key(city: str) -> str | None:
    k = (city or "").strip().lower().split(",")[0].strip()
    k = ALIASES.get(k, k)
    return k if k in CITIES else None


def city_info(city: str):
    k = city_key(city)
    return CITIES[k] if k else None


def nearest_neighborhood(city: str, lat: float, lng: float, max_km: float = 6.0) -> str | None:
    info = city_info(city)
    if not info:
        return None
    best, best_d = None, 1e9
    for name, nlat, nlng in info[3]:
        d = haversine_km((lat, lng), (nlat, nlng))
        if d < best_d:
            best, best_d = name, d
    return best if best_d <= max_km else None
