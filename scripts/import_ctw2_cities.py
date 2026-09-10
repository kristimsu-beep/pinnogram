import asyncio
import csv
import io
import os
import zipfile
from collections import defaultdict

import motor.motor_asyncio


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

ZIP_PATH = os.path.join(
    BASE_DIR,
    "data",
    "cities1000.zip"
)

MONGO_URI = "mongodb+srv://admin:jx0SNeMpug5XSz3w@robux.wb9rz4o.mongodb.net/?appName=Robux"

TARGET_CITY_COUNT = 10000


# ============================================================
# CHECK MONGODB URI
# ============================================================

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI environment variable is not set."
    )


# ============================================================
# CONNECT TO A SEPARATE CTW2 CITIES DATABASE
# ============================================================

print("Connecting to MongoDB Atlas...")

mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_URI
)

cities_db = mongo_client["ctw2_cities_db"]

city_catalog = cities_db["city_catalog"]
city_states = cities_db["city_states"]


# ============================================================
# READ GEONAMES
# ============================================================

def read_cities():

    print("Opening cities1000.zip...")

    if not os.path.exists(ZIP_PATH):
        raise FileNotFoundError(
            f"Could not find:\n{ZIP_PATH}"
        )

    cities = []

    with zipfile.ZipFile(ZIP_PATH, "r") as archive:

        txt_files = [
            name
            for name in archive.namelist()
            if name.endswith(".txt")
        ]

        if not txt_files:
            raise RuntimeError(
                "No TXT file found inside cities1000.zip."
            )

        txt_name = txt_files[0]

        print(
            f"Reading GeoNames file: {txt_name}"
        )

        with archive.open(txt_name) as raw:

            text_file = io.TextIOWrapper(
                raw,
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

                    population = int(
                        row[14] or 0
                    )

                except (ValueError, TypeError):

                    continue

                # Only populated places.
                if feature_class != "P":
                    continue

                if not name:
                    continue

                if not country_code:
                    continue

                if not (
                    -90 <= latitude <= 90
                ):
                    continue

                if not (
                    -180 <= longitude <= 180
                ):
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

    return cities


# ============================================================
# CALCULATE CITY IMPORTANCE
# ============================================================

def city_priority(city):

    population = city["real_population"]

    feature = city["feature_code"]

    score = population

    # National capital
    if feature == "PPLC":
        score += 10_000_000

    # First-level administrative center
    elif feature == "PPLA":
        score += 3_000_000

    # Second-level administrative center
    elif feature == "PPLA2":
        score += 1_500_000

    # Third-level administrative center
    elif feature == "PPLA3":
        score += 500_000

    return score


# ============================================================
# SELECT ~10,000 CITIES
# ============================================================

def select_cities(cities):

    print(
        f"GeoNames populated places: "
        f"{len(cities):,}"
    )

    # Remove duplicate IDs.
    unique = {}

    for city in cities:
        unique[city["city_id"]] = city

    cities = list(unique.values())

    # Calculate importance.
    for city in cities:
        city["_priority"] = city_priority(city)

    # Group by country.
    by_country = defaultdict(list)

    for city in cities:
        by_country[
            city["country_code"]
        ].append(city)

    selected = {}

    # --------------------------------------------------------
    # First guarantee at least one city per country.
    # --------------------------------------------------------

    for country, country_cities in by_country.items():

        country_cities.sort(
            key=lambda city: city["_priority"],
            reverse=True
        )

        city = country_cities[0]

        selected[city["city_id"]] = city

    print(
        "Countries represented: "
        f"{len(selected):,}"
    )

    # --------------------------------------------------------
    # Sort all remaining cities by importance.
    # --------------------------------------------------------

    remaining = []

    for city in cities:

        if city["city_id"] in selected:
            continue

        remaining.append(city)

    remaining.sort(
        key=lambda city: city["_priority"],
        reverse=True
    )

    # Avoid one country taking almost all 10k.
    MAX_PER_COUNTRY = 250

    country_counts = defaultdict(int)

    for city in selected.values():
        country_counts[
            city["country_code"]
        ] += 1

    # --------------------------------------------------------
    # Fill the remaining slots.
    # --------------------------------------------------------

    for city in remaining:

        if len(selected) >= TARGET_CITY_COUNT:
            break

        country = city["country_code"]

        if (
            country_counts[country]
            >= MAX_PER_COUNTRY
        ):
            continue

        selected[city["city_id"]] = city

        country_counts[country] += 1

    # --------------------------------------------------------
    # Fallback if country limits prevented 10k.
    # --------------------------------------------------------

    if len(selected) < TARGET_CITY_COUNT:

        for city in remaining:

            if len(selected) >= TARGET_CITY_COUNT:
                break

            if city["city_id"] in selected:
                continue

            selected[city["city_id"]] = city

    result = list(selected.values())

    result.sort(
        key=lambda city: city["_priority"],
        reverse=True
    )

    # Exactly 10,000 when enough data exists.
    if len(result) > TARGET_CITY_COUNT:
        result = result[:TARGET_CITY_COUNT]

    return result


# ============================================================
# IMPORT INTO MONGODB
# ============================================================

async def import_cities():

    cities = read_cities()

    cities = select_cities(cities)

    print()
    print(
        f"Selected {len(cities):,} cities."
    )

    # --------------------------------------------------------
    # Clear ONLY the CTW2 city database collections.
    # --------------------------------------------------------

    print("Clearing old city catalog...")

    await city_catalog.delete_many({})

    print("Clearing old city states...")

    await city_states.delete_many({})

    # --------------------------------------------------------
    # Build catalog documents.
    # --------------------------------------------------------

    catalog_documents = []

    for city in cities:

        catalog_documents.append({

            "city_id": city["city_id"],

            "name": city["name"],

            "ascii_name": city["ascii_name"],

            "lat": city["lat"],

            "lng": city["lng"],

            "country_code":
                city["country_code"],

            "feature_code":
                city["feature_code"],

            # Real-world population is stored only
            # for city importance/reference.
            "real_population":
                city["real_population"],

            # GeoJSON Point.
            "location": {
                "type": "Point",
                "coordinates": [
                    city["lng"],
                    city["lat"]
                ]
            }
        })

    # --------------------------------------------------------
    # Build CTW2 game-state documents.
    # --------------------------------------------------------

    state_documents = []

    for city in cities:

        state_documents.append({

            "city_id": city["city_id"],

            # No country owns the city initially.
            "owner_username": None,

            # EVERY CTW2 CITY STARTS AT 100.
            "population": 100,

            # Capital will be selected later.
            "is_capital": False
        })

    # --------------------------------------------------------
    # Insert catalog.
    # --------------------------------------------------------

    print("Inserting city catalog...")

    for start in range(
        0,
        len(catalog_documents),
        1000
    ):

        batch = catalog_documents[
            start:start + 1000
        ]

        await city_catalog.insert_many(
            batch,
            ordered=False
        )

        print(
            f"Catalog: "
            f"{min(start + 1000, len(catalog_documents)):,}"
            f"/{len(catalog_documents):,}"
        )

    # --------------------------------------------------------
    # Insert states.
    # --------------------------------------------------------

    print("Creating city game states...")

    for start in range(
        0,
        len(state_documents),
        1000
    ):

        batch = state_documents[
            start:start + 1000
        ]

        await city_states.insert_many(
            batch,
            ordered=False
        )

        print(
            f"States: "
            f"{min(start + 1000, len(state_documents)):,}"
            f"/{len(state_documents):,}"
        )

    # --------------------------------------------------------
    # Indexes.
    # --------------------------------------------------------

    print("Creating indexes...")

    await city_catalog.create_index(
        [("location", "2dsphere")]
    )

    await city_catalog.create_index(
        [("city_id", 1)],
        unique=True
    )

    await city_catalog.create_index(
        [("country_code", 1)]
    )

    await city_states.create_index(
        [("city_id", 1)],
        unique=True
    )

    await city_states.create_index(
        [("owner_username", 1)]
    )

    await city_states.create_index(
        [("is_capital", 1)]
    )

    print()
    print("======================================")
    print(" CTW2 CITY IMPORT COMPLETE")
    print("======================================")
    print(
        f"Cities: {len(cities):,}"
    )
    print(
        "Database: ctw2_cities_db"
    )
    print(
        "Catalog: city_catalog"
    )
    print(
        "States: city_states"
    )
    print(
        "Starting population: 100"
    )
    print(
        "2dsphere index: READY"
    )
    print("======================================")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        import_cities()
    )
