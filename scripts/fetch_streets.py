#!/usr/bin/env python3
"""
Build pipeline for the Paris Streets Quiz.

Pulls the canonical, per-arrondissement street name list from French
Wikipedia, matches each name against live OpenStreetMap geometry, scores
each street's real-world "fame" (for difficulty tiers), projects the
geometry into a flat SVG coordinate space, and writes the single static
data file the game loads: ../data/paris_streets.js

Run: python3 fetch_streets.py
Re-run anytime to refresh the data; cached HTTP responses under
cache/ make reruns fast unless you delete the cache.
"""
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
WIKI_CACHE = os.path.join(CACHE, "wikipedia")
DATA_OUT = os.path.join(HERE, "..", "data", "paris_streets.js")
REPORT_OUT = os.path.join(HERE, "build_report.txt")

UA = "ParisStreetsQuizBuild/1.0 (contact: vadim.limousin@lmd.ipsl.fr)"
WIKI_API = "https://fr.wikipedia.org/w/api.php"
OVERPASS_API = "https://overpass-api.de/api/interpreter"

os.makedirs(WIKI_CACHE, exist_ok=True)
os.makedirs(os.path.dirname(DATA_OUT), exist_ok=True)

report_lines = []


def report(line):
    print(line)
    report_lines.append(line)


# ---------------------------------------------------------------------------
# Stage 1: pull the street list from Wikipedia, per arrondissement
# ---------------------------------------------------------------------------

TYPE_WORDS = {
    "rue", "avenue", "boulevard", "place", "quai", "impasse", "allee", "allée",
    "passage", "pont", "square", "cours", "villa", "cite", "cité", "sentier",
    "chemin", "esplanade", "promenade", "parvis", "cour", "hameau", "viaduc",
    "degre", "degré", "galerie", "terrasse", "voie",
}

NON_STREET_PREFIXES = ("liste ", "dénomination ", "denomination ", "numérotation ",
                        "numerotation ", "histoire ", "dictionnaire ", "plan ")
LINK_WORDS = {"de", "du", "des", "la", "le", "les", "l", "d", "au", "aux", "un", "une"}


