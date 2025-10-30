from playwright.sync_api import sync_playwright

OUTPUT = "storage_state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # headful so you can log in
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://www.facebook.com/login", wait_until="load", timeout=60000)
    print("\nLog in to Facebook in the opened window. When you're fully logged in (news feed loads), return here and press Enter.\n")
    input("Press Enter to save storage state... ")
    ctx.storage_state(path=OUTPUT)
    print(f"Saved storage state to: {OUTPUT}")
    browser.close()
