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

TWO SOURCES, EACH FOR THE THING IT ACTUALLY KNOWS
-------------------------------------------------
  * WooCommerce  -> WHICH items exist and what their feed ids are.
  * Airtable     -> HOW MANY of each are actually in the shop.

Neither one can do both, and using the wrong one for either half is how this
goes wrong quietly.

THE ID RULE (this is the part that fails silently)
--------------------------------------------------
The `id` column MUST match the primary feed byte-for-byte or the row is
silently dropped — no error, no warning, the product simply never appears.

Verified against the live account on 2026-08-15:
  * The plugin publishes ids of the form  gla_<wordpress_post_id>
  * For VARIABLE products it publishes one item PER VARIATION, using the
    VARIATION's post id — NOT the parent product id.
  * Sampled live Merchant Center ids (gla_14681, gla_14480, gla_17194,
    gla_14677, gla_14521, gla_14768) are all variation ids. None is a parent.
  * Simple products publish under their own product id.
  * External/affiliate products are absent from the primary feed. Including
    one put the count at 415 against Merchant Center's 414; skipping it
    matches exactly.

Airtable cannot supply these ids. Its "🌐 Product ID (Website)" field is
populated on only ~547 of 894 records and holds PARENT product ids, so it
matched just 80 of the 414 feed items. WooCommerce is the only source for ids.

THE AVAILABILITY RULE
---------------------
Availability comes from Airtable's "🎁 Total In Stock" rollup — the real
count of pieces in the shop — joined to WooCommerce by SKU.

It deliberately does NOT come from:
  * WooCommerce `stock_status` — the site does not manage stock at all
    (`manage_stock` is False and `stock_quantity` is None on every item), so
    that flag is hand-set and stale.
  * Airtable's "Stock Status" single-select — also hand-maintained, and it
    lags real stock movement.

Measured on 2026-08-15, the difference is not cosmetic:
    by hand-set flag   409 in stock /  5 out of stock
    by real quantity   377 in stock / 37 out of stock
29 items flagged "In Stock" had a true quantity of zero. Google physically
verifies in-store inventory as part of Free local listings, so overstating
availability is the failure mode that matters most here.

CREDENTIALS
-----------
All read-only:
  WC_KEY / WC_SECRET   WooCommerce REST (ids + SKUs)
  AIRTABLE_TOKEN       Airtable (stock quantities)
No WordPress app password. Nothing here writes to the website or to Airtable.

USAGE
-----
    py local_inventory_feed.py                 # write the feed
    py local_inventory_feed.py --report        # write it + print an audit
    py local_inventory_feed.py --compare FILE  # diff against an older feed
    py local_inventory_feed.py --min-rows 350  # refuse to write a short feed
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

# Airtable — Product Catalog (Admin) base, 🛒 Product table.
# Field IDs rather than names, so a rename in Airtable cannot break this.
AT_BASE = "appzPOhUnZdSsfiQr"
AT_TABLE = "tblYCHM9TTV3RPOHb"
AT_FIELD_SKU = "fldRaSxoPcb4IpdPS"   # Product Code (SKU) / name
AT_FIELD_QTY = "fldXtKfE51caGtml6"   # 🎁 Total In Stock  (rollup)

IN_STOCK = "in stock"
OUT_OF_STOCK = "out of stock"

DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nova-local-inventory.txt"
)

PER_PAGE = 100
TIMEOUT = 90


def load_credentials():
    """Env vars win, so the same file runs locally and on a cloud runner."""
    def pick(name):
        return os.environ.get(name) or getattr(cfg, name, None)

    key, secret, token = pick("WC_KEY"), pick("WC_SECRET"), pick("AIRTABLE_TOKEN")
    missing = [n for n, v in
               (("WC_KEY", key), ("WC_SECRET", secret), ("AIRTABLE_TOKEN", token))
               if not v]
    if missing:
        sys.exit(
            f"Missing {', '.join(missing)}. Set them as environment variables, "
            f"or put them in a local nova_config.py next to this script."
        )
    return (key, secret), token


# ─────────────────────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────────────────────

def _get(session, url, headers=None, **params):
    """GET with a few retries — this runs unattended, so a blip must not
    produce a truncated feed. A short feed is worse than no feed: Google
    reads missing rows as 'no longer in store'."""
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after 4 attempts — {last}")


def _wc_pages(session, url, **params):
    out, page = [], 1
    while True:
        r = _get(session, url, per_page=PER_PAGE, page=page, **params)
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", 0) or 0)
        if total_pages and page >= total_pages:
            break
        page += 1
    return out


