
import os
import zipfile
import asyncio
from pathlib import Path

import motor.motor_asyncio


# ============================================================
# CTW2 CITIES IMPORTER
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

ZIP_PATH = BASE_DIR / "data" / "cities1000.zip"

# IMPORTANT:
# The real MongoDB URI is stored in Render Environment Variables.
# Never put the actual password/URI directly into this file.
MONGO_URI = os.environ.get("MONGO_URI")

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI environment variable is not set."
    )


# ============================================================
# DATABASE
# ============================================================

mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_URI
)

cities_db = mongo_client["ctw2_cities_db"]

city_catalog = cities_db["city_catalog"]
city_states = cities_db["city_states"]


# ============================================================
# SETTINGS
# ============================================================

TARGET_CITY_COUNT = 10000

# Maximum number of selected cities from one country.
# This prevents one very large country from taking over
# the entire 10,000-city selection.
MAX_CITIES_PER_COUNTRY = 250


# ============================================================
# COUNTRY PRIORITY
# ============================================================

def calculate_city_priority(population, feature_code):
    """
    Gives important administrative cities a priority boost.

    PPLC  = capital
    PPLA  = first-order administrative seat
    PPLA2 = second-order administrative seat
    PPLA3 = third-order administrative seat
    """

    try:
        population = int(population or 0)
    except Exception:
        population = 0

    priority = population

    if feature_code == "PPLC":
        priority += 100_000_000

    elif feature_code == "PPLA":
        priority += 50_000_000

    elif feature_code == "PPLA2":
        priority += 20_000_000

    elif feature_code == "PPLA3":
        priority += 10_000_000

    return priority


# ============================================================
# IMPORT FUNCTION
# ============================================================

