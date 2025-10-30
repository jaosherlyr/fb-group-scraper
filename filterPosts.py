#!/usr/bin/env python3
# filterPosts.py — nicely formatted output + skips already processed posts + run log CSV
#
# Run log CSV (no skipped rows):
#   filter_log.csv
#     headers: url, comment scraped, status
#     status ∈ {Accepted, Saved - no admin, Rejected - w/admin, Rejected - no admin}

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- Paths ---
HERE = Path(__file__).resolve().parent
POST_COMMENTS = str(HERE / "postComments.py")
POST_DETAILS  = str(HERE / "postDetails.py")

CSV_ACCEPTED  = HERE / "output/filtered_post.csv"               # admin-like present (and admin mentions snake)
CSV_NONADMIN  = HERE / "output/filtered_post_non_admin.csv"     # no admin; non-admin snake mentions
CSV_REJECTED  = HERE / "output/rejected_post.csv"               # rejected cases
CSV_LOG       = HERE / "log/filter_log.csv"                  # new run log (no skipped rows)
STAFF_FILE    = HERE / "input/group_staff.txt"

# --- Targets: common & scientific names ---
COMMON_NAMES = [
    r"philippine\s+cobra", 
    r"philippines\s+cobra",
    r"phillipine\s+cobra",
    r"ph\s+cobra",
    r"samar\s+cobra",
    r"king\s+cobra",
    r"philippine\s+spitting\s+cobra",   # explicit common name
]

SCIENTIFIC_NAMES = [
    r"naja\s+philippinensis",           # Philippine cobra
    r"naja\s+samarensis",               # Samar cobra
    r"ophiophagus\s+hannah",            # King cobra
    # NEW: generic species mentions (allow (), spaces, optional period)
    r"\(?\s*naja\s*sp\.?\s*\)?",        # Naja sp. / (Naja sp.)
    r"\(?\s*naja\s*spp\.?\s*\)?",       # Naja spp. / (Naja spp.)
]

TARGET_PATTERNS = [re.compile(p, re.I) for p in (COMMON_NAMES + SCIENTIFIC_NAMES)]

# --- Utils: normalize odd Unicode italics and whitespace to make matching reliable ---
_SANS_ITALIC_UP = "𝘈𝘉𝘊𝘋𝘌𝘍𝘎𝘏𝘐𝘑𝘒𝘓𝘔𝘕𝘖𝘗𝘘𝘙𝘚𝘛𝘜𝘝𝘞𝘟𝘠𝘡"
_ASCII_UP       = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SANS_ITALIC_LO = "𝘢𝘣𝘤𝘥𝘦𝘧𝘨𝘩𝘪𝘫𝘬𝘭𝘮𝘯𝘰𝘱𝘲𝘳𝘴𝘵𝘶𝘷𝘸𝘹𝘺𝘻"
_ASCII_LO       = "abcdefghijklmnopqrstuvwxyz"
_SANSTRANS = str.maketrans(
    {**{u: a for u, a in zip(_SANS_ITALIC_UP, _ASCII_UP)},
     **{u: a for u, a in zip(_SANS_ITALIC_LO, _ASCII_LO)}}
)

def normalize_fancy_letters(s: str) -> str:
    if not s:
        return ""
    return s.translate(_SANSTRANS)

def oneline(s: Optional[str]) -> str:
    """Make a single-line, ASCII-friendly string for robust matching & CSV."""
    if not s:
        return ""
    s = normalize_fancy_letters(s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    u = u.replace("://m.facebook.", "://www.facebook.").replace("://facebook.", "://www.facebook.")
    u = re.sub(r"[?#].*$", "", u)
    u = u.rstrip("/")
    return u

def text_matches_targets(text: Optional[str]) -> bool:
    if not text:
        return False
    t = oneline(text)
    for pat in TARGET_PATTERNS:
        if pat.search(t):
            return True
    return False

def has_admin_tag(text: Optional[str]) -> bool:
    return bool(text) and "#admin" in text.lower()

def load_staff_names() -> List[str]:
    names: List[str] = []
    if STAFF_FILE.exists():
        for line in STAFF_FILE.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name:
                names.append(name.lower())
    return names

def commenter_in_staff_list(comment: Dict, staff_names_lower: List[str]) -> bool:
    name = oneline(comment.get("commenter") or "")
    return name.lower() in staff_names_lower if name else False

def is_group_staff_comment(comment: Dict, staff_names_lower: List[str]) -> bool:
    if commenter_in_staff_list(comment, staff_names_lower):
        return True
    for key in ("group_staff", "is_admin", "is_moderator"):
        if bool(comment.get(key)):
            return True
    role = (comment.get("role") or "").strip().lower()
    if role in {"admin", "moderator", "group admin", "group moderator"}:
        return True
    badges = comment.get("badges")
    if isinstance(badges, list):
        for b in badges:
            label = ""
            if isinstance(b, str):
                label = b
            elif isinstance(b, dict):
                label = b.get("label") or b.get("name") or ""
            if "admin" in (label or "").lower() or "moderator" in (label or "").lower():
                return True
    commenter_info = comment.get("commenter_info") or {}
    for key in ("is_admin", "is_moderator", "group_staff"):
        if bool(commenter_info.get(key)):
            return True
    return False

def is_admin_like(comment: Dict, staff_names_lower: List[str]) -> bool:
    return is_group_staff_comment(comment, staff_names_lower) or has_admin_tag(comment.get("text") or "")

def choose_one_comment_pref_admin_tag(comments: List[Dict]) -> Optional[Dict]:
    if not comments:
        return None
    for c in comments:
        if has_admin_tag(c.get("text") or ""):
            return c
    return comments[0]

# --- Subprocess helpers ---
def run_script_for_json(script_path: str, url: str) -> Dict:
    cmd = [sys.executable, script_path, url]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(HERE),
    )
    out = proc.stdout.strip()
    marker = "—— RESULT ——"
    after = out.split(marker, 1)[1].strip() if marker in out else out
    m = re.search(r"\{[\s\S]*\}\s*$", after)
    if not m:
        print(f"Could not find JSON in {script_path} output.")
        print(out)
        sys.exit(1)
    return json.loads(m.group(0))

