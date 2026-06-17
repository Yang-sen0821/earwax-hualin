"""支出管理頁 + ROI/財務單一來源 本機驗證（throwaway sqlite 放 %TEMP%）。

不污染交付庫：自建獨立 sqlite，跑完刪除。
"""
import os
import sys
import tempfile

# ---- throwaway sqlite，務必在 import config/db/app 之前設好 ----
_TMP = tempfile.mkdtemp(prefix="fc_exp_test_")
_DB = os.path.join(_TMP, "test.db").replace("\\", "/")
os.environ["DATABASE_URL"] = "sqlite:///" + _DB
os.environ["ENABLE_MOBILE"] = "true"

import init_db
from app import app
from db import get_session, ExtraExpense, FinancialEntry, Order, OrderItem, Customer
from blueprints import reports as R

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def login(c, user, pw):
    return c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)


def main():
    print("=== throwaway DB:", _DB)
    init_db.create_all()
    db = get_session()
    init_db.seed(db)
    db.commit()

    app.config["TESTING"] = True

    # ----------------------------------------------------------------
    # ① /expenses owner 200 + 新增 5 筆不同類別，落地 extra_expenses 帶正確 category
    # ----------------------------------------------------------------
    with app.test_client() as c:
        login(c, "owner", "owner123")
        r = c.get("/reports/expenses")
        check("① /expenses owner 200", r.status_code == 200, f"status={r.status_code}")
        check("① 導覽列含支出管理入口", "支出管理" in r.get_data(as_text=True))

        seeds = [
            ("面膜進貨A", "面膜進貨成本", 30000),
            ("洗臉機設備", "設備", 80000),
            ("PIF建檔", "建檔費", 14700),
            ("包材一批", "包材", 5000),
            ("FB廣告", "行銷", 3000),
        ]
        for nm, cat, amt in seeds:
            rr = c.post("/reports/expenses/add", data={
                "name": nm, "category": cat, "amount": str(amt),
                "expense_date": "2026-06-17", "note": "test",
            }, follow_redirects=True)
            check(f"① 新增 {cat} {amt}", rr.status_code == 200)

    db2 = get_session()
    rows = db2.query(ExtraExpense).all()
    check("① extra_expenses 共 5 筆", len(rows) == 5, f"count={len(rows)}")
    cat_amt = {e.category: float(e.amount) for e in rows}
    check("① category=面膜進貨成本 30000", cat_amt.get("面膜進貨成本") == 30000, str(cat_amt))
    check("① category=設備 80000", cat_amt.get("設備") == 80000)
    check("① category=建檔費 14700", cat_amt.get("建檔費") == 14700)
    check("① category=包材 5000", cat_amt.get("包材") == 5000)
    check("① category=行銷 3000", cat_amt.get("行銷") == 3000)

    # ----------------------------------------------------------------
    # ④ 確認沒有重複計算：seed settings cost_*_test 仍為 0，但即便非 0 也不該被加
    #    這裡直接把 settings 設成大值，證明 _roi_report 不再讀它
    # ----------------------------------------------------------------
    from db import Setting
    for k in ("cost_product_test", "cost_packaging_test", "cost_marketing_test"):
        s = db2.query(Setting).filter_by(key=k).first()
        if s:
            s.value = "999999"   # 故意設天價，若被加總就會穿幫
    # 同時塞 financial_entries 成本分錄（舊來源），證明不再被計入成本側
    db2.add(FinancialEntry(entry_type="product_cost", amount=111111))
    db2.add(FinancialEntry(entry_type="packaging", amount=222222))
    db2.add(FinancialEntry(entry_type="marketing", amount=333333))
    db2.commit()

    # ----------------------------------------------------------------
    # 給一筆收入樣本：建一張已付款訂單 100000，供 ROI/回本驗算
    # ----------------------------------------------------------------
    cust = Customer(name="測試客")
    db2.add(cust)
    db2.flush()
    o = Order(order_no="T-0001", customer_id=cust.id, total_amount=100000,
              payment_status="paid", shipping_status="pending")
    db2.add(o)
    db2.commit()

    # ----------------------------------------------------------------
    # ② ROI 總投入成本 = 132700；商品成本=30000；設備計入；毛利/淨利/ROI/回本算對
    # ----------------------------------------------------------------
    db3 = get_session()
    with app.test_request_context():
        roi = R._roi_report(db3)
    EXPECT_TOTAL = 132700
    check("② 總投入成本 = 132700", roi["total_cost"] == EXPECT_TOTAL,
          f"total_cost={roi['total_cost']}")
    check("② 商品成本 = 30000", roi["product_cost"] == 30000, str(roi["product_cost"]))
    check("② 設備計入 = 80000", roi["equipment_cost"] == 80000, str(roi["equipment_cost"]))
    check("② 建檔費 = 14700", roi["setup_cost"] == 14700)
    check("② 包材 = 5000", roi["packaging_cost"] == 5000)
    check("② 行銷 = 3000", roi["marketing_cost"] == 3000)
    check("② 其他 = 0", roi["other_expense"] == 0)
    # 收入 100000：毛利=100000-30000=70000；淨利=100000-132700=-32700
    check("② 總收入 = 100000", roi["total_revenue"] == 100000, str(roi["total_revenue"]))
    check("② 毛利 = 70000", roi["gross_profit"] == 70000, str(roi["gross_profit"]))
    check("② 淨利 = -32700", roi["net_profit"] == -32700, str(roi["net_profit"]))
    exp_roi = -32700 / 132700 * 100
    check("② ROI% 正確", abs(roi["roi_pct"] - exp_roi) < 1e-6, f"{roi['roi_pct']:.4f} vs {exp_roi:.4f}")
    # 回本：門檻=132700，已回收=100000，未回本，尚差 32700
    check("② 回本門檻 = 132700", roi["payback_threshold"] == 132700)
    check("② 已回收 = 100000", roi["recovered"] == 100000)
    check("② 尚需回收 = 32700", roi["remaining_to_recover"] == 32700, str(roi["remaining_to_recover"]))
    check("② 未回本", roi["paid_back"] is False)

    # ④ 重複計算檢查：總投入應仍是 132700，未被 settings(999999*3) 或 fe 成本分錄汙染
    check("④ 無重複計算（settings/分錄成本未被加）", roi["total_cost"] == EXPECT_TOTAL,
          f"total_cost={roi['total_cost']} (若被汙染會遠大於 132700)")

    # 財務報表也走同一來源
    with app.test_request_context():
        fin = R._financial_report(db3)
    check("④ 財務淨利 = 收入-總投入 = -32700", fin["net_profit"] == -32700, str(fin["net_profit"]))
    check("④ 財務商品成本 = 30000", fin["product_cost"] == 30000)
    check("④ 財務 total_cost = 132700", fin["total_cost"] == 132700)

    # ----------------------------------------------------------------
    # ROI 頁不再有舊「單一數字成本編輯」表單，且 /roi/cost/update 路由已移除
    # ----------------------------------------------------------------
    rules = {str(r.rule) for r in app.url_map.iter_rules()}
    check("⑤ /reports/expenses 路由存在", "/reports/expenses" in rules)
    check("⑤ 舊 /reports/roi/cost/update 已移除", "/reports/roi/cost/update" not in rules)
    check("⑤ 舊 /reports/roi/extra/add 已移除", "/reports/roi/extra/add" not in rules)
    check("⑤ 舊 /reports/roi/other/add 已移除", "/reports/roi/other/add" not in rules)

    with app.test_client() as c:
        login(c, "owner", "owner123")
        rroi = c.get("/reports/roi")
        txt = rroi.get_data(as_text=True)
        check("⑤ ROI 頁 200", rroi.status_code == 200)
        check("⑤ ROI 頁有去支出管理連結", "/reports/expenses" in txt)
        check("⑤ ROI 頁顯示總投入 132700", "132700" in txt, "(看 ROI 頁是否渲染金額)")

        # ③ 編輯/刪除某品項 → 重算正確
        # 編輯：把行銷 3000 改成 4000 → 總投入 +1000 = 133700
        mk = db3.query(ExtraExpense).filter_by(category="行銷").first()
        c.post(f"/reports/expenses/{mk.id}/update", data={
            "name": "FB廣告改", "category": "行銷", "amount": "4000",
            "expense_date": "2026-06-17", "note": "edit",
        }, follow_redirects=True)
        # 刪除：刪掉包材 5000 → 再 -5000 = 128700
        pk = db3.query(ExtraExpense).filter_by(category="包材").first()
        c.post(f"/reports/expenses/{pk.id}/delete", follow_redirects=True)

    db4 = get_session()
    with app.test_request_context():
        roi2 = R._roi_report(db4)
    check("③ 編輯+刪除後 總投入 = 128700",
          roi2["total_cost"] == 128700, f"total_cost={roi2['total_cost']}")
    check("③ 編輯後 行銷 = 4000", roi2["marketing_cost"] == 4000)
    check("③ 刪除後 包材 = 0", roi2["packaging_cost"] == 0)

    # ----------------------------------------------------------------
    # ⑤ 收入/庫存仍唯讀無編輯入口（ROI 頁不含收入/庫存編輯表單）
    # ----------------------------------------------------------------
    with app.test_client() as c:
        login(c, "owner", "owner123")
        txt = c.get("/reports/roi").get_data(as_text=True)
        check("⑤ ROI 頁無收入編輯入口（唯讀字樣）", "唯讀" in txt)
        # 收入相關沒有 form action 指向修改收入
        check("⑤ ROI 頁無 roi_cost_update 表單", "roi_cost_update" not in txt and "roi/cost/update" not in txt)

    # ----------------------------------------------------------------
    # ⑥ staff / viewer 打 /expenses 與編輯 → 403
    # ----------------------------------------------------------------
    for role, pw in [("staff", "staff123"), ("viewer", "viewer123")]:
        with app.test_client() as c:
            login(c, role, pw)
            rg = c.get("/reports/expenses")
            check(f"⑥ {role} GET /expenses 403", rg.status_code == 403, f"status={rg.status_code}")
            rp = c.post("/reports/expenses/add", data={"name": "x", "category": "其他", "amount": "1"})
            check(f"⑥ {role} POST /expenses/add 403", rp.status_code == 403, f"status={rp.status_code}")
    # accounting 可進
    with app.test_client() as c:
        login(c, "accounting", "accounting123")
        ra = c.get("/reports/expenses")
        check("⑥ accounting GET /expenses 200", ra.status_code == 200, f"status={ra.status_code}")

    # ----------------------------------------------------------------
    # ⑦ 其他報表頁/原功能沒壞
    # ----------------------------------------------------------------
    with app.test_client() as c:
        login(c, "owner", "owner123")
        for path in ["/reports", "/reports/sales", "/reports/inventory",
                     "/reports/finance", "/products", "/inventory",
                     "/orders", "/customers", "/health"]:
            rr = c.get(path, follow_redirects=True)
            check(f"⑦ {path} 200", rr.status_code == 200, f"status={rr.status_code}")

    # ----------------------------------------------------------------
    print("\n=== SUMMARY ===")
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  -", f)
        print("RESULT: FAIL")
    else:
        print("RESULT: PASS")


if __name__ == "__main__":
    try:
        main()
    finally:
        # 清理 throwaway DB
        try:
            from db import engine
            engine.dispose()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(_TMP, ignore_errors=True)
        except Exception:
            pass
    sys.exit(1 if FAIL else 0)
