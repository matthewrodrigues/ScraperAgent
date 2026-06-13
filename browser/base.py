from playwright.async_api import async_playwright, BrowserContext
import asyncio
import os

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "browser_profile")


async def get_browser_context(marketplace: str) -> BrowserContext:
    """Returns a persistent Playwright browser context for the given marketplace."""
    profile_path = os.path.join(PROFILE_DIR, marketplace)
    os.makedirs(profile_path, exist_ok=True)

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=profile_path,
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return context
