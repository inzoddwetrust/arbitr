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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# === Configuration ===
BASE_URL = "https://kad.arbitr.ru/"
HEADLESS = True  # Must be True in this environment
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
    await page.wait_for_timeout(2000)

    # Force load WASM module early (it's an anti-bot protection)
    log.info("Pre-loading WASM module...")
    await page.evaluate("""
        // Trigger WASM loading by simulating user activity
        const wasmScript = document.createElement('script');
        wasmScript.src = 'https://kad.arbitr.ru/Wasm/api/v1/wasm.js?_=' + Date.now();
        document.head.appendChild(wasmScript);
    """)

    # Wait for WASM to actually load
    try:
        await page.wait_for_response(
            lambda response: "wasm_bg.wasm" in response.url,
            timeout=10000
        )
        log.info("WASM binary loaded")
        await page.wait_for_timeout(2000)  # Give WASM time to initialize
    except:
        log.warning("WASM did not load, trying without it...")

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

            # Extract date
            date_elem = row.locator("td.num div.civil span, td.num div.administrative span")
            case_date = (await date_elem.text_content()).strip() if await date_elem.count() > 0 else ""

            # Extract court and judge
            court_cell = row.locator("td.court")
            judge = (await court_cell.locator("div.judge").text_content()).strip()
            court = (await court_cell.locator("div:not(.judge)").text_content()).strip()

            # Extract plaintiff
            plaintiff_cell = row.locator("td.plaintiff")
            plaintiff = (await plaintiff_cell.locator("span.js-rollover").first.text_content()).strip()

            # Extract defendant
            defendant_cell = row.locator("td.respondent")
            defendant = (await defendant_cell.locator("span.js-rollover").first.text_content()).strip()

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
        "А40-185772/2022",  # Simple: 1 instance
        # "А40-57726/2024",   # Complex: 2 instances (uncomment to test)
    ]

    async with async_playwright() as p:
        # Launch browser
        log.info("Launching browser...")
        browser: Browser = await p.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
        )

        # Create context with stealth-like settings
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ru-RU",
        )

        page = await context.new_page()

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