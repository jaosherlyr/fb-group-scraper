#!/usr/bin/env python3
# postComments.py
# Usage: python postComments.py "https://www.facebook.com/groups/.../posts/..." [max_comments]
#
# Output (printed after the marker line "—— RESULT ——"):
# {
#   "url": "...",
#   "scraped_total": M,
#   "comments": [ { "commenter": "...", "text": "..." }, ... ]
# }

import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse
from playwright.sync_api import sync_playwright, Browser, TimeoutError as PWTimeout

# ---------------- Config / Regex (same scraping logic) ----------------
RE_SORT_BTN = re.compile(r"^(All comments|Most relevant|Newest|Top comments)$", re.I)

RE_VIEW_REPLIES   = re.compile(r"(view|see)\s+(all\s+)?\d+\s+(more\s+)?repl", re.I)
RE_VIEW_COMMENTS  = re.compile(r"(view|see)\s+(all\s+)?\d+\s+(more\s+|previous\s+)?comment", re.I)
RE_GENERIC_MORE   = re.compile(r"^more (repl|comment)s?$", re.I)
RE_SEE_MORE_TRUNC = re.compile(r"^see more(\s*\.\.\.)?$", re.I)
RE_CATCH_ANY      = re.compile(r"(view|see|more).*(repl|comment)", re.I)

PRIMARY_COMMENT_WRAPPER = "div.xwib8y2.xpdmqnj.x1g0dm76.x1y1aw1k"  # common desktop row wrapper

COMMENT_BLOCK_FALLBACKS = [
    "div[role='article'][aria-label^='Comment']",
    "article[aria-label^='Comment']",
    "div[aria-label^='Comment']",
    # mobile-ish / legacy fallbacks (helpful on share links & m.facebook.com):
    'div[role="article"][data-ad-preview="comment"]',
    'article[role="article"][data-ad-preview="comment"]',
    # very loose: last resort
    "div[role='article'] div[dir='auto'] div[role='article']",
]

# ---------------- Helpers (same) ----------------
def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def get_inner_text_with_emojis(node):
    """inner_text + include any <img alt='...'> emoji alts that FB uses."""
    try:
        raw = node.inner_text(timeout=0) or ""
    except Exception:
        raw = ""
    try:
        imgs = node.locator("img[alt]")
        n = imgs.count()
        for i in range(n):
            alt = (imgs.nth(i).get_attribute("alt") or "").strip()
            if alt and alt not in raw:
                raw = (raw + " " + alt).strip()
    except Exception:
        pass
    return raw

def text_content(node):
    """Prefer DOM textContent; fallback to inner_text."""
    try:
        t = node.evaluate("el => (el.textContent || '').trim()")
        if t:
            return clean_ws(t)
    except Exception:
        pass
    try:
        t = node.inner_text(timeout=0) or ""
        return clean_ws(t)
    except Exception:
        return ""

def launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-dev-shm-usage",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
        ],
    )

def _storage_state_if_any() -> Optional[str]:
    path = "storage_state.json"
    return path if os.path.exists(path) else None

def new_desktop_context(browser):
    return browser.new_context(
        storage_state=_storage_state_if_any(),
        java_script_enabled=True,
        viewport={"width": 1366, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )

def to_mobile_url(url: str) -> str:
    try:
        pu = urlparse(url)
        host = pu.netloc or "www.facebook.com"
        if host.startswith("www."):
            host = host.replace("www.", "m.", 1)
        elif host and not host.startswith("m."):
            host = "m." + host
        return urlunparse((pu.scheme or "https", host, pu.path, pu.params, pu.query, pu.fragment))
    except Exception:
        return url

def extract_post_id(url: str) -> Optional[str]:
    # Expect /posts/<digits>
    m = re.search(r"/posts/(\d+)", url)
    return m.group(1) if m else None

# ---------------- Sorting / Expanders (same) ----------------
def detect_sort_label(page):
    try:
        hint = page.locator("[aria-label*='sorted by']").first
        if hint and hint.count() > 0:
            al = (hint.get_attribute("aria-label") or "").strip()
            m = re.search(r"sorted by\s+(.+)$", al, flags=re.I)
            if m:
                label = m.group(1).strip()
                return (label, ("all" in label.lower()))
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name=RE_SORT_BTN).first
        if btn and btn.count() > 0:
            txt = clean_ws(btn.inner_text() or "")
            if not txt:
                txt = clean_ws(btn.get_attribute("aria-label") or "")
            if txt:
                return (txt, ("all" in txt.lower()))
    except Exception:
        pass
    try:
        nodes = page.locator("div[role='button'], a[role='button'], span, div")
        n = min(nodes.count(), 500)
        for i in range(n):
            t = clean_ws(nodes.nth(i).inner_text() or "")
            if t and RE_SORT_BTN.search(t):
                label = RE_SORT_BTN.search(t).group(1)
                return (label, ("all" in label.lower()))
    except Exception:
        pass
    return (None, None)

