#!/usr/bin/env python3
"""Build a small, personal XMLTV guide for an Xtream playlist.

Credentials come from env var PLAYLIST_INI (CI) or secrets/playlist.txt (local),
never printed. Output: out/epg.xml.gz (+ out/index.html). Local audit files go to reports/.
Stdlib only.
"""
import argparse, csv, difflib, gzip, io, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache")
OUT = os.path.join(ROOT, "out")
REPORTS = os.path.join(ROOT, "reports")
UA = {"User-Agent": "Mozilla/5.0 (EPG-builder)", "Accept-Encoding": "gzip"}


# ---------------------------------------------------------------- helpers
class SafeError(RuntimeError):
    """Error whose message is safe to print (never contains URLs)."""


def log(msg):
    print(msg, flush=True)


def fetch(url, label, timeout=240, retries=3):
    last = "unknown"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # never chain: the exception text may contain the URL
            last = type(e).__name__ + (f" {e.code}" if hasattr(e, "code") else "")
            time.sleep(2 * (attempt + 1))
    raise SafeError(f"{label}: download failed ({last})")


def load_playlist_ini():
    text = os.environ.get("PLAYLIST_INI")
    if text is None:
        path = os.path.join(ROOT, "secrets", "playlist.txt")
        if not os.path.exists(path):
            raise SafeError("no PLAYLIST_INI env var and no secrets/playlist.txt")
        text = open(path, encoding="utf-8-sig").read()
    secs, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        m = re.match(r"^\[(.*)\]$", line)
        if m:
            cur = {}
            secs.append(cur)
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip().lower()] = v.strip()
    secs = [s for s in secs if s.get("server")]
    if not secs:
        raise SafeError("playlist file has no [section] with server=")
    return secs[0]


def base_url(server):
    b = server.strip().rstrip("/")
    return b if re.match(r"^https?://", b) else "http://" + b


# ---------------------------------------------------------------- names
QUALITY = r"\b(HD|FHD|UHD|4K|8K|SD|HEVC|H265|RAW|50FPS|60FPS|MULTI|BACKUP|LQ|HQ)\b"
STOP = r"\b(NETWORK|CHANNEL|THE|TV|LIVE)\b"


def norm_keys(name):
    """Return (strict, loose) normalized forms of a channel name."""
    n = name.upper().replace("&", " AND ")
    n = re.sub(r"^\s*[A-Z]{2,3}(?:[- ][A-Z]{1,6})?\s*[|:]\s*(?=\S)", "", n)  # "IN-NEWS | ", "UK: "
    n = re.sub(r"^\s*[\[\(|]?\s*(US|UK|USA|IN|PK|CA|AR|FR|TR|GB)\s*[\]\):|\-]\s*", "", n)
    n = re.sub(r"^\s*(US|UK|USA|IN|PK|GB)\s+(?=[A-Z0-9])", "", n)
    n = re.sub(r"\([^)]{0,12}\)|\*", " ", n)
    n = re.sub(QUALITY, " ", n)
    strict = re.sub(r"[^A-Z0-9]", "", n)
    loose = re.sub(r"[^A-Z0-9]", "", re.sub(STOP, " ", n))
    return strict, loose


def id_stem(cid):
    return re.sub(r"\.[a-z]{2}\d*$", "", cid.lower())


def is_placeholder_id(eid):
    return (not eid) or bool(re.search(r"\([A-Za-z]{1,3}\)\s*$", eid))


def slug(name):
    return re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".") or "channel"


# ---------------------------------------------------------------- time
TS = re.compile(r"^\s*(\d{14}|\d{12})\s*([+-]\d{4})?")


def parse_ts(s):
    """-> (aware UTC datetime or None, was_naive)"""
    m = TS.match(s or "")
    if not m:
        return None, False
    d = m.group(1).ljust(14, "0")
    dt = datetime.strptime(d, "%Y%m%d%H%M%S")
    off = m.group(2)
    if off:
        sign = 1 if off[0] == "+" else -1
        dt = dt - sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        return dt.replace(tzinfo=timezone.utc), False
    return dt.replace(tzinfo=timezone.utc), True


