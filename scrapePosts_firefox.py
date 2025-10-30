#!/usr/bin/env python3
# scrapePosts_firefox_recover.py — continuous scroll + auto-recover from browser crash

import os, re, csv, json, time, warnings, platform
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, InvalidSessionIdException

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

# ── CONFIG ───────────────────────────────────────────────────────────────
FB_EMAIL  = os.getenv("FB_EMAIL") or ""
FB_PASS   = os.getenv("FB_PASS") or ""
GROUP_URL = os.getenv("FB_GROUP_URL") or "https://www.facebook.com/groups/900072927547214/"

USE_MOBILE = True
HEADLESS   = False

TARGET_NEW = 3000
MAX_SCROLLS = 10000
PAUSE = 1.0
NUDGE_TRIES = 20
IDLE_LIMIT_SEC = 15.0

PROFILE_DIR = Path("./.firefox_profile")
COOKIE_PATH = Path("state/fb_cookies.json")
OUT_CSV = Path("input/group_post_urls.csv")
DONE_CSV = Path("state/done_urls.csv")

POST_PATTERNS = re.compile(r"/posts/|/permalink/|/story\.php\?story_fbid=|/photo\.php", re.I)

# ── CSV helpers ──────────────────────────────────────────────────────────
def detect_url_col(fields):
    if not fields: return None
    cleaned=[(f or "").strip().lower() for f in fields]
    for want in ("post url","post_url","url","link"):
        if want in cleaned: return fields[cleaned.index(want)]
    for i,c in enumerate(cleaned):
        if "url" in c: return fields[i]
    return fields[0]

def ensure_csv_header(path: Path, header="post_url"):
    if not path.exists() or path.stat().st_size==0:
        with path.open("w",newline="",encoding="utf-8") as f:
            csv.writer(f,lineterminator="\n").writerow([header])

def load_existing_urls(path: Path) -> set:
    if not path.exists() or path.stat().st_size==0: return set()
    urls=set()
    with path.open("r",newline="",encoding="utf-8") as f:
        reader=csv.DictReader(f)
        col=detect_url_col(reader.fieldnames or ["post_url"])
        for row in reader:
            raw=(row.get(col) or "").strip()
            if raw: urls.add(canonicalize_url(raw))
    return urls

def append_one(path: Path, url: str):
    with path.open("a",newline="",encoding="utf-8") as f:
        w=csv.writer(f,lineterminator="\n")
        w.writerow([url]); f.flush()
        try: os.fsync(f.fileno())
        except: pass

# ── URL helpers ──────────────────────────────────────────────────────────
def _strip_query_frag(u): return u.split("#",1)[0].split("?",1)[0]
def canonicalize_url(u):
    if not u: return ""
    u=u.strip()
    if u.startswith("/"): u="https://www.facebook.com"+u
    u=re.sub(r"^http://","https://",u,flags=re.I)
    u=u.replace("://m.facebook.","://www.facebook.").replace("://facebook.","://www.facebook.")
    return _strip_query_frag(u).rstrip("/")
def to_mobile(url): return url.replace("https://www.facebook.com","https://m.facebook.com")
def force_chronological(url): return f"{url}{'&' if '?' in url else '?'}sorting_setting=CHRONOLOGICAL"
def absolutize_href(h):
    if not h: return ""
    if h.startswith(("http://","https://")): return h
    if h.startswith("/"):
        host="https://m.facebook.com" if USE_MOBILE else "https://www.facebook.com"
        return host+h
    return h

# ── Firefox helpers ──────────────────────────────────────────────────────
def make_driver():
    opts=FirefoxOptions()
    if HEADLESS: opts.add_argument("-headless")
    PROFILE_DIR.mkdir(parents=True,exist_ok=True)
    opts.set_preference("profile",str(PROFILE_DIR.resolve()))
    opts.set_preference("permissions.default.image",2)
    opts.set_preference("dom.ipc.processCount",1)
    opts.set_preference("dom.webnotifications.enabled",False)
    service=FirefoxService()
    d=webdriver.Firefox(service=service,options=opts)
    d.set_window_size(1400,900)
    print("🦊 Firefox WebDriver ready.")
    return d

def recreate_driver_with_cookies():
    d=make_driver()
    wait=WebDriverWait(d,15)
    if not load_cookies(d):
        login_if_needed(d,wait)
    return d,wait

def save_cookies(d):
    try: COOKIE_PATH.write_text(json.dumps(d.get_cookies()))
    except: pass
def load_cookies(d):
    if not COOKIE_PATH.exists(): return False
    try: cookies=json.loads(COOKIE_PATH.read_text())
    except: return False
    d.get("https://www.facebook.com/")
    for c in cookies:
        c.pop("sameSite",None)
        try: d.add_cookie(c)
        except: pass
    d.refresh(); time.sleep(1.5)
    return True