def try_force_all_comments(page) -> bool:
    forced = False
    try:
        sort_btn = page.get_by_role("button", name=RE_SORT_BTN).first
        if sort_btn and sort_btn.count() > 0 and sort_btn.is_visible():
            sort_btn.click(timeout=1500)
            time.sleep(0.2)
            picked = False
            for loc in [
                page.get_by_role("menuitem", name=re.compile(r"^All comments$", re.I)),
                page.locator("div[role='menuitem']:has-text('All comments')"),
                page.locator("div[role='menuitemcheckbox']:has-text('All comments')"),
                page.locator("div[role='option']:has-text('All comments')"),
                page.get_by_text(re.compile(r"^All comments$", re.I)),
            ]:
                if loc and loc.count() > 0:
                    try:
                        loc.first.click(timeout=1500)
                        picked = True
                        break
                    except Exception:
                        continue
            if not picked:
                txt = clean_ws(sort_btn.inner_text() or sort_btn.get_attribute("aria-label") or "")
                if "all comments" in txt.lower():
                    picked = True
            forced = picked
    except Exception:
        pass
    return forced

def js_click_expanders(page) -> int:
    return page.evaluate(
        """(patterns) => {
          const [reReplies, reComments, reGeneric, reSeeMore, reAny] =
            patterns.map(s => new RegExp(s, 'i'));
          const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') return false;
            const rect = el.getBoundingClientRect();
            return !!(el.offsetParent !== null || rect.width || rect.height);
          };
          const clickable = (el) => el.closest('div[role="button"], a[role="button"], button') || el;
          const txtOf = (el) => (el.innerText || '').trim();
          const ariaOf = (el) => (el.getAttribute && el.getAttribute('aria-label') || '').trim();

          const nodes = Array.from(document.querySelectorAll('div[role="button"], a[role="button"], button, span, div'));
          let clicks = 0;
          for (const el of nodes) {
            const txt = txtOf(el);
            const al  = ariaOf(el);
            const match =
              reReplies.test(txt) || reComments.test(txt) || reGeneric.test(txt) || reSeeMore.test(txt) ||
              reReplies.test(al)  || reComments.test(al)  || reGeneric.test(al)  || reSeeMore.test(al)  ||
              reAny.test(txt)     || reAny.test(al);
            if (match) {
              const btn = clickable(el);
              if (btn && isVisible(btn)) {
                try { btn.click(); clicks += 1; } catch (e) {}
              }
            }
          }
          return clicks;
        }""",
        [
            RE_VIEW_REPLIES.pattern,
            RE_VIEW_COMMENTS.pattern,
            RE_GENERIC_MORE.pattern,
            RE_SEE_MORE_TRUNC.pattern,
            RE_CATCH_ANY.pattern,
        ],
    )

def expand_all_comments_and_replies(page, passes: int = 6) -> int:
    total = 0
    for _ in range(passes):
        clicks = js_click_expanders(page)
        total += clicks
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PWTimeout:
            pass
        page.mouse.wheel(0, 2000)
        time.sleep(0.25)
        if clicks == 0:
            clicks2 = js_click_expanders(page)
            total += clicks2
            if clicks2 == 0:
                break
    return total

def ensure_comments_in_view(page):
    targets = [
        "div[aria-label='Write a comment']",
        "div[aria-label='Comments']",
        PRIMARY_COMMENT_WRAPPER,
    ] + COMMENT_BLOCK_FALLBACKS + ["ul[role='list']"]
    for t in targets:
        try:
            loc = page.locator(t).first
            if loc and loc.count() > 0:
                loc.scroll_into_view_if_needed(timeout=1500)
                time.sleep(0.2)
                return
        except Exception:
            continue
    for _ in range(3):
        page.mouse.wheel(0, 2000)
        time.sleep(0.2)

# ---------------- Smart scrolling (same) ----------------
def count_comment_blocks(page) -> int:
    try:
        blocks = page.locator(PRIMARY_COMMENT_WRAPPER)
        n = blocks.count()
        if n == 0:
            blocks = page.locator(",".join(COMMENT_BLOCK_FALLBACKS))
            n = blocks.count()
        return n
    except Exception:
        return 0

