#!/usr/bin/env python3
# scrapePosts.py — warm-up first (no collecting), THEN collect NEW unique post URLs
# - Collects ONLY URLs not in group_post_urls.csv NOR done_urls.csv
# - Ignores any URLs visible right after warm-up (baseline snapshot)
# - Stops after collecting TARGET_NEW new URLs (default 3000)
# - Appends each URL immediately (open → write → flush → fsync → close per URL)

import os, re, csv, json, time, warnings, subprocess, tempfile, shutil, platform
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

# ── CONFIG ────────────────────────────────────────────────────────────────────
FB_EMAIL      = os.getenv("FB_EMAIL") or ""
FB_PASS       = os.getenv("FB_PASS") or ""
GROUP_URL     = os.getenv("FB_GROUP_URL") or "https://www.facebook.com/groups/900072927547214/"

USE_MOBILE    = True
HEADLESS      = False

TARGET_NEW         = 5000       # collect this many NEW (unique) URLs
PRE_SCROLL_ROUNDS  = 0          # keep 0; long warmups blow memory

MAX_SCROLLS        = 6000
PAUSE              = 1.0
STALL_LIMIT        = 8
IDLE_LIMIT_SEC     = 18.0
NUDGE_TRIES        = 16

USE_CLEAN_PROFILE = True

PROFILE_DIR   = Path("./.chrome_profile")
COOKIE_PATH   = Path("state/fb_cookies.json")
OUT_CSV       = Path("input/group_post_urls.csv")
DONE_CSV      = Path("state/done_urls.csv")

# 🔒 Chrome version pin (soft): warn if not 131.x
TARGET_CHROME_MAJOR = os.getenv("CHROME_VERSION_PIN", "131").strip()

POST_PATTERNS = re.compile(r"/posts/|/permalink/|/story\.php\?story_fbid=|/photo\.php", re.I)

# 🧯 Memory safety knobs
PRUNE_KEEP_LAST = 160                # keep only last N articles in DOM
MAX_ARTICLES_BEFORE_RELOAD = 900     # if DOM grows this big, soft reload
SOFT_RELOAD_EVERY_SCROLLS = 1000      # periodic soft reload cadence

# ── URL normalization ─────────────────────────────────────────────────────────
def _strip_query_frag(u: str) -> str:
    u = u.split("#", 1)[0]
    u = u.split("?", 1)[0]
    return u

def canonicalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("/"):
        u = "https://www.facebook.com" + u
    u = re.sub(r"^http://", "https://", u, flags=re.I)
    u = u.replace("://m.facebook.", "://www.facebook.").replace("://facebook.", "://www.facebook.")
    u = _strip_query_frag(u)
    u = u.rstrip("/")
    return u

def to_mobile(url: str) -> str:
    return (url.replace("https://www.facebook.com", "https://m.facebook.com")
               .replace("http://www.facebook.com", "https://m.facebook.com"))

