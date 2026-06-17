"""Throwaway 回本計算本機驗證腳本（不污染交付庫）。

用法：先設定 DATABASE_URL 指向 %TEMP% sqlite，再執行本檔。
驗證：
  ① /reports/roi owner 200 且頁面同時含 ROI 與「回本計算」區塊
  ② 情境A（總投入 630、收入 1500 → 已回本、淨賺 870、進度 238.10%）算對
  ③ 情境B（投入 630、收入 300 → 未回本、進度 47.62%、尚需 330）算對
  ④ 總成本 0 → 進度 N/A 不崩
  ⑤ staff/viewer 403
  ⑥ 原 ROI 數字與既有頁面沒被改壞
"""
import os
import sys

assert os.environ.get("DATABASE_URL", "").startswith("sqlite"), \
    "必須用 throwaway sqlite，請先設 DATABASE_URL"

import init_db
from db import get_session, Order, ExtraExpense, FinancialEntry, Product, Setting

PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + ((" :: " + extra) if extra else ""))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + extra) if extra else ""))


from blueprints.reports import _roi_report

# =====================================================================
# 情境 A：總投入 630、收入 1500 → 已回本、淨賺 870、進度 238.10%
# 沿用 roi_test 同一套樣本（o1+o2=1500 收入；成本 300+80+100+100+50=630）
# =====================================================================
init_db.create_all()
db = get_session()
init_db.seed(db)

o1 = Order(order_no="P-001", total_amount=1000, payment_status="paid", shipping_status="pending")
o2 = Order(order_no="P-002", total_amount=500, payment_status="unpaid", shipping_status="shipped")
o3 = Order(order_no="P-003", total_amount=999, payment_status="unpaid", shipping_status="pending")  # 排除
db.add_all([o1, o2, o3])
db.flush()
db.add(FinancialEntry(order_id=o1.id, entry_type="product_cost", amount=300))
db.add(FinancialEntry(order_id=o1.id, entry_type="marketing", amount=100))
db.add(FinancialEntry(order_id=o1.id, entry_type="shipping", amount=50))  # 其他分錄
s = db.query(Setting).filter_by(key="cost_packaging_test").first()
s.value = "80"
db.add(ExtraExpense(name="PIF", amount=70))
db.add(ExtraExpense(name="版模費", amount=30))
db.commit()

rA = _roi_report(db)
# 預期回本欄位
check("A 總投入成本=630（未動 ROI 口徑）", abs(rA["total_cost"] - 630) < 0.01, f"got {rA['total_cost']}")
check("A 累積收入=1500", abs(rA["total_revenue"] - 1500) < 0.01, f"got {rA['total_revenue']}")
check("A 回本門檻=630", abs(rA["payback_threshold"] - 630) < 0.01, f"got {rA['payback_threshold']}")
check("A 已回收=1500", abs(rA["recovered"] - 1500) < 0.01, f"got {rA['recovered']}")
check("A 回本進度%≈238.10", abs(rA["payback_pct"] - (1500/630*100)) < 0.01, f"got {rA['payback_pct']:.4f}")
check("A 尚需回收=0", abs(rA["remaining_to_recover"] - 0) < 0.01, f"got {rA['remaining_to_recover']}")
check("A 已回本=True", rA["paid_back"] is True, f"got {rA['paid_back']}")
check("A 淨賺=870", abs(rA["net_earned"] - 870) < 0.01, f"got {rA['net_earned']}")
# ROI 部分沒被改壞
check("A ROI 淨利=870 未變", abs(rA["net_profit"] - 870) < 0.01, f"got {rA['net_profit']}")
check("A ROI 毛利=1200 未變", abs(rA["gross_profit"] - 1200) < 0.01, f"got {rA['gross_profit']}")
check("A ROI%≈138.10 未變", abs(rA["roi_pct"] - (870/630*100)) < 0.01, f"got {rA['roi_pct']:.4f}")

# =====================================================================
# 情境 B：投入 630、收入 300 → 未回本、進度 47.62%、尚需 330
# 把上面樣本的收入調成 300：刪 o1/o2，只留一張已付款 total=300
# =====================================================================
for o in db.query(Order).all():
    db.delete(o)
