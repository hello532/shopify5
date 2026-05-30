#!/usr/bin/env python3
"""从域名列表中批量检测 Shopify 站点（通过 /products.json）"""
import json, ssl, time, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DOMAINS_FILE = Path.home() / ".hermes/config/monitor_domains.json"
OUTPUT_FILE = Path.home() / "Desktop/amazon/shopify_domains.txt"
REPORT_FILE = Path.home() / "Desktop/amazon/shopify_detect_report.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
TIMEOUT = 12
MAX_WORKERS = 80

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def check_shopify(domain):
    """尝试 /products.json，返回 (domain, is_shopify, product_count, reason)"""
    url = f"https://{domain}/products.json?limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    try:
        resp = opener.open(req, timeout=TIMEOUT)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if isinstance(data, dict) and "products" in data:
            count = len(data["products"])
            # 取首页 count 不代表总数，但证明是 Shopify
            return (domain, True, count, "ok")
        return (domain, False, 0, "no products key")
    except urllib.request.HTTPError as e:
        return (domain, False, 0, f"HTTP {e.code}")
    except Exception as e:
        return (domain, False, 0, str(e)[:80])

def main():
    domains = json.loads(DOMAINS_FILE.read_text())
    print(f"总域名: {len(domains)}")
    
    shopify_domains = []
    non_shopify = []
    errors = []
    done = 0
    start = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_shopify, d): d for d in domains}
        for f in as_completed(futures):
            domain, is_shop, count, reason = f.result()
            done += 1
            if is_shop:
                shopify_domains.append({"domain": domain, "products": count})
                sys.stdout.write(f"\r✓ Shopify: {len(shopify_domains)} | 扫描: {done}/{len(domains)}")
            elif "HTTP 4" not in reason and "HTTP 5" not in reason:
                non_shopify.append({"domain": domain, "reason": reason})
            else:
                errors.append({"domain": domain, "reason": reason})
            
            if done % 500 == 0:
                sys.stdout.write(f"\r进度: {done}/{len(domains)} | Shopify: {len(shopify_domains)} | 耗时: {time.time()-start:.0f}s")
    
    elapsed = time.time() - start
    
    # 按产品数排序
    shopify_domains.sort(key=lambda x: x["products"], reverse=True)
    
    # 写入文件
    OUTPUT_FILE.write_text("\n".join(d["domain"] for d in shopify_domains), encoding="utf-8")
    REPORT_FILE.write_text(json.dumps({
        "total_scanned": len(domains),
        "shopify_count": len(shopify_domains),
        "non_shopify_count": len(non_shopify),
        "error_count": len(errors),
        "elapsed_seconds": round(elapsed, 1),
        "shopify_domains": shopify_domains,
        "sample_non_shopify": non_shopify[:20],
        "sample_errors": errors[:20]
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    
    print(f"\n\n=== 完成 ===")
    print(f"扫描: {len(domains)} | 耗时: {elapsed:.0f}s")
    print(f"Shopify: {len(shopify_domains)} 个")
    print(f"非Shopify: {len(non_shopify)} | 错误: {len(errors)}")
    print(f"\nShopify 域名已写入: {OUTPUT_FILE}")
    print(f"完整报告: {REPORT_FILE}")
    print(f"\nTOP 20 Shopify 站:")
    for d in shopify_domains[:20]:
        print(f"  {d['domain']:40s} {d['products']} products")

if __name__ == "__main__":
    main()