def force_chronological(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sorting_setting=CHRONOLOGICAL"

def absolutize_href(href: str) -> str:
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/"):
        host = "https://m.facebook.com" if USE_MOBILE else "https://www.facebook.com"
        return host + href
    return href

# ── CSV helpers ───────────────────────────────────────────────────────────────
def detect_url_col(fieldnames):
    if not fieldnames:
        return None
    cleaned = [ (fn or "").strip().lower() for fn in fieldnames ]
    for want in ("post url", "post_url", "url", "link"):
        if want in cleaned:
            return fieldnames[cleaned.index(want)]
    for i, c in enumerate(cleaned):
        if "url" in c:
            return fieldnames[i]
    return fieldnames[0]

def ensure_csv_header(csv_path: Path, header="post_url"):
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerow([header])

def load_existing_urls(csv_path: Path) -> set:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return set()
    urls = set()
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        col = detect_url_col(reader.fieldnames or ["post_url"])
        for row in reader:
            raw = (row.get(col) or "").strip()
            if raw:
                urls.add(canonicalize_url(raw))
    return urls

def append_one(csv_path: Path, url_canonical: str):
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow([url_canonical])
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

# ── Browser helpers ──────────────────────────────────────────────────────────
def browser_version(binary_path: str) -> str:
    try:
        out = subprocess.check_output([binary_path, "--version"], text=True).strip()
        m = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", out)
        return m.group(1) if m else out
    except Exception:
        return "?"

def find_browser_binary() -> str:
    env_bin = os.getenv("CHROME_BINARY")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    candidates = [
        "/Applications/Google Chrome 131.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        str(Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise RuntimeError("❌ No Chrome/Chromium found. Set CHROME_BINARY to a v131 binary.")

def warn_if_wrong_major(ver: str, want_major: str):
    m = re.match(r"(\d+)\.", ver or "")
    have = m.group(1) if m else "?"
    if have != want_major:
        print(f"⚠️  Chrome version is {ver} (major {have}), not {want_major}. "
              f"Selenium Manager will still fetch a matching driver for {ver}. "
              f"To force {want_major}, set CHROME_BINARY to a v{want_major} install.")

_tmp_profile_dir: Optional[Path] = None

def make_driver():
    global _tmp_profile_dir
    arch = platform.machine().lower()
    is_arm = arch in ("arm64", "aarch64")
    is_intel = ("x86_64" in arch) or ("i386" in arch)

    chrome_bin = os.getenv("CHROME_BINARY", "").strip()
    driver_path = os.getenv("CHROMEDRIVER", "").strip()
    here = Path(__file__).resolve().parent

    if not chrome_bin or not os.path.exists(chrome_bin):
        pat = (
            "chrome/mac_arm-131.*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
            if is_arm else
            "chrome/mac-131.*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        )
        matches = list(here.glob(pat))
        if matches:
            chrome_bin = str(matches[0])
        else:
            for p in [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                str(Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]:
                if os.path.exists(p):
                    chrome_bin = p
                    break

    if not driver_path or not os.path.exists(driver_path):
        dpat = (
            "chromedriver/mac_arm-131.*/chromedriver-mac-*/chromedriver"
            if is_arm else
            "chromedriver/mac-131.*/chromedriver-mac-*/chromedriver"
        )
        dmatches = list(here.glob(dpat))
        driver_path = str(dmatches[0]) if dmatches else ""

    if not chrome_bin:
        raise RuntimeError("No Chrome binary found for your arch. Set CHROME_BINARY.")

    if is_intel and "mac-arm" in chrome_bin:
        raise RuntimeError("Intel Mac but CHROME_BINARY points to arm64 build.")
    if is_arm and "mac-x64" in chrome_bin:
        raise RuntimeError("Apple silicon but CHROME_BINARY points to x64 build.")

    if driver_path and os.path.exists(driver_path):
        try: os.chmod(driver_path, 0o755)
        except: pass

    ver = browser_version(chrome_bin)
    print(f"🔎 Browser detected: {chrome_bin} (version {ver or '?'})")
    print(f"🧩 Chromedriver: {driver_path if driver_path else 'Selenium Manager (auto)'}")

    opts = Options()
    opts.binary_location = chrome_bin
    if HEADLESS:
        opts.add_argument("--headless=new")

    # Stability / memory flags
    opts.add_argument("--start-maximized")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--no-zygote")
    opts.add_argument("--renderer-process-limit=1")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_argument("--use-angle=metal")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--force-color-profile=srgb")

    # profile
    if USE_CLEAN_PROFILE:
        tmp_dir = Path(tempfile.mkdtemp(prefix="cft_profile_"))
        _tmp_profile_dir = tmp_dir
        opts.add_argument(f"--user-data-dir={tmp_dir}")
    else:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        opts.add_argument(f"--user-data-dir={PROFILE_DIR.resolve()}")
        opts.add_argument("--profile-directory=Default")

    service = Service(executable_path=driver_path) if driver_path else Service()
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_window_size(1400, 900)
    return driver

def cleanup_temp_profile():
    global _tmp_profile_dir
    if _tmp_profile_dir and _tmp_profile_dir.exists():
        try:
            shutil.rmtree(_tmp_profile_dir, ignore_errors=True)
        finally:
            _tmp_profile_dir = None

# ── Cookies ───────────────────────────────────────────────────────────────────
def save_cookies(driver):
    try: COOKIE_PATH.write_text(json.dumps(driver.get_cookies()))
    except: pass

def load_cookies(driver):
    if not COOKIE_PATH.exists(): return False
    try: cookies = json.loads(COOKIE_PATH.read_text())
    except: return False
    driver.get("https://www.facebook.com/")
    for c in cookies:
        c.pop("sameSite", None)
        try: driver.add_cookie(c)
        except: pass
    driver.refresh(); time.sleep(1.5)
    return True

# ── JS helpers ────────────────────────────────────────────────────────────────
JS_START_FEED_OBSERVER = r"""
if (!window.__feedObs) {
  window.__feedAdded = 0;
  window.__articleCount = document.querySelectorAll("div[role='article'], article").length;
  window.__feedObs = new MutationObserver((muts) => {
    let added = 0;
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n && n.nodeType === 1) {
          if (n.matches && n.matches("div[role='article'], article")) added++;
          else if (n.querySelector && n.querySelector("div[role='article'], article")) added++;
        }
      }
    }
    if (added > 0) {
      window.__feedAdded += added;
      window.__articleCount = document.querySelectorAll("div[role='article'], article").length;
    }
  });
  window.__feedObs.observe(document.body, { childList: true, subtree: true });
}
return {count: window.__articleCount, added: window.__feedAdded};
"""

JS_GET_FEED_COUNTS = r"""
return {
  count: (window.__articleCount || document.querySelectorAll("div[role='article'], article").length),
  added: (window.__feedAdded || 0)
};
"""

JS_RESET_ADDED = r"window.__feedAdded = 0; return true;"

JS_SNAPSHOT_HREFS = r"""
const out = new Set();
const add = (h) => { if (!h) return; out.add(h.split('#')[0].split('?')[0]); };
const roots = document.querySelectorAll("div[role='article'], article");
if (roots.length) {
  roots.forEach(r => r.querySelectorAll("a[href]").forEach(a => add(a.getAttribute("href"))));
} else {
  document.querySelectorAll("a[href]").forEach(a => add(a.getAttribute("href")));
}
return Array.from(out);
"""

JS_GET_ARTICLE_COUNT = r"""
return (window.__articleCount || document.querySelectorAll("div[role='article'], article").length) || 0;
"""

# keep only the newest N article nodes (argument[0])
JS_PRUNE_OLD_ARTICLES = r"""
var keepLast = arguments[0] >>> 0;
var nodes = Array.from(document.querySelectorAll("div[role='article'], article"));
var removeCount = Math.max(0, nodes.length - keepLast);
for (var i = 0; i < removeCount; i++) {
  try { nodes[i].remove(); } catch(e) {}
}
window.__articleCount = document.querySelectorAll("div[role='article'], article").length;
return window.__articleCount;
"""

# ── Safe JS exec with crash recovery ──────────────────────────────────────────
def js(driver, script, *args, allow_crash_recover=False, current_url=None):
    try:
        return driver.execute_script(script, *args)
    except WebDriverException as e:
        msg = str(e).lower()
        if allow_crash_recover and ("tab crashed" in msg or "session deleted" in msg or "invalid session id" in msg):
            # bubble up so caller can fully recreate driver
            raise
        raise

# ── Gates & misc ──────────────────────────────────────────────────────────────
def dismiss_cookie_banners(driver):
    try:
        for xp in [
            "//button//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'accept all')]",
            "//button//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'allow all')]",
            "//div[@role='dialog']//button[.//span[contains(.,'Accept') or contains(.,'Allow')]]"
        ]:
            for b in driver.find_elements(By.XPATH, xp):
                try: b.click(); time.sleep(0.5)
                except: pass
    except: pass

def login_if_needed(driver, wait):
    driver.get("https://www.facebook.com/"); time.sleep(2)
    dismiss_cookie_banners(driver)
    try:
        email = wait.until(EC.presence_of_element_located((By.ID, "email")))
        pwd   = driver.find_element(By.ID, "pass")
        if not FB_EMAIL or not FB_PASS:
            print("ℹ️ Skipping explicit login — likely already logged in.")
            return
        email.clear(); email.send_keys(FB_EMAIL)
        pwd.clear();   pwd.send_keys(FB_PASS); pwd.send_keys(Keys.ENTER)
        wait.until(lambda d: "login" not in d.current_url.lower())
        time.sleep(2)
        print("✅ Logged in (fresh).")
    except Exception:
        print("ℹ️ Skipping explicit login — likely already logged in.")

# ── Warm-up (NO collecting) ───────────────────────────────────────────────────
def warmup_scrolls(driver, wait, start_url: str, rounds: int):
    driver.get(start_url)
    time.sleep(3)
    dismiss_cookie_banners(driver)
    js(driver, JS_START_FEED_OBSERVER)
    print(f"\n⏳ Warm-up: {rounds} scroll passes (no collecting)…\n")
    for _ in range(rounds):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        start_wait = time.time()
        saw_new = False
        while time.time() - start_wait < IDLE_LIMIT_SEC:
            time.sleep(0.5)
            counts = js(driver, JS_GET_FEED_COUNTS) or {}
            if counts.get("added", 0) > 0:
                saw_new = True
                js(driver, JS_RESET_ADDED)
                break
        if not saw_new:
            for _ in range(NUDGE_TRIES):
                driver.execute_script("window.scrollBy(0, 1200);")
                time.sleep(0.6)
                counts = js(driver, JS_GET_FEED_COUNTS) or {}
                if counts.get("added", 0) > 0:
                    js(driver, JS_RESET_ADDED)
                    break
        time.sleep(PAUSE)

# ── Driver (re)creation helpers ───────────────────────────────────────────────
def recreate_driver_with_cookies() -> webdriver.Chrome:
    driver = make_driver()
    wait = WebDriverWait(driver, 15)
    if not load_cookies(driver):
        login_if_needed(driver, wait)
    return driver

def soft_reload(driver, start_url: str):
    try:
        driver.get(start_url)
        time.sleep(2.0)
        js(driver, JS_START_FEED_OBSERVER)
        js(driver, JS_RESET_ADDED)
    except Exception:
        pass

# ── Collection phase (AFTER warm-up) ──────────────────────────────────────────
def collect_after_warmup(driver, wait, group_url: str, target_new: int, max_scrolls: int, pause: float):
    start_url = force_chronological(to_mobile(group_url) if USE_MOBILE else group_url)

    warmup_scrolls(driver, wait, start_url, PRE_SCROLL_ROUNDS)

    raw_baseline = js(driver, JS_SNAPSHOT_HREFS) or []
    baseline = {
        canonicalize_url(absolutize_href(h))
        for h in raw_baseline
        if h and POST_PATTERNS.search(h)
    }
    print(f"\n📸 Baseline after warm-up: {len(baseline)} URLs (will be ignored).\n")

    ensure_csv_header(OUT_CSV, header="post_url")
    ensure_csv_header(DONE_CSV, header="url")
    existing = load_existing_urls(OUT_CSV)
    done     = load_existing_urls(DONE_CSV)
    seen     = set(existing) | set(done) | set(baseline)

    print(f"🧾 Resume state: {len(existing)} in {OUT_CSV.name}, {len(done)} in {DONE_CSV.name}")
    print("⚙️  Duplicate-skip mode uses canonical URLs (m.→www, no query, no trailing slash).\n")

    js(driver, JS_START_FEED_OBSERVER)

    newly_added = 0
    stalls = 0
    last_soft_reload = -SOFT_RELOAD_EVERY_SCROLLS  # force check on first loop

    def collect_now() -> int:
        nonlocal newly_added
        hrefs = js(driver, JS_SNAPSHOT_HREFS) or []
        discovered = {
            canonicalize_url(absolutize_href(h))
            for h in hrefs
            if h and POST_PATTERNS.search(h)
        }
        delta = discovered - seen
        if delta:
            for u in sorted(delta):
                append_one(OUT_CSV, u)
                seen.add(u)
                newly_added += 1
            print(f"➕ Added {len(delta)} new unique URLs (this run new={newly_added}/{target_new})")
        return len(delta)

    i = 0
    while i < max_scrolls:
        # Proactive reload and DOM pruning to avoid OOM
        try:
            article_count = js(driver, JS_GET_ARTICLE_COUNT) or 0
        except WebDriverException:
            article_count = 0

        if (i - last_soft_reload) >= SOFT_RELOAD_EVERY_SCROLLS or article_count >= MAX_ARTICLES_BEFORE_RELOAD:
            print(f"♻️  Soft reload at scroll {i+1} (articles≈{article_count})")
            soft_reload(driver, start_url)
            last_soft_reload = i

        # prune old nodes (keep only last PRUNE_KEEP_LAST)
        try:
            js(driver, JS_PRUNE_OLD_ARTICLES, PRUNE_KEEP_LAST)
        except WebDriverException:
            pass

        try:
            added = collect_now()
        except WebDriverException as e:
            print(f"🧯 Driver error (collect_now): {e.__class__.__name__} — hard restart")
            try:
                driver.quit()
            except Exception:
                pass
            driver = recreate_driver_with_cookies()
            wait = WebDriverWait(driver, 15)
            driver.get(start_url); time.sleep(2)
            js(driver, JS_START_FEED_OBSERVER)
            last_soft_reload = i
            added = collect_now()

        print(f"\n📜 Collect scroll {i+1}/{max_scrolls}")

        if target_new and newly_added >= target_new:
            print(f"✅ Reached target {target_new} NEW URLs.")
            break

        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        except WebDriverException:
            print("💥 Scroll failed — hard restart")
            try:
                driver.quit()
            except Exception:
                pass
            driver = recreate_driver_with_cookies()
            wait = WebDriverWait(driver, 15)
            driver.get(start_url); time.sleep(2)
            js(driver, JS_START_FEED_OBSERVER)

        start_wait = time.time()
        saw_new = False
        while time.time() - start_wait < IDLE_LIMIT_SEC:
            time.sleep(0.5)
            try:
                counts = js(driver, JS_GET_FEED_COUNTS) or {}
            except WebDriverException:
                counts = {}
            if counts.get("added", 0) > 0:
                saw_new = True
                try: js(driver, JS_RESET_ADDED)
                except WebDriverException: pass
                break

        if not saw_new:
            for _ in range(NUDGE_TRIES):
                try:
                    driver.execute_script("window.scrollBy(0, 1500);")
                except WebDriverException:
                    print("💥 Nudge failed — soft reload")
                    soft_reload(driver, start_url)
                    break
                time.sleep(0.6)
                try:
                    counts = js(driver, JS_GET_FEED_COUNTS) or {}
                except WebDriverException:
                    counts = {}
                if counts.get("added", 0) > 0:
                    try: js(driver, JS_RESET_ADDED)
                    except WebDriverException: pass
                    break

        if added == 0 and not saw_new:
            stalls += 1
            print(f"⛳️ No growth (stall {stalls}/{STALL_LIMIT}).")
            if stalls >= STALL_LIMIT:
                print("🏁 End of feed (or too slow to load more).")
                break
        else:
            stalls = 0

        i += 1
        time.sleep(pause)

    print(f"\n✅ Finished. NEW URLs appended to {OUT_CSV.name} (skipping those in {DONE_CSV.name}).\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    driver = make_driver()
    wait = WebDriverWait(driver, 15)
    try:
        if not load_cookies(driver):
            print("ℹ️ No cookies file — will try explicit login.")
        login_if_needed(driver, wait)
        save_cookies(driver)

        start_url = force_chronological(to_mobile(GROUP_URL) if USE_MOBILE else GROUP_URL)
        collect_after_warmup(
            driver, wait,
            group_url=GROUP_URL,
            target_new=TARGET_NEW,
            max_scrolls=MAX_SCROLLS,
            pause=PAUSE
        )
    finally:
        try: save_cookies(driver)
        except: pass
        try: driver.quit()
        except: pass
        cleanup_temp_profile()

if __name__ == "__main__":
    main()
