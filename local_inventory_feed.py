"""
Nova — Google Merchant Center LOCAL INVENTORY feed generator.

WHAT THIS IS FOR
----------------
Free local listings (products showing in the Google Business Profile / Maps
panel) need a SECOND feed alongside the primary product feed. The primary feed
is produced by the "Google for WooCommerce" plugin via the Content API. This
script produces the local inventory feed, which the plugin cannot generate —
a known gap, not a WooCommerce limitation.

Merchant Center must fetch this file AT LEAST ONCE PER DAY. A manual upload
does not hold: the "Add inventory" step in
  Marketing methods > Free local listings > Thailand > Review setup status
reverts to "Not started" once the data goes stale, which re-greys the
"Request inventory verification" button. That is why this is a scheduled job
and not a one-off export.

THE ID RULE (this is the part that fails silently)
--------------------------------------------------
The `id` column MUST match the primary feed byte-for-byte or the row is
silently dropped — no error, no warning, the product simply never appears.

Verified against the live account on 2026-08-15:
  * The plugin publishes ids of the form  gla_<wordpress_post_id>
  * For VARIABLE products it publishes one item PER VARIATION, using the
    VARIATION's post id — NOT the parent product id.
  * Sampled live Merchant Center ids (gla_14681, gla_14480, gla_17194,
    gla_14677, gla_14521, gla_14768) are all variation ids. None of them is
    a parent product id.
  * Simple products publish under their own product id.

So: emit one row per published VARIATION, plus one row per published SIMPLE
product. Do NOT emit parent ids for variable products — those are not in the
primary feed and every such row is wasted.

Airtable is deliberately NOT the source here. Airtable's
"🌐 Product ID (Website)" field is populated on only ~547 of 894 records and
does not line up with the ids the plugin actually publishes. WooCommerce is
the system of record for what is on the site, so we read WooCommerce.

CREDENTIALS
-----------
Read-only. Needs WC_KEY / WC_SECRET only — no Airtable token, no WordPress
app password. Keep it that way: it means a cloud runner holds one read-scoped
credential pair rather than the whole nova_config.py set.

USAGE
-----
    py local_inventory_feed.py                 # write the feed
    py local_inventory_feed.py --report        # write it + print a full audit
    py local_inventory_feed.py --compare FILE  # diff against an older feed
    py local_inventory_feed.py --out PATH      # choose output path
"""

import argparse
import os
import sys
import time
from collections import Counter

import requests

# nova_config.py is the local convenience path and is deliberately absent on a
# cloud runner, where credentials arrive as environment variables instead.
# Missing it is normal, not an error — only having neither source is.
try:
    import nova_config as cfg
except ImportError:
    cfg = None


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

WC_URL = "https://www.nova-collection.com"
API = f"{WC_URL}/wp-json/wc/v3"

# The store code as declared in Google Business Profile > advanced settings.
# MUST match GBP byte-for-byte, including spaces and capitalisation.
# Proven working in the 2026-08-01 upload.
STORE_CODE = "Nova Collection Thapae"

# The prefix the Google for WooCommerce plugin puts on every item id.
ID_PREFIX = "gla_"

# WooCommerce stock_status -> Google local inventory availability.
# Google's TSV accepts the spaced legacy forms; the 2026-08-01 feed used
# "in stock" and parsed cleanly, so we keep exactly that.
AVAILABILITY_MAP = {
    "instock": "in stock",
    "outofstock": "out of stock",
    "onbackorder": "limited availability",
}
DEFAULT_AVAILABILITY = "out of stock"

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nova-local-inventory.txt"
)

PER_PAGE = 100
TIMEOUT = 90


def load_credentials():
    """Env vars win, so the same file runs locally and on a cloud runner."""
    key = os.environ.get("WC_KEY") or getattr(cfg, "WC_KEY", None)
    secret = os.environ.get("WC_SECRET") or getattr(cfg, "WC_SECRET", None)
    if not key or not secret:
        sys.exit(
            "Missing WC_KEY / WC_SECRET. Set them as environment variables, "
            "or put them in a local nova_config.py next to this script."
        )
    return (key, secret)


# ─────────────────────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────────────────────

def _get(session, url, **params):
    """GET with a few retries — this runs unattended, so a blip must not
    produce a truncated feed. A short feed is worse than no feed: Google
    reads missing rows as 'no longer in store'."""
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after 4 attempts — {last}")


def fetch_published_products(session):
    """Every parent-level product with status=publish."""
    out, page = [], 1
    while True:
        r = _get(session, f"{API}/products",
                 status="publish", per_page=PER_PAGE, page=page)
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", 0) or 0)
        if total_pages and page >= total_pages:
            break
        page += 1
    return out