def fold_accents(s):
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def normalize(text):
    text = text.replace("œ", "oe").replace("Œ", "OE").replace("æ", "ae").replace("Æ", "AE")
    text = fold_accents(text).lower()
    text = re.sub(r"[-'’.,]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_prefix_tokens(tokens):
    tokens = list(tokens)
    if tokens[:2] == ["rond", "point"]:
        tokens = tokens[2:]
    elif tokens and tokens[0] in TYPE_WORDS:
        tokens = tokens[1:]
    while tokens and tokens[0] in LINK_WORDS:
        tokens = tokens[1:]
    return tokens


def core_key(display_name):
    tokens = normalize(display_name).split(" ") if normalize(display_name) else []
    return " ".join(strip_prefix_tokens(tokens))


def full_key(display_name):
    return normalize(display_name)


def wiki_get(params, cache_key):
    cache_path = os.path.join(WIKI_CACHE, cache_key + ".json")
    if os.path.exists(cache_path):
        return json.load(open(cache_path))
    for attempt in range(4):
        try:
            r = requests.get(WIKI_API, params=params, headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                json.dump(d, open(cache_path, "w"), ensure_ascii=False)
                return d
            time.sleep(2 * (attempt + 1))
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Wikipedia API failed for {cache_key}")


def fetch_category_members(cat_title, cache_key):
    """categorymembers of one category, {title, ns} pairs, no cmtype filter
    (so both plain pages (ns=0) and subcategories (ns=14) come back)."""
    d = wiki_get(
        {
            "action": "query", "format": "json", "list": "categorymembers",
            "cmtitle": cat_title, "cmlimit": 500,
        },
        cache_key,
    )
    return [(m["title"], m["ns"]) for m in d["query"]["categorymembers"]]


def is_probably_a_street(title):
    c = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
    low = c.lower()
    if low.startswith(NON_STREET_PREFIXES):
        return False, c
    first = low.split(" ")[0] if low else ""
    is_rond_point = low.startswith("rond-point") or low.startswith("rond point")
    if fold_accents(first) not in {fold_accents(w) for w in TYPE_WORDS} and not is_rond_point:
        return False, c
    return True, c


def fetch_arrondissement_titles():
    report("=== Stage 1: Wikipedia street titles per arrondissement ===")
    # "Catégorie:Rue à Paris" only covers literal "Rue"-titled articles.
    # French Wikipedia keeps a SEPARATE parallel category tree per street
    # type (Avenue, Boulevard, Place, Impasse, Square, Cité, Villa, ...),
    # all nested under one true master tree: "Voie dans le Xe
    # arrondissement de Paris" -> per-type subcategory -> street pages.
    # A handful of very famous streets (e.g. "Avenue des Champs-Élysées")
    # go one level deeper still: the type subcategory contains the
    # street's own eponymous container category instead of the article
    # directly. Walk all three levels rather than trusting depth 1.
    master = wiki_get(
        {
            "action": "query", "format": "json", "list": "categorymembers",
            "cmtitle": "Catégorie:Voie de Paris par arrondissement", "cmtype": "subcat", "cmlimit": 50,
        },
        "arr_master_subcats",
    )
    arr_cats = [m["title"] for m in master["query"]["categorymembers"]]
    if len(arr_cats) != 20:
        report(f"WARNING: expected 20 arrondissement subcategories, found {len(arr_cats)}")

    candidates = []  # list of (title, arrondissement)
    depth2_recursions = 0
    for arr_cat in arr_cats:
        m = re.search(r"(\d+)e?r?\s+arrondissement", arr_cat)
        arr = int(m.group(1)) if m else None
        level0 = fetch_category_members(arr_cat, "cat_" + re.sub(r"\W+", "_", arr_cat))
        for title, ns in level0:
            if ns == 0:
                candidates.append((title, arr))
                continue
            # ns == 14: a type-level subcategory, e.g. "Rue dans le Xe...",
            # "Avenue dans le Xe...", "Cité (voie) dans le Xe..."
            level1 = fetch_category_members(title, "cat_" + re.sub(r"\W+", "_", title))
            for title2, ns2 in level1:
                if ns2 == 0:
                    candidates.append((title2, arr))
                    continue
                # ns2 == 14: an eponymous container for one very famous
                # street (e.g. "Catégorie:Avenue des Champs-Élysées").
                # Only take the one member page whose title matches the
                # category's own name — never sweep in unrelated
                # sub-articles (landmarks, history sections, etc.).
                depth2_recursions += 1
                eponym = title2.split(":", 1)[-1]
                level2 = fetch_category_members(title2, "cat_" + re.sub(r"\W+", "_", title2))
                for title3, ns3 in level2:
                    if ns3 == 0 and title3 == eponym:
                        candidates.append((title3, arr))
                        break

    report(f"Raw titles across 20 arrondissements: {len(candidates)}")
    report(f"Eponymous-category recursions (famous streets filed under their own category): {depth2_recursions}")

    cleaned = {}  # title -> arrondissement (dedup, first wins)
    dropped = []
    for t, arr in candidates:
        ok, c = is_probably_a_street(t)
        if not ok:
            dropped.append(t)
            continue
        if c not in cleaned:
            cleaned[c] = arr
    report(f"Dropped as non-street (no type-word prefix / index page): {len(dropped)}")
    report(f"Cleaned + deduped candidate streets: {len(cleaned)}")
    return cleaned  # dict title -> arrondissement


def fetch_bridge_titles():
    """Bridges aren't filed under any arrondissement's 'Voie' tree (they
    don't belong to one side) — a dedicated category covers them."""
    report("\n=== Stage 1c: bridges ===")
    try:
        members = fetch_category_members("Catégorie:Pont sur la Seine à Paris", "cat_ponts_seine")
    except RuntimeError:
        report("Could not fetch bridge category; skipping.")
        return {}
    bridges = {}
    dropped = []
    for title, ns in members:
        if ns != 0:
            continue
        ok, c = is_probably_a_street(title)
        if ok:
            bridges[c] = None  # arrondissement resolved later via spatial join, like landmarks
        else:
            dropped.append(title)
    report(f"Bridge candidates: {len(bridges)} (dropped {len(dropped)} non-bridge pages)")
    return bridges


# ---------------------------------------------------------------------------
# Stage 1b: landmark supplement
#
# French Wikipedia's per-arrondissement "Rue dans le Xe arrondissement de
# Paris" categories are near-exhaustive for ordinary streets, but many of
# Paris's most famous streets are notable enough to get their own eponymous
# category tree instead (e.g. "Avenue des Champs-Élysées" is categorized
# under "Catégorie:Avenue des Champs-Élysées", not any arrondissement's
# street category) — so Stage 1 misses them entirely. This hand-curated
# list patches that gap. Their geometry comes from the already-fetched OSM
# data (Stage 2); their arrondissement is resolved by a real point-in-
# polygon spatial join (below), not guessed.
# ---------------------------------------------------------------------------

LANDMARK_SUPPLEMENT = [
    "Avenue des Champs-Élysées", "Avenue Montaigne", "Avenue Foch", "Avenue Victor Hugo",
    "Avenue de l'Opéra", "Avenue de la Grande Armée", "Avenue Kléber", "Avenue Marceau",
    "Avenue Bosquet", "Avenue des Ternes", "Avenue Jean-Jaurès", "Avenue de Clichy",
    "Avenue Parmentier", "Avenue Daumesnil", "Avenue d'Italie", "Avenue de Wagram",
    "Avenue de Friedland", "Avenue Hoche", "Avenue de Villiers", "Avenue Niel",
    "Avenue Mozart", "Avenue Rapp", "Avenue de Breteuil", "Avenue de Suffren",
    "Boulevard Saint-Germain", "Boulevard Saint-Michel", "Boulevard Haussmann",
    "Boulevard Raspail", "Boulevard du Montparnasse", "Boulevard Voltaire",
    "Boulevard Beaumarchais", "Boulevard Malesherbes", "Boulevard de Belleville",
    "Boulevard de la Chapelle", "Boulevard de Ménilmontant", "Boulevard Barbès",
    "Boulevard de Sébastopol", "Boulevard de Magenta", "Boulevard de Strasbourg",
    "Boulevard des Capucines", "Boulevard des Italiens", "Boulevard Diderot",
    "Boulevard Richard-Lenoir", "Boulevard Pereire", "Boulevard Arago",
    "Place de la Concorde", "Place Vendôme", "Place de la République",
    "Place des Vosges", "Place Charles-de-Gaulle", "Place Denfert-Rochereau",
    "Place de la Nation", "Place d'Italie", "Place Pigalle", "Place des Ternes",
    "Place Saint-Sulpice", "Place du Tertre", "Place de la Madeleine",
    "Place des Victoires", "Place du Châtelet", "Place Saint-Michel",
    "Place de la Contrescarpe", "Place Saint-Germain-des-Prés",
    "Quai des Orfèvres", "Quai de Montebello", "Quai d'Orsay", "Quai Voltaire",
    "Quai de la Tournelle", "Quai des Grands Augustins", "Quai de Béthune",
    "Quai de Bourbon", "Quai Branly", "Quai de Jemmapes", "Quai de Valmy",
    "Pont Neuf", "Pont Alexandre III", "Pont des Arts", "Pont Marie",
    "Pont Royal", "Pont Notre-Dame", "Pont Saint-Louis",
]


# ---------------------------------------------------------------------------
# Stage 1e: new landmark points, for the Daily Challenge "known places" pool
#
# A handful of arrondissements' truly iconic landmark isn't a street/place
# entry at all (the Eiffel Tower, a train station, a cemetery...). Resolved
# via exact Wikidata QIDs (not name matching) — the same disambiguation
# technique that fixed the earlier "Paris, Texas" area-matching bug, since a
# name-only search risks the same kind of false match (there are dozens of
# OSM points named "Père Lachaise" alone: métro stops, bike stations, cafés).
# ---------------------------------------------------------------------------

NEW_LANDMARKS = [
    ("Tour Eiffel", "Q243"),
    ("Gare de Lyon", "Q614049"),
    ("Bibliothèque nationale de France - François-Mitterrand", "Q24192554"),
    ("Parc des Buttes-Chaumont", "Q532113"),
    ("Cimetière du Père-Lachaise", "Q311"),
    ("Square des Batignolles", "Q623260"),
]

# The Daily Challenge "known places" pool: one per arrondissement (mostly
# reusing existing Place/Parvis/street entries that are already iconic real
# points on the map; the 5 above are the only ones needing new geocoding).
KNOWN_PLACES = [
    "Place Vendôme", "Boulevard Montmartre", "Place des Vosges",
    "Parvis Notre-Dame - place Jean-Paul-II", "Place du Panthéon",
    "Place Saint-Sulpice", "Tour Eiffel", "Place de la Concorde",
    "Place de l'Opéra", "Place de la Bataille-de-Stalingrad",
    "Boulevard Voltaire", "Gare de Lyon",
    "Bibliothèque nationale de France - François-Mitterrand",
    "Place Denfert-Rochereau", "Pont Mirabeau",
    "Place du Trocadéro-et-du-11-Novembre", "Square des Batignolles",
    "Parvis du Sacré-Cœur", "Parc des Buttes-Chaumont",
    "Cimetière du Père-Lachaise",
]

# Shorter names for the Daily Challenge UI only — the underlying street
# entry (used for matching player guesses in both modes) keeps its full
# official name; this only overrides what's shown in the route header
# and place-marker label, since people don't refer to these landmarks by
# their full official name in everyday speech.
PLACE_DISPLAY_OVERRIDES = {
    "Parvis Notre-Dame - place Jean-Paul-II": "Notre-Dame",
    "Place du Panthéon": "Panthéon",
    "Place Saint-Sulpice": "Saint-Sulpice",
    "Bibliothèque nationale de France - François-Mitterrand": "BNF",
    "Place Denfert-Rochereau": "Denfert-Rochereau",
    "Place du Trocadéro-et-du-11-Novembre": "Trocadéro",
    "Square des Batignolles": "Batignolles",
    "Parvis du Sacré-Cœur": "Sacré-Cœur",
    "Parc des Buttes-Chaumont": "Buttes-Chaumont",
    "Cimetière du Père-Lachaise": "Père-Lachaise",
}


def fetch_landmark_points():
    report("\n=== Stage 1e: new landmark points (Daily Challenge places pool) ===")
    query_parts = "\n".join(f'  nwr["wikidata"="{wid}"](area.paris);' for _, wid in NEW_LANDMARKS)
    query = f"""
[out:json][timeout:90];
area["name"="Paris"]["boundary"="administrative"]["admin_level"="8"]["wikidata"="Q90"]->.paris;
(
{query_parts}
);
out center tags;
"""
    d = overpass_query(query, "landmark_points")
    by_wikidata = {}
    for el in d["elements"]:
        wid = el.get("tags", {}).get("wikidata")
        if not wid or wid in by_wikidata:
            continue
        center = el.get("center") or {"lat": el.get("lat"), "lon": el.get("lon")}
        if center.get("lat") is None:
            continue
        by_wikidata[wid] = (center["lat"], center["lon"])

    points = {}
    for name, wid in NEW_LANDMARKS:
        if wid in by_wikidata:
            points[name] = by_wikidata[wid]
        else:
            report(f"  WARNING: landmark not resolved via wikidata={wid}: {name}")
    report(f"Landmark points resolved: {len(points)}/{len(NEW_LANDMARKS)}")
    return points


def fetch_arrondissement_polygons():
    cache_path = os.path.join(CACHE, "arrondissement_boundaries.json")
    if os.path.exists(cache_path):
        d = json.load(open(cache_path))
    else:
        query = """
[out:json][timeout:150];
area["name"="Paris"]["boundary"="administrative"]["admin_level"="8"]["wikidata"="Q90"]->.paris;
relation(area.paris)["boundary"="administrative"]["admin_level"="9"];
out geom;
"""
        d = None
        for attempt in range(5):
            try:
                r = requests.post(OVERPASS_API, data={"data": query}, headers={"User-Agent": UA}, timeout=170)
                if r.status_code == 200:
                    d = r.json()
                    break
            except requests.RequestException:
                pass
            time.sleep(15)
        if d is None:
            raise RuntimeError("Failed to fetch arrondissement boundaries")
        json.dump(d, open(cache_path, "w"))

    polys = {}
    for rel in d["elements"]:
        if rel.get("type") != "relation":
            continue
        name = rel.get("tags", {}).get("name", "")
        m = re.search(r"(\d+)", name)
        num = int(m.group(1)) if m else None
        if num is None:
            continue
        outer_lines = []
        for mem in rel.get("members", []):
            if mem.get("role") == "outer" and "geometry" in mem:
                pts = [(p["lon"], p["lat"]) for p in mem["geometry"] if p]
                if len(pts) >= 2:
                    outer_lines.append(LineString(pts))
        if not outer_lines:
            continue
        merged = unary_union(outer_lines)
        polygons = list(polygonize(merged))
        if polygons:
            polys[num] = unary_union(polygons)
    return polys


def assign_arrondissement_by_point(polygons, lon, lat):
    pt = Point(lon, lat)
    for num, poly in polygons.items():
        if poly.contains(pt):
            return num
    best_num, best_dist = None, None
    for num, poly in polygons.items():
        dist = poly.distance(pt)
        if best_dist is None or dist < best_dist:
            best_num, best_dist = num, dist
    return best_num


def add_titles_via_spatial_join(candidates, osm_by_name, titles, label):
    """Merge a list of street/bridge titles into `candidates`, resolving
    each one's arrondissement by a real point-in-polygon spatial join on
    its OSM geometry midpoint (used for anything not sourced from a
    Wikipedia arrondissement category in the first place)."""
    report(f"\n=== {label} ===")
    # Dedupe by full key (accent/case/hyphen-insensitive full name), NOT
    # core key and NOT raw exact title. Core key is too aggressive: two
    # genuinely different streets can share one (e.g. "Pont des Arts"
    # and "Villa des Arts" both reduce to "arts") — that's a real,
    # expected collision the matching algorithm already handles via the
    # ambiguous-flag/fullKey mechanism, not a reason to drop one here.
    # But raw exact title is too strict: "Pont Alexandre III" and "Pont
    # Alexandre-III" are the SAME bridge under two spellings and must
    # collapse to one entry, not create a visible duplicate.
    existing_full_keys = {full_key(t) for t in candidates}
    polygons = fetch_arrondissement_polygons()
    # Same exact-then-normalized fallback as the main Wikipedia-sourced
    # matching (Stage 2) — this path was doing exact-title lookup only,
    # which missed real matches differing just in hyphen/space style
    # (e.g. candidate "Pont de Bir-Hakeim" vs OSM's "Pont de Bir Hakeim").
    norm_index = {}
    for name in osm_by_name:
        norm_index.setdefault(normalize(name), []).append(name)
    added, skipped_present, skipped_no_osm = 0, 0, []
    for title in titles:
        if full_key(title) in existing_full_keys:
            skipped_present += 1
            continue
        polylines = osm_by_name.get(title)
        if not polylines:
            osm_names = norm_index.get(normalize(title))
            if osm_names:
                polylines = [p for n in osm_names for p in osm_by_name[n]]
        if not polylines:
            # OSM sometimes appends extra wording, e.g. a dedication
            # ("Pont de Grenelle" -> "Pont de Grenelle - Cadets de
            # Saumur") — accept a strict word-boundary prefix match.
            prefix = normalize(title) + " "
            osm_names = [n for n in osm_by_name if normalize(n).startswith(prefix)]
            if osm_names:
                polylines = [p for n in osm_names for p in osm_by_name[n]]
        if not polylines:
            skipped_no_osm.append(title)
            continue
        mid_lat, mid_lon = polylines[0][len(polylines[0]) // 2]
        arr = assign_arrondissement_by_point(polygons, mid_lon, mid_lat)
        candidates[title] = arr
        existing_full_keys.add(full_key(title))
        added += 1
    report(f"Added: {added}")
    report(f"Already present (no-op): {skipped_present}")
    if skipped_no_osm:
        report(f"Not found in OSM data (skipped): {skipped_no_osm}")
    return candidates


# ---------------------------------------------------------------------------
# Stage 1d: map background layers (boundary outline, river, parks) —
# purely decorative, not guessable quiz entries.
# ---------------------------------------------------------------------------

def overpass_query(query, cache_key):
    cache_path = os.path.join(CACHE, cache_key + ".json")
    if os.path.exists(cache_path):
        return json.load(open(cache_path))
    d = None
    for attempt in range(5):
        try:
            r = requests.post(OVERPASS_API, data={"data": query}, headers={"User-Agent": UA}, timeout=170)
            if r.status_code == 200:
                d = r.json()
                break
        except requests.RequestException:
            pass
        time.sleep(15)
    if d is None:
        raise RuntimeError(f"Overpass query failed for {cache_key}")
    json.dump(d, open(cache_path, "w"))
    return d


def _assemble_closed_rings(segments):
    """Stitch a pile of way fragments (as OSM returns them for a multi-
    member relation — dozens of short segments, each ending where the
    next one begins) into actual closed rings, via the same
    union-then-polygonize approach already used for arrondissement
    polygons. Treating every fragment as its own already-closed ring
    (the naive approach) draws a spurious straight line from each
    fragment's end back to its own start — that's what produced both
    the straight "phantom lake" and the extra diagonal lines cutting
    across Bois de Vincennes."""
    lines = [LineString([(lon, lat) for lat, lon in seg]) for seg in segments if len(seg) >= 2]
    if not lines:
        return []
    merged = unary_union(lines)
    polys = list(polygonize(merged))
    return [[(lat, lon) for lon, lat in poly.exterior.coords] for poly in polys]


def fetch_paris_boundary_rings():
    report("\n=== Stage 1d: Paris administrative boundary (outline) ===")
    query = """
[out:json][timeout:90];
relation["boundary"="administrative"]["admin_level"="8"]["name"="Paris"]["wikidata"="Q90"];
out geom;
"""
    d = overpass_query(query, "paris_boundary")
    segments = []
    for rel in d["elements"]:
        for mem in rel.get("members", []):
            if mem.get("role") == "outer" and "geometry" in mem:
                pts = [(p["lat"], p["lon"]) for p in mem["geometry"] if p]
                if len(pts) >= 2:
                    segments.append(pts)
    rings = _assemble_closed_rings(segments)
    report(f"Boundary outer ring segments: {len(segments)} -> assembled into {len(rings)} closed ring(s)")
    return rings


def _polygon_from_rings(rings, buffer_deg=0.01):
    """Build Paris's actual (non-rectangular) shape from its boundary
    outer rings, buffered outward a little (parks/water can sit just
    past the strict admin line, e.g. along the périphérique)."""
    lines = [LineString([(lon, lat) for lat, lon in r]) for r in rings if len(r) >= 2]
    merged = unary_union(lines)
    polys = list(polygonize(merged))
    poly = unary_union(polys) if polys else None
    return poly.buffer(buffer_deg) if poly else None


def _clip_ring_to_paris(ring, paris_polygon):
    """Real geometric clipping, not a point-count heuristic: a feature
    like the Seine is one continuous shape running for hundreds of km
    beyond the city, so neither "is the whole assembled ring mostly
    inside Paris" (throws the whole thing away) nor "drop any member
    fragment that's mostly outside before stitching" (can remove a
    fragment the ring needed to close at all, silently yielding nothing)
    works. Intersecting the actual polygon against Paris's real shape
    cuts it at the true boundary and keeps exactly the part that's
    genuinely there."""
    if paris_polygon is None:
        return [ring]
    try:
        poly = Polygon([(lon, lat) for lat, lon in ring])
        if not poly.is_valid:
            poly = poly.buffer(0)
        clipped = poly.intersection(paris_polygon)
    except Exception:
        return []
    if clipped.is_empty:
        return []
    polys = [clipped] if clipped.geom_type == "Polygon" else [
        g for g in getattr(clipped, "geoms", []) if g.geom_type == "Polygon"
    ]
    result = []
    for p in polys:
        result.append([(lat, lon) for lon, lat in p.exterior.coords])
        for interior in p.interiors:
            result.append([(lat, lon) for lon, lat in interior.coords])
    return result


def _rings_from_elements(elements, paris_polygon=None):
    """For water/park features: one element -> list of closed rings
    (its own geometry if a way, or its outer+inner members stitched
    into actual closed rings if a relation — a relation's boundary is
    usually many short way fragments, not one ring per member), each
    clipped to Paris's real shape — used to build a single evenodd-fill
    SVG path per element so holes (islands, exclusions) render
    correctly without needing further polygon boolean ops downstream."""
    rings = []
    for el in elements:
        if el["type"] == "way" and "geometry" in el:
            pts = [(p["lat"], p["lon"]) for p in el["geometry"] if p]
            if len(pts) >= 3:
                rings.extend(_clip_ring_to_paris(pts, paris_polygon))
        elif el["type"] == "relation":
            segments = [
                [(p["lat"], p["lon"]) for p in mem["geometry"] if p]
                for mem in el.get("members", [])
                if mem.get("role") in ("outer", "inner") and "geometry" in mem
            ]
            for ring in _assemble_closed_rings(segments):
                rings.extend(_clip_ring_to_paris(ring, paris_polygon))
    return rings


def fetch_water_rings(paris_polygon):
    """Returns a list of GROUPS of rings — one group per OSM element, each
    group's rings (outer + inner) rendered as one evenodd-fill path so
    islands (île de la Cité, île Saint-Louis) stay unpainted."""
    report("\n=== Stage 1d: Seine / water features ===")
    query = """
[out:json][timeout:120];
area["name"="Paris"]["boundary"="administrative"]["admin_level"="8"]["wikidata"="Q90"]->.paris;
(
  way["natural"="water"](area.paris);
  relation["natural"="water"](area.paris);
  way["waterway"="riverbank"](area.paris);
);
out geom;
"""
    d = overpass_query(query, "paris_water")
    groups = []
    for el in d["elements"]:
        rings = _rings_from_elements([el], paris_polygon)
        if rings:
            groups.append(rings)
    report(f"Water elements: {len(groups)} (each clipped to Paris's real shape — e.g. the Seine's own relations run for hundreds of km beyond the city)")
    return groups


def fetch_park_rings(paris_polygon):
    report("\n=== Stage 1d: major parks ===")
    query = """
[out:json][timeout:120];
area["name"="Paris"]["boundary"="administrative"]["admin_level"="8"]["wikidata"="Q90"]->.paris;
(
  way["leisure"="park"](area.paris);
  relation["leisure"="park"](area.paris);
  way["landuse"="forest"](area.paris);
  relation["landuse"="forest"](area.paris);
);
out geom;
"""
    d = overpass_query(query, "paris_parks")
    # Keep only the largest parks by rough projected area, so the map
    # isn't cluttered with every tiny square garden.
    scored = []
    for el in d["elements"]:
        rings = _rings_from_elements([el], paris_polygon)
        if not rings:
            continue
        outer = max(rings, key=len)
        lat0 = sum(p[0] for p in outer) / len(outer)
        cos_lat0 = __import__("math").cos(__import__("math").radians(lat0))
        area = 0.0
        pts = [(lon * cos_lat0, lat) for lat, lon in outer]
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            area += x1 * y2 - x2 * y1
        area = abs(area) / 2
        scored.append((area, rings))
    scored.sort(key=lambda x: -x[0])
    top = scored[:25]
    groups = [rings for _, rings in top]
    report(f"Parks kept (top 25 by area): {len(groups)}")
    return groups


# ---------------------------------------------------------------------------
# Stage 2: bulk OSM fetch (one bounded query, then match locally)
# ---------------------------------------------------------------------------

def fetch_osm_ways():
    report("\n=== Stage 2: OpenStreetMap geometry (one bounded query) ===")
    cache_path = os.path.join(CACHE, "osm_ways_geom.json")
    if os.path.exists(cache_path):
        report("Using cached OSM response.")
        return json.load(open(cache_path))
    query = """
[out:json][timeout:180];
area["name"="Paris"]["boundary"="administrative"]["admin_level"="8"]["wikidata"="Q90"]->.paris;
way(area.paris)["highway"]["name"];
out geom;
"""
    t0 = time.time()
    r = requests.post(OVERPASS_API, data={"data": query}, headers={"User-Agent": UA}, timeout=240)
    r.raise_for_status()
    d = r.json()
    report(f"Overpass fetch: {len(d['elements'])} ways in {round(time.time()-t0,1)}s")
    json.dump(d, open(cache_path, "w"), ensure_ascii=False)
    return d


def group_osm_ways_by_name(osm_data):
    by_name = {}
    for el in osm_data["elements"]:
        name = el.get("tags", {}).get("name")
        if not name or "geometry" not in el:
            continue
        pts = [(p["lat"], p["lon"]) for p in el["geometry"] if p]
        if len(pts) >= 2:
            by_name.setdefault(name, []).append(pts)
    return by_name


def cluster_polylines(polylines, threshold_deg=0.005):
    """Group a street's polylines into spatial clusters via single-
    linkage on point-to-point proximity (threshold ~500m — generous
    enough to keep a real street's own legitimate gaps merged, e.g. one
    interrupted by a park, while still separating features that are
    genuinely unrelated). Needed because two completely different OSM
    features can share an exact name by coincidence (e.g. "Avenue de la
    Chapelle" names both the real street near Barbès and an unrelated
    path by Père-Lachaise cemetery, ~5km apart); naively merging them
    creates a bogus "teleport" edge in the Daily Challenge's adjacency
    graph."""
    n = len(polylines)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def min_dist(p1, p2):
        best = None
        for a in p1:
            for b in p2:
                d = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
                if best is None or d < best:
                    best = d
        return best ** 0.5 if best is not None else float("inf")

    for i in range(n):
        for j in range(i + 1, n):
            if min_dist(polylines[i], polylines[j]) <= threshold_deg:
                union(i, j)

    clusters = {}
    for i in range(n):
        r = find(i)
        clusters.setdefault(r, []).append(i)
    return list(clusters.values())


def declutter_matched_geometry(matched, arr_polygons):
    """Post-process match_candidates_to_osm's output: drop spatially-
    implausible outlier fragments from any entry whose merged geometry
    spans multiple far-apart real-world locations under one shared name.

    Which cluster to KEEP is decided by whether it actually lands in
    the arrondissement Wikipedia filed the street under, not by which
    cluster has more points — raw point count is a bad proxy for "the
    real, well-connected street" (e.g. "Avenue de la Chapelle" split
    into a 20-point dead end by Père-Lachaise and a 3-point fragment in
    the 17e, where Wikipedia actually filed it; keeping the bigger
    cluster by point count kept the dead end and disconnected
    Père-Lachaise from the rest of the graph)."""
    report("\n=== Stage 2b: spatial sanity check on merged street geometry ===")
    fixed = 0
    for title in list(matched.keys()):
        polylines, arr = matched[title]
        if len(polylines) < 2:
            continue
        clusters = cluster_polylines(polylines)
        if len(clusters) < 2:
            continue

        def cluster_score(idxs):
            pts = [p for i in idxs for p in polylines[i]]
            lat0 = sum(p[0] for p in pts) / len(pts)
            lon0 = sum(p[1] for p in pts) / len(pts)
            computed_arr = assign_arrondissement_by_point(arr_polygons, lon0, lat0)
            matches_hint = 1 if (arr is not None and computed_arr == arr) else 0
            total_points = sum(len(polylines[i]) for i in idxs)
            return (matches_hint, total_points)

        clusters.sort(key=cluster_score, reverse=True)
        kept = [polylines[i] for i in clusters[0]]
        dropped_count = sum(len(polylines[i]) for c in clusters[1:] for i in c)
        matched[title] = (kept, arr)
        fixed += 1
        report(f"  '{title}': kept {len(clusters[0])}/{len(polylines)} segments "
               f"(chosen by matching arrondissement {arr}, not by point count), "
               f"dropped {dropped_count} points across implausible fragment(s) "
               f"(likely an unrelated OSM feature sharing the same name)")
    report(f"Streets with implausible fragments removed: {fixed}")
    return matched


def match_candidates_to_osm(candidates, osm_by_name):
    report("\n=== Matching candidate streets to OSM ways ===")
    norm_index = {}
    for name in osm_by_name:
        norm_index.setdefault(normalize(name), []).append(name)

    matched = {}   # display_name -> list of polylines (each a list of (lat,lon))
    unmatched = []
    ambiguous_norm = []
    for title, arr in candidates.items():
        if title in osm_by_name:
            matched[title] = (osm_by_name[title], arr)
            continue
        key = normalize(title)
        osm_names = norm_index.get(key)
        if osm_names and len(osm_names) == 1:
            matched[title] = (osm_by_name[osm_names[0]], arr)
        elif osm_names and len(osm_names) > 1:
            # multiple distinct OSM names normalize the same way; merge all their
            # geometry under this street (rare, e.g. accent variants in OSM data)
            polylines = []
            for n in osm_names:
                polylines.extend(osm_by_name[n])
            matched[title] = (polylines, arr)
            ambiguous_norm.append((title, osm_names))
        else:
            # OSM sometimes appends extra wording, e.g. a dedication
            # ("Pont de Grenelle" -> "Pont de Grenelle - Cadets de
            # Saumur") — accept a strict word-boundary prefix match as
            # a last resort. Same fallback tier used for bridges and
            # landmarks in add_titles_via_spatial_join; kept here too so
            # a name resolved that way at sourcing time doesn't silently
            # fail this separate, later matching pass.
            prefix = key + " "
            prefix_names = [n for n in osm_by_name if normalize(n).startswith(prefix)]
            if prefix_names:
                polylines = [p for n in prefix_names for p in osm_by_name[n]]
                matched[title] = (polylines, arr)
            else:
                unmatched.append(title)

    report(f"Matched: {len(matched)} / {len(candidates)}")
    report(f"Unmatched (dropped): {len(unmatched)}")
    if ambiguous_norm:
        report(f"Normalized-key OSM collisions merged: {len(ambiguous_norm)}")
    report("Sample unmatched: " + ", ".join(unmatched[:20]))
    return matched, unmatched


# ---------------------------------------------------------------------------
# Stage 3: fame score (Wikipedia langlinks + linkshere), threaded
# ---------------------------------------------------------------------------

def _fetch_wiki_page_data(title):
    """One cached Wikipedia query for a title's langlinks/linkshere/categories.
    Returns None on a hard fetch failure, else (langlinks, linkshere,
    is_paris_page, resolved_title). `redirects=1` follows plain redirects
    automatically (e.g. "Cour de Rome" silently redirects to "Rue de
    Rome") — resolved_title is that final title, which can itself still be
    a disambiguation page (see fetch_fame_signal's two-hop fallback)."""
    cache_key = "fameV3_" + re.sub(r"\W+", "_", title)[:120]
    cache_path = os.path.join(WIKI_CACHE, cache_key + ".json")
    if os.path.exists(cache_path):
        d = json.load(open(cache_path))
    else:
        params = {
            "action": "query", "format": "json", "prop": "langlinks|linkshere|categories",
            "titles": title, "lllimit": "500", "lhlimit": "500", "lhnamespace": "0",
            "cllimit": "500", "redirects": "1",
        }
        d = None
        for attempt in range(5):
            try:
                r = requests.get(WIKI_API, params=params, headers={"User-Agent": UA}, timeout=20)
                if r.status_code == 200:
                    d = r.json()
                    break
            except requests.RequestException:
                pass
            time.sleep(2 * (attempt + 1))
        if d is None:
            return None
        json.dump(d, open(cache_path, "w"), ensure_ascii=False)
    try:
        page = next(iter(d["query"]["pages"].values()))
        langlinks = len(page.get("langlinks", []))
        linkshere = len(page.get("linkshere", []))
        categories = [c["title"] for c in page.get("categories", [])]
        resolved_title = page.get("title", title)
    except (KeyError, StopIteration):
        langlinks, linkshere, categories, resolved_title = 0, 0, [], title
    is_paris_page = any("paris" in c.lower() for c in categories)
    return langlinks, linkshere, is_paris_page, resolved_title


def fetch_fame_signal(title):
    # Also fetches the page's own categories, so compute_fame_scores can tell
    # whether this title actually resolved to a Paris street's article. Three
    # confirmed failure modes when it doesn't, tried in order:
    #   - a bare street name is often a disambiguation page or a plain
    #     redirect on French Wikipedia, since the name is common enough to
    #     exist in several French-speaking cities (e.g. "Rue Saint-Denis"
    #     lists Paris/Montréal/Nantes/Liège versions; the real Paris article
    #     lives at "Rue Saint-Denis (Paris)") — try the ORIGINAL title's
    #     "(Paris)" variant first, since that's the overwhelmingly common
    #     pattern (it works even when the bare title redirects somewhere
    #     unhelpful, e.g. "Avenue Foch" redirects to a generic cross-France
    #     "list of streets honoring Foch" page that has no "(Paris)" variant
    #     of its own — "Avenue Foch (Paris)" is still the right guess).
    #   - occasionally the redirect lands on ANOTHER disambiguation page
    #     instead (e.g. "Cour de Rome" redirects to "Rue de Rome", itself a
    #     disambiguation page — the real article is "Rue de Rome (Paris)",
    #     not "Cour de Rome (Paris)") — only tried if the first guess fails.
    #   - the bare title is, on French Wikipedia, an entirely unrelated and
    #     far more famous subject (e.g. "Villa d'Este" is the Tivoli garden
    #     in Italy, not the minor Paris street of the same name) — naively
    #     scoring it gave that street a UNESCO World Heritage Site's
    #     langlinks/backlinks count. Not recoverable by either "(Paris)"
    #     guess, so falls back to a fame score of 0 (tier 4) rather than
    #     trusting any of the wrong pages' numbers.
    result = _fetch_wiki_page_data(title)
    if result is None:
        return title, 0, 0, False
    langlinks, linkshere, is_paris_page, resolved_title = result
    if not is_paris_page:
        candidates = []
        if not title.endswith("(Paris)"):
            candidates.append(title + " (Paris)")
        if resolved_title != title and not resolved_title.endswith("(Paris)"):
            candidates.append(resolved_title + " (Paris)")
        for candidate in candidates:
            fallback = _fetch_wiki_page_data(candidate)
            if fallback is not None and fallback[2]:
                return title, fallback[0], fallback[1], True
    return title, langlinks, linkshere, is_paris_page


def compute_fame_scores(titles, synthetic_titles=frozenset()):
    report("\n=== Stage 3: fame scoring (Wikipedia langlinks + backlinks) ===")
    scores = {}
    mismatches = []
    t0 = time.time()
    # Lower than the original 8: each title can now cost up to 2 sequential
    # requests (the "(Paris)" retry), and 8-way concurrency on top of that
    # was triggering enough transient failures/rate-limiting under
    # sustained load to misclassify hundreds of genuinely famous streets
    # as unrecoverable title collisions.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_fame_signal, t) for t in titles]
        done = 0
        for fut in as_completed(futures):
            title, langlinks, linkshere, is_paris_page = fut.result()
            # Synthetic landmarks (Tour Eiffel, Gare de Lyon...) are resolved
            # via an exact Wikidata QID, not a bare-title lookup, so their
            # real article is correct even though it's a monument/station/
            # park page with no "Paris" category of its own — only the
            # title-lookup path (every other street) needs this sanity check.
            if not is_paris_page and title not in synthetic_titles:
                scores[title] = 0
                mismatches.append(title)
            else:
                scores[title] = langlinks * 10 + linkshere
            done += 1
            if done % 500 == 0:
                report(f"  fame-scored {done}/{len(titles)}")
    report(f"Fame scoring done in {round(time.time()-t0,1)}s")
    if mismatches:
        report(f"Title collisions with an unrelated (non-Paris) Wikipedia page — "
               f"forced to tier 4 instead of trusting the wrong page's fame signal "
               f"({len(mismatches)}): {', '.join(sorted(mismatches))}")
    return scores


def assign_tiers(scores):
    ranked = sorted(scores.keys(), key=lambda t: -scores[t])
    n = len(ranked)
    cut1 = min(50, n)
    cut2 = min(200, n)
    cut3 = min(600, n)
    tiers = {}
    for i, title in enumerate(ranked):
        if i < cut1:
            tiers[title] = 1
        elif i < cut2:
            tiers[title] = 2
        elif i < cut3:
            tiers[title] = 3
        else:
            tiers[title] = 4
    report(f"\nTier cutoffs: tier1<= {cut1}, tier2<= {cut2}, tier3<= {cut3}, tier4 = rest ({n - cut3})")
    report("Sample tier 1 (most famous): " + ", ".join(ranked[:15]))
    return tiers


# ---------------------------------------------------------------------------
# Stage 4: projection + collision detection + output
# ---------------------------------------------------------------------------

def project_all(matched, extra_rings=None):
    all_lats = []
    all_lons = []
    for polylines, _arr in matched.values():
        for line in polylines:
            for lat, lon in line:
                all_lats.append(lat)
                all_lons.append(lon)
    for ring in (extra_rings or []):
        for lat, lon in ring:
            all_lats.append(lat)
            all_lons.append(lon)
    lat_min, lat_max = min(all_lats), max(all_lats)
    lon_min, lon_max = min(all_lons), max(all_lons)
    lat0 = (lat_min + lat_max) / 2
    import math
    cos_lat0 = math.cos(math.radians(lat0))

    width = (lon_max - lon_min) * cos_lat0
    height = lat_max - lat_min
    target_w = 1000
    scale = target_w / width if width else 1
    target_h = height * scale

    def proj(lat, lon):
        x = (lon - lon_min) * cos_lat0 * scale
        y = (lat_max - lat) * scale
        return round(x, 2), round(y, 2)

    return proj, target_w, target_h


def slugify(title):
    s = normalize(title)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s or "street"


def _ring_to_subpath(ring, proj, close=True):
    pts = [proj(lat, lon) for lat, lon in ring]
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    return d + " Z" if close else d


def build_adjacency_graph(id_to_points, synthetic_ids):
    """Two streets are adjacent if their geometry shares a coordinate — a
    real intersection in OSM always shares an exact node between the ways
    that cross there. Every vertex is checked, not just endpoints, since
    a street can be crossed mid-segment."""
    report("\n=== Stage 5: street-adjacency graph (Daily Challenge connectivity) ===")
    coord_index = {}
    for sid, pts in id_to_points.items():
        seen_here = set()
        for lat, lon in pts:
            key = (round(lat, 7), round(lon, 7))
            if key in seen_here:
                continue
            seen_here.add(key)
            coord_index.setdefault(key, set()).add(sid)

    adjacency = {sid: set() for sid in id_to_points}
    for ids in coord_index.values():
        if len(ids) < 2:
            continue
        ids = list(ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                adjacency[ids[i]].add(ids[j])
                adjacency[ids[j]].add(ids[i])

    edge_count = sum(len(v) for v in adjacency.values()) // 2
    report(f"Adjacency from shared coordinates: {len(adjacency)} nodes, {edge_count} edges")

    # Synthetic landmark points (Tour Eiffel etc.) won't naturally share a
    # coordinate with any street — connect each to its nearest few real
    # streets so it's actually reachable in the graph, not an isolated
    # node. Only connecting to the single nearest street is fragile: the
    # geocoded point for a park (e.g. Square des Batignolles) can land
    # closer to a named path *inside* the park than to any surrounding
    # real street, and that internal path is itself a dead end with no
    # other connections — stranding the landmark entirely. Restricting
    # candidates to streets that already have at least one real neighbor
    # (degree >= 1 in the graph built above) filters those dead ends out.
    CONNECT_COUNT = 3
    real_ids = [sid for sid in id_to_points if sid not in synthetic_ids and adjacency.get(sid)]
    for sid in synthetic_ids:
        if not id_to_points.get(sid):
            continue
        lat0, lon0 = id_to_points[sid][0]
        dists = []
        for rid in real_ids:
            best_for_rid = None
            for lat, lon in id_to_points[rid]:
                d = (lat - lat0) ** 2 + (lon - lon0) ** 2
                if best_for_rid is None or d < best_for_rid:
                    best_for_rid = d
            dists.append((best_for_rid, rid))
        dists.sort(key=lambda t: t[0])
        chosen = [rid for _, rid in dists[:CONNECT_COUNT]]
        if chosen:
            for rid in chosen:
                adjacency[sid].add(rid)
                adjacency[rid].add(sid)
            report(f"  Connected landmark '{sid}' to nearest streets: {', '.join(chosen)}")
        else:
            report(f"  WARNING: could not connect landmark '{sid}' to any street")

    return {sid: sorted(neighbors) for sid, neighbors in adjacency.items()}


def first_point_from_path_d(path_d):
    m = re.match(r"M(-?[\d.]+),(-?[\d.]+)", path_d)
    return (float(m.group(1)), float(m.group(2))) if m else (0, 0)


def build_places(streets_by_name):
    report("\n=== Stage 6: known-places pool (Daily Challenge) ===")
    places = []
    for name in KNOWN_PLACES:
        entry = streets_by_name.get(name)
        if not entry:
            report(f"  WARNING: known place not found among final streets: {name}")
            continue
        x, y = first_point_from_path_d(entry["pathD"])
        places.append({
            "id": entry["id"],
            "displayName": PLACE_DISPLAY_OVERRIDES.get(name, entry["displayName"]),
            "arrondissement": entry["arrondissement"],
            "anchorStreetId": entry["id"],
            "x": x,
            "y": y,
        })
    report(f"Places resolved: {len(places)}/{len(KNOWN_PLACES)}")
    return places


def build_output(matched, tiers, boundary_rings, water_groups, park_groups, synthetic_titles):
    report("\n=== Stage 4: projection, keys, collisions ===")
    all_extra_points = list(boundary_rings)
    for group in water_groups + park_groups:
        all_extra_points.extend(group)
    proj, view_w, view_h = project_all(matched, extra_rings=all_extra_points)

    streets = []
    core_map = {}
    raw_points_list = []  # parallel to `streets`, raw (lat,lon) for adjacency
    for title, (polylines, arr) in matched.items():
        subpaths = []
        for line in polylines:
            pts = [proj(lat, lon) for lat, lon in line]
            d = "M" + " L".join(f"{x},{y}" for x, y in pts)
            subpaths.append(d)
        path_d = " ".join(subpaths)

        ck = core_key(title)
        fk = full_key(title)
        entry = {
            "id": slugify(title),
            "displayName": title,
            "arrondissement": arr,
            "tier": tiers.get(title, 4),
            "coreKey": ck,
            "fullKey": fk,
            "ambiguous": False,
            "synthetic": title in synthetic_titles,
            "pathD": path_d,
        }
        streets.append(entry)
        raw_points_list.append([pt for line in polylines for pt in line])
        core_map.setdefault(ck, []).append(entry)

    collisions = 0
    for ck, entries in core_map.items():
        if len(entries) > 1:
            collisions += 1
            for e in entries:
                e["ambiguous"] = True
    report(f"coreKey collisions detected (marked ambiguous, require full name to match): {collisions} groups")

    # ensure unique ids
    seen_ids = {}
    for e in streets:
        base = e["id"]
        if base in seen_ids:
            seen_ids[base] += 1
            e["id"] = f"{base}-{seen_ids[base]}"
        else:
            seen_ids[base] = 0

    # Build id -> raw (lat,lon) points now, while `streets` and
    # `raw_points_list` are still aligned by index (the sort below breaks
    # that alignment) and ids are final (post-dedup).
    id_to_points = {streets[i]["id"]: raw_points_list[i] for i in range(len(streets))}
    synthetic_ids = {e["id"] for e in streets if e["synthetic"]}
    adjacency = build_adjacency_graph(id_to_points, synthetic_ids)

    streets_by_name = {e["displayName"]: e for e in streets}
    places = build_places(streets_by_name)

    streets.sort(key=lambda e: (e["arrondissement"] or 99, e["displayName"]))

    report(f"\nFinal street count: {len(streets)}")
    by_tier = {}
    for e in streets:
        by_tier[e["tier"]] = by_tier.get(e["tier"], 0) + 1
    for t in sorted(by_tier):
        report(f"  tier {t}: {by_tier[t]} streets")

    boundary_path = " ".join(_ring_to_subpath(r, proj, close=True) for r in boundary_rings)
    water_paths = [" ".join(_ring_to_subpath(r, proj) for r in group) for group in water_groups]
    park_paths = [" ".join(_ring_to_subpath(r, proj) for r in group) for group in park_groups]

    js = "// Auto-generated by scripts/fetch_streets.py — do not edit by hand.\n"
    js += "window.PARIS_VIEWBOX = " + json.dumps({"minX": 0, "minY": 0, "width": round(view_w, 1), "height": round(view_h, 1)}) + ";\n"
    js += "window.PARIS_STREETS = " + json.dumps(streets, ensure_ascii=False) + ";\n"
    js += "window.PARIS_BOUNDARY = " + json.dumps(boundary_path) + ";\n"
    js += "window.PARIS_WATER = " + json.dumps(water_paths) + ";\n"
    js += "window.PARIS_PARKS = " + json.dumps(park_paths) + ";\n"
    js += "window.PARIS_ADJACENCY = " + json.dumps(adjacency) + ";\n"
    js += "window.PARIS_PLACES = " + json.dumps(places, ensure_ascii=False) + ";\n"

    with open(DATA_OUT, "w") as f:
        f.write(js)
    report(f"\nWrote {DATA_OUT} ({os.path.getsize(DATA_OUT)/1024:.0f} KB)")


def main():
    candidates = fetch_arrondissement_titles()
    bridge_titles = fetch_bridge_titles()
    osm_data = fetch_osm_ways()
    osm_by_name = group_osm_ways_by_name(osm_data)
    candidates = add_titles_via_spatial_join(
        candidates, osm_by_name, LANDMARK_SUPPLEMENT,
        "Stage 1b: landmark supplement (Wikipedia categorization gap)")
    candidates = add_titles_via_spatial_join(
        candidates, osm_by_name, list(bridge_titles.keys()),
        "Stage 1c: bridges (not filed under any arrondissement)")
    matched, unmatched = match_candidates_to_osm(candidates, osm_by_name)
    arr_polygons = fetch_arrondissement_polygons()
    matched = declutter_matched_geometry(matched, arr_polygons)

    # New landmark points for the Daily Challenge places pool — these are
    # geocoded directly via Wikidata, not matched against OSM street names,
    # so they're merged into `matched` here rather than going through
    # match_candidates_to_osm. Each becomes a degenerate single-point
    # "polyline" (same point twice) so it flows through the exact same
    # projection/output code path as every other street.
    landmark_points = fetch_landmark_points()
    synthetic_titles = set(landmark_points.keys())
    for name, (lat, lon) in landmark_points.items():
        arr = assign_arrondissement_by_point(arr_polygons, lon, lat)
        matched[name] = ([[(lat, lon), (lat, lon)]], arr)

    boundary_rings = fetch_paris_boundary_rings()
    paris_polygon = _polygon_from_rings(boundary_rings)
    water_groups = fetch_water_rings(paris_polygon)
    park_groups = fetch_park_rings(paris_polygon)

    titles = list(matched.keys())
    scores = compute_fame_scores(titles, synthetic_titles)
    tiers = assign_tiers(scores)

    build_output(matched, tiers, boundary_rings, water_groups, park_groups, synthetic_titles)

    with open(REPORT_OUT, "w") as f:
        f.write("\n".join(report_lines) + "\n")
    report(f"\nBuild report written to {REPORT_OUT}")


if __name__ == "__main__":
    main()
