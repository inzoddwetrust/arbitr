#!/usr/bin/env python3
"""
PoC Crawler for kad.arbitr.ru
Phase 0: Proof of Concept

Step 1: Search by case number
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
            await page.wait_for_timeout(500)  # Wait for animation
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

    # Screenshot before search
    await page.screenshot(path="debug_before_search.png")
    log.info("Saved screenshot: debug_before_search.png")

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
    log.info("Triggered input events")

    # Wait for and intercept the Suggest API response
    suggest_data = None
    try:
        async with page.expect_response(
                lambda resp: "Suggest/CaseNum" in resp.url,
                timeout=5000
        ) as response_info:
            await page.wait_for_timeout(100)  # Allow request to be made

        response = await response_info.value
        if response.status == 200:
            suggest_data = await response.json()
            log.info(f"Suggest API response: {suggest_data}")
    except Exception as e:
        log.warning(f"Could not intercept Suggest response: {e}")

    # Wait for suggest dropdown to appear
    await page.wait_for_timeout(1000)

    # Screenshot suggest
    await page.screenshot(path="debug_suggest_visible.png")
    log.info("Saved screenshot: debug_suggest_visible.png")

    # Extract GUID from suggest dropdown or API response
    case_guid = None

    # Try to get GUID from the suggest item's id attribute
    suggest_item = page.locator("#b-suggest li a, .b-suggest li a").first
    if await suggest_item.count() > 0:
        suggest_text = await suggest_item.text_content()
        case_guid = await suggest_item.get_attribute("id")
        log.info(f"Found suggest item: '{suggest_text}' with GUID: {case_guid}")

        # Check if GUID is valid (not all zeros)
        if case_guid and case_guid != "00000000-0000-0000-0000-000000000000":
            # Navigate directly to the case card!
            card_url = f"https://kad.arbitr.ru/Card/{case_guid}"
            log.info(f"Navigating directly to case card: {card_url}")
            await page.goto(card_url, wait_until="domcontentloaded")
            return True
        else:
            log.info("GUID is empty or invalid, trying keyboard navigation...")

    # Fallback: Try keyboard navigation
    await page.keyboard.press("ArrowDown")
    log.info("Pressed ArrowDown to select suggest item")
    await page.wait_for_timeout(200)

    await page.keyboard.press("Enter")
    log.info("Pressed Enter to confirm selection")

    # Wait for search AJAX request to be made
    log.info("Waiting for search request...")
    try:
        search_response = await page.wait_for_response(
            lambda r: "Kad/Search" in r.url or "/Card/" in r.url,
            timeout=15000
        )
        log.info(f"Search/navigation detected: {search_response.url[:100]}")
    except:
        log.warning("No search request detected within timeout")

    # Wait a bit more for results
    await page.wait_for_timeout(1000)

    # Check if loading indicator appeared
    loading = page.locator(".b-case-loading:not([style*='none']), .loading")
    if await loading.count() > 0:
        log.info("Loading indicator detected, waiting...")

    await page.wait_for_timeout(2000)
    await page.screenshot(path="debug_after_click.png")
    log.info("Saved screenshot: debug_after_click.png")

    # Log current URL (maybe we navigated?)
    current_url = page.url
    log.info(f"Current URL: {current_url}")

    # Check for CAPTCHA
    captcha = page.locator(".b-pravocaptcha-modal_wrapper:not(:empty), .g-recaptcha, iframe[src*='recaptcha']")
    if await captcha.count() > 0:
        log.warning("⚠️ CAPTCHA detected! Manual intervention needed.")
        await page.screenshot(path="debug_captcha.png")
        return False

    # Wait for results to load
    # Could be either search results table OR case card page (if we navigated directly)
    try:
        await page.wait_for_selector(
            "table#b-cases tbody tr, div.b-noResults:not(.g-hidden), input#caseId",
            timeout=30000
        )

        # Check if we landed on case card directly
        case_id_input = page.locator("input#caseId")
        if await case_id_input.count() > 0:
            log.info("Landed on case card page directly!")
            return True

        log.info("Search results loaded")
        return True
    except Exception as e:
        log.error(f"Timeout waiting for search results: {e}")
        # Save screenshot for debugging
        await page.screenshot(path="debug_search_timeout.png")
        log.info("Saved debug screenshot: debug_search_timeout.png")
        return False


async def navigate_to_case_card(page: Page, case_url: str) -> dict | None:
    """
    Navigate to a case card and extract case details.

    Returns dict with case info or None if failed.
    """
    log.info(f"Navigating to case card: {case_url}")

    await page.goto(case_url, wait_until="domcontentloaded", timeout=30000)

    # Wait for case card to load - wait for chrono items (instances)
    try:
        # Wait for the chronology section which contains instances
        await page.wait_for_selector(
            "div.b-chrono-item-header.js-chrono-item-header",
            timeout=15000
        )
        log.info("Case card loaded - found chrono items")

        # Give it a moment to fully render
        await page.wait_for_timeout(1000)

    except Exception as e:
        log.error(f"Failed to load case card: {e}")
        await page.screenshot(path="debug_case_card_error.png")
        html = await page.content()
        with open("debug_case_card.html", "w", encoding="utf-8") as f:
            f.write(html)
        log.info("Saved HTML to debug_case_card.html")
        return None

    # Take screenshot
    await page.screenshot(path="debug_case_card.png")
    log.info("Saved screenshot: debug_case_card.png")

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

    log.info(
        f"Case: {case_info.get('case_number', 'N/A')} | GUID: {case_info.get('guid', 'N/A')} | Status: {case_info.get('status', 'N/A')}")

    # Find all instances (судебные инстанции)
    instances = []
    instance_headers = page.locator("div.b-chrono-item-header.js-chrono-item-header")
    instance_count = await instance_headers.count()
    log.info(f"Found {instance_count} instance(s)")

    for i in range(instance_count):
        header = instance_headers.nth(i)

        instance_info = {
            "court_code": await header.get_attribute("data-court"),
            "instance_id": await header.get_attribute("data-id"),
        }

        # Instance type (Первая инстанция / Апелляционная инстанция)
        instance_type_elem = header.locator("div.l-col strong")
        if await instance_type_elem.count() > 0:
            instance_info["instance_type"] = (await instance_type_elem.text_content()).strip()

        # Registration date
        reg_date_elem = header.locator("span.b-reg-date")
        if await reg_date_elem.count() > 0:
            instance_info["reg_date"] = (await reg_date_elem.text_content()).strip()

        # Instance case number
        case_num_elem = header.locator("strong.b-case-instance-number")
        if await case_num_elem.count() > 0:
            instance_info["case_number"] = (await case_num_elem.text_content()).strip()

        # Court name
        court_name_elem = header.locator("span.instantion-name a")
        if await court_name_elem.count() > 0:
            instance_info["court_name"] = (await court_name_elem.text_content()).strip()

        # Decision PDF link
        pdf_link_elem = header.locator("h2.b-case-result a[href*='PdfDocument']")
        if await pdf_link_elem.count() > 0:
            instance_info["decision_pdf"] = await pdf_link_elem.get_attribute("href")
            # Get decision text (clean it up)
            decision_text = await pdf_link_elem.text_content()
            instance_info["decision_text"] = " ".join(decision_text.split()).strip()

        instances.append(instance_info)
        log.info(f"  [{i + 1}] {instance_info.get('instance_type', 'N/A')}: {instance_info.get('court_name', 'N/A')}")
        log.info(
            f"      Case: {instance_info.get('case_number', 'N/A')} | Date: {instance_info.get('reg_date', 'N/A')}")
        if instance_info.get('decision_pdf'):
            log.info(f"      PDF: {instance_info.get('decision_text', 'N/A')[:60]}...")

    case_info["instances"] = instances

    return case_info


async def download_case_pdfs(page: Page, case_details: dict, download_dir: str = "./downloads") -> list[str]:
    """
    Download all PDFs from case card.

    Opens PDF in new tab, waits for WASM antibot, intercepts PDF response.

    Args:
        page: Playwright page (must be on case card page)
        case_details: Dict from navigate_to_case_card with instances
        download_dir: Base directory for downloads

    Returns:
        List of downloaded file paths
    """
    case_number = case_details.get("case_number", "unknown")

    # Sanitize case number for folder name (replace / with -)
    safe_case_number = case_number.replace("/", "-")

    # Create folder for this case
    case_dir = Path(download_dir) / safe_case_number
    case_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"📁 Download folder: {case_dir}")

    downloaded = []
    instances = case_details.get("instances", [])

    for inst in instances:
        pdf_url = inst.get("decision_pdf")
        if not pdf_url:
            continue

        # Extract filename from URL
        filename = pdf_url.split("/")[-1]
        filepath = case_dir / filename

        # Skip if already downloaded
        if filepath.exists():
            log.info(f"⏭️  Already exists: {filename}")
            downloaded.append(str(filepath))
            continue

        log.info(f"⬇️  Downloading: {filename[:60]}...")

        try:
            # Open new tab
            pdf_page = await page.context.new_page()

            # Variable to capture PDF content
            pdf_content = None

            # Set up response interceptor BEFORE navigating
            async def handle_response(response):
                nonlocal pdf_content
                # Look for PDF response
                if "Pdf" in response.url:
                    content_type = response.headers.get("content-type", "")
                    log.info(f"   📡 PDF-related response: {response.status} {content_type[:30]} {response.url[:80]}")
                    if response.status == 200 and "application/pdf" in content_type:
                        try:
                            pdf_content = await response.body()
                            log.info(f"   📦 Intercepted PDF ({len(pdf_content)} bytes)")
                        except Exception as e:
                            log.warning(f"   Failed to get body: {e}")

            pdf_page.on("response", handle_response)

            # Navigate - use domcontentloaded, not load (load may never fire for PDF)
            await pdf_page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)

            # Wait for WASM antibot check to complete
            await pdf_page.wait_for_timeout(2000)

            # Check if we're on an antibot page
            salto_div = pdf_page.locator("#salto")
            if await salto_div.count() > 0 and not pdf_content:
                log.info("   🔐 WASM antibot detected, waiting for completion...")

                # Wait for the antibot form to disappear (WASM submits it automatically)
                try:
                    await pdf_page.locator("#searchForm").wait_for(state="detached", timeout=30000)
                    log.info("   ✅ Antibot form submitted")
                except Exception as e:
                    log.warning(f"   Form didn't disappear: {e}")

                # Give time for PDF to load after form submission
                await pdf_page.wait_for_timeout(5000)

            # Check if we got PDF via interceptor
            if pdf_content and pdf_content[:4] == b'%PDF':
                filepath.write_bytes(pdf_content)
                file_size = len(pdf_content) / 1024
                log.info(f"✅ Saved (intercepted): {filename} ({file_size:.1f} KB)")
                downloaded.append(str(filepath))
            else:
                # Try to check if browser is displaying PDF
                current_url = pdf_page.url
                log.info(f"   Current URL: {current_url}")

                # Check if there's an embed/object with PDF
                embed = pdf_page.locator("embed, object, iframe").first
                if await embed.count() > 0:
                    embed_src = await embed.get_attribute("src") or await embed.get_attribute("data")
                    if embed_src:
                        log.info(f"   Found embed: {embed_src[:50]}...")

                # Take screenshot for debugging
                screenshot_path = case_dir / f"debug_{filename}.png"
                await pdf_page.screenshot(path=str(screenshot_path))
                log.info(f"   📸 Debug screenshot: {screenshot_path.name}")

                # Save HTML for analysis
                html_path = case_dir / f"debug_{filename}.html"
                html_path.write_text(await pdf_page.content())
                log.warning(f"⚠️  Could not get PDF, saved debug files")

            await pdf_page.close()

        except Exception as e:
            log.error(f"❌ Failed to download {filename}: {e}")

    log.info(f"📥 Downloaded {len(downloaded)} of {len(instances)} PDFs")
    return downloaded


async def extract_search_results(page: Page) -> list[dict]:
    """
    Extract case information from search results.

    Returns list of cases with their metadata.
    """
    results = []

    # Check if we have results
    rows = page.locator("table#b-cases tbody tr")
    count = await rows.count()

    if count == 0:
        log.warning("No results found")
        return results

    log.info(f"Found {count} result(s)")

    for i in range(count):
        row = rows.nth(i)

        try:
            # Extract case number and link
            case_link = row.locator("td.num a.num_case")
            case_number = (await case_link.text_content()).strip()
            case_url = await case_link.get_attribute("href")

            # Extract date (from the span inside civil/administrative div)
            date_elem = row.locator("td.num span").first
            case_date = (await date_elem.text_content()).strip() if await date_elem.count() > 0 else ""

            # Extract judge (div.judge with title)
            judge_elem = row.locator("td.court div.judge")
            judge = (await judge_elem.get_attribute("title")) if await judge_elem.count() > 0 else ""

            # Extract court (div with title but NOT .judge)
            court_elem = row.locator("td.court div[title]:not(.judge)")
            court = (await court_elem.get_attribute("title")) if await court_elem.count() > 0 else ""

            # Extract plaintiff - get direct text, not nested spans
            plaintiff_elem = row.locator("td.plaintiff div.b-container > div > span.js-rollover").first
            if await plaintiff_elem.count() > 0:
                # Get only the direct text content (before hidden span)
                plaintiff = await page.evaluate(
                    "(el) => el.childNodes[0]?.textContent?.trim() || ''",
                    await plaintiff_elem.element_handle()
                )
            else:
                plaintiff = ""

            # Extract defendant - same approach
            defendant_elem = row.locator("td.respondent div.b-container > div > span.js-rollover").first
            if await defendant_elem.count() > 0:
                defendant = await page.evaluate(
                    "(el) => el.childNodes[0]?.textContent?.trim() || ''",
                    await defendant_elem.element_handle()
                )
            else:
                defendant = ""

            case_info = {
                "case_number": case_number,
                "url": case_url,
                "date": case_date,
                "court": court,
                "judge": judge,
                "plaintiff": plaintiff,
                "defendant": defendant,
            }
            results.append(case_info)

            log.info(f"  [{i + 1}] {case_number} | {court} | {plaintiff[:30]}... vs {defendant[:30]}...")

        except Exception as e:
            log.error(f"Error extracting row {i}: {e}")
            continue

    return results


async def main():
    """Main entry point."""

    # Test case numbers
    test_cases = [
        "А40-57726/2024",  # Complex: 2 instances
        # "А40-185772/2022",  # Simple: 1 instance
    ]

    async with async_playwright() as p:
        # Launch browser with anti-detection arguments
        log.info("Launching browser...")
        # Try Firefox instead of Chromium (different fingerprint)
        browser: Browser = await p.firefox.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
        )

        # Create context with downloads enabled
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            accept_downloads=True,  # Enable file downloads
        )

        page = await context.new_page()

        # Apply stealth mode to avoid bot detection
        if HAS_STEALTH:
            await stealth_async(page)
            log.info("Stealth mode applied")
        else:
            log.warning("Stealth mode not available - applying basic patches...")
            # Basic stealth patches (Firefox-compatible)
            await page.add_init_script("""
                // Overwrite the 'webdriver' property
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

        # Log network requests for debugging
        async def log_request(request):
            # Log all POST requests and any request to arbitr.ru
            if request.method == "POST" or "arbitr.ru" in request.url:
                log.info(f"🌐 Request: {request.method} {request.url[:100]}")

        async def log_response(response):
            if response.request.method == "POST" or "Kad" in response.url:
                log.info(f"📥 Response: {response.status} {response.url[:100]}")

        page.on("request", log_request)
        page.on("response", log_response)

        # Log browser console messages
        page.on("console", lambda msg: log.info(f"🖥️ Console: {msg.text}") if msg.type != "warning" else None)
        page.on("pageerror", lambda err: log.error(f"❌ Page error: {err}"))

        try:
            # Navigate to the main page
            log.info(f"Navigating to {BASE_URL}")
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            log.info("Page loaded (DOM ready)")

            # Test search
            for case_number in test_cases:
                log.info(f"\n{'=' * 50}")
                log.info(f"Testing case: {case_number}")
                log.info('=' * 50)

                success = await search_by_case_number(page, case_number)

                if success:
                    results = await extract_search_results(page)
                    log.info(f"Extracted {len(results)} case(s)")

                    for r in results:
                        log.info(f"\nCase details:")
                        for k, v in r.items():
                            log.info(f"  {k}: {v}")

                        # Navigate to case card if URL available
                        if r.get("url"):
                            case_details = await navigate_to_case_card(page, r["url"])
                            if case_details:
                                log.info(f"\n=== Case Card Details ===")
                                log.info(f"  GUID: {case_details.get('guid')}")
                                log.info(f"  Status: {case_details.get('status')}")
                                log.info(f"  Instances: {len(case_details.get('instances', []))}")

                                for inst in case_details.get("instances", []):
                                    log.info(f"\n  --- Instance ---")
                                    for k, v in inst.items():
                                        log.info(f"    {k}: {v}")

                                # Download PDFs
                                if case_details.get("instances"):
                                    downloaded = await download_case_pdfs(page, case_details)
                                    log.info(f"\n📦 Total downloaded: {len(downloaded)} files")

                # Reset for next search (reload page)
                if len(test_cases) > 1:
                    await page.goto(BASE_URL, wait_until="networkidle")

            # Keep browser open for inspection
            log.info("\n" + "=" * 50)
            log.info("Done! Browser will close in 5 seconds...")
            log.info("=" * 50)
            await asyncio.sleep(5)

        except Exception as e:
            log.error(f"Error: {e}")
            raise
        finally:
            await browser.close()
            log.info("Browser closed")


if __name__ == "__main__":
    asyncio.run(main())