def scroll_host_or_window(page):
    try:
        did_host = page.evaluate(
            """() => {
              const qs = [
                "div[aria-label='Comments']",
                "div[role='feed']",
                "div[role='main']",
                "div[role='article']",
                "div.x1n2onr6.x1ja2u2z.x78zum5"
              ];
              let el = null;
              for (const sel of qs) {
                const n = document.querySelector(sel);
                if (n) { el = n; break; }
              }
              function scrollableAncestor(node) {
                let cur = node;
                while (cur) {
                  const s = getComputedStyle(cur);
                  const oh = cur.scrollHeight, ch = cur.clientHeight;
                  const overflowY = s.overflowY || s.overflow || "visible";
                  if (oh && ch && oh > ch && /(auto|scroll)/.test(overflowY)) return cur;
                  cur = cur.parentElement;
                }
                return null;
              }
              if (el) {
                const host = scrollableAncestor(el);
                if (host) {
                  host.scrollTo({top: host.scrollTop + 2400, behavior: "instant"});
                  return true;
                }
              }
              window.scrollBy(0, 2400);
              return false;
            }"""
        )
        return bool(did_host)
    except Exception:
        page.mouse.wheel(0, 2400)
        return False

def page_heightsig(page) -> int:
    try:
        return int(page.evaluate("() => document.body ? document.body.scrollHeight : 0"))
    except Exception:
        return 0

def scroll_until_plateau(page, max_rounds: int = 30, stable_rounds_needed: int = 3) -> int:
    total_rounds = 0
    stable_rounds = 0
    last_height = page_heightsig(page)
    last_count = count_comment_blocks(page)

    while total_rounds < max_rounds:
        scroll_host_or_window(page)
        try:
            page.wait_for_load_state("networkidle", timeout=2500)
        except PWTimeout:
            pass
        time.sleep(0.25)

        clicks = expand_all_comments_and_replies(page, passes=1)

        new_height = page_heightsig(page)
        new_count = count_comment_blocks(page)

        changed = (new_height != last_height) or (new_count != last_count) or (clicks > 0)
        total_rounds += 1
        if changed:
            stable_rounds = 0
        else:
            stable_rounds += 1

        last_height, last_count = new_height, new_count

        if stable_rounds >= stable_rounds_needed:
            break

    return total_rounds

# ---------------- Comment extraction (same) ----------------
def extract_commenter(block):
    """Best-effort commenter name from a comment block."""
    def _from_link(a):
        try:
            name_span = a.locator("span[dir='auto']").first
            if name_span and name_span.count() > 0:
                txt = text_content(name_span)
                if txt:
                    return txt
        except Exception:
            pass
        try:
            txt = text_content(a)
            if txt:
                return txt
        except Exception:
            pass
        try:
            svg = a.locator("svg[aria-label]").first
            if svg and svg.count() > 0:
                txt = (svg.get_attribute("aria-label") or "").strip()
                if txt:
                    return clean_ws(txt)
        except Exception:
            pass
        return None

    # group-user link
    try:
        a = block.locator('a[href*="/groups/"][href*="/user/"]').first
        if a and a.count() > 0 and (a.get_attribute("aria-hidden") != "true"):
            name = _from_link(a);  return name
    except Exception:
        pass
    # profile/people
    try:
        a = block.locator('a[href*="/profile.php"], a[href*="/people/"]').first
        if a and a.count() > 0 and (a.get_attribute("aria-hidden") != "true"):
            name = _from_link(a);  return name
    except Exception:
        pass
    # any link with user-ish href
    try:
        a = block.locator(
            "a[role='link'][href*='/user/'], a[role='link'][href*='/profile.php'], a[role='link'][href*='/people/']"
        ).first
        if a and a.count() > 0:
            name = _from_link(a);  return name
    except Exception:
        pass
    # sometimes name is a plain span near top
    try:
        span = block.locator("span[dir='auto']").first
        if span and span.count() > 0:
            txt = text_content(span)
            if txt and 1 <= len(txt.split()) <= 6:
                return txt
    except Exception:
        pass
    return None

def extract_comment_text(block) -> Optional[str]:
    try:
        lines = []
        body = block.locator("div.x1lliihq.xjkvuk6.x1iorvi4")
        if body.count() == 0:
            body = block
        divs = body.locator("div[dir='auto']")
        for j in range(min(120, divs.count())):
            t = clean_ws(get_inner_text_with_emojis(divs.nth(j)))
            if t:
                lines.append(t)
        if len(lines) < 1:
            spans = body.locator("span[dir='auto']")
            for j in range(min(120, spans.count())):
                t = clean_ws(get_inner_text_with_emojis(spans.nth(j)))
                if t:
                    lines.append(t)
        if lines:
            seen, deduped = set(), []
            for ln in lines:
                if not ln or re.fullmatch(r"[·\-\u2022]\s*", ln):
                    continue
                if ln not in seen:
                    seen.add(ln); deduped.append(ln)
            if deduped:
                return "\n".join(deduped).strip()
    except Exception:
        pass
    return None

