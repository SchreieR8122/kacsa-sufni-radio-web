#!/usr/bin/env python3
"""Refresh fuel averages and official Hungarian e-vignette prices."""
import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DATA_PATH = "data/prices.json"
FUEL_LIST_URL = "https://holtankoljak.hu/uzemanyag_arvaltozasok/"
TOLL_URL = "https://nemzetiutdij.hu/hu/e-matrica-arak"
USER_AGENT = "KacsaSufniRadioPriceBot/1.0 (+https://kacsa.media.hu/)"

MONTHS = {
    "január": 1, "február": 2, "március": 3, "április": 4,
    "május": 5, "június": 6, "július": 7, "augusztus": 8,
    "szeptember": 9, "október": 10, "november": 11, "december": 12,
}


def get_html(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


def strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("\xa0", " ")


def latest_fuel_article(listing_html, fallback_url):
    links = re.findall(
        r"""href\s*=\s*["']([^"']*uzemanyagar_valtozas_\d{4}_\d{2}_\d{2}_tol[^"']*)["']""",
        listing_html,
        flags=re.I,
    )
    if not links:
        return fallback_url
    unique = {urljoin(FUEL_LIST_URL, link) for link in links}
    def slug_date(url):
        match = re.search(r"uzemanyagar_valtozas_(\d{4})_(\d{2})_(\d{2})_tol", url)
        return match.groups() if match else ("0000", "00", "00")
    return sorted(unique, key=slug_date, reverse=True)[0]


def parse_hungarian_date(text):
    match = re.search(
        r"Üzemanyagár-változás\s*[–-]\s*(\d{4})\.\s*([a-záéíóöőúüű]+)\s+(\d{1,2})\.?",
        text,
        flags=re.I,
    )
    if match:
        year, month_name, day = match.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    # Fallback to an ISO date only when the page clearly contains the price heading.
    if "95-ös benzin" in text.lower() and "gázolaj" in text.lower():
        match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", text)
        if match:
            return "-".join(match.groups())
    return None


def parse_fuel():
    listing = get_html(FUEL_LIST_URL)
    fallback = "https://holtankoljak.hu/uzemanyag_arvaltozasok/arvalt/uzemanyagar_valtozas_2026_10_08_tol"
    article_url = latest_fuel_article(listing, fallback)
    html = get_html(article_url)
    text = strip_tags(html)
    page_date = parse_hungarian_date(text)
    petrol = re.search(r"95[-‑–]ös\s+benzin\s*:\s*([\d\s.,]+)\s*Ft\s*/\s*liter", text, re.I)
    diesel = re.search(r"Gázolaj\s*:\s*([\d\s.,]+)\s*Ft\s*/\s*liter", text, re.I)
    if not page_date or not petrol or not diesel:
        raise ValueError("Could not confidently identify a dated petrol and diesel price on Holtankoljak.")
    def amount(match):
        return int(re.sub(r"\D", "", match.group(1)))
    return {
        "petrol95": amount(petrol),
        "diesel": amount(diesel),
        "priceDate": page_date,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": FUEL_LIST_URL,
        "article": article_url,
    }


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def parse_tolls():
    html = get_html(TOLL_URL)
    parser = TableParser()
    parser.feed(html)
    rows = {}
    for row in parser.rows:
        if not row:
            continue
        category = re.sub(r"\*+", "", row[0]).strip().upper()
        if category not in {"D1", "D2", "D1M", "U"}:
            continue
        values = []
        for cell in row[1:]:
            match = re.search(r"([\d\s]+)\s*Ft", cell, re.I)
            if match:
                values.append(int(re.sub(r"\D", "", match.group(1))))
        if len(values) >= 7:
            rows[category] = values[:7]
    if not {"D1", "D2", "D1M", "U"}.issubset(rows):
        raise ValueError("Could not parse all expected D1/D2/D1M/U rows from the official toll page.")
    def product(values):
        return {
            "daily": values[0], "weekly": values[1], "monthly": values[2],
            "annualNational": values[3], "annualCounty": values[4],
            "bazCounty": values[5], "m1Regional": values[6],
        }
    return {
        "year": 2026,
        "effectiveFrom": "2026-01-01",
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": TOLL_URL,
        "products": {key: product(rows[key]) for key in ("D1", "D2", "D1M", "U")},
    }


def main():
    with open(DATA_PATH, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    success_count = 0
    for label, parser, key in (("fuel", parse_fuel, "fuel"), ("toll", parse_tolls, "toll")):
        try:
            fresh = parser()
            if key == "fuel":
                data[key].update(fresh)
            else:
                data[key] = fresh
            success_count += 1
            print(f"Updated {label}: {fresh}")
        except Exception as exc:
            print(f"WARNING: {label} data was not changed: {exc}", file=sys.stderr)
    if success_count == 0:
        raise SystemExit("Both price sources failed; leaving data file unchanged.")
    with open(DATA_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