def fmt_ts(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


# ---------------------------------------------------------------- xml sources
def open_xml(path):
    with open(path, "rb") as f:
        magic = f.read(2)
    return gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")


def read_channels(path):
    """channel id -> [display names]; stops at the first <programme>."""
    chans = {}
    with open_xml(path) as f:
        for ev, el in ET.iterparse(f, events=("start", "end")):
            if ev == "start" and el.tag == "programme":
                break
            if ev == "end" and el.tag == "channel":
                chans[el.get("id")] = [d.text.strip() for d in el.findall("display-name") if d.text]
                el.clear()
    return chans


def read_programmes(path, wanted, lo, hi, naive_tz_hours):
    """Yield (channel_id, start, stop, title, sub, desc, cat) for wanted channels in [lo, hi]."""
    naive = 0
    out = []
    with open_xml(path) as f:
        it = ET.iterparse(f, events=("start", "end"))
        _, root = next(it)
        n = 0
        for ev, el in it:
            if ev != "end" or el.tag != "programme":
                continue
            n += 1
            if n % 20000 == 0:
                root.clear()
            ch = el.get("channel")
            if ch in wanted:
                s, ns = parse_ts(el.get("start"))
                e, ne = parse_ts(el.get("stop"))
                naive += ns or ne
                if s and ns:
                    s -= timedelta(hours=naive_tz_hours)
                if e and ne:
                    e -= timedelta(hours=naive_tz_hours)
                if s and not e:
                    e = s + timedelta(minutes=30)
                if s and e and e > s and e > lo and s < hi:
                    def txt(tag):
                        x = el.find(tag)
                        return (x.text or "").strip() if x is not None else ""
                    out.append((ch, s, e, txt("title"), txt("sub-title"), txt("desc"), txt("category")))
            el.clear()
    return out, naive


# ---------------------------------------------------------------- build
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-hours", type=float, default=12, help="reuse downloads newer than this")
    ap.add_argument("--no-fetch", action="store_true", help="use cache only")
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(REPORTS, exist_ok=True)
    now = datetime.now(timezone.utc)
    lo, hi = now - timedelta(hours=cfg["hours_back"]), now + timedelta(days=cfg["days_ahead"])

    # ---- playlist (live channels only), kept in memory; no stream URLs are read
    pl = load_playlist_ini()
    b = base_url(pl["server"])
    cred = urllib.parse.urlencode({"username": pl["username"], "password": pl["password"]})

    def api(action):
        raw = fetch(f"{b}/player_api.php?{cred}&action={action}", f"playlist {action}", timeout=180)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)

    cats = {c["category_id"]: re.sub(r"\s+", " ", c["category_name"].replace("�", "")).strip()
            for c in api("get_live_categories")}
    live = api("get_live_streams")
    log(f"playlist: {len(live)} live channels, {len(cats)} groups")

    want_titles = {}
    for g in cfg["groups"]:
        for t in g["titles"]:
            want_titles[t.upper()] = g["region"]
    prov_prefixes = [p.upper() for p in cfg["provider_only_prefixes"]]

    watch_groups, watch_names = set(), []
    wl = os.path.join(ROOT, "watchlist.txt")
    if os.path.exists(wl):
        for line in open(wl, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("group:"):
                watch_groups.add(re.sub(r"\s+", " ", line[6:]).strip().upper())
            else:
                watch_names.append(norm_keys(line)[0])
    use_watch = bool(watch_groups or watch_names)

    chans = []  # selected playlist channels
    for l in live:
        cat = cats.get(l.get("category_id"), "")
        cu = cat.upper()
        region, provider_only = want_titles.get(cu), False
        if region is None and any(cu.startswith(p) for p in prov_prefixes):
            region, provider_only = "*", True
        if region is None:
            continue
        name = (l.get("name") or "").strip()
        if use_watch and cu not in watch_groups and not any(w and w in norm_keys(name)[0] for w in watch_names):
            continue
        chans.append({"name": name, "eid": (l.get("epg_channel_id") or "").strip(), "cat": cat,
                      "region": region, "provider_only": provider_only})
    log(f"selected: {sum(not c['provider_only'] for c in chans)} channels in configured groups"
        f" + {sum(c['provider_only'] for c in chans)} provider-only candidates"
        f"{' (watchlist active)' if use_watch else ''}")

    # ---- group playlist channels into output keys
    keys = {}
    for c in chans:
        cid = c["eid"] or slug(c["name"])
        k = keys.setdefault(cid, {"id": cid, "names": [], "eids": set(), "region": c["region"],
                                  "cats": set(), "provider_only": c["provider_only"]})
        if c["name"] not in k["names"]:
            k["names"].append(c["name"])
        if c["eid"]:
            k["eids"].add(c["eid"])
        k["cats"].add(c["cat"])
        k["provider_only"] = k["provider_only"] and c["provider_only"]
    log(f"unique output channels: {len(keys)}")

    # ---- download sources
    sources = sorted(cfg["sources"], key=lambda s: s["priority"])
    paths = {}
    for s in sources:
        p = os.path.join(CACHE, s["id"] + ".xml")
        fresh = os.path.exists(p) and (time.time() - os.path.getmtime(p)) < args.cache_hours * 3600
        if not fresh and not args.no_fetch:
            try:
                url = f"{b}/xmltv.php?{cred}" if s.get("kind") == "provider" else s["url"]
                data = fetch(url, "source " + s["id"], timeout=600)
                open(p, "wb").write(data)
            except SafeError as e:
                log(f"  ! {e} (skipped)")
        if os.path.exists(p):
            paths[s["id"]] = p
            log(f"  source {s['id']:22s} {os.path.getsize(p)/1e6:7.1f} MB")

    # ---- aliases
    aliases = []
    with open(os.path.join(ROOT, "aliases.csv"), encoding="utf-8") as f:
        for row in csv.reader(l for l in f if l.strip() and not l.lstrip().startswith("#")):
            if len(row) >= 2:
                aliases.append((re.compile(row[0].strip(), re.I), row[1].strip()))

    # ---- match
    src_chans, src_index = {}, {}
    for s in sources:
        if s["id"] not in paths:
            continue
        ch = read_channels(paths[s["id"]])
        src_chans[s["id"]] = ch
        idx = {"id": {}, "stem": {}, "strict": {}, "loose": {}, "dn": {}}
        for cid, names in ch.items():
            idx["id"][cid] = cid
            idx["stem"].setdefault(id_stem(cid), cid)
            idx["dn"].setdefault(cid.lower(), cid)
            for nm in names + [cid.split(".")[0].replace("_", " ")]:
                st, lo_ = norm_keys(nm)
                if st:
                    idx["strict"].setdefault(st, cid)
                if lo_:
                    idx["loose"].setdefault(lo_, cid)
                    idx["dn"].setdefault(nm.lower(), cid)
        idx["loose_list"] = list(idx["loose"])
        src_index[s["id"]] = idx

    def digits(x):
        return re.findall(r"\d+", x)

    def match(key, s):
        """Best guide channel id in source s for this key -> (id, method) or None."""
        if s["regions"] != ["*"] and key["region"] != "*" and key["region"] not in s["regions"]:
            return None
        if key["provider_only"] and s.get("kind") != "provider":
            return None
        if s["id"] != "provider" and key["region"] == "*":
            return None
        idx = src_index[s["id"]]
        for eid in key["eids"]:
            if eid in idx["id"]:
                return idx["id"][eid], "id"
            if not is_placeholder_id(eid) and id_stem(eid) in idx["stem"]:
                return idx["stem"][id_stem(eid)], "id-stem"
        if key["provider_only"]:
            return None
        for nm in key["names"]:
            for rx, target in aliases:
                m = rx.fullmatch(nm.strip())
                if m:
                    t = m.expand(target)
                    hit = idx["id"].get(t) or idx["dn"].get(t.lower())
                    if hit:
                        return hit, "alias"
        for nm in key["names"]:
            st, lo_ = norm_keys(nm)
            if st and st in idx["strict"]:
                return idx["strict"][st], "name"
            if lo_ and lo_ in idx["loose"]:
                return idx["loose"][lo_], "name-loose"
        if key["region"] == "AR":  # beIN: aliases only; fuzzy mixes up language/country variants
            return None
        for nm in key["names"]:
            lo_ = norm_keys(nm)[1]
            if len(lo_) < 5:
                continue
            cands = [c for c in idx["loose_list"] if abs(len(c) - len(lo_)) <= 3 and c[0] == lo_[0]]
            for c in difflib.get_close_matches(lo_, cands, n=3, cutoff=0.9):
                if digits(c) == digits(lo_):
                    return idx["loose"][c], "fuzzy"
        return None

    mapping = defaultdict(list)  # key id -> [(source id, guide id, method)]
    methods = Counter()
    for kid, key in keys.items():
        for s in sources:
            if s["id"] not in src_index:
                continue
            m = match(key, s)
            if m:
                mapping[kid].append((s["id"], m[0], m[1]))
                methods[(s["id"], m[1])] += 1
    log(f"channels with at least one guide mapping: {len(mapping)} / {len(keys)}")

    # ---- read programmes (filtered) and merge by priority without overlap
    naive_hours = {s["id"]: s.get("naive_tz_hours", 0) for s in sources}
    by_source = defaultdict(lambda: defaultdict(list))  # source -> guide id -> programmes
    for s in sources:
        sid = s["id"]
        if sid not in paths:
            continue
        wanted = {g for lst in mapping.values() for (ssid, g, _) in lst if ssid == sid}
        if not wanted:
            continue
        progs, naive = read_programmes(paths[sid], wanted, lo, hi, naive_hours[sid])
        for p in progs:
            by_source[sid][p[0]].append(p)
        log(f"  {sid:22s} {len(wanted):4d} channels mapped, {len(progs):6d} programmes in window"
            f"{f', {naive} naive timestamps (assumed UTC{naive_hours[sid]:+d})' if naive else ''}")

    final = {}  # key id -> list of programmes
    contrib = defaultdict(Counter)
    for kid, lst in mapping.items():
        taken = []
        for sid, gid, _ in lst:
            for p in sorted(by_source[sid].get(gid, []), key=lambda x: x[1]):
                if all(p[2] <= t[1] or p[1] >= t[2] for t in taken):
                    taken.append(p)
                    contrib[kid][sid] += 1
        if taken:
            final[kid] = sorted(taken, key=lambda x: x[1])

    # ---- write XMLTV
    tv = ET.Element("tv", {"generator-info-name": "personal-epg-builder"})
    maxd = cfg["max_desc_chars"]
    kept = 0
    for kid, progs in final.items():
        key = keys[kid]
        ce = ET.SubElement(tv, "channel", {"id": kid})
        for nm in key["names"][:6]:
            ET.SubElement(ce, "display-name").text = nm
    for kid, progs in final.items():
        for (_, s, e, title, sub, desc, cat) in progs:
            pe = ET.SubElement(tv, "programme", {"start": fmt_ts(s), "stop": fmt_ts(e), "channel": kid})
            ET.SubElement(pe, "title").text = title or "No title"
            if sub:
                ET.SubElement(pe, "sub-title").text = sub[:maxd]
            if desc:
                ET.SubElement(pe, "desc").text = desc[:maxd]
            if cat:
                ET.SubElement(pe, "category").text = cat
            kept += 1
    buf = io.BytesIO()
    ET.ElementTree(tv).write(buf, encoding="utf-8", xml_declaration=True)
    raw = buf.getvalue()
    gz = gzip.compress(raw, 9, mtime=0)
    open(os.path.join(OUT, "epg.xml.gz"), "wb").write(gz)
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(
        "<!doctype html><meta charset=utf-8><title>EPG</title><p>Personal XMLTV guide: "
        f"<a href=epg.xml.gz>epg.xml.gz</a> (generated {now:%Y-%m-%d %H:%M} UTC)</p>")

    # ---- report (local only)
    regs = defaultdict(lambda: [0, 0])
    for kid, key in keys.items():
        if key["provider_only"] and kid not in final:
            continue
        r = key["region"] if key["region"] != "*" else "provider-only (PK/INR)"
        regs[r][0] += 1
        regs[r][1] += kid in final
    log("\n=== RESULT ===")
    for r, (n, c) in regs.items():
        log(f"  {r:24s} {c:4d} / {n:4d} channels have programmes ({100*c//max(1,n)}%)")
    log(f"  programmes: {kept}   xml: {len(raw)/1e6:.2f} MB   gz: {len(gz)/1e6:.2f} MB")
    log("  match methods: " + ", ".join(f"{k[0]}:{k[1]}={v}" for k, v in sorted(methods.items())))
    cg = defaultdict(lambda: [0, 0])
    for kid, key in keys.items():
        for cat in key["cats"]:
            if key["provider_only"]:
                continue
            cg[cat][0] += 1
            cg[cat][1] += kid in final
    with open(os.path.join(REPORTS, "unmatched.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "playlist_name", "epg_id"])
        for kid, key in keys.items():
            if kid not in final and not key["provider_only"]:
                w.writerow([sorted(key["cats"])[0], key["names"][0], sorted(key["eids"])[0] if key["eids"] else ""])
    with open(os.path.join(REPORTS, "mapping.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group", "playlist_name", "source", "guide_channel", "guide_name", "method", "programmes_used"])
        for kid, lst in mapping.items():
            for sid, gid, meth in lst:
                gn = (src_chans[sid].get(gid) or [""])[0]
                w.writerow([sorted(keys[kid]["cats"])[0], keys[kid]["names"][0], sid, gid, gn, meth, contrib[kid].get(sid, 0)])
    log("  per group:")
    for cat, (n, c) in sorted(cg.items()):
        log(f"    {cat:28s} {c:4d} / {n:4d} ({100*c//max(1,n)}%)")


if __name__ == "__main__":
    try:
        main()
    except SafeError as e:
        print("ERROR:", e)
        sys.exit(1)
    except Exception as e:
        print("ERROR:", type(e).__name__, "(details suppressed: may contain credentials)")
        sys.exit(1)
