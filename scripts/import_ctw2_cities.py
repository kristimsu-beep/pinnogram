import csv
import io
import os
import random
import zipfile
from collections import defaultdict

from pymongo import MongoClient, UpdateOne


# ============================================================
# CONFIG
# ============================================================

ZIP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "cities1000.zip"
)

MONGODB_URI = os.environ.get("MONGODB_URI")

# Change this only if your CTW2 database uses another name.
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "pinnogram")

TARGET_CITY_COUNT = 10000


# ============================================================
# CHECK DATABASE CONNECTION
# ============================================================

if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI environment variable is not set."
    )

if not os.path.exists(ZIP_PATH):
    raise FileNotFoundError(
        f"Could not find GeoNames file:\n{ZIP_PATH}"
    )


# ============================================================
# CONNECT TO MONGODB
# ============================================================

print("Connecting to MongoDB...")

client = MongoClient(MONGODB_URI)
db = client[MONGODB_DATABASE]

city_catalog = db["ctw2_city_catalog"]
city_states = db["ctw2_cities"]


# ============================================================
# READ GEONAMES
# ============================================================

print("Reading cities1000.zip...")

cities = []

with zipfile.ZipFile(ZIP_PATH, "r") as archive:

    txt_files = [
        name
        for name in archive.namelist()
        if name.endswith(".txt")
        and not name.startswith("__")
    ]

    if not txt_files:
        raise RuntimeError(
            "No GeoNames TXT file was found inside cities1000.zip."
        )

    txt_name = txt_files[0]

    print(f"Using: {txt_name}")

    with archive.open(txt_name) as raw_file:

        text_file = io.TextIOWrapper(
            raw_file,
            encoding="utf-8"
        )

        reader = csv.reader(
            text_file,
            delimiter="\t"
        )

        for row in reader:

            if len(row) < 19:
                continue

            try:
                geoname_id = int(row[0])
                name = row[1].strip()
                ascii_name = row[2].strip()

                latitude = float(row[4])
                longitude = float(row[5])

                feature_class = row[6].strip()
                feature_code = row[7].strip()

                country_code = row[8].strip()

                population = int(row[14] or 0)

            except (ValueError, TypeError):
                continue

            # We only want actual populated places.
            if feature_class != "P":
                continue

            if not name:
                continue

            if not country_code:
                continue

            if not (-90 <= latitude <= 90):
                continue

            if not (-180 <= longitude <= 180):
                continue

            cities.append({
                "city_id": geoname_id,
                "name": name,
                "ascii_name": ascii_name or name,
                "lat": latitude,
                "lng": longitude,
                "country_code": country_code,
                "feature_code": feature_code,
                "real_population": population
            })


print(f"Loaded {len(cities):,} populated places.")


# ============================================================
# REMOVE DUPLICATES
# ============================================================

unique = {}

for city in cities:
    unique[city["city_id"]] = city

cities = list(unique.values())

print(f"After deduplication: {len(cities):,}")


# ============================================================
# CITY PRIORITY
# ============================================================

def city_priority(city):

    feature = city["feature_code"]
    population = city["real_population"]

    score = population

    # Strongly prioritize national capitals.
    if feature == "PPLC":
        score += 10_000_000

    # Administrative capitals / seats.
    elif feature == "PPLA":
        score += 3_000_000

    elif feature == "PPLA2":
        score += 1_500_000

    elif feature == "PPLA3":
        score += 500_000

    return score


for city in cities:
    city["_priority"] = city_priority(city)


# ============================================================
# GUARANTEE COUNTRY COVERAGE
# ============================================================

by_country = defaultdict(list)

for city in cities:
    by_country[city["country_code"]].append(city)


selected = {}
remaining_candidates = []


for country_code, country_cities in by_country.items():

    country_cities.sort(
        key=lambda city: city["_priority"],
        reverse=True
    )

    # Guarantee at least one important city
    # from every country represented in GeoNames.
    first_city = country_cities[0]

    selected[first_city["city_id"]] = first_city

    # Keep the rest available for global selection.
    for city in country_cities[1:]:
        remaining_candidates.append(city)


print(
    f"Guaranteed country coverage: "
    f"{len(selected):,} cities"
)


# ============================================================
# GLOBAL SELECTION
# ============================================================

remaining_candidates.sort(
    key=lambda city: city["_priority"],
    reverse=True
)


