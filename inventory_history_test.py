# -*- coding: utf-8 -*-
"""CR-9 驗收：「歷史輸入」與「已消耗」欄（2026-08-31，森哥 20:47 原話：多一欄歷史輸入庫存）。

口徑：歷史輸入(product, pool) = 五分類現有合計 + 累計消耗
  累計消耗 = |SALE + SALE_REVERSAL 淨額| + 出庫類（GIFT/TRIAL/PR/KOL_SAMPLE/STAFF_USE/INSTORE_USE/
             SCRAP/SCRAP_LOSS）與 PAPERBAG_OUT 的 |負 qty_delta| 加總
  不計：SPLIT_BOX / RELEASE_RESERVE / ADJUSTMENT / SEED / PURCHASE / RESTOCK / IMPORT
        （已反映在現有合計；轉移／校正改變合計時，歷史輸入跟著合計變——這是預期）

執行：python inventory_history_test.py
"""
import os
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = os.path.join(tempfile.gettempdir(), "flora_court_invhistory_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP.replace("\\", "/")
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
init_db.main()

import inventory_service as inv  # noqa: E402
from db import get_session, Product, InventoryBalance, InventoryMovement, Order, User  # noqa: E402
from blueprints.reports import _inventory_report  # noqa: E402
from app import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {}  {}".format("PASS" if ok else "FAIL", name, detail))


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def fresh():
    s = get_session()
    s.expire_all()
    return s


def pool_total(pid, pool):
    db = fresh()
    return sum(int(b.qty or 0) for b in db.query(InventoryBalance)
               .filter_by(product_id=pid, inventory_pool=pool).all())


def consumed(pid, pool):
    return inv.consumed_by_pool(fresh()).get((pid, pool), 0)


def hist(pid, pool):
    return pool_total(pid, pool) + consumed(pid, pool)


def report_row(pid, pool):
    rows = [x for x in _inventory_report(fresh())
            if x["product_id"] == pid and x["inventory_pool"] == pool]
    return rows[0] if rows else None


def page_cells(html):
    """庫存頁每池列的 (合計, 歷史輸入, 已消耗) 三元組清單。"""
    tot = [int(x) for x in re.findall(r'class="pool-total-cell"[^>]*>\s*(\d+)\s*<', html)]
    his = [int(x) for x in re.findall(r'class="pool-hist-cell"[^>]*>\s*(\d+)\s*<', html)]
    con = [int(x) for x in re.findall(r'class="pool-consumed-cell"[^>]*>\s*(\d+)\s*<', html)]
    return list(zip(tot, his, con))


db = get_session()
mask = db.query(Product).filter_by(sku="FC-MASK-001").first()
pkg = db.query(Product).filter_by(is_packaging=True).first()
owner = db.query(User).filter_by(username="owner").first()
MID, PID = mask.id, pkg.id
PKG_NAME = pkg.name

# ---- 造資料：裸片池清空後 seed normal=10、pr=5（RESTOCK 不計入消耗）----
for b in db.query(InventoryBalance).filter_by(product_id=MID, inventory_pool="loose_piece").all():
    db.delete(b)
db.flush()
inv._apply_delta(db, MID, "loose_piece", "normal", 10, "RESTOCK", "test",
                 created_by=owner.id, allow_create_on_increase=True)
inv._apply_delta(db, MID, "loose_piece", "pr", 5, "RESTOCK", "test",
                 created_by=owner.id, allow_create_on_increase=True)
db.commit()

# 0) 初始：合計 15、已消耗 0、歷史輸入 15（RESTOCK / SEED 不算消耗）
check("0a) 初始 裸片合計=15", pool_total(MID, "loose_piece") == 15)
check("0b) 初始 已消耗=0（RESTOCK/SEED 不計）", consumed(MID, "loose_piece") == 0)
check("0c) 初始 歷史輸入=15", hist(MID, "loose_piece") == 15)
box_hist0 = hist(MID, "boxed")
box_total0 = pool_total(MID, "boxed")
check("0d) 盒裝 seed 只有 SEED movement → 已消耗 0、歷史輸入=合計",
      consumed(MID, "boxed") == 0 and box_hist0 == box_total0, f"total={box_total0} hist={box_hist0}")

c = app.test_client()
login(c, "owner", "owner123")

# 1) 建一張賣 3 片的單 → 合計 12、已消耗 3、歷史輸入 15
r = c.post("/orders/new", data={
    "new_customer_name": "歷史輸入測試客", "recipient_name": "歷史輸入測試客",
    "item_product_id": [str(MID)], "item_combo_code": ["LOOSE"],
    "item_qty": ["3"], "item_amount": ["150"],
}, follow_redirects=True)
oid = fresh().query(Order).order_by(Order.id.desc()).first().id
check("1a) 賣 3 片後 裸片合計=12", pool_total(MID, "loose_piece") == 12,
      f"total={pool_total(MID, 'loose_piece')}")
check("1b) 已消耗=3", consumed(MID, "loose_piece") == 3, f"consumed={consumed(MID, 'loose_piece')}")
check("1c) 歷史輸入=15（合計 12 + 消耗 3）", hist(MID, "loose_piece") == 15)

html = c.get("/inventory/").get_data(as_text=True)
check("1d) 庫存頁表頭有「歷史輸入」與「已消耗」欄",
      ">歷史輸入</th>" in html and ">已消耗</th>" in html)
cells = page_cells(html)
check("1e) 庫存頁裸片列 (合計,歷史輸入,已消耗)=(12,15,3)", (12, 15, 3) in cells, f"cells={cells}")
check("1f) 庫存頁每列皆 歷史輸入 = 合計 + 已消耗",
      len(cells) > 0 and all(h == t + k for t, h, k in cells))
check("1g) 說明文字含「歷史輸入＝原始入庫總量」與「庫存校正不計入」",
      "歷史輸入＝原始入庫總量" in html and "庫存校正不計入" in html)

row = report_row(MID, "loose_piece")
check("1h) _inventory_report 裸片列 hist_in=15 consumed=3 total=12",
      row is not None and row["hist_in"] == 15 and row["consumed"] == 3 and row["total"] == 12,
      f"row={row}")
rr = c.get("/reports/inventory")
html_r = rr.get_data(as_text=True)
check("1i) 報表 200 且表頭「歷史輸入」在「合計」後、「已售」保留",
      rr.status_code == 200 and html_r.find(">合計</th>") < html_r.find(">歷史輸入</th>") < html_r.find(">已售</th>"))

# 2) 作廢該單回補（SALE_REVERSAL）→ 合計 15、已消耗 0、歷史輸入 15
r = c.post(f"/orders/{oid}/void", data={"reason": "CR-9 測試作廢"}, follow_redirects=True)
check("2a) 作廢成功", "已作廢" in r.get_data(as_text=True))
check("2b) 作廢回補後 裸片合計=15", pool_total(MID, "loose_piece") == 15)
check("2c) 已消耗自動扣回=0", consumed(MID, "loose_piece") == 0)
check("2d) 歷史輸入仍=15", hist(MID, "loose_piece") == 15)
db = fresh()
n_rev = db.query(InventoryMovement).filter_by(movement_type="SALE_REVERSAL", product_id=MID).count()
check("2e) 確有 SALE_REVERSAL movement", n_rev >= 1, f"n={n_rev}")

# 3) SPLIT_BOX（盒 -1、片 +5）：不計消耗；合計變 → 歷史輸入跟著變（預期）
db = fresh()
res = inv.split_box(db, MID, 1, "test")
db.commit()
check("3a) split_box ok", res.ok, str(res.error))
check("3b) SPLIT_BOX 後 裸片合計=20、已消耗仍 0、歷史輸入=20（隨合計變，預期）",
      pool_total(MID, "loose_piece") == 20 and consumed(MID, "loose_piece") == 0
      and hist(MID, "loose_piece") == 20)
check("3c) SPLIT_BOX 後 盒裝 已消耗仍 0、歷史輸入=原−1（隨合計變，預期）",
      consumed(MID, "boxed") == 0 and hist(MID, "boxed") == box_hist0 - 1
      and pool_total(MID, "boxed") == box_total0 - 1)

# 4) ADJUSTMENT（quick_adjust 裸片 normal → 100）：不計消耗；合計變 → 歷史輸入跟著變（預期）
db = fresh()
res = inv.quick_adjust(db, MID, "loose_piece", "normal", 100, "test", actor_id=owner.id)
db.commit()
check("4a) quick_adjust ok", res.ok, str(res.error))
check("4b) ADJUSTMENT 後 裸片合計=105（normal 100 + pr 5）、已消耗仍 0、歷史輸入=105（隨合計變，預期）",
      pool_total(MID, "loose_piece") == 105 and consumed(MID, "loose_piece") == 0
      and hist(MID, "loose_piece") == 105)

# 5) GIFT 出庫 2 片：計入消耗；歷史輸入不變（105）
db = fresh()
res = inv.deduct_out(db, MID, "loose_piece", "normal", "GIFT", 2, "test")
db.commit()
check("5a) GIFT 出庫 ok", res.ok, str(res.error))
check("5b) GIFT 後 合計 103、已消耗 2、歷史輸入 105 不變",
      pool_total(MID, "loose_piece") == 103 and consumed(MID, "loose_piece") == 2
      and hist(MID, "loose_piece") == 105)

# 6) PAPERBAG_OUT 計入紙袋消耗（包材-共用 paper_bag）
bag_total0 = pool_total(PID, "paper_bag")
bag_cons0 = consumed(PID, "paper_bag")
db = fresh()
res = inv.deduct_for_shipment(db, oid, "test")
db.commit()
check("6a) deduct_for_shipment ok", res.ok, str(res.error))
check("6b) PAPERBAG_OUT 後 紙袋合計 −1、已消耗 +1、歷史輸入不變",
      pool_total(PID, "paper_bag") == bag_total0 - 1
      and consumed(PID, "paper_bag") == bag_cons0 + 1
      and hist(PID, "paper_bag") == bag_total0 + bag_cons0,
      f"total={pool_total(PID, 'paper_bag')} consumed={consumed(PID, 'paper_bag')}")
row = report_row(PID, "paper_bag")
check("6c) 報表 包材紙袋列 hist_in = total + consumed",
      row is not None and row["hist_in"] == row["total"] + row["consumed"] and row["consumed"] == bag_cons0 + 1)

# 7) 庫存頁：面膜卡紙袋列（全 0 且無歷史）隱藏；包材卡有顯示
html = c.get("/inventory/").get_data(as_text=True)
check("7a) 面膜卡紙袋列全 0 無歷史 → 隱藏（無提示小字）",
      "紙袋庫存記於「包材-共用」" not in html and hist(MID, "paper_bag") == 0)
check("7b) 包材-共用商品卡有顯示", "・包材" in html and PKG_NAME in html)
cells = page_cells(html)
check("7c) 每商品列數：面膜 2 列（盒裝/裸片）+ 包材 3 列 = 顯示列全部滿足 hist=total+consumed",
      all(h == t + k for t, h, k in cells) and (103, 105, 2) in cells, f"cells={cells}")

# 8) 手機版
mh = c.get("/m/inventory")
mhtml = mh.get_data(as_text=True)
check("8a) 手機庫存頁 200 且有「歷史輸入」小字", mh.status_code == 200 and "歷史輸入" in mhtml)
m_hist = [int(x) for x in re.findall(r'class="pool-hist"[^>]*>歷史輸入\s*(\d+)\s*<', mhtml)]
# 裸片池：全商品加總（只有面膜有裸片）→ 105
check("8b) 手機裸片池 歷史輸入=105", 105 in m_hist, f"m_hist={m_hist}")

print("\n" + "=" * 60)
print("通過 {}/{}".format(sum(RESULTS), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
