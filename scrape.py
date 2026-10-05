"""Scrape Nehru Place PC hardware price lists into a single consolidated CSV.

Outputs:
  data/nehru_place_prices.csv
"""

import csv
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- Configuration ---
INDEX_URL = "https://www.nehruplacemarket.com/price-list.html"
OUTPUT_FILE = Path("data/nehru_place_prices.csv")
REQUEST_DELAY = 1.0  # Politeness delay between page requests (seconds)
REQUEST_TIMEOUT = 30  # Socket timeout (seconds)
MAX_RETRIES = 3
USER_AGENT = "nehru-place-price-scraper/2.0 (open-data dump; github-actions)"

FIELDS = [
    "category",
    "sub_list",
    "model",
    "specifications",
    "price_inr",
    "seller_name",
    "seller_phone",
    "page_last_updated",
    "scraped_at_utc",
]

# Slugs (from index page URLs) targeted for PC builds
PC_PARTS_SLUGS = {
    "cpu-price-list",
    "motherboard-price-list",
    "ram-price-list",
    "graphicscard-price-list",
    "harddisk-price-list",
    "cabinet-price-list",
    "monitor-price-list",
    "led-lcd-price-list",
    "keyboard-mouse-price-list",
    "ups-invertor-price-list",
    "cpu-fan-price",
    "dvdwriter-price-list",
    "networking-price-list",
    "multimedia-price-list",
}

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("nehru_scraper")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

UPDATED_RE = re.compile(
    r"Last Updated on:\s*(\w+ \d{1,2}, \d{4})\s*-\s*(\d{1,2}:\d{2}\s*[ap]m)",
    re.IGNORECASE,
)


class ScrapeMetrics:
    def __init__(self):
        self.start_time = time.time()
        self.total_requests = 0
        self.retry_count = 0
        self.failed_requests = 0
        self.total_items_parsed = 0
        self.anomalies_missing_price = 0
        self.categories_processed = 0
        self.categories_failed = 0
        self.category_breakdown = {}  # {category: {'rows': int, 'pages': int, 'status': str}}

    def finish(self):
        self.duration_seconds = round(time.time() - self.start_time, 2)


metrics = ScrapeMetrics()


def safe_request(url: str, retries: int = MAX_RETRIES) -> BeautifulSoup | None:
    """Fetch HTML with retry backoff and error tracking."""
    for attempt in range(1, retries + 1):
        metrics.total_requests += 1
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            metrics.retry_count += 1
            logger.warning(
                "Request failed (attempt %d/%d) for URL '%s': %s",
                attempt,
                retries,
                url,
                exc,
            )
            if attempt < retries:
                time.sleep(attempt * 2)
            else:
                metrics.failed_requests += 1
                logger.error("Exhausted retries. Could not fetch URL: %s", url)
    return None


def clean_text(text: str) -> str:
    """Normalize internal spacing and clean NBSP characters."""
    return re.sub(r"\s+", " ", text).strip()


def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].replace(".html", "")


def parse_timestamp(header_text: str) -> str:
    """Convert 'Last Updated on: October 5, 2026 - 6:04 pm' to ISO format 'YYYY-MM-DD HH:MM'."""
    match = UPDATED_RE.search(header_text)
    if not match:
        return ""
    normalized = f"{match.group(1)} {match.group(2).replace(' ', '').upper()}"
    try:
        return datetime.strptime(normalized, "%B %d, %Y %I:%M%p").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return normalized


def extract_category_links(scrape_all: bool) -> dict[str, tuple[str, str]]:
    """Crawl the main landing page table to find category URLs."""
    logger.info("Accessing root catalogue page: %s", INDEX_URL)
    soup = safe_request(INDEX_URL)
    if not soup:
        logger.critical("Fatal: Landing index page unreachable.")
        sys.exit(1)

    categories = {}
    anchor_tags = soup.select("table a[href]")
    if not anchor_tags:
        logger.error("Site structure alert: No category hyperlinks found inside table.")
        return categories

    for a in anchor_tags:
        href = a.get("href", "").strip()
        label = clean_text(a.get_text())
        if not href:
            continue

        full_url = urljoin(INDEX_URL, href)
        slug = slug_from_url(full_url)

        if full_url.endswith(".php") and not scrape_all:
            continue  # Non-standard page template
        if scrape_all or slug in PC_PARTS_SLUGS:
            categories[slug] = (label, full_url)

    logger.info("Identified %d categories to scrape.", len(categories))
    return categories