# Prevent one country from taking over the entire 10k.
country_counts = defaultdict(int)

for city in selected.values():
    country_counts[city["country_code"]] += 1


MAX_PER_COUNTRY = 250


for city in remaining_candidates:

    if len(selected) >= TARGET_CITY_COUNT:
        break

    country = city["country_code"]

    if country_counts[country] >= MAX_PER_COUNTRY:
        continue

    selected[city["city_id"]] = city
    country_counts[country] += 1


# ============================================================
# IF STILL UNDER 10K
# ============================================================

if len(selected) < TARGET_CITY_COUNT:

    print(
        "Country limits prevented reaching 10,000. "
        "Filling remaining slots without the country cap."
    )

    for city in remaining_candidates:

        if len(selected) >= TARGET_CITY_COUNT:
            break

        if city["city_id"] in selected:
            continue

        selected[city["city_id"]] = city


cities = list(selected.values())


# ============================================================
# FINAL SORT
# ============================================================

cities.sort(
    key=lambda city: city["city_id"]
)


if len(cities) > TARGET_CITY_COUNT:

    cities = cities[:TARGET_CITY_COUNT]


print(
    f"Final CTW2 city count: {len(cities):,}"
)


# ============================================================
# PREPARE CATALOG DOCUMENTS
# ============================================================

catalog_documents = []

for city in cities:

    catalog_documents.append({
        "city_id": city["city_id"],

        "name": city["name"],
        "ascii_name": city["ascii_name"],

        "lat": city["lat"],
        "lng": city["lng"],

        "country_code": city["country_code"],
        "feature_code": city["feature_code"],

        # Real-world population is kept only as metadata.
        # CTW2 gameplay population starts at 100.
        "real_population": city["real_population"],

        "location": {
            "type": "Point",
            "coordinates": [
                city["lng"],
                city["lat"]
            ]
        }
    })


# ============================================================
# PREPARE GAME STATE
# ============================================================

state_documents = []

for city in cities:

    state_documents.append({
        "city_id": city["city_id"],

        # Nobody owns the city initially.
        "owner_username": None,

        # Every CTW2 city starts at exactly 100.
        "population": 100,

        # Capital will be selected later.
        "is_capital": False
    })


# ============================================================
# REPLACE OLD IMPORT
# ============================================================

print("Clearing previous CTW2 city catalog...")

city_catalog.delete_many({})

print("Clearing previous CTW2 city state...")

city_states.delete_many({})


# ============================================================
# INSERT CATALOG
# ============================================================

print("Inserting city catalog...")

if catalog_documents:

    for start in range(
        0,
        len(catalog_documents),
        1000
    ):

        batch = catalog_documents[
            start:start + 1000
        ]

        city_catalog.insert_many(
            batch,
            ordered=False
        )

        print(
            f"Catalog inserted: "
            f"{min(start + 1000, len(catalog_documents)):,}"
            f"/{len(catalog_documents):,}"
        )


# ============================================================
# INSERT GAME STATE
# ============================================================

print("Creating CTW2 city states...")

if state_documents:

    for start in range(
        0,
        len(state_documents),
        1000
    ):

        batch = state_documents[
            start:start + 1000
        ]

        city_states.insert_many(
            batch,
            ordered=False
        )

        print(
            f"State inserted: "
            f"{min(start + 1000, len(state_documents)):,}"
            f"/{len(state_documents):,}"
        )


# ============================================================
# INDEXES
# ============================================================

print("Creating MongoDB indexes...")

city_catalog.create_index(
    [("location", "2dsphere")]
)

city_catalog.create_index(
    [("city_id", 1)],
    unique=True
)

city_catalog.create_index(
    [("country_code", 1)]
)

city_states.create_index(
    [("city_id", 1)],
    unique=True
)

city_states.create_index(
    [("owner_username", 1)]
)

city_states.create_index(
    [("is_capital", 1)]
)


# ============================================================
# FINISHED
# ============================================================

print()
print("==============================================")
print(" CTW2 CITY IMPORT COMPLETE")
print("==============================================")
print(
    f"Cities imported: {len(cities):,}"
)
print(
    "Collection: ctw2_city_catalog"
)
print(
    "Collection: ctw2_cities"
)
print(
    "Population: 100 for every city"
)
print(
    "Geospatial index: READY"
)
print("==============================================")


client.close()
