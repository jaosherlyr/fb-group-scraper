#!/usr/bin/env python3
# postDetails.py
# Usage: python postDetails.py "https://www.facebook.com/groups/.../posts/..."

import json
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Browser, TimeoutError as PWTimeout

# -------------------------
# Helpers
# -------------------------
def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def now_local():
    return datetime.now()

def _try_parse_isoish(s: str):
    """Try to parse ISO-like strings quickly. Returns date() or None."""
    if not s:
        return None
    s = s.strip()
    # Direct YYYY-MM-DD
    m = re.match(r"^\d{4}-\d{2}-\d{2}$", s)
    if m:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            pass
    # ISO-8601 with time and maybe TZ
    try:
        # Normalize space in AM/PM if any weirdness
        s2 = s.replace(" ", "")
        # Try several common layouts
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                pass
        # Last resort: split at 'T' and parse left part
        if "T" in s:
            left = s.split("T", 1)[0]
            return datetime.strptime(left, "%Y-%m-%d").date()
    except Exception:
        pass
    return None

def _try_parse_epoch(s: str):
    """Detect epoch seconds/ms and return date() or None."""
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d{10,13}", s):
        try:
            val = int(s)
            if len(s) == 13:
                val = val / 1000.0
            dt = datetime.fromtimestamp(val)
            return dt.date()
        except Exception:
            return None
    return None