# ── JS ───────────────────────────────────────────────────────────────────
JS_START_FEED_OBSERVER = r"""
if(!window.__feedObs){
  window.__feedAdded=0;
  window.__articleCount=document.querySelectorAll("div[role='article'],article").length;
  window.__feedObs=new MutationObserver(muts=>{
    let add=0;
    for(const m of muts){
      for(const n of m.addedNodes){
        if(n && n.nodeType===1){
          if(n.matches&&n.matches("div[role='article'],article")) add++;
          else if(n.querySelector&&n.querySelector("div[role='article'],article")) add++;
        }
      }
    }
    if(add>0){
      window.__feedAdded+=add;
      window.__articleCount=document.querySelectorAll("div[role='article'],article").length;
    }
  });
  window.__feedObs.observe(document.body,{childList:true,subtree:true});
}
return {count:window.__articleCount,added:window.__feedAdded};
"""
JS_GET_FEED_COUNTS="return {count:(window.__articleCount||document.querySelectorAll('div[role=\"article\"],article').length),added:(window.__feedAdded||0)};"
JS_RESET_ADDED="window.__feedAdded=0;return true;"
JS_SNAPSHOT_HREFS=r"""
const out=new Set();
const add=h=>{if(!h)return;out.add(h.split('#')[0].split('?')[0]);};
(document.querySelectorAll("div[role='article'],article")||[]).forEach(r=>r.querySelectorAll("a[href]").forEach(a=>add(a.getAttribute("href"))));
return Array.from(out);
"""

def js(d,code,*args):
    try: return d.execute_script(code,*args)
    except (WebDriverException,InvalidSessionIdException): return None

# ── Login helpers ────────────────────────────────────────────────────────
def dismiss_cookie_banners(d):
    try:
        for xp in [
            "//button//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'accept all')]",
            "//button//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'allow all')]"
        ]:
            for b in d.find_elements(By.XPATH,xp):
                try: b.click(); time.sleep(0.3)
                except: pass
    except: pass

def login_if_needed(d,wait):
    d.get("https://www.facebook.com/"); time.sleep(2)
    dismiss_cookie_banners(d)
    try:
        email=wait.until(EC.presence_of_element_located((By.ID,"email")))
        pwd=d.find_element(By.ID,"pass")
        if not FB_EMAIL or not FB_PASS:
            print("ℹ️ Skipping explicit login — likely already logged in.")
            return
        email.clear(); email.send_keys(FB_EMAIL)
        pwd.clear(); pwd.send_keys(FB_PASS); pwd.send_keys(Keys.ENTER)
        wait.until(lambda dd: "login" not in dd.current_url.lower())
        print("✅ Logged in.")
    except Exception:
        print("ℹ️ Skipping explicit login — likely already logged in.")

# ── Collection ───────────────────────────────────────────────────────────
def collect(driver,wait):
    start_url=force_chronological(to_mobile(GROUP_URL) if USE_MOBILE else GROUP_URL)
    driver.get(start_url); time.sleep(3)
    js(driver,JS_START_FEED_OBSERVER)
    baseline={canonicalize_url(absolutize_href(h)) for h in (js(driver,JS_SNAPSHOT_HREFS) or []) if h and POST_PATTERNS.search(h)}
    ensure_csv_header(OUT_CSV,"post_url"); ensure_csv_header(DONE_CSV,"url")
    existing=load_existing_urls(OUT_CSV); done=load_existing_urls(DONE_CSV)
    seen=set(existing)|set(done)|baseline
    print(f"📸 Baseline={len(baseline)}, Resume={len(existing)} URLs")

    new_count=0
    for i in range(MAX_SCROLLS):
        try:
            hrefs=js(driver,JS_SNAPSHOT_HREFS) or []
            found={canonicalize_url(absolutize_href(h)) for h in hrefs if h and POST_PATTERNS.search(h)}
            delta=found-seen
            if delta:
                for u in sorted(delta):
                    append_one(OUT_CSV,u)
                    seen.add(u); new_count+=1
                print(f"➕ Added {len(delta)} (total={new_count}/{TARGET_NEW})")
            if TARGET_NEW and new_count>=TARGET_NEW:
                print("✅ Target reached."); break

            driver.execute_script("window.scrollTo(0,document.body.scrollHeight);")
            time.sleep(PAUSE)
            counts=js(driver,JS_GET_FEED_COUNTS) or {}
            if counts.get("added",0)==0 and not delta:
                for j in range(NUDGE_TRIES):
                    driver.execute_script(f"window.scrollBy(0,{1000+(j*200)});")
                    time.sleep(0.5)
                    counts=js(driver,JS_GET_FEED_COUNTS) or {}
                    if counts.get("added",0)>0: break
                else:
                    print("🏁 Probably end of feed."); break
            if i%25==0: print(f"📜 Scroll {i+1}/{MAX_SCROLLS}")
        except (InvalidSessionIdException,WebDriverException):
            print("💥 Browser crash — restarting Firefox...")
            try: driver.quit()
            except: pass
            driver,wait=recreate_driver_with_cookies()
            driver.get(start_url); time.sleep(2)
            js(driver,JS_START_FEED_OBSERVER)
            continue
    print("✅ Done.")

# ── Main ─────────────────────────────────────────────────────────────────
def main():
    d=make_driver()
    w=WebDriverWait(d,15)
    try:
        if not load_cookies(d):
            print("ℹ️ No cookies found — will log in.")
        login_if_needed(d,w)
        save_cookies(d)
        collect(d,w)
    finally:
        try: save_cookies(d)
        except: pass
        try: d.quit()
        except: pass

if __name__=="__main__":
    main()