def fetch_feed_items(session, verbose=True):
    """Every item the primary feed contains, as (id, sku, kind).

    One row per published VARIATION for variable products, one per published
    SIMPLE product, and nothing for external products.
    """
    products = _wc_pages(session, f"{API}/products", status="publish")
    if verbose:
        print(f"  published parent-level products: {len(products)}")

    items, skipped = [], Counter()
    variable = [p for p in products if p.get("type") == "variable"]

    for p in products:
        ptype = p.get("type")
        if ptype == "variable":
            continue          # the parent id is NOT in the primary feed
        if ptype == "external":
            skipped["external"] += 1
            continue
        items.append({"id": p["id"], "sku": p.get("sku") or "", "kind": "simple"})

    for i, p in enumerate(variable, 1):
        if verbose and (i % 50 == 0 or i == len(variable)):
            print(f"  variations: {i}/{len(variable)} parents expanded")
        for v in _wc_pages(session, f"{API}/products/{p['id']}/variations",
                           status="publish"):
            items.append({"id": v["id"], "sku": v.get("sku") or "",
                          "kind": "variation"})

    return items, skipped


def fetch_stock_by_sku(token, verbose=True):
    """SKU (lowercased) -> real quantity in the shop, from Airtable."""
    session = requests.Session()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.airtable.com/v0/{AT_BASE}/{AT_TABLE}"

    by_sku, offset, pages = {}, None, 0
    while True:
        params = {
            "pageSize": PER_PAGE,
            "returnFieldsByFieldId": "true",
            "fields[]": [AT_FIELD_SKU, AT_FIELD_QTY],
        }
        if offset:
            params["offset"] = offset
        r = _get(session, url, headers=headers, **params)
        data = r.json()
        for rec in data.get("records", []):
            f = rec.get("fields", {})
            sku = (f.get(AT_FIELD_SKU) or "").strip().lower()
            if sku:
                by_sku[sku] = f.get(AT_FIELD_QTY) or 0
        pages += 1
        offset = data.get("offset")
        if not offset:
            break
    if verbose:
        print(f"  Airtable SKUs with stock figures: {len(by_sku)} "
              f"({pages} pages)")
    return by_sku


# ─────────────────────────────────────────────────────────────
# BUILD
# ─────────────────────────────────────────────────────────────

def build_rows(items, stock_by_sku):
    """Join the two sources into feed rows. Returns (rows, stats, unmatched)."""
    rows, stats, unmatched = [], Counter(), []
    seen = set()

    for it in items:
        sku = (it["sku"] or "").strip().lower()
        if sku in stock_by_sku:
            qty = stock_by_sku[sku]
            availability = IN_STOCK if qty > 0 else OUT_OF_STOCK
            stats[f"{it['kind']}:{availability}"] += 1
        else:
            # No Airtable row for this SKU. Do NOT guess "in stock" — claiming
            # a piece is in the shop when we cannot confirm it is exactly what
            # inventory verification catches.
            availability = OUT_OF_STOCK
            unmatched.append(it["sku"])
            stats["UNMATCHED-SKU"] += 1

        gid = f"{ID_PREFIX}{it['id']}"
        if gid in seen:
            stats["DUPLICATE-DROPPED"] += 1
            continue
        seen.add(gid)
        rows.append({"id": gid, "store_code": STORE_CODE,
                     "availability": availability})

    return rows, stats, unmatched


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
    ap.add_argument(
        "--max-unmatched", type=int, default=25, metavar="N",
        help="abort if more than N feed SKUs have no Airtable stock row. A "
             "spike here means the SKU join has broken, not that the shop "
             "emptied (default 25).",
    )
    args = ap.parse_args()

    wc_auth, at_token = load_credentials()
    wc = requests.Session()
    wc.auth = wc_auth

    print("Reading WooCommerce (feed ids)…")
    items, skipped = fetch_feed_items(wc)
    print("Reading Airtable (real stock)…")
    stock_by_sku = fetch_stock_by_sku(at_token)

    rows, stats, unmatched = build_rows(items, stock_by_sku)

    if not rows:
        sys.exit("Refusing to write an empty feed — Google would read it as "
                 "'everything out of stock'.")

    if args.min_rows and len(rows) < args.min_rows:
        sys.exit(
            f"Refusing to write: built {len(rows)} rows, expected at least "
            f"{args.min_rows}. This usually means a source read was partial. "
            f"Leaving the previous feed in place is safer than publishing a "
            f"short one."
        )

    if len(unmatched) > args.max_unmatched:
        sys.exit(
            f"Refusing to write: {len(unmatched)} feed SKUs had no Airtable "
            f"stock row (limit {args.max_unmatched}). The SKU join has "
            f"probably broken. First few: {unmatched[:10]}"
        )

    write_feed(rows, args.out)
    print(f"\nWrote {len(rows)} rows to {args.out}")

    avail = Counter(r["availability"] for r in rows)
    print(f"  in stock: {avail[IN_STOCK]}   out of stock: {avail[OUT_OF_STOCK]}")
    if unmatched:
        print(f"  ⚠ {len(unmatched)} SKUs had no Airtable stock row "
              f"(listed out of stock): {unmatched[:10]}")
    for k, v in skipped.items():
        print(f"  skipped {k}: {v}")

    if args.report:
        print("\nBreakdown:")
        for k in sorted(stats):
            print(f"  {k:<32} {stats[k]}")

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
