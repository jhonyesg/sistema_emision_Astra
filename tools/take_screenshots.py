"""Generate dashboard screenshots for the README using a headless browser."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:5006"
OUT = Path(__file__).parent / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)


async def shoot(page, name: str, full: bool = False):
    target = OUT / name
    await page.screenshot(path=str(target), full_page=full)
    print(f"[ok] {target} ({target.stat().st_size // 1024} KB)")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        page = await ctx.new_page()

        page.on("pageerror", lambda exc: print(f"[pageerror] {exc}"))
        page.on("console", lambda msg: print(f"[console:{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)

        await page.goto(BASE + "/", wait_until="networkidle")
        # wait for the table to populate
        await page.wait_for_selector("#streams-tbody tr", timeout=15000)
        await page.wait_for_timeout(3500)

        await shoot(page, "dashboard-overview.png", full=True)

        # Header controls close-up
        header = page.locator(".controls-header")
        await header.scroll_into_view_if_needed()
        await shoot_clip(page, header, "dashboard-header.png")

        # Toolbar with toggles
        await shoot_clip(page, page.locator(".toolbar"), "dashboard-toolbar.png")

        # Status bar
        await shoot_clip(page, page.locator(".status-bar"), "dashboard-statusbar.png")

        # Streams table only
        await shoot_clip(page, page.locator(".table-container"), "dashboard-table.png")

        # Details modal: Logs tab
        await page.locator("#streams-tbody tr .cell-actions .btn-info").first.click()
        await page.wait_for_selector("#details-modal", state="visible", timeout=5000)
        await page.wait_for_timeout(1500)
        await shoot_clip(page, page.locator("#details-modal .modal-content"), "modal-details-logs.png")
        await page.close()

        page = await ctx.new_page()
        await page.goto(BASE + "/", wait_until="networkidle")
        await page.wait_for_selector("#streams-tbody tr", timeout=15000)
        await page.wait_for_timeout(2500)

        # Details modal: Comando tab
        await page.locator("#streams-tbody tr .cell-actions .btn-info").first.click()
        await page.wait_for_selector("#details-modal", state="visible", timeout=5000)
        await page.locator(".tab-btn", has_text="Comando").click()
        await page.wait_for_timeout(500)
        await shoot_clip(page, page.locator("#details-modal .modal-content"), "modal-details-command.png")
        await page.close()

        page = await ctx.new_page()
        await page.goto(BASE + "/", wait_until="networkidle")
        await page.wait_for_selector("#streams-tbody tr", timeout=15000)
        await page.wait_for_timeout(2500)

        # Errors modal
        await page.locator("button", has_text="Errores").click()
        await page.wait_for_selector("#errors-modal", timeout=5000)
        await page.wait_for_timeout(500)
        await shoot_clip(page, page.locator("#errors-modal .modal-content"), "modal-errors.png")
        await page.close()

        page = await ctx.new_page()
        await page.goto(BASE + "/", wait_until="networkidle")
        await page.wait_for_selector("#streams-tbody tr", timeout=15000)
        await page.wait_for_timeout(2500)

        # INI modal
        await page.locator("button", has_text="INI").click()
        await page.wait_for_selector("#ini-modal", timeout=5000)
        await page.wait_for_timeout(500)
        await shoot_clip(page, page.locator("#ini-modal .modal-content"), "modal-ini.png")
        await page.close()

        await browser.close()


async def shoot_clip(page, locator, name: str):
    target = OUT / name
    await locator.scroll_into_view_if_needed()
    await locator.screenshot(path=str(target))
    print(f"[ok] {target} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())
