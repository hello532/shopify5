"""Verify end-to-end launch."""
import os
os.environ["SHOPIFY_SHOP"] = "demo.myshopify.com"
os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"] = "dummy"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v3.launch import launch
res = launch(6, dry_run=True)
print(f"overall ok: {res['ok']}")
print()
print("=== steps ===")
for name, st in res["steps"].items():
    mark = "✓" if st.get("ok") else "✗"
    print(f"  {mark} {name}: {[k for k in st.keys() if k != 'would_send']}")
print()
print(f"=== creatives ({len(res['creatives'])}) ===")
for cv in res["creatives"]:
    print(f"  · {cv['variant_type']}: {(cv['hook'] or '')[:60]}")
print()
print(f"=== ads_manager_paste 字段: {'ads_manager_paste' in res} ===")
