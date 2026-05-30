"""Diagnose duplicate products in v3."""
from v3 import db

with db.conn() as c:
    print("=== 1) 同标题重复(可能同品不同 shop) ===")
    rows = c.execute("""
        SELECT title, COUNT(*) AS n,
               GROUP_CONCAT(DISTINCT shop_domain) AS shops,
               GROUP_CONCAT(id) AS ids
        FROM products
        WHERE title IS NOT NULL AND title != ''
        GROUP BY LOWER(TRIM(title))
        HAVING n > 1
        ORDER BY n DESC LIMIT 15
    """).fetchall()
    for r in rows:
        print(f"  ×{r['n']}  ids=[{r['ids']}]")
        print(f"          title: {r['title'][:70]}")
        print(f"          shops: {r['shops']}")

    print("\n=== 2) 同 shop 下重复 handle/title ===")
    rows = c.execute("""
        SELECT shop_domain, handle, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
        FROM products WHERE shop_domain IS NOT NULL
        GROUP BY shop_domain, handle HAVING n > 1 LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"  ×{r['n']}  {r['shop_domain']}/{r['handle']}  ids={r['ids']}")

    print("\n=== 3) 单 product 多 decision 记录(应只显示最新) ===")
    rows = c.execute("""
        SELECT product_id, COUNT(*) AS n FROM decisions GROUP BY product_id
        HAVING n > 1 ORDER BY n DESC LIMIT 5
    """).fetchall()
    for r in rows:
        print(f"  pid={r['product_id']}: {r['n']} 条")

    print("\n=== 4) API 实际返回 ===")
    rows = c.execute("""
        SELECT p.id, p.title, p.shop_domain
        FROM products p
        JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
        ORDER BY p.id LIMIT 30
    """).fetchall()
    seen_titles = {}
    for r in rows:
        key = (r['title'] or '').lower().strip()
        seen_titles.setdefault(key, []).append((r['id'], r['shop_domain']))
    dups = {k: v for k, v in seen_titles.items() if len(v) > 1}
    if dups:
        print(f"  API 返回的 30 条里, {len(dups)} 个标题重复:")
        for title, instances in list(dups.items())[:5]:
            print(f"    × {title[:60]}")
            for pid, sd in instances:
                print(f"        pid={pid} shop={sd}")
    else:
        print("  前30条无标题重复")

    print("\n=== 5) 总览 ===")
    n_total = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    n_with_dec = c.execute("SELECT COUNT(DISTINCT product_id) FROM decisions").fetchone()[0]
    n_dec_rows = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    print(f"  products: {n_total}")
    print(f"  decisions: {n_dec_rows} 行, 涉及 {n_with_dec} 个 distinct product")
    n_distinct_titles = c.execute("SELECT COUNT(DISTINCT LOWER(TRIM(title))) FROM products WHERE title IS NOT NULL").fetchone()[0]
    print(f"  distinct titles: {n_distinct_titles}")
    print(f"  → 标题级重复率: {(n_total - n_distinct_titles)/n_total*100:.1f}%")
