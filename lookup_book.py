#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BOOKS_DIR = Path(__file__).parent / "books"
USER_AGENT = "SporadicalCataloguePilot/0.1"


def normalise_isbn(value: str) -> str:
    """Remove formatting and retain digits or a terminal X."""
    return "".join(character for character in value.upper() if character.isdigit() or character == "X")


def is_valid_isbn13(isbn: str) -> bool:
    if len(isbn) != 13 or not isbn.isdigit():
        return False

    total = sum(
        int(character) * (1 if index % 2 == 0 else 3)
        for index, character in enumerate(isbn[:12])
    )
    expected_check_digit = (10 - total % 10) % 10
    return expected_check_digit == int(isbn[-1])


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise RuntimeError(
                f"Quota exceeded while retrieving {url}"
            ) from error

        raise RuntimeError(
            f"HTTP {error.code} while retrieving {url}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach {url}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON returned by {url}") from error


def load_sporadical_record(isbn: str) -> dict[str, Any]:
    path = BOOKS_DIR / f"{isbn}.json"

    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {path}: {error}") from error


def get_open_library_record(isbn: str) -> dict[str, Any]:
    fields = ",".join(
        [
            "key",
            "title",
            "subtitle",
            "author_name",
            "publisher",
            "first_publish_year",
            "publish_date",
            "number_of_pages_median",
            "language",
            "isbn",
            "cover_i",
        ]
    )

    query = urllib.parse.urlencode(
        {
            "isbn": isbn,
            "fields": fields,
            "limit": 5,
        }
    )

    data = fetch_json(f"https://openlibrary.org/search.json?{query}")
    documents = data.get("docs", [])

    if not documents:
        return {}

    record = documents[0]

    cover_id = record.get("cover_i")
    cover_url = (
        f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
        if cover_id
        else None
    )

    return {
        "title": record.get("title"),
        "subtitle": record.get("subtitle"),
        "contributors": [
            {"name": name, "role": "author"}
            for name in record.get("author_name", [])
        ] or None,
        "publisher": first_item(record.get("publisher")),
        "publication_date": normalise_date(
            first_item(record.get("publish_date"))
            or record.get("first_publish_year")
        ),
        "language": first_item(record.get("language")),
        "page_count": record.get("number_of_pages_median"),
        "cover_url": cover_url,
        "external_ids": {
            "openlibrary": record.get("key")
        },
    }


def get_google_books_record(isbn: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"q": f"isbn:{isbn}"})
    data = fetch_json(f"https://www.googleapis.com/books/v1/volumes?{query}")
    items = data.get("items", [])

    if not items:
        return {}

    item = items[0]
    volume = item.get("volumeInfo", {})
    image_links = volume.get("imageLinks", {})

    return {
        "title": volume.get("title"),
        "subtitle": volume.get("subtitle"),
        "contributors": [
            {"name": name, "role": "author"}
            for name in volume.get("authors", [])
        ] or None,
        "publisher": volume.get("publisher"),
        "publication_date": normalise_date(volume.get("publishedDate")),
        "language": volume.get("language"),
        "page_count": volume.get("pageCount"),
        "subjects": volume.get("categories"),
        "long_description": volume.get("description"),
        "cover_url": (
            image_links.get("extraLarge")
            or image_links.get("large")
            or image_links.get("medium")
            or image_links.get("thumbnail")
        ),
        "external_ids": {
            "google_books": item.get("id")
        },
    }


def first_item(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value


def normalise_date(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def has_usable_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def merge_metadata(
    sporadical: dict[str, Any],
    open_library: dict[str, Any],
    google_books: dict[str, Any],
) -> dict[str, Any]:
    suppress = set(sporadical.get("suppress_import", []))

    fields = [
        "isbn_13",
        "title",
        "subtitle",
        "contributors",
        "publisher",
        "publication_date",
        "language",
        "format",
        "page_count",
        "short_description",
        "long_description",
        "subjects",
        "collections",
        "cover_url",
    ]

    sources = [
        ("sporadical", sporadical),
        ("openlibrary", open_library),
        ("google_books", google_books),
    ]

    merged: dict[str, Any] = {}

    for field in fields:
        if field in suppress:
            merged[field] = {
                "value": None,
                "source": "suppressed_by_sporadical",
            }
            continue

        selected_value = None
        selected_source = None

        for source_name, source_record in sources:
            candidate = source_record.get(field)
            if has_usable_value(candidate):
                selected_value = candidate
                selected_source = source_name
                break

        merged[field] = {
            "value": selected_value,
            "source": selected_source,
        }

    merged["external_ids"] = {
        "value": {
            **google_books.get("external_ids", {}),
            **open_library.get("external_ids", {}),
            **sporadical.get("external_ids", {}),
        },
        "source": "combined",
    }

    return merged


def find_conflicts(
    sporadical: dict[str, Any],
    open_library: dict[str, Any],
    google_books: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    conflicts: dict[str, dict[str, Any]] = {}

    fields = [
        "title",
        "subtitle",
        "contributors",
        "publisher",
        "publication_date",
        "language",
        "page_count",
        "cover_url",
    ]

    for field in fields:
        values = {
            source: record.get(field)
            for source, record in [
                ("sporadical", sporadical),
                ("openlibrary", open_library),
                ("google_books", google_books),
            ]
            if has_usable_value(record.get(field))
        }

        serialised_values = {
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            for value in values.values()
        }

        if len(serialised_values) > 1:
            conflicts[field] = values

    return conflicts


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 lookup_book.py ISBN", file=sys.stderr)
        return 1

    isbn = normalise_isbn(sys.argv[1])

    if not is_valid_isbn13(isbn):
        print(f"Invalid ISBN-13: {isbn}", file=sys.stderr)
        return 1

    try:
        sporadical = load_sporadical_record(isbn)
        source_errors: dict[str, str] = {}

        try:
            open_library = get_open_library_record(isbn)
        except RuntimeError as error:
            open_library = {}
            source_errors["openlibrary"] = str(error)

        try:
            google_books = get_google_books_record(isbn)
        except RuntimeError as error:
            google_books = {}
            source_errors["google_books"] = str(error)

        merged = merge_metadata(
            sporadical,
            open_library,
            google_books,
        )

        result = {
            "isbn": isbn,
            "sources_found": {
                "sporadical": bool(sporadical),
                "openlibrary": bool(open_library),
                "google_books": bool(google_books),
            },
            "source_errors": source_errors,
            "merged": merged,
            "conflicts": find_conflicts(
                sporadical,
                open_library,
                google_books,
            ),
            "raw_normalised_sources": {
                "sporadical": sporadical,
                "openlibrary": open_library,
                "google_books": google_books,
            },
        }

        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
