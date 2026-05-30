#!/usr/bin/env python3
"""批量产品监控 — 用 shopify_ultimate.py 抓取+检测变化"""
import importlib.util, json, sys, time
from pathlib import Path
from datetime import datetime

import os as _os
_HOME = Path(_os.environ.get("HOME", "")).expanduser()
if not _HOME.exists() or not (_HOME / "Desktop" / "amazon").exists():
    _HOME = Path("/Users/doi")
BASE = _HOME / "Desktop/amazon"
BATCH = BASE / "competitors_batch.txt"
WATCHLIST = BASE / "output/shopify_monitor/competitors_watchlist.json"
OUTPUT = BASE / "output/shopify_monitor"
OUTPUT.mkdir(parents=True, exist_ok=True)

# 加载模块 + 注入 USER_AGENT
spec = importlib.util.spec_from_file_location('shopify_ultimate', str(BASE / 'shopify_ultimate.py'))
mod = importlib.util.module_from_spec(spec)
mod.USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
spec.loader.exec_module(mod)

def main():
    # 优先从 watchlist.json 加载，fallback 到 batch.txt
    domains = []
    if WATCHLIST.exists():
        try:
            data = json.loads(WATCHLIST.read_text(encoding="utf-8"))
            domains = [item["domain"] for item in data if isinstance(item, dict)]
        except:
            pass
    if not domains and BATCH.exists():
        domains = [l.strip() for l in BATCH.read_text().splitlines() if l.strip()]
    
    if not domains:
        print("❌ 无监控域名 (competitors_watchlist.json 和 competitors_batch.txt 都为空)")
        return
    
    print(f"监控域名: {len(domains)}")
    
    results = []
    start = time.time()
    
    for i, domain in enumerate(domains, 1):
        print(f"\n[{i}/{len(domains)}] {domain}")
        try:
            session = mod.get_session()
            # 旧快照
            old_snap = mod.load_snapshot(domain)
            old_products = old_snap.get("products", {})
            print(f"  旧快照: {len(old_products)} 产品")
            
            # 抓取
            raw_products = mod.fetch_all_products(domain, session)
            if not raw_products:
                results.append({"domain": domain, "total": 0, "new": 0, "removed": 0, "price": 0, "status": "no_products"})
                print(f"  无产品")
                continue
            
            # 检测变化
            changes = mod.detect_changes(domain, raw_products)
            new_ids = changes.get("new_products", [])
            
            # 真实价格变动
            real_price = []
            for up in changes.get("updated_products", []):
                if up.get("old_price") != up.get("new_price"):
                    real_price.append(up)
            
            # removed
            current_ids = {str(p.get("id")) for p in raw_products}
            old_ids = set(old_products.keys())
            removed = old_ids - current_ids
            
            # 导出 Excel
            mod.monitor_monitor_export_excel(domain, raw_products)
            
            # 保存快照
            parsed = [mod.parse_product(p) for p in raw_products]
            mod.save_snapshot(domain, parsed)
            
            results.append({
                "domain": domain,
                "total": len(raw_products),
                "new": len(new_ids),
                "removed": len(removed),
                "price": len(real_price),
                "status": "ok"
            })
            print(f"  总计: {len(raw_products)} | 新品: {len(new_ids)} | 下架: {len(removed)} | 改价: {len(real_price)}")
            
        except Exception as e:
            results.append({"domain": domain, "total": 0, "new": 0, "removed": 0, "price": 0, "status": f"error: {str(e)[:80]}"})
            print(f"  ERROR: {e}")
    
    elapsed = time.time() - start
    
    # 保存报告
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = OUTPUT / f"batch_monitor_{ts}.json"
    report_file.write_text(json.dumps({
        "timestamp": ts,
        "elapsed": round(elapsed, 1),
        "results": results
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    
    # 汇总
    ok = [r for r in results if r["status"] == "ok"]
    total_p = sum(r["total"] for r in ok)
    total_new = sum(r["new"] for r in ok)
    total_removed = sum(r["removed"] for r in ok)
    total_price = sum(r["price"] for r in ok)
    
    print(f"\n{'='*50}")
    print(f"完成 | {elapsed:.0f}s | {len(ok)}/{len(domains)} 成功")
    print(f"产品总数: {total_p} | 新品: {total_new} | 下架: {total_removed} | 改价: {total_price}")
    print(f"报告: {report_file}")
    
    # 变化摘要
    changed = [r for r in ok if r["new"] > 0 or r["removed"] > 0 or r["price"] > 0]
    if changed:
        print(f"\n有变化的站点:")
        for r in changed:
            parts = []
            if r["new"]: parts.append(f"新品{r['new']}")
            if r["removed"]: parts.append(f"下架{r['removed']}")
            if r["price"]: parts.append(f"改价{r['price']}")
            print(f"  {r['domain']}: {', '.join(parts)}")
    else:
        print(f"\n所有站点无变化")

if __name__ == "__main__":
    main()