def fetch_variations(session, product_id):
    """Published variations of one variable product."""
    out, page = [], 1
    while True:
        r = _get(session, f"{API}/products/{product_id}/variations",
                 status="publish", per_page=PER_PAGE, page=page)
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", 0) or 0)
        if total_pages and page >= total_pages:
            break
        page += 1
    return out


# ─────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────

def availability_for(stock_status):
    return AVAILABILITY_MAP.get(stock_status, DEFAULT_AVAILABILITY)


def build_rows(session, verbose=True):
    """Return (rows, stats). One row per feed item, matching the primary feed."""
    products = fetch_published_products(session)
    if verbose:
        print(f"  published parent-level products: {len(products)}")

    rows = []
    stats = Counter()
    variable = [p for p in products if p.get("type") == "variable"]

    for p in products:
        ptype = p.get("type")
        if ptype == "variable":
            continue  # handled below — the parent id is NOT in the primary feed
        if ptype == "external":
            # Affiliate/external products link off-site and are not purchasable
            # in the shop, so they are absent from the primary feed. Including
            # one put the row count at 415 against Merchant Center's 414;
            # excluding it matches exactly. (2026-08-15: only GR99-14kr-0.50.)
            stats["skipped:external"] += 1
            continue
        rows.append({
            "id": f"{ID_PREFIX}{p['id']}",
            "store_code": STORE_CODE,
            "availability": availability_for(p.get("stock_status")),
        })
        stats[f"simple:{p.get('stock_status')}"] += 1

    for i, p in enumerate(variable, 1):
        if verbose and (i % 25 == 0 or i == len(variable)):
            print(f"  variations: {i}/{len(variable)} parents expanded")
        for v in fetch_variations(session, p["id"]):
            # A variation inherits the parent's stock status when it does not
            # manage its own stock. WooCommerce already resolves this into
            # stock_status, so read it directly rather than re-deriving it.
            rows.append({
                "id": f"{ID_PREFIX}{v['id']}",
                "store_code": STORE_CODE,
                "availability": availability_for(v.get("stock_status")),
            })
            stats[f"variation:{v.get('stock_status')}"] += 1

    # A duplicate id makes Merchant Center reject the whole row set for that
    # item, so collapse defensively and report if it ever happens.
    seen, deduped = set(), []
    for row in rows:
        if row["id"] in seen:
            stats["DUPLICATE-DROPPED"] += 1
            continue
        seen.add(row["id"])
        deduped.append(row)

    return deduped, stats


def write_feed(rows, path):
    """Tab-separated, LF endings, UTF-8 — the format proven on 2026-08-01.

    Written to a temp file and moved into place so a crash mid-write can never
    leave a truncated feed at the URL Google fetches.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("id\tstore_code\tavailability\n")
        for r in rows:
            f.write(f"{r['id']}\t{r['store_code']}\t{r['availability']}\n")
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Build Nova's Google local inventory feed.")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output path")
    ap.add_argument("--report", action="store_true", help="print an audit summary")
    ap.add_argument("--compare", metavar="FILE", help="diff against an existing feed")
    ap.add_argument(
        "--min-rows", type=int, default=0, metavar="N",
        help="abort without writing if fewer than N rows were built. Use this "
             "in scheduled runs: a partial fetch that silently writes a short "
             "feed tells Google the missing products are no longer in store.",
    )
    args = ap.parse_args()

    session = requests.Session()
    session.auth = load_credentials()

    print("Reading WooCommerce…")
    rows, stats = build_rows(session)

    if not rows:
        sys.exit("Refusing to write an empty feed — Google would read it as "
                 "'everything out of stock'.")

    if args.min_rows and len(rows) < args.min_rows:
        sys.exit(
            f"Refusing to write: built {len(rows)} rows, expected at least "
            f"{args.min_rows}. This usually means the WooCommerce read was "
            f"partial. Leaving the previous feed in place is safer than "
            f"publishing a short one."
        )

    write_feed(rows, args.out)
    print(f"\nWrote {len(rows)} rows to {args.out}")

    if args.report:
        print("\nBreakdown:")
        for k in sorted(stats):
            print(f"  {k:<32} {stats[k]}")
        avail = Counter(r["availability"] for r in rows)
        print("\nAvailability:")
        for k, v in avail.most_common():
            print(f"  {k:<32} {v}")

    if args.compare:
        old = set()
        with open(args.compare, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i == 0 or not line.strip():
                    continue
                old.add(line.split("\t")[0].strip())
        new = {r["id"] for r in rows}
        print(f"\nCompared against {args.compare}:")
        print(f"  rows there / here          {len(old)} / {len(new)}")
        print(f"  in both                    {len(old & new)}")
        print(f"  only in the old file       {len(old - new)}")
        print(f"  new here                   {len(new - old)}")


if __name__ == "__main__":
    main()
