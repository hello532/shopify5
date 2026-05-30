"""Verify dedupe end-to-end."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from v3 import api, db

sql = """
SELECT p.id, p.title, p.shop_domain, p.handle, p.price_usd, sc.composite_score, d.decision
FROM products p
JOIN decisions d ON d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)
LEFT JOIN scores sc ON sc.id = (SELECT MAX(id) FROM scores WHERE product_id = p.id)
WHERE p.shop_domain IS NOT NULL
  AND p.shop_domain NOT IN ('test.com','example.com')
  AND p.title IS NOT NULL AND TRIM(p.title) != ''
"""
with db.conn() as c:
    rows = [dict(r) for r in c.execute(sql).fetchall()]
print(f"原始 rows: {len(rows)}")
deduped = api._dedupe_same_product(rows)
print(f"去重后:    {len(deduped)}")
print(f"被合并:    {len(rows) - len(deduped)} 个 listing")
print()
print("=== 有同款的代表卡 ===")
shown = 0
for r in deduped:
    if r.get("duplicates"):
        shown += 1
        print(f"  [{r['decision']:<7}] {r['shop_domain']}/{r['handle']}")
        print(f"          title: {(r['title'] or '')[:60]}")
        print(f"          +{len(r['duplicates'])} 同款:")
        for d in r["duplicates"]:
            print(f"             · {d['shop_domain']}/{d['handle']} [{d['decision']}]")
        if shown >= 8:
            break

print()
print("=== 决策分布(去重后) ===")
from collections import Counter
ct = Counter(r["decision"] for r in deduped)
for k, v in ct.items():
    print(f"  {k}: {v}")