# 收入分錄與訂單收入都清空，留成本不動 → 收入 300（一張已付款）
oB = Order(order_no="P-B01", total_amount=300, payment_status="paid", shipping_status="pending")
db.add(oB)
db.commit()
rB = _roi_report(db)
check("B 總投入成本=630（成本不變）", abs(rB["total_cost"] - 630) < 0.01, f"got {rB['total_cost']}")
check("B 累積收入=300", abs(rB["total_revenue"] - 300) < 0.01, f"got {rB['total_revenue']}")
check("B 回本進度%≈47.62", abs(rB["payback_pct"] - (300/630*100)) < 0.01, f"got {rB['payback_pct']:.4f}")
check("B 尚需回收=330", abs(rB["remaining_to_recover"] - 330) < 0.01, f"got {rB['remaining_to_recover']}")
check("B 已回本=False", rB["paid_back"] is False, f"got {rB['paid_back']}")
check("B 淨賺=0（未回本）", abs(rB["net_earned"] - 0) < 0.01, f"got {rB['net_earned']}")

# =====================================================================
# 情境 C：總成本 0 → 進度 N/A 不崩
# 清掉所有成本來源（financial_entries 成本分錄 / extra_expenses / settings 測試值）
# =====================================================================
for fe in db.query(FinancialEntry).all():
    db.delete(fe)
for ee in db.query(ExtraExpense).all():
    db.delete(ee)
for key in ("cost_product_test", "cost_packaging_test", "cost_marketing_test"):
    st = db.query(Setting).filter_by(key=key).first()
    if st:
        st.value = "0"
db.commit()
rC = _roi_report(db)
check("C 總投入成本=0", abs(rC["total_cost"] - 0) < 0.01, f"got {rC['total_cost']}")
check("C 回本進度%=None（防除零）", rC["payback_pct"] is None, f"got {rC['payback_pct']}")
check("C ROI%=None（防除零，未崩）", rC["roi_pct"] is None, f"got {rC['roi_pct']}")
check("C 已回本=False（成本0不判已回本）", rC["paid_back"] is False, f"got {rC['paid_back']}")

# =====================================================================
# HTTP 層：頁面渲染 + 權限
# 重建情境 A（含成本與收入）讓頁面有數字可看
# =====================================================================
from app import app as flask_app
flask_app.config["TESTING"] = True

# 重新塞回情境 A 資料
init_db.seed(db)
hA1 = Order(order_no="H-001", total_amount=1000, payment_status="paid", shipping_status="pending")
hA2 = Order(order_no="H-002", total_amount=500, payment_status="unpaid", shipping_status="shipped")
db.add_all([hA1, hA2])
db.flush()
db.add(FinancialEntry(order_id=hA1.id, entry_type="product_cost", amount=300))
db.add(FinancialEntry(order_id=hA1.id, entry_type="marketing", amount=100))
db.add(FinancialEntry(order_id=hA1.id, entry_type="shipping", amount=50))
s = db.query(Setting).filter_by(key="cost_packaging_test").first()
s.value = "80"
db.add(ExtraExpense(name="PIF", amount=70))
db.add(ExtraExpense(name="版模費", amount=30))
db.commit()

with flask_app.test_client() as c:
    c.post("/login", data={"username": "owner", "password": "owner123"})
    resp = c.get("/reports/roi")
    body = resp.get_data(as_text=True)
    check("owner /reports/roi 200", resp.status_code == 200, f"status {resp.status_code}")
    check("頁面含 ROI 區塊（投報率/總投入成本）", "投報率" in body and "總投入成本" in body)
    check("頁面含『回本計算』區塊", "回本計算" in body)
    check("頁面含回本進度元素（回本進度 / bar-fill）", "回本進度" in body and "bar-fill" in body)
    check("頁面含已回本判定字樣", "已回本" in body)
    check("頁面含收入數字 1500", "1500" in body)

# staff / viewer -> 403
for u, pw in [("staff", "staff123"), ("viewer", "viewer123")]:
    with flask_app.test_client() as c:
        c.post("/login", data={"username": u, "password": pw})
        resp = c.get("/reports/roi")
        check(f"{u} /reports/roi 403", resp.status_code == 403, f"status {resp.status_code}")

# accounting 應可進
with flask_app.test_client() as c:
    c.post("/login", data={"username": "accounting", "password": "accounting123"})
    check("accounting /reports/roi 200", c.get("/reports/roi").status_code == 200)

# 既有頁面沒被改壞
with flask_app.test_client() as c:
    c.post("/login", data={"username": "owner", "password": "owner123"})
    check("既有 /reports/finance 200", c.get("/reports/finance").status_code == 200)
    check("既有 /reports 200", c.get("/reports/").status_code == 200)
    check("既有 /reports/sales 200", c.get("/reports/sales").status_code == 200)
    check("既有 /reports/inventory 200", c.get("/reports/inventory").status_code == 200)

print("\n========== SUMMARY ==========")
print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL GREEN")
