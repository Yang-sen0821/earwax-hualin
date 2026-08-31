# -*- coding: utf-8 -*-
"""CR-6 驗收：訂單列表勾選批次作廢（POST /orders/void-bulk）— 2026-08-31。

throwaway sqlite 放 %TEMP%，不污染交付庫；env 在 import 任何 app 模組前先設定。
驗證項目：
  A 列表 UI：owner/staff 看得到勾選框、全選、「作廢勾選訂單」按鈕、原因欄；viewer 看不到；作廢單列無勾選框
  B owner 批次作廢 3 單（2 未出貨 + 1 已出貨且 paid）→ 2 回補、1 不回補、3 voided、3 audit 同 bulk_group_id、paid→refunded
  C staff 批次含已出貨單 → 整批拒絕（明列單號）、0 筆變動
  D 已作廢單被跳過（不重複 audit、不二次回補）；staff 純未出貨批次可成功
  E 缺原因 → 整批拒絕、0 筆變動；空選 → 提示；不存在 id → 整批拒絕；viewer 403
  F 單筆作廢（CR-4）audit 不帶 bulk 欄，行為不變；手機列表無批次入口
"""
import os
import sys
import json
import tempfile

from werkzeug.datastructures import MultiDict

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_TMP_DB = os.path.join(tempfile.gettempdir(), "flora_court_bulkvoid_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
from db import (  # noqa: E402
    get_session, Order, InventoryBalance, InventoryMovement, Product, Shipment, AuditLog,
)
from app import create_app  # noqa: E402

PASS = []
FAIL = []
CB = 'name="order_ids"'


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def fresh():
    s = get_session()
    s.expire_all()
    return s


def bal(pid, pool, cat="normal"):
    db = fresh()
    b = db.query(InventoryBalance).filter_by(
        product_id=pid, inventory_pool=pool, stock_category=cat).first()
    q = b.qty if b else None
    db.close()
    return q


def last_order_id():
    db = fresh()
    o = db.query(Order).order_by(Order.id.desc()).first()
    oid, ono = o.id, o.order_no
    db.close()
    return oid, ono


def order(oid):
    db = fresh()
    o = db.query(Order).filter_by(id=oid).first()
    db.close()
    return o


def mv_count(oid, mtype=None):
    db = fresh()
    q = db.query(InventoryMovement).filter_by(ref_type="order", ref_id=str(oid))
    if mtype:
        q = q.filter_by(movement_type=mtype)
    n = q.count()
    db.close()
    return n


def all_mv_count():
    db = fresh()
    n = db.query(InventoryMovement).count()
    db.close()
    return n


def audit_count(action, oid=None):
    db = fresh()
    q = db.query(AuditLog).filter_by(action=action)
    if oid is not None:
        q = q.filter_by(target_id=str(oid))
    n = q.count()
    db.close()
    return n


def audit_last(action, oid):
    db = fresh()
    a = (db.query(AuditLog).filter_by(action=action, target_id=str(oid))
         .order_by(AuditLog.id.desc()).first())
    d = json.loads(a.detail) if a else None
    db.close()
    return d


def shipments_count(oid):
    db = fresh()
    n = db.query(Shipment).filter_by(order_id=oid).count()
    db.close()
    return n


def voided_count():
    db = fresh()
    n = db.query(Order).filter(Order.voided_at.isnot(None)).count()
    db.close()
    return n


def new_box_order(client, pid, qty=1):
    client.post("/orders/new", data={
        "item_product_id": str(pid), "item_combo_code": "BOX",
        "item_qty": str(qty), "item_amount": str(300 * qty),
    }, follow_redirects=True)
    return last_order_id()


def bulk(client, ids, reason, follow=True, query=""):
    data = MultiDict([("order_ids", str(i)) for i in ids])
    if reason is not None:
        data.add("reason", reason)
    return client.post("/orders/void-bulk" + query, data=data, follow_redirects=follow)


def main():
    init_db.create_all()
    s = get_session()
    try:
        init_db.seed(s)
    finally:
        s.close()

    app = create_app()
    app.config["TESTING"] = True
    oc = app.test_client()
    login(oc, "owner", "owner123")
    sc = app.test_client()
    login(sc, "staff", "staff123")
    vc = app.test_client()
    login(vc, "viewer", "viewer123")

    db = get_session()
    pid = db.query(Product).filter_by(sku="FC-MASK-001").first().id
    db.close()
    box0 = bal(pid, "boxed")
    res0 = bal(pid, "boxed", "reserved")
    print(f"初始：盒裝normal={box0} 盒裝reserved={res0}")

    # 建 3 單：o1 盒×2（未出貨）、o2 盒×1（未出貨）、o3 盒×1（出貨 + paid）
    o1, n1 = new_box_order(sc, pid, 2)
    o2, n2 = new_box_order(sc, pid, 1)
    o3, n3 = new_box_order(sc, pid, 1)
    check("前置：3 單建立、盒裝 = 初始−4", bal(pid, "boxed") == box0 - 4, f"now={bal(pid, 'boxed')}")
    oc.post(f"/orders/{o3}/shipping", data={"shipping_status": "shipped", "carrier": "黑貓",
                                            "tracking_no": "B1"}, follow_redirects=True)
    oc.post(f"/orders/{o3}/payment", data={"payment_status": "paid"}, follow_redirects=True)
    check("前置：o3 已出貨（shipments 1 列）且 paid", shipments_count(o3) == 1
          and order(o3).payment_status == "paid")
    box_pre = bal(pid, "boxed")

    # =====================================================================
    # A. 列表 UI
    # =====================================================================
    html = oc.get("/orders/").get_data(as_text=True)
    check("A owner 列表每列有勾選框（3 個 order_ids）", html.count(CB) == 3
          and f'value="{o1}"' in html and f'value="{o3}"' in html, f"count={html.count(CB)}")
    check("A owner 列表有全選、作廢按鈕、原因欄、action=/orders/void-bulk",
          'id="bulk-select-all"' in html and "作廢勾選訂單" in html
          and 'id="bulk-void-reason"' in html and "/orders/void-bulk" in html)
    shtml = sc.get("/orders/").get_data(as_text=True)
    check("A staff 列表也有勾選框與按鈕", CB in shtml and "作廢勾選訂單" in shtml)
    vhtml = vc.get("/orders/").get_data(as_text=True)
    check("A viewer 列表無勾選框 / 無作廢按鈕", CB not in vhtml
          and "作廢勾選訂單" not in vhtml and 'id="bulk-select-all"' not in vhtml)

    # =====================================================================
    # C. staff 批次含已出貨單 → 整批拒絕、0 筆變動（先測拒絕，再測 owner 成功）
    # =====================================================================
    mv_all = all_mv_count()
    r = bulk(sc, [o1, o2, o3], "staff 批次")
    t = r.get_data(as_text=True)
    check("C staff 批次含已出貨 → 拒絕訊息明列已出貨單號", r.status_code == 200
          and "已出貨" in t and n3 in t and "整批未執行" in t)
    check("C 整批拒絕：0 筆 voided", voided_count() == 0)
    check("C 整批拒絕：庫存不動、movement 不增、audit 0", bal(pid, "boxed") == box_pre
          and all_mv_count() == mv_all and audit_count("order_void") == 0)
    check("C 整批拒絕：o3 仍 paid", order(o3).payment_status == "paid")

    # =====================================================================
    # E. 缺原因 / 空選 / 不存在 / 非整數 / viewer
    # =====================================================================
    r = bulk(oc, [o1, o2], "")
    check("E 缺原因（空字串）→ 拒絕", "原因" in r.get_data(as_text=True) and voided_count() == 0)
    r = bulk(oc, [o1, o2], None)
    check("E 完全沒帶 reason 欄 → 拒絕", "原因" in r.get_data(as_text=True) and voided_count() == 0)
    r = bulk(oc, [], "有原因但空選")
    check("E 空選 → 提示「請先勾選」", "請先勾選" in r.get_data(as_text=True) and voided_count() == 0)
    r = bulk(oc, [o1, 999999], "含不存在")
    check("E 含不存在 id → 整批拒絕、o1 未作廢", "不存在" in r.get_data(as_text=True)
          and order(o1).voided_at is None and voided_count() == 0)
    r = oc.post("/orders/void-bulk", data={"order_ids": "abc", "reason": "x"}, follow_redirects=True)
    check("E 非整數 id → 拒絕", "有誤" in r.get_data(as_text=True) and voided_count() == 0)
    r = bulk(vc, [o1], "viewer", follow=False)
    check("E viewer 批次作廢 403", r.status_code == 403 and voided_count() == 0, f"status={r.status_code}")
    check("E 以上皆未變動庫存 / movement / audit", bal(pid, "boxed") == box_pre
          and all_mv_count() == mv_all and audit_count("order_void") == 0)

    # =====================================================================
    # B. owner 批次作廢 3 單（含重複勾選同一 id；帶篩選 query 回列表）
    # =====================================================================
    r = bulk(oc, [o1, o2, o3, o1], "客戶整批取消", query="?show_voided=1&q=")
    t = r.get_data(as_text=True)
    check("B 成功訊息「已作廢 3 筆（回補 2 筆庫存）」", "已作廢 3 筆（回補 2 筆庫存）" in t)
    check("B 訊息註明已出貨 1 筆不回補、已退款 1 筆", "已出貨單 1 筆不回補" in t and "已退款 1 筆" in t)
    check("B 回列表且保留篩選（show_voided=1 → 三單灰字列出）", n1 in t and n2 in t and n3 in t
          and "line-through" in t)
    check("B 3 筆 voided", voided_count() == 3
          and all(order(x).voided_at is not None for x in (o1, o2, o3)))
    check("B void_reason / voided_by 落地", order(o1).void_reason == "客戶整批取消"
          and order(o1).voided_by is not None)
    check("B 未出貨 2 單回補：盒裝 = 初始−1（o3 已出貨不回補）", bal(pid, "boxed") == box0 - 1,
          f"now={bal(pid, 'boxed')} expect={box0 - 1}")
    check("B o1/o2 各 1 筆 SALE_REVERSAL、o3 0 筆", mv_count(o1, "SALE_REVERSAL") == 1
          and mv_count(o2, "SALE_REVERSAL") == 1 and mv_count(o3, "SALE_REVERSAL") == 0)
    check("B reserved 未動", bal(pid, "boxed", "reserved") == res0)
    check("B o3 paid → refunded；o1 仍 unpaid", order(o3).payment_status == "refunded"
          and order(o1).payment_status == "unpaid")
    check("B shipments 列保留", shipments_count(o3) == 1)
    check("B audit order_void 共 3 筆（重複勾選不重複寫）", audit_count("order_void") == 3
          and audit_count("order_void", o1) == 1)
    d1, d2, d3 = audit_last("order_void", o1), audit_last("order_void", o2), audit_last("order_void", o3)
    check("B audit detail bulk=True 且三筆同一 bulk_group_id",
          all(d and d.get("bulk") is True for d in (d1, d2, d3))
          and d1["bulk_group_id"] == d2["bulk_group_id"] == d3["bulk_group_id"]
          and len(d1["bulk_group_id"]) == 32)
    check("B audit 沿用 CR-4 欄位：o1 stock_reversed=True、o3 shipped=True/refunded",
          d1["stock_reversed"] is True and len(d1["reversal_movement_ids"]) == 1
          and d3["shipped"] is True and d3["stock_reversed"] is False
          and d3["payment_status_after"] == "refunded" and d3["reason"] == "客戶整批取消")
    html = oc.get("/orders/?show_voided=1").get_data(as_text=True)
    check("A 作廢單列無勾選框（show_voided=1 三單皆作廢 → 0 個 order_ids）",
          html.count(CB) == 0 and n1 in html)
    lst = oc.get("/orders/").get_data(as_text=True)
    check("B 預設列表不含已作廢三單", not any(n in lst for n in (n1, n2, n3)))

    # =====================================================================
    # D. 已作廢單被跳過；staff 純未出貨批次成功
    # =====================================================================
    o4, n4 = new_box_order(sc, pid, 1)
    box_o4 = bal(pid, "boxed")
    r = bulk(sc, [o1, o4], "staff 第二批（含已作廢）")
    t = r.get_data(as_text=True)
    check("D staff 批次：o4 作廢、o1 跳過", order(o4).voided_at is not None
          and "已作廢 1 筆（回補 1 筆庫存）" in t and "跳過" in t and n1 in t)
    check("D o1 不重複 audit / 不二次回補", audit_count("order_void", o1) == 1
          and mv_count(o1, "SALE_REVERSAL") == 1 and bal(pid, "boxed") == box_o4 + 1)
    check("D o4 audit bulk_group_id 與第一批不同",
          audit_last("order_void", o4)["bulk_group_id"] != d1["bulk_group_id"])
    check("D o1 void_reason 未被覆蓋", order(o1).void_reason == "客戶整批取消")
    r = bulk(sc, [o1, o4], "全都作廢過")
    check("D 全部皆已作廢 → 提示無需處理、audit 不增", "皆已作廢" in r.get_data(as_text=True)
          and audit_count("order_void") == 4)

    o5, n5 = new_box_order(sc, pid, 1)
    o6, n6 = new_box_order(sc, pid, 1)
    r = bulk(sc, [o5, o6], "staff 未出貨兩單")
    check("D staff 純未出貨批次成功 2 筆", "已作廢 2 筆（回補 2 筆庫存）" in r.get_data(as_text=True)
          and voided_count() == 6)

    # =====================================================================
    # F. 單筆作廢不受影響（audit 無 bulk 欄）；手機無批次入口
    # =====================================================================
    o7, n7 = new_box_order(sc, pid, 1)
    r = sc.post(f"/orders/{o7}/void", data={"reason": "單筆"}, follow_redirects=True)
    d7 = audit_last("order_void", o7)
    check("F 單筆作廢仍可用且 audit 無 bulk 欄", order(o7).voided_at is not None
          and d7 and "bulk" not in d7 and "bulk_group_id" not in d7)
    check("F 期末庫存 = 初始 − 1（僅 o3 已出貨不回補）", bal(pid, "boxed") == box0 - 1,
          f"now={bal(pid, 'boxed')}")
    check("F 手機列表無批次入口（v1 不做）", "void-bulk" not in sc.get("/m/orders").get_data(as_text=True))

    print("\n==== 結果 ====")
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        print("\n總結：FAIL")
        return 1
    print("\n總結：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
