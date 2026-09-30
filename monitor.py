#!/usr/bin/env python3
"""Daily monitor: scan the ATS watchlist for fresh entry-level/junior SWE postings.

- Reads ~/workspace/newgrad-jobs/ats-watchlist.json
- For each board, pulls the public ATS API job list
- A posting is FRESH if its ATS timestamp is within the last 5 days, OR if it
  was never seen on any previous scan (first-sighting backstop — catches
  postings with bad/missing ATS dates; the seen record lives in seen.json)
- Keeps entry-level/junior titles, NY/NJ/CT/PA or remote-US locations
- Live-verifies each posting URL (HTTP 200) before adding
- Prunes entries older than 5 days by ATS date AND by first-sighting date
- Pushes updated jobs.json to the fresh-grad-jobs GitHub repo (Vercel redeploys)
- Writes a run report + borderline review queue (Jev-adjudicated)
"""
import json, re, subprocess, sys, time, html
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HOME = "/home/hatch"
WATCHLIST = f"{HOME}/workspace/newgrad-jobs/ats-watchlist.json"
JOBS_FILE = f"{HOME}/workspace/fresh-grad-jobs/jobs.json"
SEEN_FILE = f"{HOME}/workspace/fresh-grad-jobs/seen.json"
REPORT_DIR = f"{HOME}/workspace/newgrad-jobs/monitor-reports"
WINDOW_DAYS = 5
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
GH_API = f"{HOME}/workspace/skills/github/bin/gh-api"

TITLE_AUTO = re.compile(
    r"(new.?grad|early.?career|university.?grad|entry.?level|junior|"
    r"engineer\s+i\b(?!i)|software\s+engineer\s+i\b(?!i)|swe\s*i\b|sde\s*i\b|"
    r"0\s*[-–]\s*1\s*(yr|year))", re.I)
TITLE_BORDERLINE = re.compile(
    r"(associate|engineer\s+ii\b|\bii\b|forward.?deployed|founding|"
    r"0\s*[-–]\s*2|1\s*[-–]\s*2|2\s*(yr|year))", re.I)
TITLE_TECH = re.compile(r"(engineer|developer|software|swe\b|sde\b)", re.I)
LOC_NY = re.compile(  # acceptable geography: NY/NJ/CT/PA + remote/US markers
    r"(new york|nyc\b|manhattan|brooklyn|queens|bronx|staten island|\bny\b|"
    r"jersey city|hoboken|newark|princeton|morristown|"
    r"stamford|greenwich|hartford|white plains|long island|"
    r"philadelphia|pittsburgh|pennsylvania|\bpa\b|"
    r"remote|anywhere|united states|\bus\b|work from home|hybrid)", re.I)
# Non-US geography: reject unless a strong US marker is also present
# (e.g. "Remote Poland" is out; "London/New York" stays).
LOC_NONUS = re.compile(
    r"(poland|spain|portugal|germany|france|ireland|netherlands|belgium|sweden|"
    r"norway|denmark|finland|switzerland|\buk\b|u\.k\.|england|london|dublin|"
    r"toronto|vancouver|canada|india|bangalore|hyderabad|singapore|sydney|"
    r"australia|japan|tokyo|brazil|mexico|argentina|colombia|chile|costa rica|"
    r"israel|tel aviv|warsaw|krakow|emea\b|apac\b|latam\b|europe)", re.I)
LOC_US_STRONG = re.compile(
    r"(new york|nyc\b|\bny\b|new jersey|\bnj\b|connecticut|\bct\b|"
    r"pennsylvania|\bpa\b|"
    r"united states|\busa?\b|u\.s\.|los angeles|san francisco|boston|"
    r"chicago|seattle|austin|denver|atlanta|washington)", re.I)

LOC_US_GENERIC = re.compile(r"united states( of america)?|\busa?\b|u\.s\.", re.I)