def parse_date_text(date_text: str):
    """
    Try many shapes that FB uses in tooltips/labels.
    Returns ISO date string (YYYY-MM-DD) or None.
    """
    if not date_text:
        return None
    s = date_text.strip()

    # 0) ISO-ish or epoch early exits
    d0 = _try_parse_isoish(s) or _try_parse_epoch(s)
    if d0:
        return d0.isoformat()

    # 1) Short relative like "3d", "3h", "30m"
    m = re.fullmatch(r"(\d+)\s*([dhm])", s, flags=re.IGNORECASE)
    if m:
        qty = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"d": timedelta(days=qty), "h": timedelta(hours=qty), "m": timedelta(minutes=qty)}.get(unit)
        if delta:
            return (now_local() - delta).date().isoformat()

    # 2) Extended relative like "3 days ago", "2 hours ago", "15 minutes ago", "2 weeks ago"
    m = re.fullmatch(r"(\d+)\s*(week|weeks|wk|wks|w)\s*ago", s, flags=re.IGNORECASE)
    if m:
        w = int(m.group(1))
        return (now_local() - timedelta(weeks=w)).date().isoformat()

    m = re.fullmatch(r"(\d+)\s*(day|days|d)\s*ago", s, flags=re.IGNORECASE)
    if m:
        d = int(m.group(1))
        return (now_local() - timedelta(days=d)).date().isoformat()

    m = re.fullmatch(r"(\d+)\s*(hour|hours|hr|hrs|h)\s*ago", s, flags=re.IGNORECASE)
    if m:
        h = int(m.group(1))
        return (now_local() - timedelta(hours=h)).date().isoformat()

    m = re.fullmatch(r"(\d+)\s*(minute|minutes|min|mins|m)\s*ago", s, flags=re.IGNORECASE)
    if m:
        mins = int(m.group(1))
        return (now_local() - timedelta(minutes=mins)).date().isoformat()

    if re.fullmatch(r"(just\s*now|moments\s*ago)", s, flags=re.IGNORECASE):
        return now_local().date().isoformat()

    # 3) "Today at 3:14 PM"
    m = re.fullmatch(r"Today\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", s, flags=re.IGNORECASE)
    if m:
        tstr = m.group(1).upper().replace(" ", "")
        today = now_local().date()
        try:
            dt = datetime.strptime(f"{today} {tstr}", "%Y-%m-%d %I:%M%p")
            return dt.date().isoformat()
        except Exception:
            pass

    # 4) "Yesterday at 3:14 PM"
    m = re.fullmatch(r"Yesterday\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", s, flags=re.IGNORECASE)
    if m:
        tstr = m.group(1).upper().replace(" ", "")
        yday = now_local().date() - timedelta(days=1)
        try:
            dt = datetime.strptime(f"{yday} {tstr}", "%Y-%m-%d %I:%M%p")
            return dt.date().isoformat()
        except Exception:
            pass

    # 5) "Month Day at 3:14 PM" (assume current year)
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", s, flags=re.IGNORECASE)
    if m:
        month_name, day_str, time_str = m.group(1), m.group(2), m.group(3)
        year = now_local().year
        try:
            dt = datetime.strptime(f"{month_name} {day_str} {year} {time_str.upper().replace(' ', '')}", "%B %d %Y %I:%M%p")
            return dt.date().isoformat()
        except Exception:
            pass

    # 6) "Month Day, Year at 3:14 PM"
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", s, flags=re.IGNORECASE)
    if m:
        month_name, day_str, year_str, time_str = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            dt = datetime.strptime(f"{month_name} {day_str} {year_str} {time_str.upper().replace(' ', '')}", "%B %d %Y %I:%M%p")
            return dt.date().isoformat()
        except Exception:
            pass

    # 7) "Month Day, Year"
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s, flags=re.IGNORECASE)
    if m:
        month_name, day_str, year_str = m.group(1), m.group(2), m.group(3)
        try:
            dt = datetime.strptime(f"{month_name} {day_str} {year_str}", "%B %d %Y")
            return dt.date().isoformat()
        except Exception:
            pass

    # 8) "Month Day" (assume current year)
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})", s, flags=re.IGNORECASE)
    if m:
        month_name, day_str = m.group(1), m.group(2)
        year = now_local().year
        try:
            dt = datetime.strptime(f"{month_name} {day_str} {year}", "%B %d %Y")
            return dt.date().isoformat()
        except Exception:
            pass

    # 9) "Weekday at 3:14 PM" → assume most recent such weekday (best effort)
    m = re.fullmatch(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", s, flags=re.IGNORECASE)
    if m:
        # map weekday name to 0..6 (Mon..Sun)
        target_name = m.group(1).lower()
        time_str = m.group(2).upper().replace(" ", "")
        wd_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
        target = wd_map.get(target_name, None)
        if target is not None:
            today = now_local()
            # roll back to most recent target weekday (including today)
            delta_days = (today.weekday() - target) % 7
            dt_day = (today - timedelta(days=delta_days)).date()
            try:
                dt = datetime.strptime(f"{dt_day} {time_str}", "%Y-%m-%d %I:%M%p")
                return dt.date().isoformat()
            except Exception:
                pass

    # 10) Still nothing.
    return None


def same_origin(href: str) -> bool:
    try:
        p = urlparse(href)
        return (p.netloc.endswith("facebook.com") or p.netloc.endswith("fb.com") or p.netloc == "")
    except Exception:
        return False

def score_author_link(text: str, href: str) -> int:
    if not text or not href:
        return -999
    t = text.strip()
    h = href

    # hard negatives
    if "/groups/" in h and "/user/" not in h:
        return -100
    if ("multi_permalinks=" in h) or any(x in h for x in ["/posts/", "/permalink/", "/photo.php", "/photos/"]):
        return -50

    score = 0
    if "/groups/" in h and "/user/" in h:
        score += 100  # strongest signal
    if "/profile.php" in h or "/people/" in h:
        score += 40
    if re.search(r"facebook\.com/[^/?#]+/?($|\?)", h):
        score += 5
    if 2 <= len(t.split()) <= 5:
        score += 3
    return score

def get_inner_text_with_emojis(node):
    try:
        raw = node.inner_text(timeout=0) or ""
    except Exception:
        raw = ""
    try:
        imgs = node.locator("img[alt]")
        for i in range(imgs.count()):
            alt = (imgs.nth(i).get_attribute("alt") or "").strip()
            if alt and alt not in raw:
                raw = (raw + " " + alt).strip()
    except Exception:
        pass
    return raw

def strip_join_bits(name: str) -> str:
    if not name:
        return name
    return re.sub(r"\s*·\s*.*$", "", name).strip()

def text_or_svg_label(a):
    txt = clean_ws(a.inner_text() or "")
    if not txt:
        svg = a.locator('svg[aria-label]').first
        if svg and svg.count() > 0:
            txt = clean_ws(svg.get_attribute("aria-label") or "")
    return txt

# -------------------------
# Browser
# -------------------------
def launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
        ],
    )