async def import_cities():

    print(
        "🌍 [CTW2 CITY IMPORT] "
        "Starting real-city import..."
    )

    if not ZIP_PATH.exists():
        raise FileNotFoundError(
            f"GeoNames file was not found: {ZIP_PATH}"
        )

    print(
        f"📦 [CTW2 CITY IMPORT] "
        f"Using file: {ZIP_PATH}"
    )

    # --------------------------------------------------------
    # Read GeoNames data
    # --------------------------------------------------------

    selected_cities = []

    async def read_geonames():

        nonlocal selected_cities

        with zipfile.ZipFile(ZIP_PATH, "r") as archive:

            txt_files = [
                name
                for name in archive.namelist()
                if name.endswith(".txt")
            ]

            if not txt_files:
                raise RuntimeError(
                    "No .txt file was found inside cities1000.zip."
                )

            txt_name = txt_files[0]

            print(
                f"📄 [CTW2 CITY IMPORT] "
                f"Reading {txt_name}..."
            )

            with archive.open(txt_name) as file:

                for raw_line in file:

                    try:
                        line = raw_line.decode(
                            "utf-8",
                            errors="replace"
                        ).rstrip("\n\r")

                        fields = line.split("\t")

                        # GeoNames standard format contains
                        # at least 19 fields.
                        if len(fields) < 19:
                            continue

                        geoname_id = fields[0]
                        name = fields[1]
                        ascii_name = fields[2]

                        try:
                            latitude = float(fields[4])
                            longitude = float(fields[5])
                        except Exception:
                            continue

                        feature_class = fields[6]
                        feature_code = fields[7]

                        country_code = fields[8]

                        try:
                            population = int(
                                fields[14] or 0
                            )
                        except Exception:
                            population = 0

                        # Only populated places.
                        if feature_class != "P":
                            continue

                        # Ignore places without coordinates.
                        if (
                            latitude < -90
                            or latitude > 90
                            or longitude < -180
                            or longitude > 180
                        ):
                            continue

                        priority = calculate_city_priority(
                            population,
                            feature_code
                        )

                        selected_cities.append({
                            "city_id": int(geoname_id),
                            "name": name,
                            "ascii_name": ascii_name,
                            "lat": latitude,
                            "lng": longitude,
                            "country_code": country_code,
                            "feature_code": feature_code,
                            "feature_class": feature_class,
                            "real_population": population,
                            "priority": priority,
                        })

                    except Exception:
                        continue

    await read_geonames()

    print(
        "🌍 [CTW2 CITY IMPORT] "
        f"Found {len(selected_cities)} populated places."
    )

    if not selected_cities:
        raise RuntimeError(
            "No populated places were found in the GeoNames file."
        )


    # ========================================================
    # SELECT BEST 10,000 CITIES
    # ========================================================

    # Sort by priority first.
    selected_cities.sort(
        key=lambda city: city["priority"],
        reverse=True
    )

    final_cities = []

    country_counts = {}

    # --------------------------------------------------------
    # First pass:
    # guarantee representation from countries
    # --------------------------------------------------------

    cities_by_country = {}

    for city in selected_cities:

        country = city["country_code"]

        if country not in cities_by_country:
            cities_by_country[country] = []

        cities_by_country[country].append(city)

    for country in cities_by_country:

        cities_by_country[country].sort(
            key=lambda city: city["priority"],
            reverse=True
        )

        best_city = cities_by_country[country][0]

        final_cities.append(best_city)

        country_counts[country] = 1

        if len(final_cities) >= TARGET_CITY_COUNT:
            break


    # --------------------------------------------------------
    # Second pass:
    # fill remaining slots by global priority
    # --------------------------------------------------------

    already_selected = {
        city["city_id"]
        for city in final_cities
    }

    for city in selected_cities:

        if len(final_cities) >= TARGET_CITY_COUNT:
            break

        city_id = city["city_id"]

        if city_id in already_selected:
            continue

        country = city["country_code"]

        current_count = country_counts.get(
            country,
            0
        )

        if current_count >= MAX_CITIES_PER_COUNTRY:
            continue

        final_cities.append(city)

        already_selected.add(city_id)

        country_counts[country] = (
            current_count + 1
        )


    # Remove temporary priority field.
    for city in final_cities:
        city.pop("priority", None)


    print(
        "🏙️ [CTW2 CITY IMPORT] "
        f"Selected {len(final_cities)} cities."
    )


    # ========================================================
    # PREPARE CATALOG DOCUMENTS
    # ========================================================

    catalog_documents = []

    for city in final_cities:

        catalog_documents.append({
            "city_id": city["city_id"],
            "name": city["name"],
            "ascii_name": city["ascii_name"],
            "lat": city["lat"],
            "lng": city["lng"],
            "country_code": city["country_code"],
            "feature_code": city["feature_code"],
            "feature_class": city["feature_class"],
            "real_population": city["real_population"],

            # GeoJSON point.
            "location": {
                "type": "Point",
                "coordinates": [
                    city["lng"],
                    city["lat"]
                ]
            }
        })


    # ========================================================
    # RESET CATALOG/STATES
    # ========================================================

    print(
        "🧹 [CTW2 CITY IMPORT] "
        "Clearing old city catalog/state data..."
    )

    await city_catalog.delete_many({})
    await city_states.delete_many({})


    # ========================================================
    # INSERT CITY CATALOG
    # ========================================================

    if catalog_documents:

        print(
            "📥 [CTW2 CITY IMPORT] "
            "Inserting city catalog..."
        )

        await city_catalog.insert_many(
            catalog_documents,
            ordered=False
        )


    # ========================================================
    # CREATE CITY STATES
    # ========================================================

    state_documents = []

    for city in final_cities:

        state_documents.append({
            "city_id": city["city_id"],

            # No country owns the city initially.
            "owner_username": None,

            # Every CTW2 city starts at 100 population.
            "population": 100,

            # Capital status starts disabled.
            "is_capital": False
        })


    if state_documents:

        print(
            "📥 [CTW2 CITY IMPORT] "
            "Creating city states..."
        )

        await city_states.insert_many(
            state_documents,
            ordered=False
        )


    # ========================================================
    # INDEXES
    # ========================================================

    print(
        "⚡ [CTW2 CITY IMPORT] "
        "Creating MongoDB indexes..."
    )

    await city_catalog.create_index(
        [("city_id", 1)],
        unique=True
    )

    await city_catalog.create_index(
        [("location", "2dsphere")]
    )

    await city_catalog.create_index(
        [("country_code", 1)]
    )

    await city_catalog.create_index(
        [("feature_code", 1)]
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


    # ========================================================
    # COMPLETE
    # ========================================================

    print(
        "🎉 [CTW2 CITY IMPORT COMPLETE] "
        f"{len(final_cities)} real cities are now "
        "available in ctw2_cities_db."
    )

    return len(final_cities)


# ============================================================
# AUTOMATIC STARTUP INITIALIZATION
# ============================================================

async def initialize_ctw2_cities():

    try:

        existing_city = await city_catalog.find_one(
            {},
            {"_id": 1}
        )

        # ----------------------------------------------------
        # Database already contains cities.
        # ----------------------------------------------------

        if existing_city:

            print(
                "🌍 [CTW2 CITIES] "
                "City database already initialized. "
                "Skipping import."
            )

            return


        # ----------------------------------------------------
        # Database is empty.
        # Perform first-time import.
        # ----------------------------------------------------

        print(
            "🌍 [CTW2 CITIES] "
            "No cities found. "
            "Starting first-time import..."
        )

        await import_cities()

        print(
            "🌍 [CTW2 CITIES] "
            "First-time city import completed successfully!"
        )

    except Exception as e:

        print(
            "🚨 [CTW2 CITIES INITIALIZATION ERROR] "
            f"{e}"
        )


# ============================================================
# MANUAL SCRIPT EXECUTION
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        import_cities()
    )
