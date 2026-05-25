"""Hub-and-spoke topology for Get ZEN.

Clusters are derived dynamically from the DB rather than hardcoded:

  • country-cluster — one per country. Pillar = /{country}/. Members =
    every /{country}/{city}/ city guide and /{country}/{city}/{topic}/
    topic deep-spoke for that country.

  • topic-cluster — one per topic. Pillar = the programmatic database
    page that matches that topic when one exists (e.g. /visa-database/
    for the "visa" topic, /cost-of-living-rankings/ for "cost-of-living",
    /internet-speeds/ for "internet"). Members = the /{country}/{city}/
    {topic}/ pages across every covered country/city for that topic.

The country-cluster takes priority when a page belongs to both — the
parent country is almost always the closer topical neighbor than a
cross-country topic siblings group.

Public API:
    cluster_for(path)     -> Cluster | None
    other_members(path)   -> list[ClusterLink]
    pillar_link_for(path) -> ClusterLink | None
    anchor_for(path)      -> str
    build_cluster_ctx(path) -> dict     # for cluster_nav macro
    list_country_paths()  -> list[str]  # /{country}/
    list_city_paths()     -> list[str]
    list_topic_paths()    -> list[str]  # /{country}/{city}/{topic}/

DB access is cached at module load. Call invalidate_cache() after seeding
or landing-page regeneration to refresh.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from src.models import SessionLocal, Country, City, Topic


# ── Topic → programmatic pillar override ─────────────────────────────────
# Each rankable topic has a /rankings/{topic}/ pillar that acts as the
# cross-cutting cluster pillar. Topics not in this map (logistics,
# scam-prevention) are informational-only and don't have a topic cluster.
_TOPIC_PILLAR_PATH: dict[str, str] = {
    "visa": "/rankings/visa/",
    "cost-of-living": "/rankings/cost-of-living/",
    "internet": "/rankings/internet/",
    "safety": "/rankings/safety/",
    "banking": "/rankings/banking/",
    "crypto": "/rankings/crypto/",
    "healthcare": "/rankings/healthcare/",
    "coworking": "/rankings/coworking/",
    "housing": "/rankings/housing/",
}


# The topics surfaced as "rankable" — used by /rankings/ hub and homepage.
# Order matters: this is the order they appear on the rankings hub.
RANKABLE_TOPIC_SLUGS: tuple[str, ...] = (
    "visa",
    "cost-of-living",
    "internet",
    "safety",
    "crypto",
    "banking",
    "healthcare",
    "coworking",
    "housing",
)


@dataclass(frozen=True)
class ClusterLink:
    path: str
    anchor: str
    description: str = ""


@dataclass(frozen=True)
class Cluster:
    key: str
    name: str
    pillar: ClusterLink
    members: tuple[ClusterLink, ...]
    summary: str = ""

    def all_paths(self) -> tuple[str, ...]:
        return (self.pillar.path,) + tuple(m.path for m in self.members)


# ── Cache ─────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache: dict | None = None


def _build_cache() -> dict:
    """Pull country/city/topic data once, build cluster index tables."""
    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).all()
        cities = session.query(City).order_by(City.name).all()
        topics = session.query(Topic).order_by(Topic.display_order).all()

        country_by_id = {c.id: c for c in countries}
        cities_by_country: dict[int, list[City]] = {}
        for ci in cities:
            cities_by_country.setdefault(ci.country_id, []).append(ci)

        country_clusters: dict[str, Cluster] = {}
        topic_clusters: dict[str, Cluster] = {}

        # ── Country clusters ────────────────────────────────────────────
        for c in countries:
            pillar_path = f"/{c.slug}/"
            pillar = ClusterLink(
                path=pillar_path,
                anchor=f"{c.name} — Digital Nomad Guide",
                description=c.summary or f"Digital nomad intelligence for {c.name}.",
            )
            members: list[ClusterLink] = []
            # Country-topic guides (the drilldown from /rankings/{topic}/)
            for t in topics:
                country_topic_path = f"/{c.slug}/{t.slug}/"
                members.append(ClusterLink(
                    path=country_topic_path,
                    anchor=f"{t.name} in {c.name}",
                    description=t.description or "",
                ))
            for ci in sorted(cities_by_country.get(c.id, []), key=lambda x: x.name):
                city_path = f"/{c.slug}/{ci.slug}/"
                members.append(ClusterLink(
                    path=city_path,
                    anchor=f"{ci.name}, {c.name} — Nomad Guide",
                    description=ci.summary or f"Practical guide for digital nomads in {ci.name}.",
                ))
                for t in topics:
                    topic_path = f"/{c.slug}/{ci.slug}/{t.slug}/"
                    members.append(ClusterLink(
                        path=topic_path,
                        anchor=f"{t.name} in {ci.name}",
                        description=t.description or "",
                    ))
            country_clusters[c.slug] = Cluster(
                key=f"country:{c.slug}",
                name=c.name,
                pillar=pillar,
                members=tuple(members),
                summary=(
                    f"Everything Get ZEN covers on {c.name} — visas, costs, "
                    f"safety, internet, neighborhoods, and the practical "
                    f"on-the-ground details for each city."
                ),
            )

        # ── Topic clusters ──────────────────────────────────────────────
        for t in topics:
            pillar_path = _TOPIC_PILLAR_PATH.get(t.slug)
            if not pillar_path:
                # Topic without a dedicated programmatic pillar — skip;
                # country-cluster fully covers these pages.
                continue
            pillar = ClusterLink(
                path=pillar_path,
                anchor=f"{t.name} — Destinations",
                description=t.description or "",
            )
            members: list[ClusterLink] = []
            for c in countries:
                for ci in sorted(cities_by_country.get(c.id, []), key=lambda x: x.name):
                    topic_path = f"/{c.slug}/{ci.slug}/{t.slug}/"
                    members.append(ClusterLink(
                        path=topic_path,
                        anchor=f"{t.name} in {ci.name}, {c.name}",
                        description="",
                    ))
            topic_clusters[t.slug] = Cluster(
                key=f"topic:{t.slug}",
                name=t.name,
                pillar=pillar,
                members=tuple(members),
                summary=(
                    f"Cross-country {t.name.lower()} coverage — every city "
                    f"deep-dive on this topic in one place."
                ),
            )

        # ── Path → cluster index (country-cluster takes priority) ───────
        path_to_country_cluster: dict[str, str] = {}
        for slug, cl in country_clusters.items():
            for p in cl.all_paths():
                path_to_country_cluster[p.rstrip("/")] = slug

        path_to_topic_cluster: dict[str, str] = {}
        for slug, cl in topic_clusters.items():
            # The pillar's own path → topic-cluster (so e.g. /visa-database/
            # itself surfaces the topic cluster). Topic-page paths (which
            # are also in country clusters) intentionally do NOT override.
            path_to_topic_cluster[cl.pillar.path.rstrip("/")] = slug

        country_pillar_paths = [f"/{c.slug}/" for c in countries]
        city_paths = [
            f"/{country_by_id[ci.country_id].slug}/{ci.slug}/"
            for ci in cities if ci.country_id in country_by_id
        ]
        topic_paths = []
        for c in countries:
            for ci in cities_by_country.get(c.id, []):
                for t in topics:
                    topic_paths.append(f"/{c.slug}/{ci.slug}/{t.slug}/")

        return {
            "country_clusters": country_clusters,
            "topic_clusters": topic_clusters,
            "path_to_country_cluster": path_to_country_cluster,
            "path_to_topic_cluster": path_to_topic_cluster,
            "country_paths": country_pillar_paths,
            "city_paths": city_paths,
            "topic_paths": topic_paths,
        }
    finally:
        session.close()


def _ensure_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is None:
            _cache = _build_cache()
        return _cache


def invalidate_cache() -> None:
    """Drop cached topology — call after country/city/topic mutations."""
    global _cache
    with _lock:
        _cache = None


# ── Public API ────────────────────────────────────────────────────────────
def _norm(path: str) -> str:
    if not path:
        return ""
    p = "/" + path.lstrip("/")
    return p.rstrip("/")


def cluster_for(path: str) -> Optional[Cluster]:
    cache = _ensure_cache()
    n = _norm(path)
    if not n:
        return None
    if n in cache["path_to_country_cluster"]:
        return cache["country_clusters"][cache["path_to_country_cluster"][n]]
    if n in cache["path_to_topic_cluster"]:
        return cache["topic_clusters"][cache["path_to_topic_cluster"][n]]
    return None


def other_members(path: str, *, limit: int = 12) -> list[ClusterLink]:
    cluster = cluster_for(path)
    if cluster is None:
        return []
    n = _norm(path)
    out: list[ClusterLink] = []
    for m in cluster.members:
        if _norm(m.path) == n:
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def pillar_link_for(path: str) -> Optional[ClusterLink]:
    cluster = cluster_for(path)
    if cluster is None:
        return None
    if _norm(cluster.pillar.path) == _norm(path):
        return None
    return cluster.pillar


def anchor_for(path: str) -> str:
    cluster = cluster_for(path)
    if cluster is None:
        return path
    n = _norm(path)
    if _norm(cluster.pillar.path) == n:
        return cluster.pillar.anchor
    for m in cluster.members:
        if _norm(m.path) == n:
            return m.anchor
    return path


def build_cluster_ctx(path: str, *, limit: int = 12) -> dict:
    cluster = cluster_for(path)
    if cluster is None:
        return {"cluster": None, "pillar": None, "others": [], "is_pillar": False}
    pillar = pillar_link_for(path)
    return {
        "cluster": cluster,
        "pillar": pillar,
        "others": other_members(path, limit=limit),
        "is_pillar": pillar is None,
    }


def list_country_paths() -> list[str]:
    return list(_ensure_cache()["country_paths"])


def list_city_paths() -> list[str]:
    return list(_ensure_cache()["city_paths"])


def list_topic_paths() -> list[str]:
    return list(_ensure_cache()["topic_paths"])
