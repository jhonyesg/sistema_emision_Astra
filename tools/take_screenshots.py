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
        await page.wait_for_function(
            "document.getElementById('ini-content').value.includes('[01_Telemedellin]')",
            timeout=5000,
        )
        await page.wait_for_timeout(400)
        # Redact the Telemedellín Original_URL (public SRT host) in-place so the
        # screenshot never shows the real domain / passphrase. The screenshot
        # script never POSTs anything back, this only edits the DOM textarea
        # for the documentation capture.
        await page.evaluate(
            """
            () => {
                const ta = document.getElementById('ini-content');
                const lines = ta.value.split('\\n');
                const idx = lines.findIndex(l => l.includes('[01_Telemedellin]'));
                if (idx === -1) return;
                // Replace only the long public-domain URL with a short placeholder
                // so it fits on a single line in the screenshot.
                lines[idx + 1] = 'Original_URL=srt://<dominio-publico-redactado>:<puerto>?passphrase=<clave-oculta>';
                ta.value = lines.join('\\n');

                const rect = ta.getBoundingClientRect();
                const cs = getComputedStyle(ta);
                const lineHeight = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) * 1.4);
                const paddingTop = parseFloat(cs.paddingTop) || 0;
                const paddingLeft = parseFloat(cs.paddingLeft) || 0;
                const paddingRight = parseFloat(cs.paddingRight) || 0;
                const urlLineIdx = idx + 1;

                const overlay = document.createElement('div');
                overlay.style.cssText = `
                    position: fixed;
                    left: ${rect.left + paddingLeft}px;
                    top:  ${rect.top  + paddingTop + urlLineIdx * lineHeight - 2}px;
                    width: ${rect.width - paddingLeft - paddingRight}px;
                    height: ${lineHeight + 4}px;
                    background: #1f1f1f;
                    border-left: 3px solid #ff5e5e;
                    color: #ffb4b4;
                    font-family: ${cs.fontFamily};
                    font-size: ${cs.fontSize};
                    line-height: ${lineHeight}px;
                    padding: 0 10px;
                    z-index: 999999;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    pointer-events: none;
                    box-sizing: border-box;
                `;
                const inner = document.createElement('span');
                inner.textContent = '\u26D4  Original_URL = srt://<dominio-p\u00fablico>:<puerto>?passphrase=**********';
                inner.style.textDecoration = 'line-through';
                inner.style.textDecorationColor = '#ff5e5e';
                inner.style.textDecorationThickness = '2px';
                inner.style.color = '#aaaaaa';
                overlay.appendChild(inner);
                const tag = document.createElement('span');
                tag.textContent = '  [OCULTO]';
                tag.style.color = '#ff5e5e';
                tag.style.fontWeight = 'bold';
                tag.style.marginLeft = '8px';
                overlay.appendChild(tag);
                document.body.appendChild(overlay);
            }
            """
        )
        await page.wait_for_timeout(150)
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
