#!/usr/bin/env python3
"""
PoC Crawler for kad.arbitr.ru
Phase 0.5: Extended Proof of Concept

Features:
- Search by case number
- Parse case card (instances, court acts)
- Parse "Электронное дело" tab with pagination
- Download all PDFs via response interceptor
"""

import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Page, Browser

# Try to import stealth, install if not available
try:
    from playwright_stealth import stealth_async

    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("playwright-stealth not installed. Run: pip install playwright-stealth")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# === Configuration ===
BASE_URL = "https://kad.arbitr.ru/"
HEADLESS = True  # Firefox headless
SLOW_MO = 100  # Milliseconds between actions


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
    Returns True if search was successful, False otherwise.
    """
    log.info(f"Searching for case: {case_number}")

    # Wait for the page to be ready
    await page.wait_for_selector("#sug-cases", timeout=10000)
    log.info("Search form loaded")

    # Wait for JS to fully initialize
    await page.wait_for_timeout(3000)

    # Close promo popup if present
    await close_promo_popup(page)

    # Find the case number input field
    case_input = page.locator("#sug-cases input")

    # Click to focus, then type
    await case_input.click()
    await page.wait_for_timeout(300)

    # Clear any existing value first
    await case_input.fill("")
    await page.wait_for_timeout(100)

    # Type the case number with delay (human-like)
    await case_input.type(case_number, delay=50)
    log.info(f"Entered case number: {case_number}")

    # Trigger input/change events to ensure JS handlers fire
    await page.evaluate("""
        const input = document.querySelector('#sug-cases input');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    """)

    # Wait for suggest dropdown
    await page.wait_for_timeout(1000)

    # Extract GUID from suggest dropdown
    case_guid = None
    suggest_item = page.locator("#b-suggest li a, .b-suggest li a").first
    if await suggest_item.count() > 0:
        case_guid = await suggest_item.get_attribute("id")
        log.info(f"Found suggest item with GUID: {case_guid}")

        # Navigate directly to case card if GUID is valid
        if case_guid and case_guid != "00000000-0000-0000-0000-000000000000":
            card_url = f"https://kad.arbitr.ru/Card/{case_guid}"
            log.info(f"Navigating directly to case card: {card_url}")
            await page.goto(card_url, wait_until="domcontentloaded")
            return True

    # Fallback: Try keyboard navigation
    await page.keyboard.press("ArrowDown")
    await page.wait_for_timeout(200)
    await page.keyboard.press("Enter")

    # Wait for navigation
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

    # Wait for results
    try:
        await page.wait_for_selector(
            "table#b-cases tbody tr, div.b-noResults:not(.g-hidden), input#caseId",
            timeout=30000
        )
        return True
    except Exception as e:
        log.error(f"Timeout waiting for search results: {e}")
        return False


async def navigate_to_case_card(page: Page, case_url: str) -> dict | None:
    """
    Navigate to a case card and extract case details.
    Returns dict with case info or None if failed.
    """
    log.info(f"Navigating to case card: {case_url}")

    await page.goto(case_url, wait_until="domcontentloaded", timeout=30000)

    # Wait for case card to load
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

    # Extract case info
    case_info = {}

    # Case GUID from hidden input
    case_id_elem = page.locator("input#caseId")
    if await case_id_elem.count() > 0:
        case_info["guid"] = await case_id_elem.get_attribute("value")
    else:
        case_info["guid"] = case_url.split("/")[-1]

    # Case number from hidden input
    case_name_elem = page.locator("input#caseName")
    if await case_name_elem.count() > 0:
        case_info["case_number"] = await case_name_elem.get_attribute("value")

    # Status
    status_elem = page.locator("div.b-case-header-desc")
    if await status_elem.count() > 0:
        case_info["status"] = (await status_elem.text_content()).strip()

    log.info(f"Case: {case_info.get('case_number', 'N/A')} | GUID: {case_info.get('guid', 'N/A')}")

    return case_info


async def collect_court_acts_pdf_urls(page: Page) -> set[str]:
    """
    Collect PDF URLs from Court Acts section (#gr_case_acts).
    This section has no pagination.
    """
    pdf_urls = set()

    court_acts_links = page.locator("#gr_case_acts a[href*='PdfDocument']")
    count = await court_acts_links.count()
    log.info(f"Found {count} PDF(s) in Court Acts section")

    for i in range(count):
        url = await court_acts_links.nth(i).get_attribute("href")
        if url:
            pdf_urls.add(url)

    return pdf_urls


async def click_cards_tab(page: Page) -> bool:
    """
    Click on "Карточки" tab to ensure it's active.
    Returns True if successful.
    """
    log.info("Clicking on 'Карточки' tab...")

    cards_tab = page.locator("div.js-case-chrono-button--cards")

    if await cards_tab.count() == 0:
        log.warning("'Карточки' tab not found")
        return False

    await cards_tab.click()

    # Wait for content to become visible
    try:
        await page.wait_for_selector(
            "#chrono_list_content:not(.g-hidden)",
            timeout=10000
        )
        log.info("'Карточки' tab opened")
        await page.wait_for_timeout(500)
        return True
    except Exception as e:
        log.error(f"Failed to open 'Карточки' tab: {e}")
        return False


async def collect_cards_all_pages(page: Page) -> set[str]:
    """
    Collect all PDF URLs from all instances in "Карточки" tab.
    Each instance is an accordion that needs to be expanded.
    Each expanded instance may have its own pagination.
    Returns deduplicated set of URLs.
    """
    all_urls = set()

    # Click on Cards tab first
    if not await click_cards_tab(page):
        return all_urls

    # Find all instance headers (accordions)
    instance_headers = page.locator("#chrono_list_content .b-chrono-item-header.js-chrono-item-header")
    instance_count = await instance_headers.count()
    log.info(f"Found {instance_count} instance(s) in Cards tab")

    for inst_idx in range(instance_count):
        header = instance_headers.nth(inst_idx)

        # Get instance name for logging
        instance_name_elem = header.locator("div.l-col strong")
        instance_name = "Unknown"
        if await instance_name_elem.count() > 0:
            instance_name = (await instance_name_elem.text_content()).strip()

        # Get instance ID
        instance_id = await header.get_attribute("data-id") or f"inst_{inst_idx}"

        log.info(f"\n  [{inst_idx + 1}/{instance_count}] {instance_name} (ID: {instance_id[:8]}...)")

        # Collect PDFs from header itself (main decision PDF)
        header_pdfs = header.locator("a[href*='PdfDocument']")
        header_pdf_count = await header_pdfs.count()
        for i in range(header_pdf_count):
            url = await header_pdfs.nth(i).get_attribute("href")
            if url:
                all_urls.add(url)

        if header_pdf_count > 0:
            log.info(f"    Header PDFs: {header_pdf_count}")

        # Find the collapse button to expand this instance
        collapse_btn = header.locator(".b-collapse.js-collapse")
        if await collapse_btn.count() == 0:
            log.info(f"    No expand button, skipping details")
            continue

        # Check if already expanded
        container = page.locator(f".b-chrono-items-container.js-chrono-items-container").nth(inst_idx)
        is_visible = False
        try:
            style = await container.get_attribute("style") or ""
            is_visible = "display: none" not in style and await container.is_visible()
        except:
            pass

        if not is_visible:
            # Click to expand
            await collapse_btn.click()
            await page.wait_for_timeout(1000)

        # Now find pagination for this instance (if any)
        # The container follows the header
        instance_container = page.locator(f".b-chrono-items-container.js-chrono-items-container").nth(inst_idx)

        # Check if container is now visible
        try:
            await instance_container.wait_for(state="visible", timeout=3000)
        except:
            log.info(f"    Container not visible, skipping")
            continue

        # Get pagination info for this instance
        pagination_items = instance_container.locator(".js-chrono-pagination-pager-item[data-page_num]")
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

        log.info(f"    Pages: {max_page}")

        # Parse all pages for this instance
        for page_num in range(1, max_page + 1):
            if page_num > 1:
                # Navigate to this page
                page_btn = instance_container.locator(f".js-chrono-pagination-pager-item[data-page_num='{page_num}']")
                if await page_btn.count() > 0:
                    await page_btn.click()
                    await page.wait_for_timeout(1500)

            # Collect PDFs from current page
            page_pdfs = instance_container.locator("a[href*='PdfDocument']")
            pdf_count = await page_pdfs.count()

            for i in range(pdf_count):
                url = await page_pdfs.nth(i).get_attribute("href")
                if url:
                    all_urls.add(url)

            if max_page > 1:
                log.info(f"      Page {page_num}/{max_page}: {pdf_count} PDF(s)")

        # Collapse back to clean up UI (optional, but good practice)
        # await collapse_btn.click()
        # await page.wait_for_timeout(300)

    log.info(f"\n📋 Total unique PDFs from Cards: {len(all_urls)}")
    return all_urls


async def click_ed_tab(page: Page) -> bool:
    """
    Click on "Электронное дело" tab.
    Returns True if successful.
    """
    log.info("Clicking on 'Электронное дело' tab...")

    # Find and click the tab
    ed_tab = page.locator("div.js-case-chrono-button--ed")

    if await ed_tab.count() == 0:
        log.warning("'Электронное дело' tab not found")
        return False

    await ed_tab.click()

    # Wait for content to become visible
    try:
        await page.wait_for_selector(
            "#chrono_ed_content:not(.g-hidden)",
            timeout=10000
        )
        log.info("'Электронное дело' tab opened")
        await page.wait_for_timeout(500)
        return True
    except Exception as e:
        log.error(f"Failed to open 'Электронное дело' tab: {e}")
        return False


async def get_ed_total_pages(page: Page) -> int:
    """
    Get total number of pages in "Электронное дело" pagination.
    Returns number of pages (minimum 1).
    """
    # Find pagination items inside ED content
    pagination_items = page.locator(
        "#chrono_ed_content .js-chrono-pagination-pager-item[data-page_num]"
    )
    count = await pagination_items.count()

    if count == 0:
        return 1

    # Find max page number
    max_page = 1
    for i in range(count):
        page_num_str = await pagination_items.nth(i).get_attribute("data-page_num")
        if page_num_str:
            try:
                page_num = int(page_num_str)
                max_page = max(max_page, page_num)
            except ValueError:
                pass

    log.info(f"📖 ED pagination: {max_page} page(s)")
    return max_page


async def parse_ed_page_documents(page: Page) -> set[str]:
    """
    Parse PDF URLs from current "Электронное дело" page.
    Returns set of URLs.
    """
    pdf_urls = set()

    # ED document links
    ed_links = page.locator("#chrono_ed_content a.b-case-chrono-ed-item-link[href*='PdfDocument']")
    count = await ed_links.count()

    for i in range(count):
        url = await ed_links.nth(i).get_attribute("href")
        if url:
            pdf_urls.add(url)

    return pdf_urls


async def navigate_ed_page(page: Page, page_num: int) -> bool:
    """
    Navigate to specific page in "Электронное дело" pagination.
    Returns True if successful.
    """
    log.info(f"  Navigating to ED page {page_num}...")

    # Find pagination item with specific page number inside ED content
    page_item = page.locator(
        f"#chrono_ed_content .js-chrono-pagination-pager-item[data-page_num='{page_num}']"
    )

    if await page_item.count() == 0:
        log.warning(f"  Page {page_num} not found in pagination")
        return False

    # Click on page number
    await page_item.click()

    # Wait for content to update (AJAX)
    await page.wait_for_timeout(1500)

    # Verify we're on the right page (active class)
    active_item = page.locator(
        "#chrono_ed_content .js-chrono-pagination-pager-item--active"
    )
    if await active_item.count() > 0:
        active_page = await active_item.get_attribute("data-page_num")
        if active_page == str(page_num):
            return True

    # Even if verification fails, content might have loaded
    return True


async def collect_ed_all_pages(page: Page) -> set[str]:
    """
    Collect all PDF URLs from all pages of "Электронное дело".
    Returns deduplicated set of URLs.
    """
    all_urls = set()

    # Click on ED tab first
    if not await click_ed_tab(page):
        return all_urls

    # Get total pages
    total_pages = await get_ed_total_pages(page)

    # Parse all pages
    for page_num in range(1, total_pages + 1):
        if page_num > 1:
            if not await navigate_ed_page(page, page_num):
                log.warning(f"  Failed to navigate to page {page_num}, continuing...")
                continue

        # Parse current page
        page_urls = await parse_ed_page_documents(page)
        log.info(f"  Page {page_num}/{total_pages}: found {len(page_urls)} PDF(s)")
        all_urls.update(page_urls)

    log.info(f"📋 Total unique PDFs from ED: {len(all_urls)}")
    return all_urls


async def download_single_pdf(
        page: Page,
        pdf_url: str,
        filepath: Path,
        idx: int,
        total: int,
        max_retries: int = 3
) -> bool:
    """
    Download single PDF with retry logic.
    Returns True if successful.
    """
    filename = filepath.name

    for attempt in range(1, max_retries + 1):
        pdf_page = None
        try:
            # Open new tab
            pdf_page = await page.context.new_page()

            # Variable to capture PDF content
            pdf_content = None

            # Set up response interceptor BEFORE navigating
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

            # Navigate with longer timeout
            await pdf_page.goto(pdf_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for WASM antibot check
            await pdf_page.wait_for_timeout(2000)

            # Check for antibot page
            salto_div = pdf_page.locator("#salto")
            if await salto_div.count() > 0 and not pdf_content:
                log.debug("  WASM antibot detected, waiting...")
                try:
                    await pdf_page.locator("#searchForm").wait_for(state="detached", timeout=30000)
                except:
                    pass
                await pdf_page.wait_for_timeout(3000)

            # Save PDF if intercepted
            if pdf_content and pdf_content[:4] == b'%PDF':
                filepath.write_bytes(pdf_content)
                file_size = len(pdf_content) / 1024
                log.info(f"[{idx}/{total}] ✅ Saved: {filename[:50]}... ({file_size:.1f} KB)")
                await pdf_page.close()
                return True
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

        # Retry delay with exponential backoff
        if attempt < max_retries:
            delay = 2 ** attempt  # 2, 4, 8 seconds
            log.info(f"[{idx}/{total}] 🔄 Retrying in {delay}s...")
            await asyncio.sleep(delay)

    log.error(f"[{idx}/{total}] ❌ Failed after {max_retries} attempts: {filename[:50]}...")
    return False


async def download_pdf_batch(
        page: Page,
        pdf_urls: set[str],
        case_dir: Path,
        delay_between: float = 0.5
) -> list[str]:
    """
    Download all PDFs from URL set.
    Uses response interceptor to capture PDF content.
    Returns list of downloaded file paths.
    """
    downloaded = []
    failed = []
    total = len(pdf_urls)

    for idx, pdf_url in enumerate(pdf_urls, 1):
        # Extract filename from URL
        filename = pdf_url.split("/")[-1]
        filepath = case_dir / filename

        # Skip if already downloaded
        if filepath.exists():
            log.info(f"[{idx}/{total}] ⏭️  Already exists: {filename[:50]}...")
            downloaded.append(str(filepath))
            continue

        log.info(f"[{idx}/{total}] ⬇️  Downloading: {filename[:50]}...")

        success = await download_single_pdf(page, pdf_url, filepath, idx, total)

        if success:
            downloaded.append(str(filepath))
        else:
            failed.append(pdf_url)

        # Small delay between downloads to avoid rate limiting
        if idx < total:
            await asyncio.sleep(delay_between)

    if failed:
        log.warning(f"\n⚠️  Failed downloads ({len(failed)}):")
        for url in failed[:10]:  # Show first 10
            log.warning(f"   - {url.split('/')[-1]}")
        if len(failed) > 10:
            log.warning(f"   ... and {len(failed) - 10} more")

    return downloaded


async def main():
    """Main entry point."""
    # Test case - big bankruptcy case with 9 pages in ED
    test_cases = [
        "А60-21280/2023",
    ]

    async with async_playwright() as p:
        log.info("Launching Firefox browser...")
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

        # Apply stealth mode
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
            # Navigate to main page
            log.info(f"Navigating to {BASE_URL}")
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            log.info("Page loaded")

            for case_number in test_cases:
                log.info(f"\n{'=' * 60}")
                log.info(f"Processing case: {case_number}")
                log.info('=' * 60)

                # Search for case
                success = await search_by_case_number(page, case_number)
                if not success:
                    log.error("Search failed, skipping case")
                    continue

                # Check if we're on case card or search results
                case_id_input = page.locator("input#caseId")
                if await case_id_input.count() > 0:
                    # Already on case card
                    case_guid = await case_id_input.get_attribute("value")
                    case_url = f"https://kad.arbitr.ru/Card/{case_guid}"
                else:
                    # Need to navigate from search results
                    case_link = page.locator("table#b-cases tbody tr td.num a.num_case").first
                    if await case_link.count() > 0:
                        case_url = await case_link.get_attribute("href")
                    else:
                        log.error("Could not find case link")
                        continue

                # Navigate to case card
                case_details = await navigate_to_case_card(page, case_url)
                if not case_details:
                    log.error("Failed to load case card")
                    continue

                log.info(f"\n--- Collecting PDF URLs ---")

                # 1. Collect PDFs from Court Acts (no pagination)
                court_acts_pdfs = await collect_court_acts_pdf_urls(page)

                # 2. Collect PDFs from "Карточки" (with pagination)
                cards_pdfs = await collect_cards_all_pages(page)

                # 3. Collect PDFs from "Электронное дело" (with pagination)
                ed_pdfs = await collect_ed_all_pages(page)

                # Merge and deduplicate
                all_pdfs = court_acts_pdfs | cards_pdfs | ed_pdfs
                log.info(f"\n📊 TOTAL UNIQUE PDFs: {len(all_pdfs)}")
                log.info(f"   - From Court Acts: {len(court_acts_pdfs)}")
                log.info(f"   - From Cards: {len(cards_pdfs)}")
                log.info(f"   - From ED: {len(ed_pdfs)}")

                # Calculate overlaps
                acts_cards_overlap = len(court_acts_pdfs & cards_pdfs)
                acts_ed_overlap = len(court_acts_pdfs & ed_pdfs)
                cards_ed_overlap = len(cards_pdfs & ed_pdfs)
                log.info(
                    f"   - Overlaps: Acts∩Cards={acts_cards_overlap}, Acts∩ED={acts_ed_overlap}, Cards∩ED={cards_ed_overlap}")

                # Create download folder
                safe_case_number = case_details.get("case_number", "unknown").replace("/", "-")
                case_dir = Path("./downloads") / safe_case_number
                case_dir.mkdir(parents=True, exist_ok=True)
                log.info(f"\n📁 Download folder: {case_dir}")

                # Download all PDFs
                log.info(f"\n--- Downloading {len(all_pdfs)} PDF(s) ---")
                downloaded = await download_pdf_batch(page, all_pdfs, case_dir)

                # Summary
                log.info(f"\n{'=' * 60}")
                log.info(f"📦 DOWNLOAD COMPLETE")
                log.info(f"   Total PDFs found: {len(all_pdfs)}")
                log.info(f"   Successfully downloaded: {len(downloaded)}")
                log.info(f"   Failed: {len(all_pdfs) - len(downloaded)}")
                log.info(f"   Location: {case_dir.absolute()}")
                log.info('=' * 60)

            # Done
            log.info("\nBrowser will close in 5 seconds...")
            await asyncio.sleep(5)

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