def find_price_tables(soup: BeautifulSoup) -> list:
    """Find all table elements containing standard 'Unit Price' headers."""
    return [
        table
        for table in soup.find_all("table")
        if table.find(string=re.compile(r"Unit Price", re.IGNORECASE))
    ]


def parse_table_title(table) -> str:
    first_tr = table.find("tr")
    if not first_tr:
        return "Unknown Section"
    text = clean_text(first_tr.get_text(" "))
    text = re.split(r"Last Updated on:|\s-\s*Page \d+ of|\s<<", text)[0]
    return re.sub(r"^Page \d+\s*-\s*", "", text).strip(" -")


def is_ditto_reference(seller_cell: str) -> bool:
    clean = re.sub(r"[\W_]+", "", seller_cell.lower())
    return clean in ("", "do", "ditto")


def parse_rows_from_table(table) -> list[dict]:
    """Parse rows from price tables, handling ditto sellers and phone separation."""
    rows = []
    last_known_seller = ""

    for tr in table.find_all("tr"):
        cells = [clean_text(td.get_text(" ")) for td in tr.find_all("td")]
        if len(cells) < 5 or not re.fullmatch(r"\d+\.?", cells[0]):
            continue  # Header, banner, or pagination row

        _, model, specs, price_raw, seller_raw = cells[:5]
        specs = re.sub(r"\s*More info(?:…|\.\.\.)?\s*$", "", specs, flags=re.I)

        # Resolve ditto notation
        if is_ditto_reference(seller_raw):
            seller_raw = last_known_seller
        last_known_seller = seller_raw

        # Separate seller name and phone
        phone_match = re.match(r"^(.*?)\s*(\d{7,12})$", seller_raw)
        if phone_match:
            seller_name, seller_phone = phone_match.group(1).strip(), phone_match.group(2)
        else:
            seller_name, seller_phone = seller_raw, ""

        # Price sanitization
        digits_only = re.sub(r"[^\d]", "", price_raw)
        if digits_only:
            unit_price = int(digits_only)
        else:
            unit_price = ""
            metrics.anomalies_missing_price += 1

        rows.append(
            {
                "model": model,
                "specifications": specs,
                "price_inr": unit_price,
                "seller_name": seller_name,
                "seller_phone": seller_phone,
            }
        )

    return rows


def separate_page_tables(soup: BeautifulSoup):
    """Separate the primary paginated table, standalone tables, and sub-list teasers."""
    tables = find_price_tables(soup)
    if not tables:
        return None, [], {}

    # Main table carries the 'Last Updated on' header
    main_table = next(
        (t for t in tables if UPDATED_RE.search(clean_text(t.get_text(" ")))), None
    )
    if main_table is None and tables:
        main_table = tables[0]

    standalone_tables = []
    teaser_urls = {}

    for t in tables:
        if t is main_table:
            continue
        teaser_anchor = t.find("a", string=re.compile(r"View full", re.IGNORECASE))
        if teaser_anchor:
            label = re.sub(
                r"(?i)^view full\s*|\s*price list\s*$",
                "",
                teaser_anchor.get_text(strip=True),
            )
            teaser_urls[urljoin(INDEX_URL, teaser_anchor["href"])] = label
        else:
            standalone_tables.append(t)

    return main_table, standalone_tables, teaser_urls


def scrape_category(category_name: str, category_url: str):
    """Crawl a single hardware category through all pages."""
    category_rows = []
    page = 1
    total_pages = 1
    discovered_teasers = {}
    is_success = True

    while page <= total_pages:
        page_url = f"{category_url}?pagenum={page}"
        soup = safe_request(page_url)
        if soup is None:
            logger.error("Category [%s]: Abandoning scrape at page %d.", category_name, page)
            is_success = False
            break

        main_tbl, standalone_tbls, teasers = separate_page_tables(soup)
        if main_tbl is None:
            logger.warning(
                "Category [%s]: No valid price table found on page %d (structure may have changed).",
                category_name,
                page,
            )
            is_success = False
            break

        main_text = clean_text(main_tbl.get_text(" "))
        last_updated_stamp = parse_timestamp(main_text)

        # Detect total pages on the first page
        if page == 1:
            page_match = re.search(r"Page 1 of (\d+)", main_text, re.IGNORECASE)
            if page_match:
                total_pages = int(page_match.group(1))
            else:
                logger.info("Category [%s]: Single-page directory detected.", category_name)
                total_pages = 1
            discovered_teasers = teasers

        # Parse primary table
        main_title = parse_table_title(main_tbl)
        main_rows = parse_rows_from_table(main_tbl)
        for row in main_rows:
            row.update(sub_list=main_title, page_last_updated=last_updated_stamp)
        category_rows.extend(main_rows)

        # Parse secondary standalone tables on this page (e.g., AMD motherboards)
        secondary_count = 0
        for st in standalone_tbls:
            sec_title = parse_table_title(st)
            sec_rows = parse_rows_from_table(st)
            for row in sec_rows:
                row.update(sub_list=sec_title, page_last_updated=last_updated_stamp)
            category_rows.extend(sec_rows)
            secondary_count += len(sec_rows)

        total_extracted_on_page = len(main_rows) + secondary_count
        logger.info(
            "Category [%s]: Parsed page %d/%d -> %d items (Main: %d, Secondary: %d)",
            category_name,
            page,
            total_pages,
            total_extracted_on_page,
            len(main_rows),
            secondary_count,
        )

        page += 1
        time.sleep(REQUEST_DELAY)

    return category_rows, (is_success and bool(category_rows)), total_pages, discovered_teasers


