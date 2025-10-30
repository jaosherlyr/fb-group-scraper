#!/usr/bin/env python3
# run_filters.py — realtime appends/removals, minimal output
# Usage:
#   python run_filters.py urls.csv

import csv
import sys
import time
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

HERE = Path(__file__).resolve().parent
FILTER_SCRIPT = str(HERE / "filterPosts.py")
LOG_FILE = HERE / "log/run_filters.log"
DONE_FILE = HERE / "state/done_urls.csv"

DELAY_SECONDS = 0.6

# ---------- Normalization ----------
def normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    u = u.replace("://m.facebook.", "://www.facebook.").replace("://facebook.", "://www.facebook.")
    for sep in ("?", "#"):
        if sep in u:
            u = u.split(sep, 1)[0]
    return u.rstrip("/")

# ---------- CSV helpers for done_urls ----------
def _clean_fieldname(fn: Optional[str]) -> Optional[str]:
    if fn is None:
        return None
    fn = fn.replace("\ufeff", "")
    fn = " ".join(fn.split())
    return fn.lower()

def _find_url_column(fieldnames: Optional[List[str]]) -> Optional[str]:
    if not fieldnames:
        return None
    cleaned_to_orig = { _clean_fieldname(fn): fn for fn in fieldnames if fn is not None }
    for wanted in ("post url", "url", "link", "post_url"):
        if wanted in cleaned_to_orig:
            return cleaned_to_orig[wanted]
    for cleaned, orig in cleaned_to_orig.items():
        if cleaned and "url" in cleaned:
            return orig
    return fieldnames[0] if fieldnames else None

def ensure_done_header():
    # Create header if file does not exist or is empty
    if (not DONE_FILE.exists()) or (DONE_FILE.exists() and DONE_FILE.stat().st_size == 0):
        with DONE_FILE.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["url"])
            f.flush()
            try: os.fsync(f.fileno())
            except: pass

def done_contains(url_norm: str) -> bool:
    if not DONE_FILE.exists() or DONE_FILE.stat().st_size == 0:
        return False
    try:
        with DONE_FILE.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            col = _find_url_column(reader.fieldnames) or "url"
            for row in reader:
                v = normalize_url((row.get(col) or "").strip())
                if v and v == url_norm:
                    return True
    except Exception:
        return False
    return False

def append_done_url_now(u: str):
    """Append to done_urls.csv immediately with flush + fsync (idempotent vs done_urls only)."""
    norm = normalize_url(u)
    if not norm:
        return
    ensure_done_header()
    if done_contains(norm):
        return
    with DONE_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow([norm])
        f.flush()
        try: os.fsync(f.fileno())
        except: pass

# ---------- Result classification (line-aware) ----------
def classify_result_text(text: str) -> str:
    """
    Return 'accept' | 'reject' | 'saved' | 'skip' | 'unknown'.
    Safe to call per-line or on cumulative text; looks for strong markers from filterPosts.py.
    """
    out = text or ""
    low = out.lower()

    # Skip first
    if "⏭️" in out or "skipped" in low or "already processed" in low:
        return "skip"

    # Accept
    if "🟢" in out or "accepted!" in low or " accepted" in low:
        return "accept"

    # Saved (non-admin)
    if "🟠" in out or "non-admin" in low or "saved - no admin" in low:
        return "saved"

    # Reject (both variants)
    if "🔴" in out and ("rejected" in low or "no admin and no snake" in low):
        return "reject"
    if "rejected - w/admin" in low or "rejected - no admin" in low:
        return "reject"

    # Generic fallbacks
    if any(k in low for k in [" reject", "✗ rejected", "denied", "blacklist", "filtered out"]):
        return "reject"

    return "unknown"

# ---------- CSV I/O for the input file ----------
def sniff_has_header(csv_path: Path) -> bool:
    with csv_path.open("r", encoding="utf-8") as f:
        sample = f.read(2048)
        try:
            return csv.Sniffer().has_header(sample)
        except Exception:
            first_line = sample.splitlines()[0] if sample else ""
            return any(c.isalpha() for c in first_line)

def find_url_column(headers: List[str]) -> Optional[str]:
    if not headers:
        return None
    preferred = {"post_url", "post url", "url", "link"}
    for h in headers:
        if h and h.strip().lower() in preferred:
            return h
    for h in headers:
        if h and "url" in h.strip().lower():
            return h
    return headers[0]

def read_rows(csv_path: Path) -> Tuple[bool, str, List[Dict[str, Any]], List[str]]:
    has_header = sniff_has_header(csv_path)
    rows: List[Dict[str, Any]] = []
    headers: List[str] = []
    url_col: Optional[str] = None

    with csv_path.open("r", encoding="utf-8") as f:
        if has_header:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            url_col = find_url_column(headers)
            if not url_col:
                raise SystemExit("Could not determine URL column in CSV.")
            for row in reader:
                rows.append({h: row.get(h, "") for h in headers})
        else:
            reader = csv.reader(f)
            headers = ["url"]
            url_col = "url"
            for row in reader:
                if not row:
                    continue
                rows.append({"url": (row[0] or "").strip()})

    return has_header, url_col, rows, headers

