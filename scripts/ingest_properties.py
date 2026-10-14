import os
import sys
# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import re
import json
import openpyxl
from decimal import Decimal
from sqlalchemy.orm import Session
from app.db.session import engine, SessionLocal, Base
from app.db.models import Property, PropertyImage, PropertySpec, DocumentChunk

def parse_bedrooms(bed_str):
    if not bed_str:
        return None, None
    bed_str = str(bed_str).replace("–", "-").strip()
    match = re.search(r"(\d+)(?:\s*-\s*(\d+))?", bed_str)
    if match:
        b_min = int(match.group(1))
        b_max = int(match.group(2)) if match.group(2) else b_min
        return b_min, b_max
    return None, None

def parse_area(area_str):
    if not area_str:
        return None, None
    area_clean = str(area_str).replace("–", "-").replace("sqm", "").replace("m²", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?", area_clean)
    if match:
        a_min = Decimal(match.group(1))
        a_max = Decimal(match.group(2)) if match.group(2) else a_min
        return a_min, a_max
    return None, None

def ingest_data():
    print("Creating all tables in PostgreSQL...")
    Base.metadata.create_all(bind=engine)
    print("Tables created.")

    # Load properties seed JSON
    with open("data/properties_seed.json", "r", encoding="utf-8") as f:
        properties_data = json.load(f)

    # Load specs JSON
    specs_map = {}
    try:
        with open("data/internal_knowledge_specs.json", "r", encoding="utf-8") as f:
            specs_data = json.load(f)
            specs_map = {item["property_id"]: item for item in specs_data}
    except Exception as e:
        print("Note on specs data:", e)

    db: Session = SessionLocal()
    try:
        inserted_props = 0
        inserted_images = 0
        inserted_specs = 0

        for item in properties_data:
            pid = item["property_id"]
            
            # Check existing
            prop = db.query(Property).filter(Property.property_id == pid).first()
            if not prop:
                prop = Property(property_id=pid)
                db.add(prop)

            details = item.get("property_details", {})
            pricing = item.get("pricing", {})
            location = item.get("location", {})
            developer = item.get("developer", {})
            ownership = item.get("ownership", {})

            b_min, b_max = parse_bedrooms(details.get("bedrooms"))
            a_min, a_max = parse_area(details.get("area"))

            # Populate property attributes
            prop.project_id = item.get("project_id")
            prop.property_name = item.get("property_name")
            prop.project_name = item.get("project_name")
            prop.property_type = item.get("property_type")
            prop.status = item.get("status")

            prop.country = location.get("country", "Thailand")
            prop.province = location.get("province")
            prop.district = location.get("district")
            prop.subdistrict = location.get("subdistrict")
            prop.area = location.get("area")
            prop.address = location.get("address")
            prop.latitude = location.get("latitude")
            prop.longitude = location.get("longitude")

            prop.developer_name = developer.get("name")
            prop.developer_description = developer.get("description")

            prop.price_display = pricing.get("price")
            prop.price_numeric_min = pricing.get("price_numeric_min")
            prop.currency = pricing.get("currency", "THB")
            prop.price_note = pricing.get("price_note")

            prop.bedrooms_display = details.get("bedrooms")
            prop.bedrooms_min = b_min
            prop.bedrooms_max = b_max
            prop.bathrooms = details.get("bathrooms")
            prop.area_display = details.get("area")
            prop.area_sqm_min = a_min
            prop.area_sqm_max = a_max
            prop.units = details.get("units")

            prop.description = item.get("description")
            prop.investment_information = item.get("investment_information")
            prop.rental_information = item.get("rental_information")
            prop.payment_information = item.get("payment_information")
            prop.ownership_type = ownership.get("ownership_type")

            # Features & Booleans
            features = item.get("features", [])
            amenities = item.get("amenities", [])
            prop.features = features
            prop.amenities = amenities
            prop.facilities = item.get("facilities", [])
            prop.highlights = item.get("highlights", [])
            prop.nearby_places = item.get("nearby_places", [])

            feature_text = " ".join(features + amenities).lower()
            prop.has_pool = "pool" in feature_text
            prop.beach_access = "beach" in feature_text
            prop.sea_view = "sea" in feature_text

            prop.detail_page_url = item.get("detail_page_url")
            prop.is_public = item.get("is_public", True)

            db.flush()
            inserted_props += 1

            # Sync Images
            images = item.get("images", [])
            db.query(PropertyImage).filter(PropertyImage.property_id == pid).delete()
            for idx, img_url in enumerate(images):
                if img_url and img_url.strip():
                    img = PropertyImage(
                        property_id=pid,
                        image_url=img_url.strip(),
                        image_host=item.get("image_host", "tajlandia.com"),
                        sort_order=idx
                    )
                    db.add(img)
                    inserted_images += 1

            # Sync Specs (for Part B Claude Admin Agent)
            if pid in specs_map:
                spec_info = specs_map[pid]
                spec_fields = spec_info.get("specifications", {})
                
                spec = db.query(PropertySpec).filter(PropertySpec.property_id == pid).first()
                if not spec:
                    spec = PropertySpec(property_id=pid)
                    db.add(spec)

                spec.roof_material = spec_fields.get("roof_material")
                spec.roof_colour = spec_fields.get("roof_colour")
                spec.flooring = spec_fields.get("flooring")
                spec.walls = spec_fields.get("walls")
                spec.doors_windows = spec_fields.get("doors_and_windows")
                spec.exterior_materials = spec_fields.get("exterior_materials")
                spec.interior_materials = spec_fields.get("interior_materials")
                spec.architectural_style = spec_fields.get("architectural_style")
                spec.construction_details = spec_fields.get("construction_details", {})
                spec.internal_notes = spec_info.get("internal_notes")
                inserted_specs += 1

        db.commit()
        print(f"Ingestion successful:")
        print(f" - Properties ingested/updated: {inserted_props}")
        print(f" - Images linked: {inserted_images}")
        print(f" - Private specs linked: {inserted_specs}")

    except Exception as e:
        db.rollback()
        print("Error during ingestion:", e)
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    ingest_data()