def write_consolidated_csv(file_path: Path, rows: list[dict]):
    """Write all collected rows into a single consolidated CSV."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Successfully exported %d records to %s", len(rows), file_path)


def output_summary(records: list[dict]):
    """Emit execution statistics to stdout and write to GITHUB_STEP_SUMMARY if available."""
    metrics.finish()

    summary_lines = [
        "## Nehru Place Scraper Run Report",
        "",
        f"- **Execution Time:** {metrics.duration_seconds}s",
        f"- **Total HTTP Requests:** {metrics.total_requests} (Retries: {metrics.retry_count}, Failed: {metrics.failed_requests})",
        f"- **Total Rows Saved:** {len(records)}",
        f"- **Pricing Anomalies Detected:** {metrics.anomalies_missing_price} (missing or placeholder prices)",
        "",
        "### Category Breakdown",
        "",
        "| Category | Scraped Pages | Total Items | Scrape Status |",
        "|---|---|---|---|",
    ]

    for cat_name, stats in metrics.category_breakdown.items():
        summary_lines.append(
            f"| {cat_name} | {stats['pages']} | {stats['rows']} | {stats['status']} |"
        )

    summary_markdown = "\n".join(summary_lines)
    logger.info("\n" + summary_markdown)

    # Populate GitHub Actions Step Summary if running in CI
    step_summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        try:
            with open(step_summary_path, "a", encoding="utf-8") as summary_file:
                summary_file.write(summary_markdown + "\n")
        except OSError as exc:
            logger.warning("Could not write to GITHUB_STEP_SUMMARY: %s", exc)


def main():
    scrape_all = "--all" in sys.argv
    all_consolidated_rows = []
    scraped_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    category_queue = extract_category_links(scrape_all)
    visited_slugs = set()

    while category_queue:
        slug, (label, url) = next(iter(category_queue.items()))
        del category_queue[slug]

        if slug in visited_slugs:
            continue
        visited_slugs.add(slug)

        logger.info(">>> Processing Category: %s (%s)", label, url)
        rows, is_successful, total_pages, teasers = scrape_category(label, url)

        # Enqueue full lists discovered via teaser links
        for teaser_url, teaser_label in teasers.items():
            t_slug = slug_from_url(teaser_url)
            if t_slug not in visited_slugs and t_slug not in category_queue:
                logger.info("Discovered sub-category teaser: %s -> Enqueueing.", teaser_label)
                category_queue[t_slug] = (teaser_label, teaser_url)

        if is_successful:
            for row in rows:
                row["category"] = label
                row["scraped_at_utc"] = scraped_timestamp
            all_consolidated_rows.extend(rows)
            metrics.categories_processed += 1
            status_flag = "Success"
        else:
            metrics.categories_failed += 1
            status_flag = "Failed / Partial"
            logger.error("Failed to complete scrape for category: %s", label)

        metrics.category_breakdown[label] = {
            "rows": len(rows),
            "pages": total_pages,
            "status": status_flag,
        }

    metrics.total_items_parsed = len(all_consolidated_rows)

    if not all_consolidated_rows:
        logger.critical("No rows extracted across any category. Aborting file write.")
        sys.exit(1)

    # Overwrite old CSV with today's complete snapshot
    write_consolidated_csv(OUTPUT_FILE, all_consolidated_rows)
    output_summary(all_consolidated_rows)


if __name__ == "__main__":
    main()
