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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- Configuration ---
INDEX_URL = "https://www.nehruplacemarket.com/price-list.html"
OUTPUT_FILE = Path("data/nehru_place_prices.csv")
REQUEST_DELAY = 0.5  # Politeness delay between paginated requests
REQUEST_TIMEOUT = 30 
MAX_RETRIES = 3
MAX_WORKERS = 10 # Number of concurrent categories to scrape
USER_AGENT = "nehru-place-price-scraper/3.0 (open-data dump; github-actions)"

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
    "cpu-price-list", "motherboard-price-list", "ram-price-list",
    "graphicscard-price-list", "harddisk-price-list", "cabinet-price-list",
    "monitor-price-list", "led-lcd-price-list", "keyboard-mouse-price-list",
    "ups-invertor-price-list", "cpu-fan-price", "dvdwriter-price-list",
    "networking-price-list", "multimedia-price-list",
}

# --- Logging & Metrics Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("nehru_scraper")

class ScrapeMetrics:
    def __init__(self):
        self.start_time = time.time()
        self.total_requests = 0
        self.retry_count = 0
        self.failed_requests = 0
        self.anomalies_missing_price = 0
        self.category_breakdown = {}

    def finish(self):
        self.duration_seconds = round(time.time() - self.start_time, 2)

metrics = ScrapeMetrics()

# Thread-safe session with connection pooling
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})
adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
session.mount("https://", adapter)
session.mount("http://", adapter)

UPDATED_RE = re.compile(
    r"Last Updated on:\s*(\w+ \d{1,2}, \d{4})\s*-\s*(\d{1,2}:\d{2}\s*[ap]m)",
    re.IGNORECASE,
)

# --- Network & Utility Methods ---

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
            if attempt < retries:
                time.sleep(attempt * 2)
            else:
                metrics.failed_requests += 1
                logger.error("Exhausted retries for URL: %s", url)
    return None

def clean_text(text: str) -> str:
    """Normalize internal spacing and clean NBSP characters."""
    return re.sub(r"\s+", " ", text).strip()

def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].replace(".html", "")

def parse_timestamp(header_text: str) -> str:
    match = UPDATED_RE.search(header_text)
    if not match:
        return ""
    normalized = f"{match.group(1)} {match.group(2).replace(' ', '').upper()}"
    try:
        return datetime.strptime(normalized, "%B %d, %Y %I:%M%p").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return normalized

# --- Parsing Methods ---