# --- CSV helpers (BOM-proof URL column detection to prevent duplicate writes) ---
def ensure_csv_with_header(csv_path: Path, headers: List[str]):
    if not csv_path.exists():
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(headers)

def append_rows(csv_path: Path, rows: List[List[str]]):
    if not rows:
        return
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerows(rows)

def _clean_fieldname(fn: Optional[str]) -> Optional[str]:
    if fn is None:
        return None
    fn = fn.replace("\ufeff", "")
    fn = re.sub(r"\s+", " ", fn.strip())
    return fn.lower()

def _find_url_column(fieldnames: Optional[List[str]]) -> Optional[str]:
    if not fieldnames:
        return None
    cleaned_to_orig = { _clean_fieldname(fn): fn for fn in fieldnames if fn is not None }
    for wanted in ("post url", "url"):
        if wanted in cleaned_to_orig:
            return cleaned_to_orig[wanted]
    for cleaned, orig in cleaned_to_orig.items():
        if cleaned and "url" in cleaned:
            return orig
    return None

def url_in_csv(csv_path: Path, url_norm: str) -> bool:
    if not csv_path.exists():
        return False
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        url_col = _find_url_column(reader.fieldnames)
        if not url_col:
            return False
        for row in reader:
            v = normalize_url(row.get(url_col) or "")
            if v and v == url_norm:
                return True
    return False

def already_processed(url_raw: str) -> Tuple[bool, List[str]]:
    url_norm = normalize_url(url_raw)
    hits = []
    if url_in_csv(CSV_ACCEPTED, url_norm):  hits.append(CSV_ACCEPTED.name)
    if url_in_csv(CSV_NONADMIN, url_norm): hits.append(CSV_NONADMIN.name)
    if url_in_csv(CSV_REJECTED, url_norm): hits.append(CSV_REJECTED.name)
    return (len(hits) > 0, hits)

# --- Pretty print helper ---
def print_status_block(scraped_total: int, admin_found: bool, snake_found: bool):
    print()
    print(f"Scraped total comments: {scraped_total}\n")
    print(f"ADMIN comment found: {'✅' if admin_found else '❌'}")
    print(f"SNAKE comment found: {'✅' if snake_found else '❌'}")
    print()

# --- Run log helper (do NOT log skipped) ---
def log_outcome(url_norm: str, scraped_total: int, status: str):
    ensure_csv_with_header(CSV_LOG, ["url", "comment scraped", "status"])
    append_rows(CSV_LOG, [[url_norm, str(scraped_total), status]])

