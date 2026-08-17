# Nova — Google local inventory feed

Generates the [local inventory feed](https://support.google.com/merchants/answer/14819809)
that Google Merchant Center needs for **Free local listings** — the products
that appear on the Google Business Profile / Maps listing.

The feed is served at:

```
https://tnt-design.github.io/nova-local-feed/nova-local-inventory.txt
```

That URL is registered in Merchant Center under
*Marketing methods → Free local listings → Thailand → Add inventory → Enter a link to your file*.

## Why this repo exists

The official **Google for WooCommerce** plugin produces the *primary* product
feed but cannot produce a *local inventory* feed — a known gap with an open
feature request. Free local listings needs both.

## How it works

Two scheduled jobs, split by how often the underlying data actually changes:

| Job | When | Reads | Touches the website? |
|---|---|---|---|
| `build-feed.yml` | twice daily, 01:00 + 13:00 ICT | cached map + Airtable | **no** |
| `refresh-map.yml` | weekly, Mon 00:00 ICT | WooCommerce | yes, paced |

GitHub Pages serves `docs/`. Google fetches the file at 03:00 ICT daily.

Nothing here writes to the website. All credentials are read-only.

### Why the map is cached

Building the id list costs about one request per variable product — roughly 330
per run. Run twice daily from a GitHub datacenter IP, that reads as scraping,
and on 2026-08-16 SiteGround's bot protection started answering with an HTTP 202
`sgcaptcha` challenge page instead of API data:

```
HTTP 202: <meta http-equiv="refresh" content="0;/.well-known/sgcaptcha/…">
```

That is the site defending itself, working as intended. The fix is to stop
generating the load, not to defeat the check.

*Which* SKUs exist changes rarely; *how many* are in stock changes daily. So the
expensive crawl runs weekly into `product_map.json`, and the daily job reads that
plus Airtable — about 5 seconds, and zero requests to nova-collection.com. The
daily workflow is not even given the WooCommerce secrets, so that stays true by
construction rather than by intention.

If a weekly refresh is ever blocked, the script falls back to the committed map
and still publishes a correct feed. A slightly stale id list beats a stale feed,
which is what reverts Merchant Center's "Add inventory" step.

To rebuild the map by hand from a trusted network:

```bash
python local_inventory_feed.py --refresh-map
```

## Two sources, each for the thing it actually knows

| Source | Supplies |
|---|---|
| WooCommerce | **which** items exist, and their feed ids |
| Airtable | **how many** of each are really in the shop |

Neither can do both, and using the wrong one for either half is how this goes
wrong quietly.

## The id rule — the part that fails silently

The `id` column must match the primary feed **exactly**, or the row is dropped
with no error and the product simply never appears.

Verified against the live account on 2026-08-15:

- ids take the form `gla_<wordpress_post_id>`
- variable products publish **one item per variation**, using the *variation's*
  id — **not** the parent product id
- simple products publish under their own id
- external/affiliate products are not in the primary feed and are skipped

Airtable cannot supply these ids. Its `🌐 Product ID (Website)` field is
populated on only ~547 of 894 records and holds *parent* product ids, so it
matched only 80 of the 414 feed items.

## The availability rule

Availability comes from Airtable's **🎁 Total In Stock** rollup — the real count
of pieces in the shop — joined to WooCommerce by SKU (414/414 match).

It deliberately does *not* come from:

- WooCommerce `stock_status` — the site does not manage stock at all
  (`manage_stock` is `False` and `stock_quantity` is `None` on every item), so
  that flag is hand-set and stale.
- Airtable's `Stock Status` single-select — also hand-maintained, and it lags
  real stock movement.

Measured 2026-08-15, the difference is not cosmetic:

| Source | In stock | Out of stock |
|---|---|---|
| hand-set flag | 409 | 5 |
| real quantity | 377 | 37 |

29 items flagged "In Stock" had a true quantity of zero. Google spot-checks
in-store inventory as part of Free local listings, so overstating availability
is the failure mode that matters most here.

## Running it by hand

```bash
python local_inventory_feed.py --report
```

Credentials come from `WC_KEY`, `WC_SECRET` and `AIRTABLE_TOKEN` environment
variables, or from a local `nova_config.py` (gitignored, and not present in this
repo — it lives in the WooCommerce Sync folder, so set the env vars instead when
running from here).

Useful flags:

| Flag | Purpose |
|---|---|
| `--report` | print a breakdown by product type and availability |
| `--compare FILE` | diff the generated ids against an older feed |
| `--min-rows N` | abort rather than publish a short feed |
| `--out PATH` | choose the output path |

## Safety notes

- `--min-rows 350` in CI stops a partial WooCommerce read from publishing a
  truncated feed. A short feed tells Google the missing products are no longer
  in store, which is worse than a stale one.
- The feed is written to a temp file and moved into place, so an interrupted
  run cannot leave a half-written file at the served URL.
- The workflow only commits when the content actually changed, so the history
  stays readable as a record of real stock movement.