# -------------------------
# Scrape
# -------------------------
def scrape_post(url: str):
    result = {
        "poster": None,
        "text": None,
        "date_iso": None,
        "url": url or "",
    }

    with sync_playwright() as p:
        browser: Browser = launch_browser(p)
        try:
            ctx = browser.new_context(
                java_script_enabled=True,
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.goto(url, wait_until="load", timeout=60_000)

            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PWTimeout:
                pass
            time.sleep(1.2)

            article = None
            candidates = page.locator("div[role='article']")
            article = candidates.first if candidates.count() > 0 else None
            if not article:
                main = page.locator("div[role='main']")
                article = main.first if main.count() > 0 else None
            if not article:
                # date fallback to today if we can't even find article
                result["date_iso"] = now_local().date().isoformat()
                return result

            try:
                article.locator("time").first.wait_for(state="attached", timeout=8_000)
            except PWTimeout:
                pass

            # -------------------
            # GROUP NAME (for exclusions)
            # -------------------
            group_name = None
            try:
                grp = article.locator("h3 a[href*='/groups/']:not([href*='/user/'])").first
                if grp and grp.count() > 0:
                    group_name = clean_ws(grp.inner_text() or "")
                    group_name = strip_join_bits(group_name)
            except Exception:
                pass

            # -------------------
            # POSTER
            # -------------------
            poster = None

            try:
                ug_links = article.locator('a[href*="/groups/"][href*="/user/"]')
                for i in range(ug_links.count()):
                    a = ug_links.nth(i)
                    name = text_or_svg_label(a)
                    name = strip_join_bits(name)
                    if name and name != group_name:
                        poster = name
                        break
            except Exception:
                pass

            if not poster:
                try:
                    header = article.locator('[data-ad-rendering-role="profile_name"]').first
                    if header and header.count() > 0:
                        a = header.locator('xpath=following::*[self::a][contains(@href,"/groups/") and contains(@href,"/user/")][1]').first
                        if a and a.count() > 0:
                            name = text_or_svg_label(a)
                            name = strip_join_bits(name)
                            if name and name != group_name:
                                poster = name
                except Exception:
                    pass

            if not poster:
                try:
                    span_name = article.locator(
                        "span.x193iq5w.xeuugli.x13faqbe.x1vvkbs.xlh3980.xvmahel.x1n0sxbx.x1nxh6w3.x1sibtaa.x1s688f.xi81zsa"
                    ).first
                    if span_name and span_name.count() > 0:
                        name = clean_ws(span_name.inner_text() or "")
                        name = strip_join_bits(name)
                        if name and name != group_name:
                            poster = name
                except Exception:
                    pass

            if not poster:
                try:
                    header = article.locator("h2, h3").first
                    if header.count() == 0:
                        header = article
                    links = header.locator("a[role='link']")
                    best_score, best_text = -9999, None
                    for i in range(links.count()):
                        node = links.nth(i)
                        if node.locator("time").count() > 0:
                            continue
                        href = node.get_attribute("href") or ""
                        if not same_origin(href):
                            continue
                        text = strip_join_bits(clean_ws(node.inner_text() or ""))
                        if not text or text == group_name:
                            if not text:
                                text = strip_join_bits(text_or_svg_label(node))
                        sc = score_author_link(text, href)
                        if sc > best_score and text and text != group_name:
                            best_score, best_text = sc, text
                    poster = best_text or None
                except Exception:
                    pass

            if not poster:
                try:
                    svg = article.locator("svg[aria-label]").first
                    if svg and svg.count() > 0:
                        cand = strip_join_bits(clean_ws(svg.get_attribute("aria-label") or ""))
                        if cand and cand != group_name:
                            poster = cand
                except Exception:
                    pass

            result["poster"] = poster or None

            # ---------------
            # DATE (YYYY-MM-DD) with robust fallbacks
            # ---------------
            date_text = None
            raw_datetime_attr = None
            try:
                # Primary: <a><time/></a>
                time_node = article.locator("a:has(time) time").first
                if time_node and time_node.count() > 0:
                    dtxt = clean_ws(time_node.inner_text() or "")
                    date_text = dtxt or None
                    # Try attributes too
                    raw_datetime_attr = (
                        time_node.get_attribute("datetime")
                        or time_node.get_attribute("title")
                        or None
                    )

                # Fallback: bare <time>
                if not date_text and not raw_datetime_attr:
                    tn = article.locator("time").first
                    if tn and tn.count() > 0:
                        dtxt = clean_ws(tn.inner_text() or "")
                        date_text = dtxt or None
                        raw_datetime_attr = (
                            tn.get_attribute("datetime")
                            or tn.get_attribute("title")
                            or None
                        )

                # Fallback: aria-label on the time link
                if not date_text and not raw_datetime_attr:
                    time_link = article.locator("a:has(time)").first
                    if time_link and time_link.count() > 0:
                        al = clean_ws(time_link.get_attribute("aria-label") or "")
                        if al:
                            date_text = al

                # Fallback: multi_permalinks anchor text/aria-label
                if not date_text and not raw_datetime_attr:
                    pl = article.locator('a[href*="multi_permalinks="]').first
                    if pl and pl.count() > 0:
                        dtxt = clean_ws(pl.inner_text() or "")
                        if not dtxt:
                            dtxt = clean_ws(pl.get_attribute("aria-label") or "")
                        if dtxt:
                            date_text = dtxt
            except Exception:
                pass

            # Parse candidates
            date_iso = None
            if raw_datetime_attr:
                # First try attribute (ISO or epoch)
                date_iso = (
                    (_try_parse_isoish(raw_datetime_attr) or _try_parse_epoch(raw_datetime_attr))
                )
                if date_iso:
                    date_iso = date_iso.isoformat()

            if not date_iso:
                date_iso = parse_date_text(date_text) if date_text else None

            # Final fallback: today
            if not date_iso:
                date_iso = now_local().date().isoformat()

            result["date_iso"] = date_iso

            # -------------------
            # TEXT (post body)
            # -------------------
            text_lines = []
            try:
                # 1) Canonical message container
                msg_container = article.locator('[data-ad-preview="message"], [data-ad-comet-preview="message"]').first
                if msg_container and msg_container.count() > 0:
                    auto_divs = msg_container.locator("div[dir='auto']")
                    for i in range(auto_divs.count()):
                        node = auto_divs.nth(i)
                        txt = clean_ws(get_inner_text_with_emojis(node))
                        if txt:
                            text_lines.append(txt)

                # 1.5) Explicit wrapper (title + location)
                wrapper_selector = (
                    "span.x193iq5w.xeuugli.x13faqbe.x1vvkbs.xlh3980.xvmahel.x1n0sxbx."
                    "x1lliihq.x1s928wv.xhkezso.x1gmr53x.x1cpjm7i.x1fgarty.x1943h6x."
                    "x4zkp8e.x3x7a5m.x6prxxf.xvq8zen.xo1l8bm.xzsf02u"
                )
                wrapper = article.locator(wrapper_selector).first
                if wrapper and wrapper.count() > 0:
                    raw = wrapper.inner_text() or ""
                    for bit in re.split(r"[\r\n]+", raw):
                        bit = clean_ws(bit)
                        if bit:
                            text_lines.append(bit)

                # 2) Fallback: strong heading + siblings
                if not text_lines:
                    title_strong = article.locator("h1 strong, h2 strong, h3 strong, h4 strong").first
                    if title_strong and title_strong.count() > 0:
                        title_heading = title_strong.locator(
                            "xpath=ancestor-or-self::h1 | ancestor-or-self::h2 | ancestor-or-self::h3 | ancestor-or-self::h4"
                        ).first
                        wrapper2 = title_strong.locator(
                            "xpath=ancestor::span[contains(@class,'x193iq5w')][1]"
                        ).first
                        if (not wrapper2) or wrapper2.count() == 0:
                            wrapper2 = title_heading

                        ttxt = clean_ws(get_inner_text_with_emojis(title_heading if title_heading and title_heading.count() > 0 else title_strong))
                        if ttxt:
                            text_lines.append(ttxt)

                        if wrapper2 and wrapper2.count() > 0:
                            parts = wrapper2.locator("strong, span[class*='x193iq5w'], div[dir='auto']")
                            for i in range(parts.count()):
                                t = clean_ws(get_inner_text_with_emojis(parts.nth(i)))
                                if t:
                                    text_lines.append(t)

                # 3) Super-fallback
                if not text_lines:
                    probes = article.locator("span[dir='auto'], div[dir='auto']")
                    for i in range(min(20, probes.count())):
                        t = clean_ws(get_inner_text_with_emojis(probes.nth(i)))
                        if t and len(t) >= 3:
                            text_lines.append(t)

                # Clean up body text
                if text_lines:
                    seen, deduped = set(), []
                    for line in text_lines:
                        if not line:
                            continue
                        if group_name and line == group_name:
                            continue
                        if result["poster"] and line == result["poster"]:
                            continue
                        if re.fullmatch(r"[·\-\u2022]\s*", line):
                            continue
                        if line not in seen:
                            seen.add(line)
                            deduped.append(line)
                    result["text"] = "\n".join(deduped).strip() or None
            except Exception:
                pass

            return result

        finally:
            browser.close()

# -------------------------
# CLI
# -------------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python postDetails.py <facebook post url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    try:
        data = scrape_post(url)
        print("—— RESULT ——")
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        print("—— RESULT ——")
        print(json.dumps({
            "poster": None,
            "text": None,
            "date_iso": datetime.now().date().isoformat(),  # ensure not null on hard errors
            "url": url,
            "error": str(e),
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

if __name__ == "__main__":
    main()
