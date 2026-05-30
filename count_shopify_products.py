#!/usr/bin/env python3
"""获取 Shopify 域名的实际产品总数"""
import json, ssl, time, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DOMAINS_FILE = Path.home() / "Desktop/amazon/shopify_domains.txt"
OUTPUT_FILE = Path.home() / "Desktop/amazon/shopify_domains_counted.json"
BATCH_FILE = Path.home() / "Desktop/amazon/competitors_batch.txt"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
TIMEOUT = 15
MAX_WORKERS = 60

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def get_product_count(domain):
    """获取站点产品总数（分页累加）"""
    total = 0
    page = 1
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            resp = opener.open(req, timeout=TIMEOUT)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            products = data.get("products", [])
            if not products:
                break
            total += len(products)
            if len(products) < 250:
                break
            page += 1
            if page > 20:  # 最多5000产品
                total = f"{total}+"
                break
        except Exception as e:
            return (domain, -1, str(e)[:60])
    return (domain, total, "ok")

def main():
    domains = [d.strip() for d in DOMAINS_FILE.read_text().splitlines() if d.strip()]
    print(f"Shopify 域名: {len(domains)}")
    
    results = []
    done = 0
    start = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(get_product_count, d): d for d in domains}
        for f in as_completed(futures):
            domain, count, reason = f.result()
            done += 1
            results.append({"domain": domain, "count": count, "reason": reason})
            if done % 50 == 0:
                sys.stdout.write(f"\r进度: {done}/{len(domains)} | 耗时: {time.time()-start:.0f}s")
    
    elapsed = time.time() - start
    
    # 分类
    valid = [r for r in results if r["count"] > 0]
    errors = [r for r in results if r["count"] == -1]
    empty = [r for r in results if r["count"] == 0]
    
    # 按产品数排序
    valid.sort(key=lambda x: x["count"] if isinstance(x["count"], int) else 9999, reverse=True)
    
    # 写入报告
    OUTPUT_FILE.write_text(json.dumps({
        "total": len(domains),
        "with_products": len(valid),
        "empty": len(empty),
        "errors": len(errors),
        "elapsed": round(elapsed, 1),
        "domains": valid,
        "error_sample": errors[:20]
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    
    # 写入 competitors_batch.txt（只写有产品的）
    batch_content = "\n".join(r["domain"] for r in valid)
    BATCH_FILE.write_text(batch_content, encoding="utf-8")
    
    print(f"\n\n=== 完成 ===")
    print(f"扫描: {len(domains)} | 耗时: {elapsed:.0f}s")
    print(f"有产品: {len(valid)} | 空站: {len(empty)} | 错误: {len(errors)}")
    print(f"\nTOP 30 Shopify 站（按产品数）:")
    for r in valid[:30]:
        print(f"  {r['domain']:45s} {r['count']:>5} products")
    print(f"\n已写入 competitors_batch.txt: {len(valid)} 个域名")
    print(f"报告: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
