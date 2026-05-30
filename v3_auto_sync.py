#!/usr/bin/env python3
"""V3 域名自动同步后台进程 —— Step 1 全量扫描分类，Step 2+ 逐步抓取。
"""
import urllib.request, json, time, sys, os
from datetime import datetime

API = "http://127.0.0.1:8001/api/v3/sync-domains"
WAIT_BETWEEN = 2
MAX_BATCHES = 0

def call_sync():
    try:
        req = urllib.request.Request(f"{API}?max_scrape=100", method="POST")
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

def main():
    batch = 0
    total_scraped = 0
    total_non_shopify = 0
    while True:
        batch += 1
        ts = datetime.now().strftime("%H:%M:%S")
        result = call_sync()

        if not result.get("ok"):
            print(f"[{ts}] batch {batch}: ERROR - {result.get('error', 'unknown')}")
            sys.stdout.flush()
            time.sleep(10)
            continue

        if not result.get("synced"):
            print(f"[{ts}] batch {batch}: DONE - {result.get('reason', 'queue empty')}")
            sys.stdout.flush()
            break

        s = result.get("stats", {})
        total_scraped += s.get('scraped_this_batch', 0)
        total_non_shopify += s.get('non_shopify_this_batch', 0)
        print(f"[{ts}] batch {batch}: "
              f"shopify_done={s.get('shopify_offset',0)}/{s.get('shopify_total',0)} "
              f"non_shopify_done={s.get('non_shopify_offset',0)}/{s.get('non_shopify_total',0)} "
              f"scraped={s.get('scraped_this_batch',0)} "
              f"non_shopify={s.get('non_shopify_this_batch',0)} "
              f"failed={s.get('failed',0)} "
              f"new_prods={s.get('total_new_products',0)} "
              f"total_scraped={total_scraped}")
        sys.stdout.flush()

        scraped = result.get("scraped", [])
        for item in scraped[:5]:
            print(f"  + {item['domain']} ({item['product_count']} products)")
        if len(scraped) > 5:
            print(f"  ... and {len(scraped)-5} more")
        sys.stdout.flush()

        shopify_rem = s.get('shopify_remaining', 0)
        non_shopify_rem = s.get('non_shopify_remaining', 0)
        if shopify_rem == 0 and non_shopify_rem == 0:
            print(f"[{ts}] All queues exhausted.")
            break

        if MAX_BATCHES > 0 and batch >= MAX_BATCHES:
            print(f"[{ts}] Reached max batches ({MAX_BATCHES}). Stopping.")
            break

        time.sleep(WAIT_BETWEEN)

    print(f"\nSync complete after {batch} batches. Total scraped: {total_scraped}")

if __name__ == "__main__":
    print(f"V3 Auto Domain Sync — targeting {API}")
    print(f"Press Ctrl+C to stop early.\n")
    try:
        main()
    except KeyboardInterrupt:
        print(f"\nStopped by user after batch.")