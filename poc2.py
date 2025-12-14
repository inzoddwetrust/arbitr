#!/usr/bin/env python3
"""
PoC2 Crawler for kad.arbitr.ru
Extended Proof of Concept with structured output

Features:
- Search by case number
- Parse all tabs: Court Acts, Cards (with accordions), Electronic Case
- Extract rich metadata from HTML (title, judge, court, signature)
- Download PDFs via response interceptor
- Convert to text with pymupdf
- Output LLM-optimized directory structure
- Graceful stop on rate limit with progress saving
- Human-like delays with jitter

Output structure:
    case_A60-21280-2023/
    ├── README.md
    ├── case.json
    ├── court_acts.json
    ├── instances/
    │   └── inst_<guid>.json
    ├── electronic_case.json
    └── documents/
        └── <guid>.json
"""

import asyncio
import hashlib
import json
import logging
import random
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, Browser, Locator

# Optional dependencies
try:
    from playwright_stealth import stealth_async

    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("playwright-stealth not installed. Run: pip install playwright-stealth")

try:
    import pymupdf

    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
    print("pymupdf not installed. Run: pip install pymupdf")

# === Logging Configuration ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# === Configuration ===
BASE_URL = "https://kad.arbitr.ru/"
HEADLESS = True
SLOW_MO = 100

# Human-like timing (seconds)
DELAY_BETWEEN_DOCS_BASE = 3.0
DELAY_BETWEEN_DOCS_JITTER = 2.0
DELAY_BETWEEN_PAGES_BASE = 2.0
DELAY_BETWEEN_PAGES_JITTER = 2.0
DELAY_BREAK_BASE = 45.0
DELAY_BREAK_JITTER = 30.0
DOCS_BEFORE_BREAK = 15  # Take a break every N documents (randomized ±5)

# Rate limit detection
RATE_LIMIT_PHRASES = [
    "Доступ к сервису ограничен",
    "Слишком много запросов",
    "Too many requests",
    "Rate limit",
]


# === Data Models ===
@dataclass
class DocumentMeta:
    """Metadata for a single document."""
    doc_id: str  # GUID from URL
    case_guid: str
    url: str
    filename: str
    date: Optional[str] = None  # ISO format YYYY-MM-DD
    doc_type: Optional[str] = None  # Определение, Решение, etc.
    title: Optional[str] = None
    court: Optional[str] = None
    judge: Optional[str] = None
    signed: bool = False
    signature_valid: bool = False
    source_tab: str = ""  # "court_acts", "cards", "electronic_case"
    instance_id: Optional[str] = None  # For cards tab
    instance_name: Optional[str] = None  # Human-readable instance name
    position: int = 0  # Position within instance (1 = newest)
    page: int = 1  # Pagination page number
    position_on_page: int = 0  # Position on the page (1-based)


@dataclass
class DocumentFull(DocumentMeta):
    """Full document with text content."""
    has_text: bool = False
    requires_ocr: bool = False
    char_count: int = 0
    text: str = ""


@dataclass
class Instance:
    """Court instance (accordion in Cards tab)."""
    instance_id: str
    name: str
    order: int = 0  # Order in the case (1, 2, 3... from top to bottom)
    court: Optional[str] = None
    documents: list[str] = field(default_factory=list)  # List of doc_ids in chronological order
    page_count: int = 1


@dataclass
class CaseInfo:
    """Case metadata."""
    case_number: str
    case_guid: str
    status: Optional[str] = None
    url: str = ""
    parsed_at: str = ""
    total_documents: int = 0
    instances_count: int = 0
    # Fingerprints for quick change detection
    fingerprints: dict = field(default_factory=dict)  # {instance_id: first_doc_id, "ed": first_doc_id}


@dataclass
class Progress:
    """Progress state for graceful stop/resume."""
    downloaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    last_updated: str = ""


# === Utility Functions ===
async def human_delay(base: float, jitter: float) -> None:
    """Sleep for a random duration to appear human-like."""
    delay = base + random.uniform(0, jitter)
    await asyncio.sleep(delay)


async def human_delay_doc() -> None:
    """Delay between document downloads."""
    await human_delay(DELAY_BETWEEN_DOCS_BASE, DELAY_BETWEEN_DOCS_JITTER)


async def human_delay_page() -> None:
    """Delay between page navigations."""
    await human_delay(DELAY_BETWEEN_PAGES_BASE, DELAY_BETWEEN_PAGES_JITTER)


async def take_break() -> None:
    """Take a coffee break."""
    delay = DELAY_BREAK_BASE + random.uniform(0, DELAY_BREAK_JITTER)
    log.info(f"☕ Taking a break ({delay:.0f}s)...")
    await asyncio.sleep(delay)


def should_take_break(doc_index: int) -> bool:
    """Determine if it's time for a break (randomized)."""
    threshold = DOCS_BEFORE_BREAK + random.randint(-5, 5)
    return doc_index > 0 and doc_index % threshold == 0


async def check_rate_limit(page: Page) -> bool:
    """Check if we've been rate limited."""
    try:
        page_text = await page.inner_text("body")
        for phrase in RATE_LIMIT_PHRASES:
            if phrase.lower() in page_text.lower():
                return True
    except:
        pass
    return False


