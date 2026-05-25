"""Seed countries, cities, and topics from JSON into the DB.

Idempotent — safe to re-run. Updates existing rows on slug match.
"""

import json
from pathlib import Path

from rich.console import Console

from src.models import init_db, SessionLocal, Country, City, Topic

console = Console()
DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "data"


def seed_countries(session) -> dict[str, int]:
    rows = json.loads((DATA_DIR / "countries.json").read_text())
    slug_to_id: dict[str, int] = {}
    for row in rows:
        existing = session.query(Country).filter_by(slug=row["slug"]).one_or_none()
        if existing is None:
            existing = Country(slug=row["slug"])
            session.add(existing)
        existing.name = row["name"]
        existing.iso_code = row.get("iso_code")
        existing.region = row.get("region")
        existing.currency = row.get("currency")
        existing.languages_json = row.get("languages")
        existing.nomad_visa_available = bool(row.get("nomad_visa_available"))
        existing.summary = row.get("summary")
        session.flush()
        slug_to_id[row["slug"]] = existing.id
    console.print(f"[green]✔[/] Seeded {len(rows)} countries")
    return slug_to_id


def seed_cities(session, country_slug_to_id: dict[str, int]) -> None:
    rows = json.loads((DATA_DIR / "cities.json").read_text())
    n = 0
    for row in rows:
        country_id = country_slug_to_id.get(row["country_slug"])
        if not country_id:
            console.print(f"[yellow]skip[/] city {row['slug']} — unknown country {row['country_slug']}")
            continue
        existing = (
            session.query(City)
            .filter_by(country_id=country_id, slug=row["slug"])
            .one_or_none()
        )
        if existing is None:
            existing = City(country_id=country_id, slug=row["slug"])
            session.add(existing)
        existing.name = row["name"]
        existing.lat = row.get("lat")
        existing.lon = row.get("lon")
        existing.population = row.get("population")
        existing.summary = row.get("summary")
        n += 1
    console.print(f"[green]✔[/] Seeded {n} cities")


def seed_topics(session) -> None:
    rows = json.loads((DATA_DIR / "topics.json").read_text())
    for row in rows:
        existing = session.query(Topic).filter_by(slug=row["slug"]).one_or_none()
        if existing is None:
            existing = Topic(slug=row["slug"])
            session.add(existing)
        existing.name = row["name"]
        existing.display_order = row.get("display_order", 0)
        existing.icon = row.get("icon")
        existing.description = row.get("description")
    console.print(f"[green]✔[/] Seeded {len(rows)} topics")


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        country_map = seed_countries(session)
        seed_cities(session, country_map)
        seed_topics(session)
        session.commit()
        console.print("[bold green]Seed complete.[/]")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
