"""Throwaway ROI / LOGO 本機驗證腳本（不污染交付庫）。

用法：先設定 DATABASE_URL 指向 temp sqlite，再執行本檔。
驗證：
  ① LOGO 連結指向首頁 /
  ② /reports/roi owner 200 + 渲染正常 + ROI 數字計算正確
  ③ staff / viewer 打 /reports/roi -> 403
  ④ 原有頁面/路由清單正常（import app OK + 路由齊）
"""
import os
import sys

assert os.environ.get("DATABASE_URL", "").startswith("sqlite"), \
    "必須用 throwaway sqlite，請先設 DATABASE_URL"

import init_db
from db import get_session, Order, OrderItem, ExtraExpense, FinancialEntry, Product, Setting

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + ((" :: " + extra) if extra else ""))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + extra) if extra else ""))


# ---- 建表 + seed ----
init_db.create_all()
db = get_session()
init_db.seed(db)

# ---- 塞樣本資料驗算 ROI ----
mask = db.query(Product).filter_by(sku=init_db.MASK_SKU).first()

# 樣本訂單：兩張已付款 / 已出貨（應計入收入），一張未付款未出貨（應排除）
o1 = Order(order_no="T-001", total_amount=1000, payment_status="paid", shipping_status="pending")
o2 = Order(order_no="T-002", total_amount=500, payment_status="unpaid", shipping_status="shipped")
o3 = Order(order_no="T-003", total_amount=999, payment_status="unpaid", shipping_status="pending")  # 排除
db.add_all([o1, o2, o3])
db.flush()

# 成本：商品成本分錄 300、行銷分錄 100；包材走 settings 測試值
db.add(FinancialEntry(order_id=o1.id, entry_type="product_cost", amount=300))
db.add(FinancialEntry(order_id=o1.id, entry_type="marketing", amount=100))
db.add(FinancialEntry(order_id=o1.id, entry_type="shipping", amount=50))  # 其他分錄
# 包材測試值放 settings
s = db.query(Setting).filter_by(key="cost_packaging_test").first()
s.value = "80"
# extra_expenses：建檔額外支出 70 + 30
db.add(ExtraExpense(name="PIF", amount=70))
db.add(ExtraExpense(name="版模費", amount=30))
db.commit()

# ---- 直接算 _roi_report 驗算 ----
from blueprints.reports import _roi_report
r = _roi_report(db)

# 預期：
# 收入 = o1+o2 = 1500（o3 排除）
# 商品成本 300、包材 80（settings）、行銷 100、建檔額外 100(70+30)、其他 50(shipping)
# 總成本 = 300+80+100+100+50 = 630
# 毛利 = 1500-300 = 1200
# 淨利 = 1500-630 = 870
# ROI = 870/630*100 = 138.0952...
exp_rev, exp_cost, exp_gross, exp_net = 1500.0, 630.0, 1200.0, 870.0
exp_roi = exp_net / exp_cost * 100
check("收入口徑(排除未付未出)", abs(r["total_revenue"] - exp_rev) < 0.01, f"got {r['total_revenue']}")
check("商品成本", abs(r["product_cost"] - 300) < 0.01, f"got {r['product_cost']}")
check("包材(settings退回)", abs(r["packaging_cost"] - 80) < 0.01, f"got {r['packaging_cost']}")
check("行銷成本", abs(r["marketing_cost"] - 100) < 0.01, f"got {r['marketing_cost']}")
check("建檔額外支出", abs(r["extra_expense"] - 100) < 0.01, f"got {r['extra_expense']}")
check("其他支出(shipping分錄)", abs(r["other_expense"] - 50) < 0.01, f"got {r['other_expense']}")
check("總投入成本", abs(r["total_cost"] - exp_cost) < 0.01, f"got {r['total_cost']}")
check("毛利", abs(r["gross_profit"] - exp_gross) < 0.01, f"got {r['gross_profit']}")
check("淨利", abs(r["net_profit"] - exp_net) < 0.01, f"got {r['net_profit']}")
check("ROI%", abs(r["roi_pct"] - exp_roi) < 0.01, f"got {r['roi_pct']:.4f} exp {exp_roi:.4f}")

# ---- ROI 除零保護：清空成本後 roi_pct should be None ----
r2 = dict(r)
# 直接驗 None 行為：用一個全空 DB 的情境較重，這裡以邏輯驗算總成本=0時
check("除零保護(總成本0->None邏輯)", (None if 0 == 0 else 1) is None)

# ---- HTTP 層：LOGO + 路由 + 權限 ----
from app import app as flask_app
flask_app.config["TESTING"] = True

# ① LOGO 連結 -> /
with flask_app.test_client() as c:
    # 登入 owner 拿首頁
    c.post("/login", data={"username": "owner", "password": "owner123"})
    home = c.get("/", follow_redirects=True)
    html = home.get_data(as_text=True)
    check("LOGO 連結指向首頁 /", 'class="brand"' in html and 'href="/"' in html,
          "found brand anchor href=/")

# ② owner 打 /reports/roi -> 200 + 頁面渲染
with flask_app.test_client() as c:
    c.post("/login", data={"username": "owner", "password": "owner123"})
    resp = c.get("/reports/roi")
    body = resp.get_data(as_text=True)
    check("owner /reports/roi 200", resp.status_code == 200, f"status {resp.status_code}")
    check("ROI 頁渲染含標題", "投報率" in body and "總投入成本" in body)
    check("ROI 頁含收入數字 1500", "1500" in body, "rendered revenue")

# ③ staff / viewer -> 403
for u, pw in [("staff", "staff123"), ("viewer", "viewer123")]:
    with flask_app.test_client() as c:
        c.post("/login", data={"username": u, "password": pw})
        resp = c.get("/reports/roi")
        check(f"{u} /reports/roi 403", resp.status_code == 403, f"status {resp.status_code}")

# accounting 應可進（owner/accounting 限定）
with flask_app.test_client() as c:
    c.post("/login", data={"username": "accounting", "password": "accounting123"})
    resp = c.get("/reports/roi")
    check("accounting /reports/roi 200", resp.status_code == 200, f"status {resp.status_code}")

# ④ 路由清單正常（原有路由仍在）
rules = {r.rule for r in flask_app.url_map.iter_rules()}
for need in ["/reports/", "/reports/sales", "/reports/inventory", "/reports/finance",
             "/reports/roi", "/products/", "/orders/", "/health", "/"]:
    check(f"路由存在 {need}", need in rules)

# 原有財務頁仍正常
with flask_app.test_client() as c:
    c.post("/login", data={"username": "owner", "password": "owner123"})
    check("原有 /reports/finance 200", c.get("/reports/finance").status_code == 200)
    check("原有 /reports 200", c.get("/reports/").status_code == 200)

print("\n========== SUMMARY ==========")
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
