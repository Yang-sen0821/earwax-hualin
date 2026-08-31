# -*- coding: utf-8 -*-
"""庫存頁／庫存報表「合計」欄 ＋ _apply_delta 新建列自動帶 unit 測試（2026-08-31）。

背景：線上庫存修正把差額登在 pr 分類，森哥看「一般」欄仍是舊數字以為未修正 → 加合計欄。

執行：python inventory_total_test.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = os.path.join(tempfile.gettempdir(), "flora_court_invtotal_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"

import init_db  # noqa: E402
init_db.main()

import inventory_service as inv  # noqa: E402
from db import get_session, Product, InventoryBalance, User  # noqa: E402
from app import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {}  {}".format("PASS" if ok else "FAIL", name, detail))


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


db = get_session()
mask = db.query(Product).filter_by(is_packaging=False).order_by(Product.id).first()
owner = db.query(User).filter_by(username="owner").first()

# 造資料：loose_piece normal=10、pr=5（seed 沒有 loose_piece/pr 列 → 走 _apply_delta 新建列）
for cat in ("normal", "reserved", "pr", "trial", "scrap"):
    b = (db.query(InventoryBalance)
         .filter_by(product_id=mask.id, inventory_pool="loose_piece", stock_category=cat).first())
    if b is not None:
        db.delete(b)
db.flush()
inv._apply_delta(db, mask.id, "loose_piece", "normal", 10, "RESTOCK", "test",
                 created_by=owner.id, allow_create_on_increase=True)
inv._apply_delta(db, mask.id, "loose_piece", "pr", 5, "RESTOCK", "test",
                 created_by=owner.id, allow_create_on_increase=True)
db.commit()

# 1) 新建列 unit 自動帶入
pr_row = (db.query(InventoryBalance)
          .filter_by(product_id=mask.id, inventory_pool="loose_piece", stock_category="pr").first())
check("1a) _apply_delta 新建 loose_piece 列 unit=piece", pr_row is not None and pr_row.unit == "piece",
      "unit=" + str(pr_row.unit if pr_row else None))
check("1b) POOL_UNIT 三池對應完整",
      inv.POOL_UNIT == {"boxed": "box", "loose_piece": "piece", "paper_bag": "bag"})

# 2) 桌面庫存頁：裸片列合計 15（一般 10 + 公關 5）
c = app.test_client()
login(c, "owner", "owner123")
html = c.get("/inventory/").get_data(as_text=True)
check("2a) 庫存頁表頭有「合計」", ">合計</th>" in html)
check("2b) 庫存頁有說明小字（合計＝一般＋預留＋公關＋試用＋損耗）",
      "合計＝一般＋預留＋公關＋試用＋損耗" in html)
import re  # noqa: E402
totals = [int(x) for x in re.findall(r'class="pool-total-cell"[^>]*>\s*(\d+)\s*<', html)]
check("2c) 裸片池合計欄 = 15（normal 10 + pr 5）", 15 in totals, "totals=" + str(totals))
# 盒裝 seed：normal 1271 + reserved 35 = 1306
check("2d) 盒裝池合計欄 = 1306（seed normal 1271 + reserved 35）", 1306 in totals)

# 3) 庫存報表：合計欄
r = c.get("/reports/inventory")
html_r = r.get_data(as_text=True)
check("3a) 報表 200", r.status_code == 200, "status=" + str(r.status_code))
check("3b) 報表表頭有「合計」在「已售」前",
      html_r.find(">合計</th>") != -1 and html_r.find(">合計</th>") < html_r.find(">已售</th>"))
from blueprints.reports import _inventory_report  # noqa: E402
rows = _inventory_report(get_session())
lp = [x for x in rows if x["product_id"] == mask.id and x["inventory_pool"] == "loose_piece"]
check("3c) _inventory_report 裸片列 total=15", len(lp) == 1 and lp[0]["total"] == 15,
      "row=" + str(lp[0] if lp else None))
check("3d) 每列皆有 total 鍵且 = 五分類加總",
      all(x["total"] == x["normal"] + x["reserved"] + x["pr"] + x["trial"] + x["scrap"] for x in rows))

print("\n" + "=" * 60)
print("通過 {}/{}".format(sum(RESULTS), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