# --- Main ---
def main():
    if len(sys.argv) != 2:
        print("Usage: python filterPosts.py <facebook post url>")
        sys.exit(1)

    post_url_input_raw = sys.argv[1].strip()
    post_url_norm = normalize_url(post_url_input_raw)

    # Skip if already processed (do NOT log skipped)
    is_done, where = already_processed(post_url_norm)
    if is_done:
        print(f"\n⏭️  SKIPPED! Already processed in [{', '.join(where)}]!\n")
        return

    staff_names_lower = load_staff_names()

    # Get all comments (unfiltered or filtered) from postComments.py
    pc_data = run_script_for_json(POST_COMMENTS, post_url_input_raw)
    comments = pc_data.get("comments") or pc_data.get("comments_filtered") or []
    scraped_total = int(pc_data.get("scraped_total") or len(comments) or 0)

    # Partition by admin first (per requirements)
    admin_comments = [c for c in comments if is_admin_like(c, staff_names_lower)]
    admin_found = len(admin_comments) > 0

    if admin_found:
        # Only check admin comments for snake mentions (ignore non-admin snakes when admin exists)
        admin_snake_comments = [c for c in admin_comments if text_matches_targets(c.get("text") or "")]
        snake_found = len(admin_snake_comments) > 0

        print_status_block(scraped_total, admin_found=True, snake_found=snake_found)

        if snake_found:
            # ACCEPTED
            pd_data = run_script_for_json(POST_DETAILS, post_url_input_raw)
            post_url = normalize_url(pd_data.get("url") or pc_data.get("url") or post_url_norm)
            post_date = oneline(pd_data.get("date_iso") or "")
            poster    = oneline(pd_data.get("poster") or "")
            post_text = oneline(pd_data.get("text") or "")
            headers_full = ["post url", "date", "poster", "post text", "commenter", "comment text"]

            # De-dup before write (safety)
            if not url_in_csv(CSV_ACCEPTED, post_url):
                chosen_admin = choose_one_comment_pref_admin_tag(admin_snake_comments) or admin_snake_comments[0]
                ensure_csv_with_header(CSV_ACCEPTED, headers_full)
                append_rows(CSV_ACCEPTED, [[
                    post_url, post_date, poster, post_text,
                    oneline(chosen_admin.get("commenter") or ""),
                    oneline(chosen_admin.get("text") or "")
                ]])
            print("🟢  ACCEPTED! Admin and Snake comment found!")
            log_outcome(post_url, scraped_total, "Accepted")
            print()
            return
        else:
            # REJECTED — with admin (but no snake)
            if not url_in_csv(CSV_REJECTED, post_url_norm):
                ensure_csv_with_header(CSV_REJECTED, ["post url", "commenter", "comment text"])
                chosen_admin_only = choose_one_comment_pref_admin_tag(admin_comments) or admin_comments[0]
                append_rows(CSV_REJECTED, [[
                    post_url_norm,
                    oneline(chosen_admin_only.get("commenter") or ""),
                    oneline(chosen_admin_only.get("text") or "")
                ]])
            print("🔴  REJECTED! Admin comment found but NOT our snake!")
            log_outcome(post_url_norm, scraped_total, "Rejected - w/admin")
            print()
            return

    # No admin comments at all — consider non-admin comments for snake
    non_admin_snake_comments = [c for c in comments if text_matches_targets(c.get("text") or "")]
    snake_found = len(non_admin_snake_comments) > 0

    print_status_block(scraped_total, admin_found=False, snake_found=snake_found)

    if snake_found:
        # SAVED — no admin, but snake mentioned by non-admin
        pd_data = run_script_for_json(POST_DETAILS, post_url_input_raw)
        post_url = normalize_url(pd_data.get("url") or pc_data.get("url") or post_url_norm)
        post_date = oneline(pd_data.get("date_iso") or "")
        poster    = oneline(pd_data.get("poster") or "")
        post_text = oneline(pd_data.get("text") or "")
        headers_full = ["post url", "date", "poster", "post text", "commenter", "comment text"]

        if not url_in_csv(CSV_NONADMIN, post_url):
            ensure_csv_with_header(CSV_NONADMIN, headers_full)
            rows = []
            first = non_admin_snake_comments[0]
            rows.append([
                post_url, post_date, poster, post_text,
                oneline(first.get("commenter") or ""),
                oneline(first.get("text") or "")
            ])
            # Subsequent rows: '-' for post fields, dedup by (commenter, text)
            seen = set([(rows[0][4], rows[0][5])])
            for c in non_admin_snake_comments[1:]:
                commenter = oneline(c.get("commenter") or "")
                ctext = oneline(c.get("text") or "")
                if (commenter, ctext) in seen:
                    continue
                seen.add((commenter, ctext))
                rows.append(["-", "-", "-", "-", commenter, ctext])
            append_rows(CSV_NONADMIN, rows)

        print(f"🟠  NON-ADMIN! Snake mentioned by Non-admin! ({len(non_admin_snake_comments)})")
        log_outcome(post_url, scraped_total, "Saved - no admin")
        print()
        return

    # REJECTED — no admin and no snake
    if not url_in_csv(CSV_REJECTED, post_url_norm):
        ensure_csv_with_header(CSV_REJECTED, ["post url", "commenter", "comment text"])
        append_rows(CSV_REJECTED, [[post_url_norm, "-", "-"]])
    print("🔴  NO ADMIN and NO SNAKE!")
    log_outcome(post_url_norm, scraped_total, "Rejected - no admin")
    print()

if __name__ == "__main__":
    main()
