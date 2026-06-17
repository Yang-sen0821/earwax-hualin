"""報表頁成本支出可編輯 — 本機驗證（throwaway sqlite，放 %TEMP%）。

不污染交付庫：DATABASE_URL 指向 %TEMP% 下的臨時 sqlite，測試結束刪除。
驗證項目（對應交辦）：
 ① owner 編輯商品/包材/行銷成本存檔 → settings 更新 → ROI 數字跟著變
 ② 新增建檔額外支出(PIF 14700) → extra_expenses 落地 → 總成本/ROI/回本重算
 ③ 編輯 / 刪除該支出 → 重算正確
 ④ 收入相關欄位頁面上沒有可編輯入口（維持唯讀）
 ⑤ staff / viewer 打編輯動作 403
 ⑥ 原 ROI / 回本 / finance 計算與其他頁沒被改壞
"""
import os
import sys
import tempfile

# ---- 在 import config/db 之前先指定臨時 sqlite ----
_tmp = os.path.join(tempfile.gettempdir(), "fc_editcost_test.db")
if os.path.exists(_tmp):
    os.remove(_tmp)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp
os.environ["ENABLE_MOBILE"] = "false"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db
from app import create_app
from db import get_session, Setting, ExtraExpense, FinancialEntry, Order
from blueprints.reports import _roi_report, _financial_report

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{(' — ' + extra) if extra else ''}")


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def main():
    init_db.create_all()
    db = get_session()
    init_db.seed(db)

    # 給一張已付款訂單，讓收入/回本有基準（不靠手改收入）
    if not db.query(Order).first():
        db.add(Order(order_no="T-0001", total_amount=50000,
                     payment_status="paid", shipping_status="pending"))
        db.commit()

    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()

    # =================================================================
    # ① owner 編輯商品/包材/行銷成本 → settings 更新 → ROI 重算
    # =================================================================
    login(client, "owner", "owner123")

    r = client.get("/reports/roi")
    check("ROI 頁 owner 可進入 (200)", r.status_code == 200, f"status={r.status_code}")
    check("ROI 頁出現成本編輯表單", b"roi_cost_update" in r.data or b"/reports/roi/cost/update" in r.data)

    db.expire_all()
    roi_before = _roi_report(db)

    client.post("/reports/roi/cost/update", data={"kind": "product", "value": "8000"})
    client.post("/reports/roi/cost/update", data={"kind": "packaging", "value": "1500"})
    client.post("/reports/roi/cost/update", data={"kind": "marketing", "value": "2500"})

    db.expire_all()
    sp = {s.key: s.value for s in db.query(Setting).all()}
    check("settings.cost_product_test 更新為 8000", sp.get("cost_product_test") == "8000.0",
          f"={sp.get('cost_product_test')}")
    check("settings.cost_packaging_test 更新為 1500", sp.get("cost_packaging_test") == "1500.0",
          f"={sp.get('cost_packaging_test')}")
    check("settings.cost_marketing_test 更新為 2500", sp.get("cost_marketing_test") == "2500.0",
          f"={sp.get('cost_marketing_test')}")

    roi_after = _roi_report(db)
    check("ROI 商品成本反映 settings (8000)", roi_after["product_cost"] == 8000.0,
          f"={roi_after['product_cost']}")
    check("ROI 總成本隨成本編輯改變",
          roi_after["total_cost"] == 8000 + 1500 + 2500,
          f"total_cost={roi_after['total_cost']}")
    check("ROI 淨利重算 = 收入 - 總成本",
          roi_after["net_profit"] == roi_after["total_revenue"] - roi_after["total_cost"])

    # =================================================================
    # ② 新增建檔額外支出 PIF 14700 → extra_expenses 落地 → 重算
    # =================================================================
    client.post("/reports/roi/extra/add", data={"name": "PIF", "amount": "14700"})
    db.expire_all()
    pif = db.query(ExtraExpense).filter_by(name="PIF").first()
    check("extra_expenses 落地 PIF 14700", pif is not None and float(pif.amount) == 14700.0,
          f"={None if not pif else float(pif.amount)}")

    roi_pif = _roi_report(db)
    check("ROI extra_expense 含 PIF", roi_pif["extra_expense"] == 14700.0,
          f"={roi_pif['extra_expense']}")
    check("ROI 總成本含建檔額外支出",
          roi_pif["total_cost"] == 8000 + 1500 + 2500 + 14700,
          f"total_cost={roi_pif['total_cost']}")
    check("回本門檻 = 總成本（含 PIF）",
          roi_pif["payback_threshold"] == roi_pif["total_cost"])

    # =================================================================
    # ③ 編輯 / 刪除該支出 → 重算正確
    # =================================================================
    client.post(f"/reports/roi/extra/{pif.id}/update", data={"name": "PIF", "amount": "10000"})
    db.expire_all()
    roi_edit = _roi_report(db)
    check("編輯 PIF→10000 後重算", roi_edit["extra_expense"] == 10000.0,
          f"={roi_edit['extra_expense']}")
    check("編輯後總成本重算",
          roi_edit["total_cost"] == 8000 + 1500 + 2500 + 10000)

    client.post(f"/reports/roi/extra/{pif.id}/delete", data={})
    db.expire_all()
    check("刪除 PIF 後 extra_expenses 移除",
          db.query(ExtraExpense).filter_by(name="PIF").first() is None)
    roi_del = _roi_report(db)
    check("刪除後 extra_expense 歸零", roi_del["extra_expense"] == 0.0,
          f"={roi_del['extra_expense']}")
    check("刪除後總成本重算", roi_del["total_cost"] == 8000 + 1500 + 2500)

    # 其他支出（financial_entries 非已知四類）新增/編輯/刪除
    client.post("/reports/roi/other/add",
                data={"entry_type": "shipping", "amount": "600", "description": "運費"})
    db.expire_all()
    fe = db.query(FinancialEntry).filter_by(entry_type="shipping").first()
    check("其他支出落地 financial_entries(shipping 600)",
          fe is not None and float(fe.amount) == 600.0)
    roi_oth = _roi_report(db)
    check("ROI other_expense 含 shipping", roi_oth["other_expense"] == 600.0,
          f"={roi_oth['other_expense']}")
    # 防呆：其他支出不得用已知四類
    rbad = client.post("/reports/roi/other/add",
                       data={"entry_type": "sale", "amount": "999"})
    db.expire_all()
    check("其他支出拒絕已知四類 sale（無新分錄）",
          db.query(FinancialEntry).filter_by(entry_type="sale").first() is None)
    client.post(f"/reports/roi/other/{fe.id}/delete", data={})
    db.expire_all()
    check("刪除其他支出後歸零",
          db.query(FinancialEntry).filter_by(entry_type="shipping").first() is None)

    # =================================================================
    # ④ 收入相關欄位頁面上沒有可編輯入口（維持唯讀）
    # =================================================================
    r = client.get("/reports/roi")
    html = r.data.decode("utf-8")
    # 不應出現任何把「收入 / 已售 / 庫存」做成可提交表單的入口
    no_revenue_form = ("revenue_update" not in html and "total_revenue\" name" not in html)
    check("收入無可編輯入口（無 revenue 編輯表單）", no_revenue_form)
    # 確認沒有針對 orders / inventory 的寫入 route 出現在 ROI 頁
    check("ROI 頁不含庫存/訂單寫入入口",
          ("/orders/" not in html) and ("inventory/adjust" not in html))

    # =================================================================
    # ⑤ staff / viewer 打編輯動作 403
    # =================================================================
    for role, pw in [("staff", "staff123"), ("viewer", "viewer123")]:
        c2 = app.test_client()
        login(c2, role, pw)
        rg = c2.get("/reports/roi")
        check(f"{role} 進 ROI 頁被擋 (403)", rg.status_code == 403, f"status={rg.status_code}")
        rp = c2.post("/reports/roi/cost/update", data={"kind": "product", "value": "1"})
        check(f"{role} 改成本 403", rp.status_code == 403, f"status={rp.status_code}")
        re = c2.post("/reports/roi/extra/add", data={"name": "x", "amount": "1"})
        check(f"{role} 新增額外支出 403", re.status_code == 403, f"status={re.status_code}")
        ro = c2.post("/reports/roi/other/add", data={"entry_type": "x", "amount": "1"})
        check(f"{role} 新增其他支出 403", ro.status_code == 403, f"status={ro.status_code}")

    # accounting 應可編輯（權限矩陣）
    c3 = app.test_client()
    login(c3, "accounting", "accounting123")
    ra = c3.post("/reports/roi/cost/update", data={"kind": "product", "value": "8000"})
    check("accounting 可改成本 (302 redirect)", ra.status_code in (302, 303),
          f"status={ra.status_code}")

    # =================================================================
    # ⑥ 原 ROI / 回本 / finance 計算與其他頁沒被改壞
    # =================================================================
    db.expire_all()
    fin = _financial_report(db)
    check("finance 報表仍可計算（淨利為數值）", isinstance(fin["net_profit"], float))
    roi_final = _roi_report(db)
    # ROI/回本公式未動：淨利=收入-總成本；毛利=收入-商品成本；回本門檻=總成本
    check("ROI 公式未變（淨利）",
          roi_final["net_profit"] == roi_final["total_revenue"] - roi_final["total_cost"])
    check("毛利公式未變", roi_final["gross_profit"] ==
          roi_final["total_revenue"] - roi_final["product_cost"])
    check("回本門檻=總成本未變", roi_final["payback_threshold"] == roi_final["total_cost"])
    # 其他頁仍 200
    for path in ["/reports/", "/reports/sales", "/reports/inventory", "/reports/finance"]:
        c4 = app.test_client()
        login(c4, "owner", "owner123")
        rr = c4.get(path)
        check(f"其他報表頁 {path} 仍 200", rr.status_code == 200, f"status={rr.status_code}")

    db.close()

    print("\n================ 結果 ================")
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
    print("OVERALL: " + ("PASS" if not FAIL else "FAIL"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    code = main()
    sys.exit(code)