def extract_category_links(scrape_all: bool) -> dict[str, tuple[str, str]]:
    logger.info("Accessing root catalogue page: %s", INDEX_URL)
    soup = safe_request(INDEX_URL)
    if not soup:
        sys.exit("Fatal: Landing index page unreachable.")

    categories = {}
    for a in soup.select("table a[href]"):
        href = a.get("href", "").strip()
        label = clean_text(a.get_text())
        if not href:
            continue

        full_url = urljoin(INDEX_URL, href)
        slug = slug_from_url(full_url)

        if full_url.endswith(".php") and not scrape_all:
            continue
        if scrape_all or slug in PC_PARTS_SLUGS:
            categories[slug] = (label, full_url)
    return categories

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
    """Adaptive parser handling 2-col, 4-col, and 5-col table structures."""
    rows = []
    last_known_seller = ""
    
    header_text = clean_text(table.find("tr").get_text(" ")).lower() if table.find("tr") else ""
    has_seller_col = "seller" in header_text or "offered by" in header_text

    for tr in table.find_all("tr"):
        cells = [clean_text(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue

        model = specs = price_raw = seller_raw = ""

        # S.N-led Tables (Standard 4 or 5 columns)
        if re.fullmatch(r"\d+\.?", cells[0]):
            if len(cells) >= 5:
                model, specs, price_raw, seller_raw = cells[1], cells[2], cells[3], cells[4]
            elif len(cells) == 4:
                if has_seller_col: # Networking (Model, Price, Seller)
                    model, specs, price_raw, seller_raw = cells[1], "", cells[2], cells[3]
                else: # KB/Mouse, Fans (Brand, Model/Specs, Price)
                    model, specs, price_raw, seller_raw = cells[1], cells[2], cells[3], ""
            elif len(cells) == 3:
                model, specs, price_raw = cells[1], "", cells[2]
                
        # 2-Column Tables (DVD Writers)
        elif len(cells) == 2 and re.search(r"\d{3,}", cells[1]):
            if "price" in cells[1].lower(): 
                continue
            model, price_raw = cells[0], cells[1]
        else:
            continue

        # Extract only digits for price
        digits_only = re.sub(r"[^\d]", "", price_raw)
        if not digits_only and "call" not in price_raw.lower():
            continue

        # Resolve seller ditto marks
        if is_ditto_reference(seller_raw):
            seller_raw = last_known_seller
        last_known_seller = seller_raw

        phone_match = re.match(r"^(.*?)\s*(\d{7,12})$", seller_raw)
        if phone_match:
            seller_name, seller_phone = phone_match.group(1).strip(), phone_match.group(2)
        else:
            seller_name, seller_phone = seller_raw, ""

        rows.append({
            "model": model, "specifications": specs,
            "price_inr": int(digits_only) if digits_only else "",
            "seller_name": seller_name, "seller_phone": seller_phone,
        })
    return rows

def parse_text_lists(soup: BeautifulSoup) -> list[dict]:
    """Fallback parser for unstructured bullet-point layouts (CRT Monitors)."""
    rows = []
    for container in soup.find_all(["p", "b", "div"]):
        for line in container.get_text("\n").split("\n"):
            line = clean_text(line)
            # Matches formats like: • Samsung 732n 17″ – 3150/-
            match = re.search(r"^[•\u2022\-\*]\s*(.*?)\s*(?:–|-)\s*(\d{3,})(?:/-)?$", line)
            if match:
                rows.append({
                    "model": match.group(1).strip(), "specifications": "",
                    "price_inr": int(match.group(2)), "seller_name": "", "seller_phone": ""
                })
    return rows

def scrape_category_worker(category_name: str, category_url: str):
    """Crawl a single hardware category through all pages."""
    category_rows = []
    page = 1
    total_pages = 1
    discovered_teasers = {}
    is_success = True

    while page <= total_pages:
        soup = safe_request(f"{category_url}?pagenum={page}")
        if soup is None:
            is_success = False
            break

        tables = [t for t in soup.find_all("table") if re.search(r"price", t.get_text(" "), re.I) and len(t.find_all("tr")) > 1]
        
        # Determine Main vs Standalone tables
        main_tbl = next((t for t in tables if UPDATED_RE.search(clean_text(t.get_text(" ")))), None)
        if main_tbl is None and tables:
            main_tbl = tables[0]

        # Extract unstructured data if no tables are found at all (Monitors)
        if not tables:
            fallback_rows = parse_text_lists(soup)
            if fallback_rows:
                for r in fallback_rows:
                    r.update(sub_list="Bullet-Point Listings", page_last_updated="")
                category_rows.extend(fallback_rows)
                break # Bullet lists generally don't paginate
            else:
                is_success = False
                break

        main_text = clean_text(main_tbl.get_text(" "))
        last_updated_stamp = parse_timestamp(main_text)

        standalone_tbls = []
        for t in tables:
            if t is main_tbl: continue
            teaser_anchor = t.find("a", string=re.compile(r"View full", re.IGNORECASE))
            if teaser_anchor:
                label = re.sub(r"(?i)^view full\s*|\s*price list\s*$", "", teaser_anchor.get_text(strip=True))
                discovered_teasers[urljoin(INDEX_URL, teaser_anchor["href"])] = label
            else:
                standalone_tbls.append(t)

        if page == 1:
            page_match = re.search(r"Page 1 of (\d+)", main_text, re.IGNORECASE)
            total_pages = int(page_match.group(1)) if page_match else 1

        # Parse primary & secondary tables
        main_title = parse_table_title(main_tbl)
        for r in parse_rows_from_table(main_tbl):
            r.update(sub_list=main_title, page_last_updated=last_updated_stamp)
            category_rows.append(r)

        for st in standalone_tbls:
            sec_title = parse_table_title(st)
            for r in parse_rows_from_table(st):
                r.update(sub_list=sec_title, page_last_updated=last_updated_stamp)
                category_rows.append(r)

        logger.info("Category [%s]: Parsed page %d/%d", category_name, page, total_pages)
        page += 1
        time.sleep(REQUEST_DELAY)

    return category_name, category_url, category_rows, (is_success and bool(category_rows)), total_pages, discovered_teasers

# --- Output & Execution ---

def output_summary(records: list[dict]):
    metrics.finish()
    summary_lines = [
        "## Nehru Place Scraper Run Report", "",
        f"- **Execution Time:** {metrics.duration_seconds}s",
        f"- **Total HTTP Requests:** {metrics.total_requests} (Retries: {metrics.retry_count}, Failed: {metrics.failed_requests})",
        f"- **Total Rows Saved:** {len(records)}", "",
        "### Category Breakdown", "",
        "| Category | Scraped Pages | Total Items | Scrape Status |",
        "|---|---|---|---|"
    ]

    for cat_name, stats in metrics.category_breakdown.items():
        summary_lines.append(f"| {cat_name} | {stats['pages']} | {stats['rows']} | {stats['status']} |")

    summary_markdown = "\n".join(summary_lines)
    logger.info("\n" + summary_markdown)

    step_summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        with open(step_summary_path, "a", encoding="utf-8") as sf:
            sf.write(summary_markdown + "\n")

def main():
    scrape_all = "--all" in sys.argv
    all_consolidated_rows = []
    scraped_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    category_queue = extract_category_links(scrape_all)
    visited_slugs = set(category_queue.keys())

    # Execute multithreaded scraping
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(scrape_category_worker, label, url): slug
            for slug, (label, url) in category_queue.items()
        }

        while futures:
            done, _ = as_completed(futures.keys(), timeout=None), None
            # Extract first completed future
            for fut in list(futures.keys()):
                if fut.done():
                    slug = futures.pop(fut)
                    try:
                        label, url, rows, is_successful, total_pages, teasers = fut.result()
                        
                        # Process discovered teaser links (e.g. Internal Hard disk)
                        for t_url, t_label in teasers.items():
                            t_slug = slug_from_url(t_url)
                            if t_slug not in visited_slugs:
                                visited_slugs.add(t_slug)
                                logger.info("Discovered sub-category: %s -> Enqueueing.", t_label)
                                futures[executor.submit(scrape_category_worker, t_label, t_url)] = t_slug

                        if is_successful:
                            for row in rows:
                                row["category"] = label
                                row["scraped_at_utc"] = scraped_timestamp
                            all_consolidated_rows.extend(rows)
                            status_flag = "Success"
                        else:
                            status_flag = "Failed / Partial"
                            logger.error("Failed or found no data for category: %s", label)

                        metrics.category_breakdown[label] = {"rows": len(rows), "pages": total_pages, "status": status_flag}

                    except Exception as exc:
                        logger.error("Fatal error processing category %s: %s", slug, exc)

    if not all_consolidated_rows:
        sys.exit("Critical: No rows extracted across any category. Aborting file write.")

    # Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_consolidated_rows)
        
    output_summary(all_consolidated_rows)

if __name__ == "__main__":
    main()