# ---------------- Membership filter (same) ----------------
def block_belongs_to_post(block, post_id: Optional[str]) -> bool:
    """Real comments often have a timestamp/permalink anchor with /posts/<post_id> in href.
       For share links without /posts/<id>, allow looser matching."""
    if not post_id:
        return True
    try:
        links = block.locator(f"a[href*='/posts/{post_id}']")
        if links.count() > 0:
            return True
    except Exception:
        pass
    try:
        links2 = block.locator("a[href*='comment_id=']")
        n = min(links2.count(), 8)
        for i in range(n):
            href = (links2.nth(i).get_attribute("href") or "")
            if f"/posts/{post_id}" in href:
                return True
    except Exception:
        pass
    return False

# ---------------- Core scraping (same logic) ----------------
def scrape_comments(url: str, max_comments: int = 1000):
    out = {
        "url": url,
        "sort_label": None,
        "is_all_comments": None,
        "expanded_clicks": 0,
        "comments": [],  # {commenter, text}
        "notes": [],
    }

    post_id = extract_post_id(url)

    with sync_playwright() as p:
        browser: Browser = launch_browser(p)
        try:
            ctx = new_desktop_context(browser)
            page = ctx.new_page()
            page.goto(url, wait_until="load", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PWTimeout:
                pass
            time.sleep(0.8)

            ensure_comments_in_view(page)
            forced_all = try_force_all_comments(page)
            sort_label, is_all = detect_sort_label(page)
            if sort_label is None and forced_all:
                sort_label, is_all = "All comments", True
            out["sort_label"], out["is_all_comments"] = sort_label, is_all

            # --- deep load by scrolling until plateau
            out["notes"].append(f"Desktop pre-count: {count_comment_blocks(page)}")
            out["expanded_clicks"] += expand_all_comments_and_replies(page, passes=3)
            rounds = scroll_until_plateau(page, max_rounds=40, stable_rounds_needed=3)
            out["notes"].append(f"Desktop scroll rounds: {rounds}")
            out["notes"].append(f"Desktop post-count: {count_comment_blocks(page)}")

            # If desktop shows nothing or very few, try mobile UI too
            def locate_blocks():
                blocks = page.locator(PRIMARY_COMMENT_WRAPPER)
                if blocks.count() == 0:
                    blocks = page.locator(",".join(COMMENT_BLOCK_FALLBACKS))
                return blocks

            blocks = locate_blocks()
            # threshold: if almost nothing, switch to mobile
            if blocks.count() < min(5, max_comments // 5):
                out["notes"].append("Few/no comment blocks on desktop; trying mobile UI.")
                m_url = to_mobile_url(url)
                page.goto(m_url, wait_until="load", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except PWTimeout:
                    pass
                time.sleep(0.6)
                ensure_comments_in_view(page)

                # Mobile: expand + long scroll until plateau
                out["expanded_clicks"] += expand_all_comments_and_replies(page, passes=6)
                m_rounds = scroll_until_plateau(page, max_rounds=60, stable_rounds_needed=4)
                out["notes"].append(f"Mobile scroll rounds: {m_rounds}")
                blocks = locate_blocks()

            # Filter to blocks that belong to this post
            filtered: List = []
            N = min(blocks.count(), max_comments * 6)  # allow extra to survive filtering
            for i in range(N):
                blk = blocks.nth(i)
                try:
                    if not blk.is_visible():
                        continue
                except Exception:
                    continue
                if block_belongs_to_post(blk, post_id):
                    filtered.append(blk)
            if not filtered:  # fallback: take visible ones if post_id detection failed
                for i in range(min(blocks.count(), max_comments * 2)):
                    blk = blocks.nth(i)
                    try:
                        if blk.is_visible():
                            filtered.append(blk)
                    except Exception:
                        continue

            # Extract
            results: List[Dict[str, Optional[str]]] = []
            for blk in filtered:
                commenter = extract_commenter(blk)
                text = extract_comment_text(blk)
                if (commenter and commenter.strip()) or (text and text.strip()):
                    results.append({"commenter": commenter, "text": text})
                if len(results) >= max_comments:
                    break

            # Deduplicate by (commenter, text)
            seen_pairs: Set[Tuple[Optional[str], Optional[str]]] = set()
            deduped: List[Dict[str, Optional[str]]] = []
            for r in results:
                key = (r.get("commenter"), r.get("text"))
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    deduped.append(r)

            out["comments"] = deduped
            out["url"] = page.url or url
            return out

        finally:
            browser.close()

# ---------------- CLI (output only: url + comments) ----------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python postComments.py <facebook post url> [max_comments]", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    max_comments = 1000
    if len(sys.argv) >= 3:
        try:
            max_comments = int(sys.argv[2])
        except Exception:
            pass

    data = scrape_comments(url, max_comments=max_comments)

    comments = data.get("comments") or []
    result = {
        "url": data.get("url", url),
        "scraped_total": len(comments),
        "comments": comments,
    }

    print("—— RESULT ——")
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