def write_rows_now(csv_path: Path, has_header: bool, headers: List[str], rows: List[Dict[str, Any]]):
    """Rewrite CSV immediately with flush + fsync."""
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if has_header:
            writer = csv.DictWriter(f, fieldnames=headers, lineterminator="\n")
            writer.writeheader()
            for r in rows:
                writer.writerow({h: r.get(h, "") for h in headers})
        else:
            w = csv.writer(f, lineterminator="\n")
            for r in rows:
                w.writerow([r.get("url", "")])
        f.flush
        try: os.fsync(f.fileno())
        except: pass

# ---------- Streaming runner with realtime actions ----------
def run_and_act_streaming(url: str,
                          csv_path: Path,
                          has_header: bool,
                          url_col: str,
                          headers: List[str],
                          remaining_rows: List[Dict[str, Any]]) -> Tuple[int, str, List[Dict[str, Any]], str]:
    """
    Stream filterPosts.py output line-by-line.
    As soon as we detect SKIP / ACCEPT / REJECT / SAVED:
      - SKIP  -> immediately remove URL from input CSV (no done append)
      - ACCEPT/REJECT/SAVED -> immediately append to done_urls.csv, then remove from input CSV
    Returns (rc, full_output, updated_remaining_rows, final_kind)
    """
    cmd = [sys.executable, FILTER_SCRIPT, url]

    # Unbuffered child for fastest line delivery
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(HERE),
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None

    full_lines: List[str] = []
    decided_kind: str = "unknown"
    acted: bool = False
    norm = normalize_url(url)

    with LOG_FILE.open("a", encoding="utf-8") as log:
        log.write(f"\n=== URL: {url} ===\n")
        for line in proc.stdout:
            # echo only child output and log it
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
            full_lines.append(line)

            # classify this line (or cumulative buffer)
            kind = classify_result_text(line)
            if kind == "unknown":
                kind = classify_result_text("".join(full_lines))

            if acted or kind == "unknown":
                continue

            # Take immediate action
            if kind == "skip":
                new_remaining = [r for r in remaining_rows if normalize_url(r.get(url_col) or "") != norm]
                if len(new_remaining) != len(remaining_rows):
                    write_rows_now(csv_path, has_header, headers, new_remaining)
                    remaining_rows[:] = new_remaining
                acted = True
                decided_kind = "skip"

            elif kind in ("accept", "reject", "saved"):
                append_done_url_now(url)
                new_remaining = [r for r in remaining_rows if normalize_url(r.get(url_col) or "") != norm]
                if len(new_remaining) != len(remaining_rows):
                    write_rows_now(csv_path, has_header, headers, new_remaining)
                    remaining_rows[:] = new_remaining
                acted = True
                decided_kind = kind

    rc = proc.wait()
    return rc, "".join(full_lines), remaining_rows, decided_kind or "unknown"

# ---------- Main ----------
def main():
    if len(sys.argv) != 2:
        print("Usage: python run_filters.py <csv_with_urls>")
        sys.exit(1)

    csv_path = Path(sys.argv[1]).expanduser().resolve()
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        sys.exit(1)

    has_header, url_col, rows, headers = read_rows(csv_path)
    if not rows:
        print("No rows found in the CSV.")
        sys.exit(1)

    remaining_rows = rows[:]

    print(f"Found {len(rows)} URLs. Running filterPosts.py on each…")
    print(f"(Logging to {LOG_FILE})\n")

    ok = 0
    fail = 0

    for i, row in enumerate(rows, 1):
        url = (row.get(url_col) or "").strip()
        if not url:
            continue

        print(f"--- [{i}/{len(rows)}] {url} ---")
        try:
            rc, out, remaining_rows, kind = run_and_act_streaming(
                url, csv_path, has_header, url_col, headers, remaining_rows
            )

            if rc == 0:
                ok += 1
                # Last-chance classification on full output if nothing detected in-stream
                if kind == "unknown":
                    kind2 = classify_result_text(out)
                    if kind2 in ("accept", "reject", "saved"):
                        append_done_url_now(url)
                        norm = normalize_url(url)
                        new_remaining = [r for r in remaining_rows if normalize_url(r.get(url_col) or "") != norm]
                        if len(new_remaining) != len(remaining_rows):
                            write_rows_now(csv_path, has_header, headers, new_remaining)
                            remaining_rows = new_remaining
            else:
                fail += 1
                print(f"⚠️ filterPosts.py exited with code {rc}")

        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            break
        except Exception as e:
            fail += 1
            print(f"⚠️ Error running filterPosts.py: {e}")
        time.sleep(DELAY_SECONDS)

    print(f"\nDone. Success: {ok} | Failed: {fail} | Total: {ok+fail}")
    print(f"Finished URLs → {DONE_FILE.name}")
    print(f"Remaining (errors/unprocessed) → {csv_path.name}")
    print(f"Full logs   → {LOG_FILE}")

if __name__ == "__main__":
    main()
