#!/usr/bin/env python3
"""
Letterboxd Watchlist Summary.

Requirements:
    pip install requests beautifulsoup4 tqdm
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag
from tqdm import tqdm

BASE_URL = "https://letterboxd.com"
WATCHLIST_PATH = "watchlist"
POSTER_SELECTORS = (
    "li.poster-container",
    "li[data-film-slug]",
    "div.poster-container",
    "div.poster",
    "[data-film-slug]",
    "div.react-component[data-component-class='LazyPoster']",
)
WATCHLIST_CONTAINER_SELECTORS = (
    ".js-watchlist-content",
    ".watchlist-content",
    ".poster-list",
    "#content",
)
ISO_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")
MINS_DURATION = re.compile(r"(\d+)\s*mins?")


@dataclass
class FilmEntry:
    title: str
    slug: str
    runtime_minutes: Optional[int] = None

    @property
    def url(self) -> str:
        slug = self.slug.strip("/")
        if not slug.startswith("film/"):
            slug = f"film/{slug}"
        return f"{BASE_URL}/{slug}/"


def normalize_username(raw: str) -> str:
    value = raw.strip().lstrip("@")
    return value.split("/")[0]


def slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").title()


def normalize_film_slug(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    slug = raw.strip().strip("/")
    if not slug:
        return None
    if slug.startswith("film/"):
        slug = slug[len("film/") :]
    slug = slug.split("/")[0]
    return slug or None


def iter_watchlist_containers(soup: BeautifulSoup) -> Iterable[Tag]:
    yielded: set[int] = set()
    for selector in WATCHLIST_CONTAINER_SELECTORS:
        for node in soup.select(selector):
            node_id = id(node)
            if node_id in yielded:
                continue
            yielded.add(node_id)
            yield node
    root_id = id(soup)
    if root_id not in yielded:
        yield soup


def slug_from_film_link(link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    parsed = urlparse(link)
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "film":
        return normalize_film_slug(parts[1])
    if parts and parts[0]:
        return normalize_film_slug(parts[-1])
    return None


def extract_from_link_nodes(soup: BeautifulSoup, seen: set[str]) -> list[FilmEntry]:
    films: list[FilmEntry] = []
    for container in iter_watchlist_containers(soup):
        for anchor in container.select("a[href^='/film/']"):
            slug = slug_from_film_link(anchor.get("href"))
            if not slug or slug in seen:
                continue
            title_candidate = (
                anchor.get("data-film-name")
                or anchor.get("aria-label")
                or anchor.get("title")
            )
            if not title_candidate:
                img = anchor.find("img")
                if img and img.get("alt"):
                    title_candidate = img["alt"]
            seen.add(slug)
            films.append(FilmEntry(title_candidate or slug_to_title(slug), slug))
    return films


def extract_slug_and_title(node: Tag) -> Optional[FilmEntry]:
    slug_candidate = (
        node.get("data-film-slug")
        or node.get("data-item-slug")
    )
    link_candidate = (
        node.get("data-film-link")
        or node.get("data-item-link")
        or node.get("data-target-link")
    )
    title_candidate = (
        node.get("data-film-name")
        or node.get("data-film-title")
        or node.get("data-item-name")
        or node.get("data-item-full-display-name")
    )

    if not slug_candidate:
        poster = node.find(attrs={"data-film-slug": True})
        if poster:
            slug_candidate = (
                poster.get("data-film-slug")
                or poster.get("data-film-link")
                or poster.get("data-target-link")
            )
            title_candidate = (
                title_candidate
                or poster.get("data-film-name")
                or poster.get("data-item-name")
                or poster.get("aria-label")
            )
    if not slug_candidate and link_candidate:
        slug_candidate = slug_from_film_link(link_candidate)

    slug = normalize_film_slug(slug_candidate)
    if not slug:
        return None
    if not title_candidate:
        img = node.find("img")
        if img and img.get("alt"):
            title_candidate = img["alt"]
    title = title_candidate or slug_to_title(slug)
    return FilmEntry(title, slug)


def extract_films_from_soup(soup: BeautifulSoup) -> list[FilmEntry]:
    selectors = ", ".join(POSTER_SELECTORS)
    nodes = soup.select(selectors)
    films: list[FilmEntry] = []
    seen: set[str] = set()
    for node in nodes:
        film = extract_slug_and_title(node)
        if not film or film.slug in seen:
            continue
        seen.add(film.slug)
        films.append(film)
    if films:
        return films
    return extract_from_link_nodes(soup, seen)


def watchlist_looks_private(soup: BeautifulSoup) -> bool:
    text = soup.get_text(" ", strip=True).lower()
    keywords = (
        "watchlist is private",
        "this list is private",
        "log in to view",
        "private watchlist",
        "just a moment",
        "sorry, we can't find",
        "perhaps you imagined it",
        "access denied",
    )
    return any(keyword in text for keyword in keywords)


def fetch_watchlist_via_rss(session: requests.Session, username: str) -> list[FilmEntry]:
    url = f"{BASE_URL}/{username}/{WATCHLIST_PATH}/rss/"
    resp = session.get(url, timeout=30)
    if resp.status_code in {401, 403, 404}:
        print(f"[i] Could not access the RSS feed (HTTP {resp.status_code}). Continuing without it.")
        return []
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "xml")
    films: list[FilmEntry] = []
    seen: set[str] = set()
    for item in soup.find_all("item"):
        link_tag = item.find("link")
        title_tag = item.find("title")
        slug = slug_from_film_link(link_tag.text if link_tag else None)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        title = title_tag.text.strip() if title_tag and title_tag.text else slug_to_title(slug)
        films.append(FilmEntry(title, slug))
    return films


def fetch_watchlist(session: requests.Session, username: str) -> list[FilmEntry]:
    films: list[FilmEntry] = []
    seen: set[str] = set()
    page = 1
    while True:
        url = f"{BASE_URL}/{username}/{WATCHLIST_PATH}/"
        if page > 1:
            url = f"{url}page/{page}/"
        resp = session.get(url, timeout=30)
        if resp.status_code == 404:
            raise ValueError("User does not exist or watchlist is private.")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        page_films = extract_films_from_soup(soup)
        if not page_films:
            if page == 1:
                if watchlist_looks_private(soup):
                    raise ValueError("The watchlist appears to be private or requires login.")
                fallback = fetch_watchlist_via_rss(session, username)
                if fallback:
                    print("[i] No posters detected in HTML, using the RSS feed as a fallback.")
                    return fallback
            break
        for film in page_films:
            if film.slug in seen:
                continue
            seen.add(film.slug)
            films.append(film)
        page += 1
    return films


def parse_iso_duration(value: str) -> Optional[int]:
    match = ISO_DURATION.match(value.strip())
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours * 60 + minutes


def parse_minutes_from_text(text: str) -> Optional[int]:
    match = MINS_DURATION.search(text)
    return int(match.group(1)) if match else None


def fetch_runtime_minutes(session: requests.Session, film: FilmEntry) -> Optional[int]:
    resp = session.get(film.url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    duration_meta = soup.find("meta", {"itemprop": "duration"})
    if duration_meta and duration_meta.get("content"):
        minutes = parse_iso_duration(duration_meta["content"])
        if minutes:
            return minutes

    runtime_tag = soup.select_one("[data-testid='runtime']") or soup.find(class_="text-link")
    if runtime_tag and runtime_tag.get_text(strip=True):
        minutes = parse_minutes_from_text(runtime_tag.get_text(" ", strip=True))
        if minutes:
            return minutes
    return None


def gather_runtimes(session: requests.Session, films: Iterable[FilmEntry], delay: float) -> None:
    film_list = list(films)
    total = len(film_list)

    with tqdm(total=total, desc="Analyzing movies", unit="movie", ncols=80) as pbar:
        for idx, film in enumerate(film_list, 1):
            try:
                film.runtime_minutes = fetch_runtime_minutes(session, film)
            except requests.RequestException as exc:
                print(f"[!] Error reading {film.title}: {exc}", file=sys.stderr)
            pbar.update(1)
            if delay and idx < total:
                time.sleep(delay)


def format_duration_hms(total_seconds: int) -> str:
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} hours, {minutes} minutes and {seconds} seconds"


def format_duration_days(total_minutes: int) -> str:
    if total_minutes < 24 * 60:
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours} hours and {minutes} minutes"
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days} days, {hours} hours and {minutes} minutes"


def print_summary(films: list[FilmEntry]) -> None:
    runtimes = [f for f in films if f.runtime_minutes]
    if not runtimes:
        print("No runtimes could be retrieved for any movie.")
        return

    total_minutes = sum(f.runtime_minutes for f in runtimes if f.runtime_minutes)
    total_seconds = total_minutes * 60
    average_seconds = total_seconds / len(runtimes)

    print(f"Time needed to watch all the movies: {format_duration_days(total_minutes)}")
    print(f"Average time per movie: {format_duration_hms(int(average_seconds))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summary of a Letterboxd watchlist.")
    parser.add_argument("username", nargs="?", help="Username (without @)")
    parser.add_argument("--delay", type=float, default=0.1, help="Pause between movie requests (s)")
    args = parser.parse_args()

    username = normalize_username(args.username or input("Letterboxd username: "))
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )

    try:
        films = fetch_watchlist(session, username)
    except ValueError as err:
        print(f"[!] {err}")
        sys.exit(1)
    except requests.RequestException as err:
        print(f"[!] Error communicating with Letterboxd: {err}")
        sys.exit(1)

    if not films:
        print("The watchlist is empty.")
        return

    print(f"Processing {len(films)} movies, this may take a while...")
    try:
        gather_runtimes(session, films, args.delay)
    except requests.RequestException as err:
        print(f"[!] Error retrieving durations: {err}")
    print_summary(films)


if __name__ == "__main__":
    main()