def save_progress(output_dir: Path, progress: Progress) -> None:
    """Save current progress for resume."""
    progress.last_updated = datetime.now().isoformat()
    progress_file = output_dir / "_progress.json"
    progress_file.write_text(
        json.dumps(asdict(progress), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info(
        f"💾 Progress saved: {len(progress.downloaded)} done, {len(progress.failed)} failed, {len(progress.pending)} pending")


def load_progress(output_dir: Path) -> Optional[Progress]:
    """Load previous progress if exists."""
    progress_file = output_dir / "_progress.json"
    if progress_file.exists():
        try:
            data = json.loads(progress_file.read_text(encoding="utf-8"))
            return Progress(**data)
        except Exception as e:
            log.warning(f"Failed to load progress: {e}")
    return None


def extract_guid_from_url(url: str) -> tuple[str, str]:
    """
    Extract case_guid and doc_guid from PDF URL.
    URL format: .../PdfDocument/{case_guid}/{doc_guid}/{filename}.pdf
    Returns (case_guid, doc_guid)
    """
    parts = url.split("/")
    # Find PdfDocument in path
    try:
        idx = parts.index("PdfDocument")
        case_guid = parts[idx + 1] if len(parts) > idx + 1 else ""
        doc_guid = parts[idx + 2] if len(parts) > idx + 2 else ""
        return case_guid, doc_guid
    except (ValueError, IndexError):
        # Fallback: hash the URL
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return "", url_hash


def extract_date_from_filename(filename: str) -> Optional[str]:
    """
    Extract date from filename like 'A60-21280-2023_20251204_Opredelenie.pdf'
    Returns ISO date string or None.
    """
    match = re.search(r'_(\d{4})(\d{2})(\d{2})_', filename)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    return None


def extract_doc_type_from_filename(filename: str) -> Optional[str]:
    """
    Extract document type from filename.
    """
    # Remove extension and split by underscore
    name = filename.rsplit(".", 1)[0]
    parts = name.split("_")
    if len(parts) >= 3:
        # Last part is usually the type
        doc_type = parts[-1]
        # Capitalize first letter
        return doc_type.capitalize() if doc_type else None
    return None


def normalize_court_name(raw: str) -> str:
    """Normalize court name (title case, clean whitespace)."""
    if not raw:
        return ""
    # Remove extra whitespace
    cleaned = " ".join(raw.split())
    # Title case but keep abbreviations
    words = cleaned.split()
    result = []
    for word in words:
        if word.isupper() and len(word) <= 3:
            result.append(word)  # Keep abbreviations like "АС"
        else:
            result.append(word.capitalize())
    return " ".join(result)


def make_safe_folder_name(name: str, max_length: int = 30) -> str:
    """
    Create filesystem-safe folder name from instance name.
    Replaces spaces with underscores, removes special chars.
    """
    # Replace spaces and problematic chars
    safe = name.replace(" ", "_").replace("/", "-").replace("\\", "-")
    # Remove other special chars
    safe = re.sub(r'[<>:"|?*]', '', safe)
    # Truncate if too long
    if len(safe) > max_length:
        safe = safe[:max_length]
    return safe


# === HTML Parsing ===
async def parse_document_metadata(link_element: Locator, source_tab: str, instance_id: Optional[str] = None) -> \
Optional[DocumentMeta]:
    """
    Parse rich metadata from document link element and its context.
    """
    try:
        url = await link_element.get_attribute("href")
        if not url or "PdfDocument" not in url:
            return None

        case_guid, doc_guid = extract_guid_from_url(url)
        filename = url.split("/")[-1] if "/" in url else ""

        doc = DocumentMeta(
            doc_id=doc_guid,
            case_guid=case_guid,
            url=url,
            filename=filename,
            source_tab=source_tab,
            instance_id=instance_id,
        )

        # Extract date and type from filename
        doc.date = extract_date_from_filename(filename)
        doc.doc_type = extract_doc_type_from_filename(filename)

        # Try to get parent container for rich metadata
        # Structure: h2.b-case-result > a (link) + spans with rollover data
        parent = link_element.locator("xpath=..")

        # Check for signature
        signed_elem = parent.locator(".g-valid_sign")
        if await signed_elem.count() > 0:
            doc.signed = True
            signed_text = await signed_elem.text_content()
            if signed_text and "Подписано" in signed_text:
                doc.signature_valid = True

        # Title from .js-judges-rollover span (inside the link usually)
        title_elem = link_element.locator(".js-judges-rollover")
        if await title_elem.count() > 0:
            title_text = await title_elem.text_content()
            if title_text:
                doc.title = title_text.strip()

        # Judge from rollover HTML
        judge_html_elem = parent.locator(".js-judges-rolloverHtml")
        if await judge_html_elem.count() > 0:
            judge_html = await judge_html_elem.inner_html()
            # Parse "Судья-докладчик:" or just judge name after <strong>
            judge_match = re.search(r'Судья[^:]*:\s*</strong>\s*<br[^>]*>\s*([^<]+)', judge_html, re.IGNORECASE)
            if judge_match:
                doc.judge = judge_match.group(1).strip()

        # Court from signers rollover HTML
        signers_html_elem = parent.locator(".js-signers-rolloverHtml")
        if await signers_html_elem.count() > 0:
            signers_html = await signers_html_elem.text_content()
            if signers_html:
                # First line is usually the court name
                lines = [l.strip() for l in signers_html.split("\n") if l.strip()]
                if lines:
                    doc.court = normalize_court_name(lines[0])

        return doc

    except Exception as e:
        log.debug(f"Error parsing document metadata: {e}")
        return None


async def parse_document_simple(url: str, source_tab: str, instance_id: Optional[str] = None) -> DocumentMeta:
    """
    Create DocumentMeta from URL only (fallback when HTML parsing fails).
    """
    case_guid, doc_guid = extract_guid_from_url(url)
    filename = url.split("/")[-1] if "/" in url else ""

    return DocumentMeta(
        doc_id=doc_guid,
        case_guid=case_guid,
        url=url,
        filename=filename,
        date=extract_date_from_filename(filename),
        doc_type=extract_doc_type_from_filename(filename),
        source_tab=source_tab,
        instance_id=instance_id,
    )


# === Browser Automation ===
async def close_promo_popup(page: Page) -> None:
    """Close the promo notification popup if present."""
    try:
        popup_close = page.locator("a.js-promo_notification-popup-close")
        if await popup_close.count() > 0 and await popup_close.is_visible():
            await popup_close.click()
            log.info("Closed promo popup")
            await page.wait_for_timeout(500)
    except Exception as e:
        log.debug(f"No promo popup or error closing: {e}")


async def search_by_case_number(page: Page, case_number: str) -> bool:
    """
    Search for a case by its number.
    Returns True if successful.
    """
    log.info(f"Searching for case: {case_number}")

    await page.wait_for_selector("#sug-cases", timeout=10000)
    log.info("Search form loaded")

    await page.wait_for_timeout(3000)
    await close_promo_popup(page)

    case_input = page.locator("#sug-cases input")
    await case_input.click()
    await page.wait_for_timeout(300)
    await case_input.fill("")
    await page.wait_for_timeout(100)
    await case_input.type(case_number, delay=50)
    log.info(f"Entered case number: {case_number}")

    # Trigger events
    await page.evaluate("""
        const input = document.querySelector('#sug-cases input');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    """)

    await page.wait_for_timeout(1000)

    # Try suggest dropdown
    case_guid = None
    suggest_item = page.locator("#b-suggest li a, .b-suggest li a").first
    if await suggest_item.count() > 0:
        case_guid = await suggest_item.get_attribute("id")
        log.info(f"Found suggest item with GUID: {case_guid}")

        if case_guid and case_guid != "00000000-0000-0000-0000-000000000000":
            card_url = f"https://kad.arbitr.ru/Card/{case_guid}"
            log.info(f"Navigating directly to case card: {card_url}")
            await page.goto(card_url, wait_until="domcontentloaded")
            return True

    # Fallback: keyboard navigation
    await page.keyboard.press("ArrowDown")
    await page.wait_for_timeout(200)
    await page.keyboard.press("Enter")

    try:
        await page.wait_for_response(
            lambda r: "Kad/Search" in r.url or "/Card/" in r.url,
            timeout=15000
        )
    except:
        log.warning("No search request detected within timeout")

    await page.wait_for_timeout(2000)

    # Check for CAPTCHA
    captcha = page.locator(".b-pravocaptcha-modal_wrapper:not(:empty), .g-recaptcha")
    if await captcha.count() > 0:
        log.warning("⚠️ CAPTCHA detected! Manual intervention needed.")
        return False

    try:
        await page.wait_for_selector(
            "table#b-cases tbody tr, div.b-noResults:not(.g-hidden), input#caseId",
            timeout=30000
        )
        return True
    except Exception as e:
        log.error(f"Timeout waiting for search results: {e}")
        return False


async def navigate_to_case_card(page: Page, case_url: str) -> Optional[CaseInfo]:
    """Navigate to case card and extract basic info."""
    log.info(f"Navigating to case card: {case_url}")

    await page.goto(case_url, wait_until="domcontentloaded", timeout=30000)

    try:
        await page.wait_for_selector(
            "div.b-chrono-item-header.js-chrono-item-header, #chrono_list_content",
            timeout=15000
        )
        log.info("Case card loaded")
        await page.wait_for_timeout(1000)
    except Exception as e:
        log.error(f"Failed to load case card: {e}")
        return None

    case_info = CaseInfo(
        case_number="",
        case_guid="",
        url=case_url,
        parsed_at=datetime.now().isoformat(),
    )

    # Case GUID
    case_id_elem = page.locator("input#caseId")
    if await case_id_elem.count() > 0:
        case_info.case_guid = await case_id_elem.get_attribute("value") or ""
    else:
        case_info.case_guid = case_url.split("/")[-1]

    # Case number
    case_name_elem = page.locator("input#caseName")
    if await case_name_elem.count() > 0:
        case_info.case_number = await case_name_elem.get_attribute("value") or ""

    # Status
    status_elem = page.locator("div.b-case-header-desc")
    if await status_elem.count() > 0:
        case_info.status = (await status_elem.text_content() or "").strip()

    log.info(f"Case: {case_info.case_number} | GUID: {case_info.case_guid}")
    return case_info


# === Tab Parsing ===
async def collect_court_acts(page: Page) -> list[DocumentMeta]:
    """
    Collect documents from Court Acts section (#gr_case_acts).
    No pagination in this section.
    """
    documents = []

    # Click on "Судебные акты" tab to ensure content is visible
    acts_tab = page.locator("#case_acts")
    if await acts_tab.count() > 0:
        await acts_tab.click()
        log.info("Clicking on 'Судебные акты' tab...")
        await page.wait_for_timeout(500)

    # Wait for container to appear
    try:
        await page.wait_for_selector("#gr_case_acts", timeout=5000)
    except:
        log.warning("Court Acts container (#gr_case_acts) not found")
        return documents

    links = page.locator("#gr_case_acts a[href*='PdfDocument']")
    count = await links.count()
    log.info(f"Found {count} PDF(s) in Court Acts section")

    for i in range(count):
        link = links.nth(i)
        doc = await parse_document_metadata(link, source_tab="court_acts")
        if doc:
            documents.append(doc)
        else:
            # Fallback
            url = await link.get_attribute("href")
            if url:
                documents.append(await parse_document_simple(url, "court_acts"))

    return documents


async def click_cards_tab(page: Page) -> bool:
    """Click on 'Карточки' tab."""
    log.info("Clicking on 'Карточки' tab...")

    cards_tab = page.locator("div.js-case-chrono-button--cards")
    if await cards_tab.count() == 0:
        log.warning("'Карточки' tab not found")
        return False

    await cards_tab.click()

    try:
        await page.wait_for_selector("#chrono_list_content:not(.g-hidden)", timeout=10000)
        log.info("'Карточки' tab opened")
        await page.wait_for_timeout(500)
        return True
    except Exception as e:
        log.error(f"Failed to open 'Карточки' tab: {e}")
        return False


async def collect_cards_all_instances(page: Page) -> tuple[list[DocumentMeta], list[Instance]]:
    """
    Collect all documents from Cards tab with accordion structure.
    Tracks position of each document for proper ordering.
    Returns (documents, instances).
    """
    documents = []
    instances = []

    if not await click_cards_tab(page):
        return documents, instances

    instance_headers = page.locator("#chrono_list_content .b-chrono-item-header.js-chrono-item-header")
    instance_count = await instance_headers.count()
    log.info(f"Found {instance_count} instance(s) in Cards tab")

    for inst_idx in range(instance_count):
        header = instance_headers.nth(inst_idx)

        # Instance name
        instance_name_elem = header.locator("div.l-col strong")
        instance_name = "Unknown"
        if await instance_name_elem.count() > 0:
            instance_name = (await instance_name_elem.text_content() or "").strip()

        # Instance ID
        instance_id = await header.get_attribute("data-id") or f"inst_{inst_idx}"

        log.info(f"\n  [{inst_idx + 1}/{instance_count}] {instance_name} (ID: {instance_id})")

        instance = Instance(
            instance_id=instance_id,
            name=instance_name,
            order=inst_idx + 1,  # 1-based order
        )

        # Position counter for this instance
        global_position = 0

        # Header PDFs (main decision)
        header_links = header.locator("a[href*='PdfDocument']")
        header_count = await header_links.count()
        for i in range(header_count):
            global_position += 1
            link = header_links.nth(i)
            doc = await parse_document_metadata(link, "cards", instance_id)
            if doc:
                doc.instance_name = instance_name
                doc.position = global_position
                doc.page = 0  # Header is "page 0"
                doc.position_on_page = i + 1
                documents.append(doc)
                instance.documents.append(doc.doc_id)

        if header_count > 0:
            log.info(f"    Header PDFs: {header_count}")

        # Expand accordion
        collapse_btn = header.locator(".b-collapse.js-collapse")
        if await collapse_btn.count() == 0:
            log.info(f"    No expand button, skipping details")
            instances.append(instance)
            continue

        container = page.locator(".b-chrono-items-container.js-chrono-items-container").nth(inst_idx)

        # Check if visible
        is_visible = False
        try:
            style = await container.get_attribute("style") or ""
            is_visible = "display: none" not in style and await container.is_visible()
        except:
            pass

        if not is_visible:
            await collapse_btn.click()
            await page.wait_for_timeout(1000)

        try:
            await container.wait_for(state="visible", timeout=3000)
        except:
            log.info(f"    Container not visible, skipping")
            instances.append(instance)
            continue

        # Pagination for this instance
        pagination_items = container.locator(".js-chrono-pagination-pager-item[data-page_num]")
        pagination_count = await pagination_items.count()

        max_page = 1
        if pagination_count > 0:
            for i in range(pagination_count):
                page_num_str = await pagination_items.nth(i).get_attribute("data-page_num")
                if page_num_str:
                    try:
                        max_page = max(max_page, int(page_num_str))
                    except ValueError:
                        pass

        instance.page_count = max_page
        log.info(f"    Pages: {max_page}")

        # Parse all pages
        for page_num in range(1, max_page + 1):
            if page_num > 1:
                page_btn = container.locator(f".js-chrono-pagination-pager-item[data-page_num='{page_num}']")
                if await page_btn.count() > 0:
                    await page_btn.click()
                    await human_delay_page()

            # Collect PDFs
            page_links = container.locator("a[href*='PdfDocument']")
            pdf_count = await page_links.count()

            for i in range(pdf_count):
                link = page_links.nth(i)
                doc = await parse_document_metadata(link, "cards", instance_id)
                if doc:
                    if doc.doc_id not in instance.documents:
                        global_position += 1
                        doc.instance_name = instance_name
                        doc.position = global_position
                        doc.page = page_num
                        doc.position_on_page = i + 1
                        documents.append(doc)
                        instance.documents.append(doc.doc_id)
                else:
                    url = await link.get_attribute("href")
                    if url:
                        doc = await parse_document_simple(url, "cards", instance_id)
                        if doc.doc_id not in instance.documents:
                            global_position += 1
                            doc.instance_name = instance_name
                            doc.position = global_position
                            doc.page = page_num
                            doc.position_on_page = i + 1
                            documents.append(doc)
                            instance.documents.append(doc.doc_id)

            if max_page > 1:
                log.info(f"      Page {page_num}/{max_page}: {pdf_count} PDF(s)")

        instances.append(instance)

    log.info(f"\n📋 Total from Cards: {len(documents)} documents, {len(instances)} instances")
    return documents, instances


async def click_ed_tab(page: Page) -> bool:
    """Click on 'Электронное дело' tab."""
    log.info("Clicking on 'Электронное дело' tab...")

    ed_tab = page.locator("div.js-case-chrono-button--ed")
    if await ed_tab.count() == 0:
        log.warning("'Электронное дело' tab not found")
        return False

    await ed_tab.click()

    try:
        await page.wait_for_selector("#chrono_ed_content:not(.g-hidden)", timeout=10000)
        log.info("'Электронное дело' tab opened")
        await page.wait_for_timeout(500)
        return True
    except Exception as e:
        log.error(f"Failed to open 'Электронное дело' tab: {e}")
        return False


async def collect_electronic_case(page: Page) -> list[DocumentMeta]:
    """
    Collect all documents from Electronic Case tab with pagination.
    """
    documents = []

    if not await click_ed_tab(page):
        return documents

    # Get total pages
    pagination_items = page.locator("#chrono_ed_content .js-chrono-pagination-pager-item[data-page_num]")
    pagination_count = await pagination_items.count()

    max_page = 1
    if pagination_count > 0:
        for i in range(pagination_count):
            page_num_str = await pagination_items.nth(i).get_attribute("data-page_num")
            if page_num_str:
                try:
                    max_page = max(max_page, int(page_num_str))
                except ValueError:
                    pass

    log.info(f"📖 ED pagination: {max_page} page(s)")

    # Parse all pages
    for page_num in range(1, max_page + 1):
        if page_num > 1:
            page_btn = page.locator(f"#chrono_ed_content .js-chrono-pagination-pager-item[data-page_num='{page_num}']")
            if await page_btn.count() > 0:
                await page_btn.click()
                await human_delay_page()

        # Collect PDFs
        links = page.locator("#chrono_ed_content a.b-case-chrono-ed-item-link[href*='PdfDocument']")
        count = await links.count()

        for i in range(count):
            link = links.nth(i)
            doc = await parse_document_metadata(link, "electronic_case")
            if doc:
                documents.append(doc)
            else:
                url = await link.get_attribute("href")
                if url:
                    documents.append(await parse_document_simple(url, "electronic_case"))

        log.info(f"  Page {page_num}/{max_page}: {count} PDF(s)")

    log.info(f"📋 Total from ED: {len(documents)} documents")
    return documents


# === PDF Download ===
async def download_single_pdf(
        page: Page,
        doc: DocumentMeta,
        output_dir: Path,
        idx: int,
        total: int,
        max_retries: int = 3
) -> Optional[bytes]:
    """
    Download single PDF with retry logic.
    Returns PDF bytes if successful, None otherwise.
    """
    pdf_url = doc.url
    filename = doc.filename or f"{doc.doc_id}.pdf"

    for attempt in range(1, max_retries + 1):
        pdf_page = None
        try:
            pdf_page = await page.context.new_page()
            pdf_content = None

            async def handle_response(response):
                nonlocal pdf_content
                if "Pdf" in response.url:
                    content_type = response.headers.get("content-type", "")
                    if response.status == 200 and "application/pdf" in content_type:
                        try:
                            pdf_content = await response.body()
                        except Exception as e:
                            log.debug(f"  Failed to get body: {e}")

            pdf_page.on("response", handle_response)

            await pdf_page.goto(pdf_url, wait_until="domcontentloaded", timeout=60000)
            await pdf_page.wait_for_timeout(2000)

            # Check for rate limit
            if await check_rate_limit(pdf_page):
                log.error("🚫 RATE LIMITED!")
                await pdf_page.close()
                return None  # Signal to stop

            # Check for antibot
            salto_div = pdf_page.locator("#salto")
            if await salto_div.count() > 0 and not pdf_content:
                log.debug("  WASM antibot detected, waiting...")
                try:
                    await pdf_page.locator("#searchForm").wait_for(state="detached", timeout=30000)
                except:
                    pass
                await pdf_page.wait_for_timeout(3000)

            if pdf_content and pdf_content[:4] == b'%PDF':
                file_size = len(pdf_content) / 1024
                log.info(f"[{idx}/{total}] ✅ {filename[:50]}... ({file_size:.1f} KB)")
                await pdf_page.close()
                return pdf_content
            else:
                log.warning(f"[{idx}/{total}] ⚠️  No PDF content (attempt {attempt}/{max_retries})")

        except Exception as e:
            log.warning(f"[{idx}/{total}] ⚠️  Attempt {attempt}/{max_retries} failed: {type(e).__name__}")

        finally:
            if pdf_page:
                try:
                    await pdf_page.close()
                except:
                    pass

        if attempt < max_retries:
            delay = 2 ** attempt + random.uniform(0, 2)
            log.info(f"[{idx}/{total}] 🔄 Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)

    log.error(f"[{idx}/{total}] ❌ Failed after {max_retries} attempts: {filename[:50]}...")
    return None


def extract_text_from_pdf(pdf_bytes: bytes) -> tuple[str, bool]:
    """
    Extract text from PDF bytes using pymupdf.
    Returns (text, requires_ocr).
    """
    if not HAS_PYMUPDF:
        return "", True

    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()

        text = "\n".join(text_parts).strip()
        requires_ocr = len(text) < 100  # Likely a scan if very little text

        return text, requires_ocr

    except Exception as e:
        log.warning(f"PDF text extraction failed: {e}")
        return "", True


# === Output Generation ===
def save_document(doc: DocumentFull, output_dir: Path, instances: list[Instance] = None) -> None:
    """
    Save single document to appropriate folder.
    - Cards documents go to instances/<folder>/<position>_<doc_id>.json
    - Other documents go to documents/<doc_id>.json
    """
    if doc.source_tab == "cards" and doc.instance_id and instances:
        # Find instance folder
        inst = next((i for i in instances if i.instance_id == doc.instance_id), None)
        if inst:
            safe_name = make_safe_folder_name(inst.name)
            folder_name = f"{inst.order:02d}_{safe_name}_{inst.instance_id[:8]}"
            inst_dir = output_dir / "instances" / folder_name
            inst_dir.mkdir(parents=True, exist_ok=True)

            # Filename: position_docid.json (e.g., 001_c72fa488.json)
            doc_file = inst_dir / f"{doc.position:03d}_{doc.doc_id[:8]}.json"
            doc_file.write_text(
                json.dumps(asdict(doc), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            return

    # Default: save to documents/
    docs_dir = output_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    doc_file = docs_dir / f"{doc.doc_id}.json"
    doc_file.write_text(
        json.dumps(asdict(doc), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def save_case_structure(
        output_dir: Path,
        case_info: CaseInfo,
        court_acts_docs: list[DocumentMeta],
        cards_docs: list[DocumentMeta],
        instances: list[Instance],
        ed_docs: list[DocumentMeta],
) -> None:
    """
    Save case structure with folder-based instances.

    Structure:
        case_XXX/
        ├── case.json
        ├── court_acts.json
        ├── electronic_case.json
        └── instances/
            ├── 01_Апелляционная_5a7f7ecc/
            │   ├── instance.json
            │   └── (documents saved separately during download)
            └── 02_Первая_44af3e0d/
                └── ...
    """

    # Build fingerprints from first doc of each instance
    fingerprints = {}
    for inst in instances:
        if inst.documents:
            fingerprints[inst.instance_id] = inst.documents[0]
    if ed_docs:
        fingerprints["electronic_case"] = ed_docs[0].doc_id
    if court_acts_docs:
        fingerprints["court_acts"] = court_acts_docs[0].doc_id

    case_info.fingerprints = fingerprints

    # case.json
    case_file = output_dir / "case.json"
    case_file.write_text(
        json.dumps(asdict(case_info), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # court_acts.json - list of doc_ids with basic metadata
    court_acts_data = {
        "tab": "court_acts",
        "count": len(court_acts_docs),
        "documents": [
            {
                "doc_id": d.doc_id,
                "date": d.date,
                "doc_type": d.doc_type,
                "title": d.title,
                "court": d.court,
                "judge": d.judge,
                "position": i + 1,
            }
            for i, d in enumerate(court_acts_docs)
        ]
    }
    (output_dir / "court_acts.json").write_text(
        json.dumps(court_acts_data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # instances/ - folder for each instance
    instances_dir = output_dir / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)

    for inst in instances:
        # Create folder name: 01_Апелляционная_5a7f7ecc
        safe_name = make_safe_folder_name(inst.name)
        folder_name = f"{inst.order:02d}_{safe_name}_{inst.instance_id[:8]}"
        inst_folder = instances_dir / folder_name
        inst_folder.mkdir(parents=True, exist_ok=True)

        # Save instance.json
        inst_data = {
            "instance_id": inst.instance_id,
            "name": inst.name,
            "order": inst.order,
            "court": inst.court,
            "page_count": inst.page_count,
            "document_count": len(inst.documents),
            "documents": inst.documents,  # List of doc_ids in order
            "folder": folder_name,
        }
        (inst_folder / "instance.json").write_text(
            json.dumps(inst_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    # electronic_case.json
    ed_data = {
        "tab": "electronic_case",
        "count": len(ed_docs),
        "documents": [
            {
                "doc_id": d.doc_id,
                "date": d.date,
                "doc_type": d.doc_type,
                "title": d.title,
                "position": i + 1,
            }
            for i, d in enumerate(ed_docs)
        ]
    }
    (output_dir / "electronic_case.json").write_text(
        json.dumps(ed_data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def generate_readme(
        output_dir: Path,
        case_info: CaseInfo,
        instances: list[Instance],
        total_docs: int,
        downloaded_count: int,
        failed_count: int,
) -> None:
    """Generate README.md for the case."""

    readme_content = f"""# Дело {case_info.case_number}

**GUID:** `{case_info.case_guid}`  
**Статус:** {case_info.status or "Не указан"}  
**URL:** [{case_info.case_number}]({case_info.url})  
**Дата парсинга:** {case_info.parsed_at}

## Статистика

| Метрика | Значение |
|---------|----------|
| Всего документов | {total_docs} |
| Скачано успешно | {downloaded_count} |
| Не удалось скачать | {failed_count} |
| Инстанций | {len(instances)} |

## Структура

- [`case.json`](case.json) — метаданные дела
- [`court_acts.json`](court_acts.json) — судебные акты
- [`electronic_case.json`](electronic_case.json) — электронное дело
- [`instances/`](instances/) — инстанции (аккордеоны из "Карточки")
- [`documents/`](documents/) — полные тексты документов

## Инстанции

| # | Название | Документов | Страниц |
|---|----------|------------|---------|
"""

    for i, inst in enumerate(instances, 1):
        readme_content += f"| {i} | {inst.name} | {len(inst.documents)} | {inst.page_count} |\n"

    readme_content += """
## Использование

Для анализа дела:
1. Начните с `case.json` для общей информации
2. Используйте `court_acts.json` для списка судебных актов
3. Загружайте отдельные документы из `documents/` по `doc_id`

## Примечания

- Документы с `requires_ocr: true` — сканы, текст не извлечён
- Поле `text` в документах содержит извлечённый текст
- Связи между табами через `doc_id`
"""

    readme_file = output_dir / "README.md"
    readme_file.write_text(readme_content, encoding="utf-8")


# === Fingerprint and Cache Functions ===
def load_cached_case_info(output_dir: Path) -> Optional[CaseInfo]:
    """Load cached case info from case.json."""
    case_file = output_dir / "case.json"
    if not case_file.exists():
        return None
    try:
        data = json.loads(case_file.read_text(encoding="utf-8"))
        return CaseInfo(**data)
    except Exception as e:
        log.warning(f"Failed to load cached case info: {e}")
        return None


def load_cached_instances(output_dir: Path) -> list[Instance]:
    """Load cached instances from instances/ folder."""
    instances = []
    instances_dir = output_dir / "instances"
    if not instances_dir.exists():
        return instances

    for folder in sorted(instances_dir.iterdir()):
        if folder.is_dir():
            inst_file = folder / "instance.json"
            if inst_file.exists():
                try:
                    data = json.loads(inst_file.read_text(encoding="utf-8"))
                    instances.append(Instance(
                        instance_id=data.get("instance_id", ""),
                        name=data.get("name", ""),
                        order=data.get("order", 0),
                        court=data.get("court"),
                        documents=data.get("documents", []),
                        page_count=data.get("page_count", 1),
                    ))
                except Exception as e:
                    log.warning(f"Failed to load instance from {folder}: {e}")

    return instances


async def check_fingerprint_quick(page: Page, cached_fingerprints: dict) -> bool:
    """
    Quick check if case structure has changed.
    Checks only first document of first Cards instance.
    Returns True if fingerprint matches (no changes).
    """
    if not cached_fingerprints:
        return False

    # Get first instance's first doc_id from Cards tab
    if not await click_cards_tab(page):
        return False

    # Find first accordion header
    instance_headers = page.locator("#chrono_list_content .b-chrono-item-header.js-chrono-item-header")
    if await instance_headers.count() == 0:
        return False

    first_header = instance_headers.first
    instance_id = await first_header.get_attribute("data-id")

    if not instance_id or instance_id not in cached_fingerprints:
        return False

    # Get first PDF link from header
    first_link = first_header.locator("a[href*='PdfDocument']").first
    if await first_link.count() == 0:
        # Try expanding accordion
        collapse_btn = first_header.locator(".b-collapse.js-collapse")
        if await collapse_btn.count() > 0:
            await collapse_btn.click()
            await page.wait_for_timeout(1000)

        # Get first link from container
        container = page.locator(".b-chrono-items-container.js-chrono-items-container").first
        first_link = container.locator("a[href*='PdfDocument']").first

    if await first_link.count() == 0:
        return False

    url = await first_link.get_attribute("href")
    if not url:
        return False

    # Extract doc_id from URL
    _, current_first_doc_id = extract_guid_from_url(url)
    cached_first_doc_id = cached_fingerprints.get(instance_id)

    if current_first_doc_id == cached_first_doc_id:
        log.info(f"✅ Fingerprint match! First doc unchanged: {current_first_doc_id[:8]}...")
        return True
    else:
        log.info(
            f"⚠️ Fingerprint mismatch! First doc changed: {cached_first_doc_id[:8] if cached_first_doc_id else 'None'}... → {current_first_doc_id[:8]}...")
        return False


def load_cached_documents_metadata(output_dir: Path, instances: list[Instance]) -> list[DocumentMeta]:
    """
    Load document metadata from cached structure (without full text).
    Used for resume when fingerprint matches.
    """
    documents = []

    # Load from instance folders
    instances_dir = output_dir / "instances"
    if instances_dir.exists():
        for folder in sorted(instances_dir.iterdir()):
            if folder.is_dir():
                for doc_file in sorted(folder.glob("*.json")):
                    if doc_file.name == "instance.json":
                        continue
                    try:
                        data = json.loads(doc_file.read_text(encoding="utf-8"))
                        # Create DocumentMeta from saved data
                        doc = DocumentMeta(
                            doc_id=data.get("doc_id", ""),
                            case_guid=data.get("case_guid", ""),
                            url=data.get("url", ""),
                            filename=data.get("filename", ""),
                            date=data.get("date"),
                            doc_type=data.get("doc_type"),
                            title=data.get("title"),
                            court=data.get("court"),
                            judge=data.get("judge"),
                            signed=data.get("signed", False),
                            signature_valid=data.get("signature_valid", False),
                            source_tab=data.get("source_tab", ""),
                            instance_id=data.get("instance_id"),
                            instance_name=data.get("instance_name"),
                            position=data.get("position", 0),
                            page=data.get("page", 1),
                            position_on_page=data.get("position_on_page", 0),
                        )
                        documents.append(doc)
                    except Exception as e:
                        log.debug(f"Failed to load doc from {doc_file}: {e}")

    # Load from documents/ folder
    docs_dir = output_dir / "documents"
    if docs_dir.exists():
        for doc_file in docs_dir.glob("*.json"):
            try:
                data = json.loads(doc_file.read_text(encoding="utf-8"))
                doc = DocumentMeta(
                    doc_id=data.get("doc_id", ""),
                    case_guid=data.get("case_guid", ""),
                    url=data.get("url", ""),
                    filename=data.get("filename", ""),
                    date=data.get("date"),
                    doc_type=data.get("doc_type"),
                    title=data.get("title"),
                    court=data.get("court"),
                    judge=data.get("judge"),
                    signed=data.get("signed", False),
                    signature_valid=data.get("signature_valid", False),
                    source_tab=data.get("source_tab", ""),
                    instance_id=data.get("instance_id"),
                    instance_name=data.get("instance_name"),
                    position=data.get("position", 0),
                    page=data.get("page", 1),
                    position_on_page=data.get("position_on_page", 0),
                )
                documents.append(doc)
            except Exception as e:
                log.debug(f"Failed to load doc from {doc_file}: {e}")

    return documents


# === Main Processing ===
async def process_case(page: Page, case_number: str, output_base: Path) -> bool:
    """
    Main processing function for a single case.
    Returns True if successful.
    """
    log.info(f"\n{'=' * 60}")
    log.info(f"Processing case: {case_number}")
    log.info('=' * 60)

    # Search for case
    success = await search_by_case_number(page, case_number)
    if not success:
        log.error("Search failed")
        return False

    # Get case URL
    case_id_input = page.locator("input#caseId")
    if await case_id_input.count() > 0:
        case_guid = await case_id_input.get_attribute("value")
        case_url = f"https://kad.arbitr.ru/Card/{case_guid}"
    else:
        case_link = page.locator("table#b-cases tbody tr td.num a.num_case").first
        if await case_link.count() > 0:
            case_url = await case_link.get_attribute("href")
        else:
            log.error("Could not find case link")
            return False

    # Navigate to case card
    case_info = await navigate_to_case_card(page, case_url)
    if not case_info:
        return False

    # Create output directory
    safe_case_number = case_info.case_number.replace("/", "-")
    output_dir = output_base / f"case_{safe_case_number}"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "documents").mkdir(exist_ok=True)

    log.info(f"\n📁 Output directory: {output_dir}")

    # Check for existing progress and cached structure
    progress = load_progress(output_dir)
    cached_case_info = load_cached_case_info(output_dir)
    use_cache = False
    instances = []
    all_docs = []
    court_acts_docs = []
    cards_docs = []
    ed_docs = []

    if progress and progress.downloaded and cached_case_info and cached_case_info.fingerprints:
        log.info(f"📂 Found previous progress: {len(progress.downloaded)} done, {len(progress.pending)} pending")
        log.info("🔍 Checking fingerprint...")

        # Quick fingerprint check
        fingerprint_match = await check_fingerprint_quick(page, cached_case_info.fingerprints)

        if fingerprint_match:
            log.info("📋 Fingerprint match — structure unchanged, but will parse for full doc list")
            # NOTE: use_cache disabled because load_cached_documents_metadata
            # only reads downloaded files, not the full list
            # TODO: fix cache to store full document list separately
        else:
            log.info("📋 Structure changed, re-parsing...")

    if not progress:
        progress = Progress()

    if not use_cache:
        # === Full parsing of all tabs ===
        log.info(f"\n--- Collecting documents ---")

        # Court Acts
        court_acts_docs = await collect_court_acts(page)

        # Cards (with instances)
        cards_docs, instances = await collect_cards_all_instances(page)

        # Electronic Case
        ed_docs = await collect_electronic_case(page)

        # Deduplicate by doc_id
        all_docs_map: dict[str, DocumentMeta] = {}
        for doc in court_acts_docs + cards_docs + ed_docs:
            if doc.doc_id not in all_docs_map:
                all_docs_map[doc.doc_id] = doc

        all_docs = list(all_docs_map.values())

        log.info(f"\n📊 TOTAL UNIQUE DOCUMENTS: {len(all_docs)}")
        log.info(f"   - Court Acts: {len(court_acts_docs)}")
        log.info(f"   - Cards: {len(cards_docs)} ({len(instances)} instances)")
        log.info(f"   - Electronic Case: {len(ed_docs)}")

        # Update case info
        case_info.total_documents = len(all_docs)
        case_info.instances_count = len(instances)

        # Save structure (without texts yet)
        save_case_structure(output_dir, case_info, court_acts_docs, cards_docs, instances, ed_docs)

    total_docs = len(all_docs)

    # === Download and process documents ===
    log.info(f"\n--- Downloading {total_docs} documents ---")

    # Filter out already downloaded
    if progress.downloaded:
        already_done = set(progress.downloaded)
        docs_to_download = [d for d in all_docs if d.doc_id not in already_done]
        log.info(f"⏭️  Skipping {len(already_done)} already downloaded")
        # Clear failed - we'll retry them
        if progress.failed:
            log.info(f"🔄 Will retry {len(progress.failed)} previously failed")
            progress.failed = []
    else:
        docs_to_download = all_docs

    # Set pending
    progress.pending = [d.doc_id for d in docs_to_download]
    save_progress(output_dir, progress)

    downloaded_count = len(progress.downloaded)
    failed_count = 0
    rate_limited = False
    consecutive_failures = 0  # Track failures in a row

    for idx, doc in enumerate(docs_to_download, 1):
        # Check for break
        if should_take_break(idx):
            await take_break()
            # Keep session alive - double refresh with proper delays
            log.info("🔄 Refreshing session (1/2)...")
            try:
                await page.goto(case_info.url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                log.info("🔄 Refreshing session (2/2)...")
                await page.goto(case_info.url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
            except Exception as e:
                log.warning(f"Session refresh failed: {e}")
            consecutive_failures = 0  # Reset after refresh

        # Download PDF
        log.info(f"\n[{downloaded_count + idx}/{total_docs}] ⬇️  {doc.filename[:50]}...")

        pdf_bytes = await download_single_pdf(page, doc, output_dir, downloaded_count + idx, total_docs)

        if pdf_bytes is None:
            # Check if rate limited (download_single_pdf returns None for rate limit)
            if await check_rate_limit(page):
                log.error("🚫 RATE LIMITED! Stopping gracefully...")
                rate_limited = True
                progress.failed.append(doc.doc_id)
                progress.pending = [d.doc_id for d in docs_to_download[idx:]]
                save_progress(output_dir, progress)
                break

            # Regular failure
            progress.failed.append(doc.doc_id)
            if doc.doc_id in progress.pending:
                progress.pending.remove(doc.doc_id)
            failed_count += 1
            consecutive_failures += 1

            # 3 failures in a row — session is stale, force refresh
            if consecutive_failures >= 3:
                log.warning("⚠️ 3 failures in a row — forcing session refresh...")
                await take_break()
                try:
                    log.info("🔄 Refreshing session (1/2)...")
                    await page.goto(case_info.url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(5000)

                    log.info("🔄 Refreshing session (2/2)...")
                    await page.goto(case_info.url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(5000)
                except Exception as e:
                    log.warning(f"Session refresh failed: {e}")
                consecutive_failures = 0
        else:
            consecutive_failures = 0  # Success — reset counter
            # Extract text
            text, requires_ocr = extract_text_from_pdf(pdf_bytes)

            # Create full document
            full_doc = DocumentFull(
                **asdict(doc),
                has_text=len(text) > 0,
                requires_ocr=requires_ocr,
                char_count=len(text),
                text=text,
            )

            # Save document
            save_document(full_doc, output_dir, instances)

            progress.downloaded.append(doc.doc_id)
            progress.pending.remove(doc.doc_id)

        # Save progress periodically
        if idx % 10 == 0:
            save_progress(output_dir, progress)

        # Human delay
        if idx < len(docs_to_download):
            await human_delay_doc()

    # Final save
    save_progress(output_dir, progress)

    downloaded_count = len(progress.downloaded)
    failed_count = len(progress.failed)

    # Generate README
    generate_readme(output_dir, case_info, instances, total_docs, downloaded_count, failed_count)

    # Summary
    log.info(f"\n{'=' * 60}")
    log.info(f"📦 PROCESSING COMPLETE")
    log.info(f"   Total documents: {total_docs}")
    log.info(f"   Downloaded: {downloaded_count}")
    log.info(f"   Failed: {failed_count}")
    log.info(f"   Location: {output_dir.absolute()}")
    if rate_limited:
        log.warning(f"   ⚠️  Rate limited! Resume later with same command.")
    log.info('=' * 60)

    return not rate_limited


async def main():
    """Main entry point."""
    # Parse arguments
    if len(sys.argv) < 2:
        print("Usage: python poc2.py <case_number> [output_dir]")
        print("Example: python poc2.py А60-21280/2023 ./output")
        sys.exit(1)

    case_number = sys.argv[1]
    output_base = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("./output")
    output_base.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        log.info("🚀 Launching Firefox browser...")
        browser: Browser = await p.firefox.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            accept_downloads=True,
        )

        page = await context.new_page()

        if HAS_STEALTH:
            await stealth_async(page)
            log.info("Stealth mode applied")
        else:
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

        try:
            log.info(f"Navigating to {BASE_URL}")
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            log.info("Page loaded")

            await process_case(page, case_number, output_base)

        except Exception as e:
            log.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            raise
        finally:
            await browser.close()
            log.info("Browser closed")


if __name__ == "__main__":
    asyncio.run(main())