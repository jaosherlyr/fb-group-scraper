````markdown
# Facebook Group Scraper + Comment Filter

This repo has a 2-step pipeline:

1. **Scrape posts** from a Facebook group → save post URLs to `input/group_post_urls.csv`
2. **Filter each post** → scrape comments → detect cobra/snake mentions → write to `output/*.csv`

It’s meant for your cobra / *Naja* SDM research workflow, so the filters are tuned for:
- admin/moderators (or names listed in `input/group_staff.txt`)
- comments that mention *Naja philippinensis*, *Naja samarensis*, *Ophiophagus hannah*, or the common names (Philippine cobra, Samar cobra, King cobra, etc.)

---

## 1. Requirements

- Python **3.9+**
- Google Chrome / Chromium (tested around **Chrome 131**)
- Selenium
- (optional) Playwright, if you want to log in once in a visible browser

Install:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -U selenium playwright
python -m playwright install       # only for the login helper
mkdir -p input output log state
````

---

## 2. Environment variables

`scrapePosts.py` reads these:

```bash
export FB_GROUP_URL="https://www.facebook.com/groups/XXXXXXXXXXXXXXX/"
export FB_EMAIL="your_fb_email@example.com"
export FB_PASS="your_fb_password"
# optional, if Chrome isn't in the usual place:
# export CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

You can also put them in a local **`.env`** (don’t commit it):

```env
FB_GROUP_URL=https://www.facebook.com/groups/XXXXXXXXXXXXXXX/
FB_EMAIL=your_fb_email@example.com
FB_PASS=your_fb_password
```

---

## 3. Login / authentication (how to use it)

You have **two** ways to stay logged in:

### Option A — Selenium cookies (this is what the code uses now)

* When you run `scrapePosts.py`, it will try to load cookies from:

  ```text
  state/fb_cookies.json
  ```

* If that file exists and is valid → it reuses your Facebook session.

* If it does **not** exist → it will try to log in using `FB_EMAIL` + `FB_PASS`.

* **Do not commit** `state/fb_cookies.json`. It contains your FB session.

### Option B — Playwright helper (manual login once)

You already have:

```bash
python save_fb_storage_state.py
```

Steps:

1. Run it
2. A real browser opens → log in to Facebook
3. Go back to the terminal → press **Enter**
4. It saves `storage_state.json`

You can adapt your scraper to read that later, but the current pipeline already works with `state/fb_cookies.json`, so **keep that file local and ignored**.

---

## 4. Step 1 — scrape posts

This fills the **input queue**:

```bash
python scrapePosts.py
```

What it does:

* opens the FB group (mobile layout by default)

* warm-up scroll (baseline) → **ignores already visible posts**

* starts scrolling and collecting **only new / unique** post URLs

* appends them to:

  ```text
  input/group_post_urls.csv
  ```

* it also reads:

  ```text
  state/done_urls.csv
  ```

  and **won’t add** URLs that were already processed before.

So you can safely run the scraper **multiple times** and it will just add new URLs.

---

## 5. Step 2 — run filters

After you have URLs in `input/group_post_urls.csv`, run:

```bash
python run_filters.py input/group_post_urls.csv
```

This will:

1. take each URL

2. call `filterPosts.py <url>`

3. `filterPosts.py` will:

   * call `postComments.py` → get all comments (admin + non-admin)
   * call `postDetails.py` → get text, poster, date
   * run the cobra / *Naja* regexes
   * decide which CSV to write to

4. `run_filters.py` will:

   * **if result = accepted / saved / rejected →** append the URL to
     `state/done_urls.csv`
   * **if result = SKIPPED (already processed) →** it will **NOT** add to `state/done_urls.csv`
   * remove the processed URL from `input/group_post_urls.csv` right away
   * write a streaming log to `log/run_filters.log`

So the input CSV becomes a **queue**: it shrinks as you process.

---

## 6. Output files

These come from **`filterPosts.py`**:

* `output/filtered_post.csv`
  → **🟢 accepted** (admin/mod/staff + snake mention)

* `output/filtered_post_non_admin.csv`
  → **🟠 saved** (no admin, but someone mentioned cobra/snake)

* `output/rejected_post.csv`
  → **🔴 rejected** (admin but not our snake, OR no admin and no snake)

Additional logs:

* `log/filter_log.csv` → every processed URL except SKIPPED
* `log/run_filters.log` → stream of the driver

Ledger:

* `state/done_urls.csv` → every URL that was **actually** classified
  (SKIPPED URLs are **not** added here)

---

## 7. Admin / staff matching

Put names (one per line) here:

```text
input/group_staff.txt
```

Example:

```text
Juan Dela Cruz
Admin Person
Page Name
```

Those will be treated like admin comments even if FB didn’t expose the badge in the scraped JSON.

---

## 8. Git / repo hygiene (important)

You said:

* ✅ you **will commit** the **outputs** (`output/*.csv`)
* ❌ you **do not** want to commit logs
* ❌ you **do not** want to commit cookies/tokens

So:

* keep: `output/`
* ignore: `state/` (cookies + done ledger), `log/`, local inputs

---

## 9. Run it end-to-end

```bash
# 1) activate
source .venv/bin/activate

# 2) scrape / collect post urls
python scrapePosts.py

# 3) run the filters on the collected urls
python run_filters.py input/group_post_urls.csv

# 4) open results
ls -l output/
```

---

## 10. Legal note

Scraping Facebook may violate FB’s Terms of Service and/or local laws.
Use only on groups you are allowed to access, and for research/educational purposes.
You are responsible for how you use this code.

````