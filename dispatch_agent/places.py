"""Track C: offline Chennai gazetteer, so disruptions can be placed anywhere by name.

Coordinates are approximate area centres. No Maps API: works offline and is
deterministic. resolve() handles case, punctuation, common aliases and typos.
"""

import difflib
import re

# name -> (lat, lng)
PLACES: dict[str, tuple[float, float]] = {
    "Guindy": (13.0067, 80.2206), "T. Nagar": (13.0418, 80.2341), "Anna Nagar": (13.0850, 80.2101),
    "Adyar": (13.0012, 80.2565), "Velachery": (12.9815, 80.2180), "Tambaram": (12.9249, 80.1000),
    "Sholinganallur": (12.9010, 80.2279), "Porur": (13.0382, 80.1565), "Mylapore": (13.0368, 80.2676),
    "Egmore": (13.0732, 80.2609), "Thiruvanmiyur": (12.9830, 80.2594), "OMR": (12.9420, 80.2430),
    "ECR": (12.9300, 80.2550), "GST Road": (12.9650, 80.1600), "Kathipara Junction": (13.0084, 80.2050),
    "Vadapalani": (13.0500, 80.2121), "Kodambakkam": (13.0521, 80.2255), "Nungambakkam": (13.0569, 80.2425),
    "Teynampet": (13.0405, 80.2503), "Saidapet": (13.0213, 80.2231), "Alandur": (13.0040, 80.2010),
    "Chromepet": (12.9516, 80.1462), "Pallavaram": (12.9675, 80.1491), "Medavakkam": (12.9200, 80.1920),
    "Perungudi": (12.9654, 80.2461), "Thoraipakkam": (12.9416, 80.2362), "Besant Nagar": (12.9990, 80.2707),
    "Royapettah": (13.0540, 80.2640), "Chennai Central": (13.0827, 80.2707), "Koyambedu": (13.0694, 80.1948),
    "Ashok Nagar": (13.0372, 80.2121), "K. K. Nagar": (13.0410, 80.1990), "Kilpauk": (13.0838, 80.2425),
    "Perambur": (13.1180, 80.2330), "Ambattur": (13.1143, 80.1548), "Chennai Airport": (12.9941, 80.1709),
    "Madipakkam": (12.9623, 80.1986), "Pallikaranai": (12.9349, 80.2137), "Navalur": (12.8459, 80.2265),
    "Marina Beach": (13.0500, 80.2824),
}

ALIASES = {
    "tnagar": "T. Nagar", "thyagaraya nagar": "T. Nagar", "pondy bazaar": "T. Nagar",
    "old mahabalipuram road": "OMR", "rajiv gandhi salai": "OMR", "it corridor": "OMR",
    "east coast road": "ECR", "gst": "GST Road", "grand southern trunk road": "GST Road",
    "kathipara": "Kathipara Junction", "kk nagar": "K. K. Nagar", "central": "Chennai Central",
    "central station": "Chennai Central", "airport": "Chennai Airport", "meenambakkam": "Chennai Airport",
    "marina": "Marina Beach", "elliots beach": "Besant Nagar", "cmbt": "Koyambedu",
}

# Rough Chennai metro bounds, for coordinates the agent supplies itself.
BOUNDS = {"lat": (12.75, 13.25), "lng": (79.95, 80.35)}

_FILLER = re.compile(r"\b(road|rd|junction|signal|area|near|at|in|the|flyover|bridge|main)\b")


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


_INDEX = {_norm(k): k for k in PLACES} | {_norm(a): k for a, k in ALIASES.items()}


def resolve(location: str) -> tuple[str, tuple[float, float]] | None:
    """Return (canonical name, (lat, lng)) for a Chennai place name, or None."""
    key = _norm(location)
    if not key:
        return None
    candidates = [key, _norm(_FILLER.sub(" ", key)), key.replace(" ", "")]
    for c in candidates:
        if c in _INDEX:
            name = _INDEX[c]
            return name, PLACES[name]
    for c in candidates:  # a known place named inside a longer phrase ("traffic near tambaram bus stand")
        for k, name in _INDEX.items():
            if len(k) >= 3 and re.search(rf"\b{re.escape(k)}\b", c):
                return name, PLACES[name]
    match = difflib.get_close_matches(candidates[1] or key, list(_INDEX), n=1, cutoff=0.8)
    if match:
        name = _INDEX[match[0]]
        return name, PLACES[name]
    return None


def in_bounds(lat: float, lng: float) -> bool:
    return BOUNDS["lat"][0] <= lat <= BOUNDS["lat"][1] and BOUNDS["lng"][0] <= lng <= BOUNDS["lng"][1]
