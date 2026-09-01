# -*- coding: utf-8 -*-
"""甲案（外泌體銷售紀錄）＋手機客戶建立＋員工權限收緊 handler 級測試。

throwaway sqlite + EARWAX_TABLE 同構表。執行：python earwax_sales_test.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = os.path.join(tempfile.gettempdir(), "flora_court_ewsales_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"
os.environ["EARWAX_TABLE"] = "test_earwax_consumables"
os.environ["ENABLE_MOBILE"] = "true"

import init_db  # noqa: E402
init_db.main()
from sqlalchemy import text  # noqa: E402
from db import get_session, AuditLog, EarwaxSale, Customer  # noqa: E402
from app import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


s = get_session()
s.execute(text(
    "CREATE TABLE test_earwax_consumables ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, category VARCHAR(20) NOT NULL DEFAULT 'consumable',"
    " name VARCHAR(100) NOT NULL, qty_on_hand INTEGER DEFAULT 0, unit_cost FLOAT DEFAULT 0,"
    " note TEXT DEFAULT '', created_at TIMESTAMP)"))
s.execute(text("INSERT INTO test_earwax_consumables (category,name,qty_on_hand) VALUES ('consumable','銀',5)"))
s.execute(text("INSERT INTO test_earwax_consumables (category,name,qty_on_hand) VALUES ('equipment','破壁機',1)"))
s.commit()

c = app.test_client()

# ---- staff 身分 ----
c.post("/login", data={"username": "staff", "password": "staff123"})

# 1) staff 建立外泌體銷售（桌面路由）
r = c.post("/earwax-sales/new", data={"item_id": "1", "qty": "2", "amount": "600",
                                       "note": "測試"}, follow_redirects=True)
qty = s.execute(text("SELECT qty_on_hand FROM test_earwax_consumables WHERE id=1")).scalar()
n_sale = s.query(EarwaxSale).count()
n_audit = s.query(AuditLog).filter_by(action="earwax_sale_create").count()
check("1) staff 建立銷售：扣庫存+紀錄+留痕", r.status_code == 200 and qty == 3
      and n_sale == 1 and n_audit == 1, f"qty={qty} sale={n_sale} audit={n_audit}")

# 2) 庫存不足 → 擋下、無紀錄
r = c.post("/earwax-sales/new", data={"item_id": "1", "qty": "99", "amount": "0"},
           follow_redirects=True)
qty = s.execute(text("SELECT qty_on_hand FROM test_earwax_consumables WHERE id=1")).scalar()
check("2) 庫存不足擋下", qty == 3 and s.query(EarwaxSale).count() == 1, f"qty={qty}")

# 3) equipment 不可售
r = c.post("/earwax-sales/new", data={"item_id": "2", "qty": "1", "amount": "0"},
           follow_redirects=True)
check("3) 儀器不可售", s.query(EarwaxSale).count() == 1)

# 4) 手機版外泌體銷售
r = c.post("/m/earwax-sales/new", data={"item_id": "1", "qty": "1", "amount": "300"},
           follow_redirects=True)
qty = s.execute(text("SELECT qty_on_hand FROM test_earwax_consumables WHERE id=1")).scalar()
check("4) 手機版建立銷售", r.status_code == 200 and qty == 2
      and s.query(EarwaxSale).count() == 2, f"qty={qty}")

# 5) 手機版清單可讀（staff）
r = c.get("/m/earwax-sales")
check("5) staff 讀手機清單", r.status_code == 200 and "銀" in r.get_data(as_text=True))

# 6) 手機版建立客戶
r = c.post("/m/customers/new", data={"name": "測試客", "phone": "0912345678"},
           follow_redirects=True)
n_cust = s.query(Customer).filter_by(name="測試客").count()
check("6) 手機版建立客戶", r.status_code == 200 and n_cust == 1, f"n={n_cust}")

# 7) staff 報表 403 矩陣（index/sales/finance/export；roi 原本就擋）
codes = {}
for path in ("/reports/", "/reports/sales", "/reports/finance",
             "/reports/export.xlsx", "/reports/roi"):
    codes[path] = c.get(path).status_code
check("7) staff 報表全擋 403", all(v == 403 for v in codes.values()), str(codes))

# 8) staff 仍可用作業面：建單頁/庫存/客戶/出貨
codes2 = {p: c.get(p, follow_redirects=True).status_code
          for p in ("/m/orders/new", "/m/inventory", "/m/customers", "/m/shipments")}
check("8) staff 作業面正常", all(v == 200 for v in codes2.values()), str(codes2))

# ---- viewer 身分 ----
c.get("/logout", follow_redirects=True)
c.post("/login", data={"username": "viewer", "password": "viewer123"})
r1 = c.get("/earwax-sales/")
r2 = c.post("/earwax-sales/new", data={"item_id": "1", "qty": "1", "amount": "0"})
check("9) viewer 擋外泌體清單與寫入", r1.status_code == 403 and r2.status_code == 403,
      f"{r1.status_code}/{r2.status_code}")

# ---- owner 身分 ----
c.get("/logout", follow_redirects=True)
c.post("/login", data={"username": "owner", "password": "owner123"})
r1 = c.get("/reports/roi")
r2 = c.get("/earwax-sales/")
check("10) owner 看 ROI 與外泌體清單", r1.status_code == 200 and r2.status_code == 200,
      f"{r1.status_code}/{r2.status_code}")

print("=" * 50)
print(f"結果：{sum(RESULTS)}/{len(RESULTS)} PASS")
sys.exit(0 if all(RESULTS) else 1)