def loc_ok(loc):
    if not LOC_NY.search(loc):
        return False
    if LOC_NONUS.search(loc) and not LOC_US_STRONG.search(loc):
        return False
    # A specific non-covered-region city with only a generic "United States"
    # suffix is out (e.g. "O Fallon, United States of America"); bare
    # "United States" or a remote/hybrid marker stays in.
    if re.search(r"remote|hybrid|work from home|anywhere", loc, re.I):
        return True
    m = LOC_US_GENERIC.search(loc)
    if m:
        place = (loc[:m.start()] + " " + loc[m.end():]).strip(" ,;-\u2013")
        if place and not LOC_NY.search(place):
            return False
    return True

def fetch(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, TimeoutError, Exception):
        return 0, ""

def fetch_post(url, payload, timeout=25):
    req = Request(url, data=json.dumps(payload).encode(),
                  headers={"User-Agent": UA, "Content-Type": "application/json",
                           "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except HTTPError as e:
        return e.code, ""
    except (URLError, TimeoutError, Exception):
        return 0, ""

def parse_ts(s):
    if not s:
        return None
    try:
        ts = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def parse_workday_relative(s, now):
    """Workday's postedOn is a relative string ('Posted Today', 'Posted 5 Days
    Ago', 'Posted 30+ Days Ago'). Map it to a calendar date; None if unknown
    or in the 30+ day bucket."""
    if not s:
        return None
    s = s.strip().lower()
    today = now.date()
    if s == "posted today":
        return today
    if s == "posted yesterday":
        return today - timedelta(days=1)
    m = re.match(r"posted (\d+)\s*\+?\s*days? ago", s)
    if m:
        n = int(m.group(1))
        if "+" in s or n > 30:
            return None
        return today - timedelta(days=n)
    return None

def workday_jobs(entry, now, cutoff_date):
    """Workday CXS API: POST {api_url}/jobs, 20/page, recency-ordered.
    Stops paging once a full page has nothing fresh (safety cap 15 pages)."""
    base = entry["api_url"].rstrip("/")
    careers = entry["careers_url"].rstrip("/")
    offset = 0
    for _ in range(15):
        code, body = fetch_post(base + "/jobs",
                                {"appliedFacets": {}, "limit": 20,
                                 "offset": offset, "searchText": ""})
        if code != 200 or not body:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        postings = data.get("jobPostings") or []
        if not postings:
            return
        fresh_on_page = 0
        for j in postings:
            try:
                title = (j.get("title") or "").strip()
                loc = (j.get("locationsText") or "").strip()
                path = (j.get("externalPath") or "").strip()
                day = parse_workday_relative(j.get("postedOn", ""), now)
                if not title or not path or not day:
                    continue
                if day < cutoff_date:
                    continue
                fresh_on_page += 1
                link = careers + (path if path.startswith("/") else "/" + path)
                ts = datetime.combine(day, datetime.min.time(),
                                      tzinfo=timezone.utc) + timedelta(hours=12)
                yield title, loc, link, ts, "workday postedOn"
            except Exception:
                continue
        if fresh_on_page == 0:
            return
        offset += 20

def phenom_jobs(entry):
    """Phenom People: POST {origin}/widgets with refineSearch; one shot."""
    origin = entry["api_url"].rstrip("/")
    job_path = entry.get("job_path", "us/en/job").strip("/")
    code, body = fetch_post(origin + "/widgets",
                            {"ddoKey": "refineSearch", "from": 0,
                             "size": 2000, "jobs": True, "counts": True})
    if code != 200 or not body:
        return
    try:
        data = json.loads(body)
    except Exception:
        return
    jobs = ((data.get("refineSearch") or {}).get("data") or {}).get("jobs") or []
    for j in jobs:
        try:
            title = (j.get("title") or "").strip()
            loc = (j.get("location") or "").strip()
            jid = str(j.get("jobId") or "").strip()
            ts = parse_ts(j.get("postedDate"))
            if title and jid and ts:
                yield title, loc, f"{origin}/{job_path}/{jid}", ts, "phenom postedDate"
        except Exception:
            continue

def eightfold_jobs(entry, cutoff):
    """Eightfold PCSX: GET {host}/api/pcsx/search?domain={domain}&start=N.
    10 results per call, recency-ordered. Stops once a full page is stale
    (safety cap 60 pages for very high-volume boards)."""
    host, _, domain = entry["board_token"].partition("/")
    base = f"https://{host}/api/pcsx/search?domain={domain}"
    start = 0
    for _ in range(60):
        code, body = fetch(f"{base}&start={start}&num=10")
        if code != 200 or not body:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        data = data.get("data", data)
        positions = data.get("positions") or []
        if not positions:
            return
        stale_page = True
        for p in positions:
            try:
                title = (p.get("name") or "").strip()
                link = (p.get("positionUrl") or "").strip()
                if link.startswith("/"):
                    link = f"https://{host}{link}"
                pts = p.get("postedTs")
                ts = (datetime.fromtimestamp(int(pts), tz=timezone.utc)
                      if pts else None)
                if not title or not link or not ts:
                    continue
                if ts < cutoff:
                    continue
                stale_page = False
                locs = p.get("locations") or []
                loc = "; ".join(locs) if isinstance(locs, list) else str(locs)
                yield title, loc.strip(), link, ts, "eightfold postedTs"
            except Exception:
                continue
        if stale_page:
            return
        start += 10

def workable_jobs(entry):
    """Workable widget API: GET apply.workable.com/api/v1/widget/accounts/{slug}."""
    slug = entry["board_token"]
    code, body = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if code != 200 or not body:
        return
    try:
        data = json.loads(body)
    except Exception:
        return
    for j in data.get("jobs") or []:
        try:
            title = (j.get("title") or "").strip()
            link = (j.get("url") or "").strip()
            ts = parse_ts(j.get("published_on")) or parse_ts(j.get("created_at"))
            if not title or not link or not ts:
                continue
            loc = ", ".join(x for x in
                            (j.get("city"), j.get("state"), j.get("country")) if x)
            if j.get("telecommuting"):
                loc = (loc + "; Remote") if loc else "Remote"
            yield title, loc.strip(), link, ts, "workable published_on"
        except Exception:
            continue

def smartrecruiters_jobs(entry, cutoff):
    """SmartRecruiters public posting API; server-side releasedAfter filter."""
    cid = entry["board_token"]
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    offset = 0
    while True:
        url = (f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
               f"?limit=100&offset={offset}&releasedAfter={cutoff_iso}")
        code, body = fetch(url)
        if code != 200 or not body:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        content = data.get("content") or []
        total = data.get("totalFound") or 0
        for c in content:
            try:
                title = (c.get("name") or "").strip()
                pid = str(c.get("id") or "").strip()
                ts = parse_ts(c.get("releasedDate"))
                if not title or not pid or not ts:
                    continue
                loc = c.get("location") or {}
                if loc.get("remote"):
                    locs = "Remote"
                else:
                    locs = ", ".join(x for x in
                                     (loc.get("city"), loc.get("region"),
                                      loc.get("country")) if x)
                link = f"https://jobs.smartrecruiters.com/{cid}/{pid}"
                yield title, locs.strip(), link, ts, "smartrecruiters releasedDate"
            except Exception:
                continue
        offset += 100
        if offset >= total or not content:
            return

def board_jobs(entry, now=None, cutoff=None):
    """Yield (title, location, url, posted_dt, date_source) from a watchlist entry."""
    ats, token = entry["ats"], entry["board_token"]
    if ats == "workday":
        yield from workday_jobs(entry, now, cutoff.date())
        return
    if ats == "phenom":
        yield from phenom_jobs(entry)
        return
    if ats == "smartrecruiters":
        yield from smartrecruiters_jobs(entry, cutoff)
        return
    if ats == "eightfold":
        yield from eightfold_jobs(entry, cutoff)
        return
    if ats == "workable":
        yield from workable_jobs(entry)
        return
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    elif ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    elif ats == "lever":
        url = f"https://api.lever.co/v0/postings/{token}"
    else:
        return
    code, body = fetch(url)
    if code != 200 or not body:
        return
    try:
        data = json.loads(body)
    except Exception:
        return
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return
    for j in jobs:
        try:
            if ats == "greenhouse":
                title = j.get("title", "")
                loc = (j.get("location") or {}).get("name", "")
                link = j.get("absolute_url", "")
                ts = parse_ts(j.get("updated_at"))
                src = "greenhouse updated_at"
            elif ats == "ashby":
                title = j.get("title", "")
                loc = j.get("locationName", "") or ""
                link = j.get("jobUrl", "")
                ts = parse_ts(j.get("publishedAt")) or parse_ts(j.get("updatedAt"))
                src = "ashby publishedAt"
            else:  # lever
                title = j.get("text", "")
                loc = (j.get("categories") or {}).get("location", "")
                link = j.get("hostedUrl", "")
                ts = parse_ts(j.get("createdAt"))
                src = "lever createdAt"
            if title and link and ts:
                yield title.strip(), loc.strip(), link.strip(), ts, src
        except Exception:
            continue

def live_ok(url):
    code, body = fetch(url)
    if code != 200 or not body:
        return False
    low = body.lower()
    return ("job" in low or "apply" in low) and len(body) > 2000

def verify_posting(entry, link):
    """Live-verify a posting URL. Returns (live: bool, exact_date|None).

    Workday posting pages are JS shells, so liveness is checked through the
    CXS detail API instead — which also yields the exact requisition
    startDate when available."""
    if entry["ats"] == "workday":
        careers = entry["careers_url"].rstrip("/")
        if not link.startswith(careers):
            return False, None
        path = link[len(careers):]
        code, body = fetch(entry["api_url"].rstrip("/") + path)
        if code != 200 or not body or "jobPostingInfo" not in body:
            return False, None
        try:
            info = json.loads(body).get("jobPostingInfo") or {}
            return True, parse_ts(info.get("startDate"))
        except Exception:
            return True, None
    return live_ok(link), None

def classify(title):
    if not TITLE_TECH.search(title):
        return None
    if TITLE_AUTO.search(title):
        # "Senior Software Engineer I" style bands are not entry-level
        if re.search(r"senior", title, re.I):
            return "borderline"
        return "entry"
    if TITLE_BORDERLINE.search(title):
        return "borderline"
    return None

def jev_adjudicate(company, role, location, posted, date_source):
    """Ask Jev whether a borderline posting is a genuine new-grad SWE fit.

    Returns (choice, confidence). Fails safe: (None, 0) on any error, which
    keeps the item in the manual review queue. Costs ~$0.00002 per call.
    """
    try:
        sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
        from dynamic_credentials import add_surrogate_to_request
        payload = {
            "model": "typesafe/jev-1.13",
            "state": (
                "Candidate: May 2026 CS graduate (Hunter College, CUNY), based in NYC, "
                "seeking entry-level/new-grad software engineering roles (0-2 years), "
                "NY/NJ/CT/PA or remote-US. "
                f"Posting under review: '{role}' at {company}, {location or 'location not listed'}, "
                f"posted {posted} (date source: {date_source})."
            ),
            "questions": {
                "new_grad_swe_fit": {
                    "type": "choice",
                    "instructions": "Is this posting a genuine entry-level/new-grad software engineering role (primarily writing production code, 0-2 years experience) suitable for the candidate above?",
                    "criteria": {
                        "yes": "Primarily software engineering work, entry-level or new-grad targeted, 0-2 years experience (titles like Software Engineer, New Grad, Associate Engineer, Engineer I, Forward Deployed Engineer new-grad programs).",
                        "no": "Senior/staff/lead/manager, or 3+ years required, or primarily sales/solutions/support/consulting/customer-facing rather than software engineering, or an internship.",
                    },
                }
            },
        }
        req = Request(
            "https://openrouter.ai/api/alpha/decisions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        add_surrogate_to_request(req, credential_name="custom.openrouter",
                                 allowed_hosts=("openrouter.ai",))
        with urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        ans = d["answers"]["new_grad_swe_fit"]
        return ans.get("choice"), float(ans.get("confidence", 0) or 0)
    except Exception:
        return None, 0

JEV_PROMOTE_CONFIDENCE = 0.95  # auto-add borderline only at very high confidence

def main():
    now = datetime.now(timezone.utc)
    # calendar-day window: anything posted since (today - 5 days) 00:00 UTC stays
    cutoff = datetime.combine((now - timedelta(days=WINDOW_DAYS)).date(),
                              datetime.min.time(), tzinfo=timezone.utc)
    today = now.strftime("%b %d, %Y")
    today_iso = now.strftime("%Y-%m-%d")
    cutoff_str = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")

    # Seen record: url -> {first_seen, last_seen, company, role}.
    # First run builds it silently (no first-sighting promotions while seeding).
    try:
        seen = json.load(open(SEEN_FILE))
        if not isinstance(seen, dict):
            seen = {}
    except Exception:
        seen = {}
    seeding = not seen

    wl = json.load(open(WATCHLIST))
    board = json.load(open(JOBS_FILE))
    existing_urls = {j["url"] for j in board["jobs"]}
    seen_titles = {(j["company"].lower(),
                    re.sub(r"\s+", " ", j["role"].lower()).strip())
                   for j in board["jobs"]}
    kept, added, borderline = [], [], []
    scanned, boards_ok, boards_fail = 0, 0, []

    for entry in wl:
        scanned += 1
        co = entry["company"]
        got = False
        try:
            board_iter = list(board_jobs(entry, now, cutoff))
        except Exception as e:
            boards_fail.append(f"{co} ({type(e).__name__})")
            continue
        for title, loc, link, ts, src in board_iter:
            got = True
            if not loc_ok(loc):
                continue
            cls = classify(title)
            if not cls:
                continue
            key = (co.lower(), re.sub(r"\s+", " ", title.lower()).strip())
            # Record the sighting (only tech-titled postings are tracked).
            first_sighting = link not in seen
            if first_sighting:
                seen[link] = {"first_seen": today_iso, "last_seen": today_iso,
                              "company": co, "role": title}
            else:
                seen[link]["last_seen"] = today_iso
            # Fresh = ATS date within window, OR never seen before
            # (first-sighting backstop for bad/missing ATS dates).
            via_first_sight = first_sighting and not seeding
            fresh_via = "ats-date" if ts >= cutoff else ("first-seen" if via_first_sight else None)
            if not fresh_via:
                continue
            if cls == "borderline":
                choice, conf = jev_adjudicate(co, title, loc, ts.strftime("%Y-%m-%d"), src)
                b = {"company": co, "role": title, "location": loc,
                     "url": link, "posted": ts.strftime("%Y-%m-%d"),
                     "posted_at": ts.isoformat(), "date_source": src,
                     "jev_choice": choice, "jev_confidence": round(conf, 3)}
                live, exact = verify_posting(entry, link)
                if (choice == "yes" and conf >= JEV_PROMOTE_CONFIDENCE
                        and link not in existing_urls and key not in seen_titles
                        and live and not (exact and exact < cutoff)):
                    if exact:
                        ts, src = exact, "workday startDate"
                    added.append({"company": co, "role": title, "location": loc or "New York, NY",
                                  "url": link, "posting_url": link,
                                  "posted": ts.strftime("%Y-%m-%d"),
                                  "posted_at": ts.isoformat(), "date_source": src,
                                  "verified": today, "exp": "entry", "fit": "jev-promoted",
                                  "first_seen": today_iso, "fresh_via": fresh_via})
                    existing_urls.add(link)
                    seen_titles.add(key)
                elif link in existing_urls or key in seen_titles:
                    continue  # already on the board (e.g. manual add); skip re-surfacing
                else:
                    borderline.append(b)
            elif cls and link not in existing_urls:
                if key in seen_titles:
                    continue
                live, exact = verify_posting(entry, link)
                if live and not (exact and exact < cutoff):
                    if exact:
                        ts, src = exact, "workday startDate"
                    exp = "junior2" if re.search(r"associate|\bii\b|2\s*(yr|year)", title, re.I) else "entry"
                    added.append({"company": co, "role": title, "location": loc or "New York, NY",
                                  "url": link, "posting_url": link,
                                  "posted": ts.strftime("%Y-%m-%d"),
                                  "posted_at": ts.isoformat(), "date_source": src,
                                  "verified": today, "exp": exp, "fit": "",
                                  "first_seen": today_iso, "fresh_via": fresh_via})
                    existing_urls.add(link)
                    seen_titles.add(key)
        if got:
            boards_ok += 1
        else:
            boards_fail.append(co)
        time.sleep(0.4)  # be polite to public APIs

    # prune anything now older than the window — by ATS date AND by first sighting
    # (a first-seen discovery stays visible for 5 days from discovery)
    for j in board["jobs"]:
        ts = parse_ts(j.get("posted_at"))
        fs = j.get("first_seen") or ""
        if (ts and ts >= cutoff) or (fs and fs >= cutoff_str):
            kept.append(j)

    board["jobs"] = kept + added
    board["count"] = len(board["jobs"])
    board["verified"] = today
    board["window"] = f"{(cutoff).strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}"
    json.dump(board, open(JOBS_FILE, "w"), indent=2)

    # persist the seen record (drop entries untouched for 90 days)
    stale_iso = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    seen = {u: v for u, v in seen.items() if v.get("last_seen", "") >= stale_iso}
    json.dump(seen, open(SEEN_FILE, "w"), indent=2)

    # push to GitHub (Vercel auto-deploys)
    sha = json.loads(subprocess.run(
        [GH_API, "GET", "/repos/johnarks/fresh-grad-jobs/contents/jobs.json"],
        capture_output=True, text=True).stdout)["sha"]
    content = open(JOBS_FILE, "rb").read()
    import base64
    body = json.dumps({"message": f"Monitor: +{len(added)} fresh, pruned to {len(kept+added)} (5-day window)",
                       "content": base64.b64encode(content).decode(), "sha": sha})
    p = subprocess.run([GH_API, "PUT", "/repos/johnarks/fresh-grad-jobs/contents/jobs.json", body],
                       capture_output=True, text=True)
    commit = json.loads(p.stdout).get("commit", {}).get("sha", "?")[:8]

    report = {"run": now.isoformat(), "boards_scanned": scanned, "boards_ok": boards_ok,
              "boards_failed": boards_fail, "added": added,
              "added_via_first_seen": sum(1 for a in added if a.get("fresh_via") == "first-seen"),
              "borderline_for_review": borderline, "pruned_to": len(board["jobs"]),
              "seen_tracked": len(seen), "seeding_run": seeding,
              "commit": commit}
    import os
    os.makedirs(REPORT_DIR, exist_ok=True)
    json.dump(report, open(f"{REPORT_DIR}/{now.strftime('%Y-%m-%d-%H%M')}.json", "w"), indent=2)

    print(f"boards: {boards_ok}/{scanned} ok | added: {len(added)} | "
          f"borderline: {len(borderline)} | total live: {len(board['jobs'])} | "
          f"seen: {len(seen)}{' (seeding)' if seeding else ''} | commit {commit}")
    for a in added:
        tag = " [first-seen]" if a.get("fresh_via") == "first-seen" else ""
        print(f"  +{tag}", a["company"], "|", a["role"][:60], "|", a["posted"])
    for b in borderline:
        print(f"  ? [{b.get('jev_choice')}/{b.get('jev_confidence')}]",
              b["company"], "|", b["role"][:60], "|", b["posted"])

if __name__ == "__main__":
    sys.exit(main())
