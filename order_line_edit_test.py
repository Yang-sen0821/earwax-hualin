# -*- coding: utf-8 -*-
"""CR-10 驗收：單筆訂單編輯可新增／刪除品項行 + 客戶頁入口 + 「已出貨」判定改規則 — 2026-09-01。

throwaway sqlite 放 %TEMP%，不污染交付庫；env 在 import 任何 app 模組前先設定。
驗證項目：
  A 未出貨單新增一行（盒 2）→ 庫存多扣 2、items 多 1、total 重算、audit lines_added
  B 刪一行 → 回補、items 少 1、audit lines_removed
  C 改數量 3→5 → 淨扣 2、audit lines_changed（qty_before/after）
  D 缺貨 rollback → 餘量與 items 不變、audit 不增
  E 規則變更：shipping_status=delivered 但 shipments 無列 → 不鎖：可加行／刪行；staff 可作廢並回補
  F 有 shipments 列的單仍鎖：POST 帶品項變更被忽略，只改備註；staff 作廢 403
  G 入口：客戶頁訂單列含 /orders/<id> 與 /orders/<id>/edit（未出貨）；有出貨紀錄無 edit；手機客戶頁含 /m/orders/<id>；
    viewer 看不到編輯入口且 GET /orders/<id>/edit 403；訂單明細頁編輯按鈕
  H 手機簡版編輯 /m/orders/<id>/edit：新增一行 → 扣庫存、audit via=mobile；有出貨紀錄導回明細
  I audit_util.summarize 人話含「新增行／刪除行／修改行」；/admin/audit 頁 200
"""
import os
import sys
import json
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_TMP_DB = os.path.join(tempfile.gettempdir(), "flora_court_lineedit_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
from db import (  # noqa: E402
    get_session, Order, OrderItem, InventoryBalance, InventoryMovement,
    Product, Shipment, AuditLog, Customer,
)
from audit_util import summarize  # noqa: E402
from app import create_app  # noqa: E402

PASS = []
FAIL = []


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


def items(oid):
    db = fresh()
    rows = db.query(OrderItem).filter_by(order_id=oid).order_by(OrderItem.id).all()
    out = [(r.combo_code, r.qty, float(r.subtotal or 0)) for r in rows]
    db.close()
    return out


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


def set_status(oid, pay=None, ship=None):
    """模擬客戶直接以 paid/delivered 建立、從未走出貨流程的歷史單（shipments 0 列）。"""
    db = fresh()
    o = db.query(Order).filter_by(id=oid).first()
    if pay:
        o.payment_status = pay
    if ship:
        o.shipping_status = ship
    db.commit()
    db.close()


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
    loose0 = bal(pid, "loose_piece")
    print(f"初始：盒裝normal={box0} 裸片normal={loose0}")

    # =====================================================================
    # A. 建單 盒×3 → 編輯新增一行 盒×2
    # =====================================================================
    sc.post("/orders/new", data={
        "new_customer_name": "CR10客", "recipient_name": "CR10客",
        "item_product_id": [str(pid)], "item_combo_code": ["BOX"],
        "item_qty": ["3"], "item_amount": ["900"],
    }, follow_redirects=True)
    oid, ono = last_order_id()
    db = fresh()
    cid = db.query(Customer).filter_by(name="CR10客").first().id
    db.close()
    check("A 建單 盒×3", bal(pid, "boxed") == box0 - 3 and items(oid) == [("BOX", 3, 900.0)])

    g = sc.get(f"/orders/{oid}/edit").get_data(as_text=True)
    check("A 編輯頁有「新增一行」與「刪除此行」JS、未鎖定", "add-row-btn" in g and "removeRow" in g
          and "刪除此行" in g and "var LOCKED = false" in g)

    r = sc.post(f"/orders/{oid}/edit", data={
        "recipient_name": "CR10客", "discount": "0",
        "item_product_id": [str(pid), str(pid)], "item_combo_code": ["BOX", "BOX"],
        "item_qty": ["3", "2"], "item_amount": ["900", "600"],
    }, follow_redirects=True)
    check("A 新增一行 200 有成功訊息", r.status_code == 200 and "訂單已更新" in r.get_data(as_text=True))
    check("A 庫存多扣 2（盒裝 = 初始−5）", bal(pid, "boxed") == box0 - 5, f"now={bal(pid, 'boxed')}")
    check("A items 多 1（2 列）", items(oid) == [("BOX", 3, 900.0), ("BOX", 2, 600.0)], str(items(oid)))
    check("A total 重算 1500", float(order(oid).total_amount) == 1500.0)
    d = audit_last("order_edit", oid)
    check("A audit lines_added 1 列（盒×2 $600）", d and len(d["lines_added"]) == 1
          and d["lines_added"][0]["qty"] == 2 and d["lines_added"][0]["combo_code"] == "BOX"
          and d["lines_removed"] == [] and d["lines_changed"] == [], str(d and d.get("lines_added")))
    check("A summarize 含「新增行」", "新增行" in summarize("order_edit", d) and "×2" in summarize("order_edit", d),
          summarize("order_edit", d))

    # =====================================================================
    # B. 刪一行（刪掉 盒×2）
    # =====================================================================
    r = sc.post(f"/orders/{oid}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid)], "item_combo_code": ["BOX"],
        "item_qty": ["3"], "item_amount": ["900"],
    }, follow_redirects=True)
    check("B 刪一行 → 回補 2（盒裝 = 初始−3）", bal(pid, "boxed") == box0 - 3, f"now={bal(pid, 'boxed')}")
    check("B items 少 1（1 列）", items(oid) == [("BOX", 3, 900.0)])
    check("B total 900", float(order(oid).total_amount) == 900.0)
    d = audit_last("order_edit", oid)
    check("B audit lines_removed 1 列（盒×2）", d and len(d["lines_removed"]) == 1
          and d["lines_removed"][0]["qty"] == 2 and d["lines_added"] == [])
    check("B summarize 含「刪除行」", "刪除行" in summarize("order_edit", d), summarize("order_edit", d))

    # =====================================================================
    # C. 改數量 3→5
    # =====================================================================
    r = sc.post(f"/orders/{oid}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid)], "item_combo_code": ["BOX"],
        "item_qty": ["5"], "item_amount": ["1500"],
    }, follow_redirects=True)
    check("C 改數量 3→5 淨扣 2（盒裝 = 初始−5）", bal(pid, "boxed") == box0 - 5, f"now={bal(pid, 'boxed')}")
    check("C items 仍 1 列 盒×5", items(oid) == [("BOX", 5, 1500.0)])
    d = audit_last("order_edit", oid)
    check("C audit lines_changed qty 3→5", d and len(d["lines_changed"]) == 1
          and d["lines_changed"][0]["qty_before"] == 3 and d["lines_changed"][0]["qty_after"] == 5
          and d["lines_added"] == [] and d["lines_removed"] == [])
    smry = summarize("order_edit", d)
    check("C summarize 含「修改行」與 3→5", "修改行" in smry and "3→5" in smry, smry)
    check("C summarize 不列未變欄位（收件人）", "收件人" not in smry, smry)

    # =====================================================================
    # D. 缺貨 rollback
    # =====================================================================
    mv_all = all_mv_count()
    n_audit = audit_count("order_edit", oid)
    r = sc.post(f"/orders/{oid}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid), str(pid)], "item_combo_code": ["BOX", "LOOSE"],
        "item_qty": ["5", "999999"], "item_amount": ["1500", "1"],
    }, follow_redirects=True)
    check("D 缺貨有提示", "庫存不足" in r.get_data(as_text=True))
    check("D 餘量不變（盒裝 初始−5、裸片 初始）", bal(pid, "boxed") == box0 - 5 and bal(pid, "loose_piece") == loose0)
    check("D items 不變", items(oid) == [("BOX", 5, 1500.0)])
    check("D movement / audit 不增", all_mv_count() == mv_all and audit_count("order_edit", oid) == n_audit)

    # 空品項（全部刪光）被拒
    r = sc.post(f"/orders/{oid}/edit", data={"discount": "0"}, follow_redirects=True)
    check("D 全部刪光被拒（至少一個品項）", "至少" in r.get_data(as_text=True) and items(oid) == [("BOX", 5, 1500.0)])

    # =====================================================================
    # E. 規則變更：delivered/paid 但 shipments 0 列 → 不鎖；可加行；staff 可作廢回補
    # =====================================================================
    set_status(oid, pay="paid", ship="delivered")
    check("E 前置：delivered 且 shipments 0 列", order(oid).shipping_status == "delivered" and shipments_count(oid) == 0)
    g = sc.get(f"/orders/{oid}/edit").get_data(as_text=True)
    check("E delivered 無出貨紀錄 → 編輯頁不鎖", "var LOCKED = false" in g and "品項與折扣鎖定" not in g)
    r = sc.post(f"/orders/{oid}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid), str(pid)], "item_combo_code": ["BOX", "LOOSE"],
        "item_qty": ["5", "2"], "item_amount": ["1500", "100"],
    }, follow_redirects=True)
    check("E delivered 單加一行 片×2 → 裸片 −2", bal(pid, "loose_piece") == loose0 - 2 and len(items(oid)) == 2,
          f"loose={bal(pid, 'loose_piece')} items={items(oid)}")
    r = sc.post(f"/orders/{oid}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid)], "item_combo_code": ["BOX"],
        "item_qty": ["5"], "item_amount": ["1500"],
    }, follow_redirects=True)
    check("E delivered 單刪一行 → 裸片回補", bal(pid, "loose_piece") == loose0 and len(items(oid)) == 1)
    det = sc.get(f"/orders/{oid}").get_data(as_text=True)
    check("E 明細頁編輯按鈕為「新增／刪除品項」版（未鎖）", "edit-order-btn" in det and "新增／刪除品項" in det)
    cr = sc.get(f"/customers/{cid}")
    cpage = cr.get_data(as_text=True)
    check("E 客戶頁 delivered 單仍有編輯快捷", f"/orders/{oid}/edit" in cpage,
          f"status={cr.status_code} lines={[l.strip()[:80] for l in cpage.splitlines() if '/orders/' in l][:6]}")
    r = sc.post(f"/orders/{oid}/void", data={"reason": "CR10 規則測試"}, follow_redirects=True)
    t = r.get_data(as_text=True)
    check("E staff 作廢 delivered 無出貨紀錄單成功且回補", order(oid).voided_at is not None
          and "庫存已回補" in t and bal(pid, "boxed") == box0, f"box={bal(pid, 'boxed')} expect={box0}")
    dv = audit_last("order_void", oid)
    check("E audit order_void shipped=False stock_reversed=True refunded", dv and dv["shipped"] is False
          and dv["stock_reversed"] is True and dv["payment_status_after"] == "refunded")

    # =====================================================================
    # F. 有 shipments 列的單仍鎖
    # =====================================================================
    sc.post("/orders/new", data={
        "customer_id": str(cid), "recipient_name": "CR10客",
        "item_product_id": [str(pid)], "item_combo_code": ["BOX"],
        "item_qty": ["1"], "item_amount": ["300"],
    }, follow_redirects=True)
    oid2, ono2 = last_order_id()
    oc.post(f"/orders/{oid2}/shipping", data={"shipping_status": "shipped", "carrier": "黑貓"},
            follow_redirects=True)
    check("F 前置：出貨建 shipments 列", shipments_count(oid2) == 1)
    box_f = bal(pid, "boxed")
    mv_f = all_mv_count()
    g = sc.get(f"/orders/{oid2}/edit").get_data(as_text=True)
    check("F 有出貨紀錄 → 編輯頁鎖定", "var LOCKED = true" in g and "品項與折扣鎖定" in g and "add-row-btn" not in g)
    r = sc.post(f"/orders/{oid2}/edit", data={
        "recipient_name": "CR10客", "note": "F 備註", "shipping_fee": "60", "discount": "50",
        "item_product_id": [str(pid), str(pid)], "item_combo_code": ["BOX", "BOX"],
        "item_qty": ["5", "1"], "item_amount": ["1500", "300"],
    }, follow_redirects=True)
    o2 = order(oid2)
    check("F POST 帶品項變更被忽略：items/discount 不變、備註運費已改", items(oid2) == [("BOX", 1, 300.0)]
          and float(o2.discount or 0) == 0.0 and o2.note == "F 備註" and float(o2.shipping_fee) == 60.0)
    check("F 庫存/movement 不動", bal(pid, "boxed") == box_f and all_mv_count() == mv_f)
    d = audit_last("order_edit", oid2)
    check("F audit items_locked=True 且 lines 皆空", d and d["items_locked"] is True and d["lines_added"] == []
          and d["lines_removed"] == [] and d["lines_changed"] == [])
    check("F summarize 含「品項鎖定」與備註變更", "品項鎖定" in summarize("order_edit", d) and "備註" in summarize("order_edit", d),
          summarize("order_edit", d))
    r = sc.post(f"/orders/{oid2}/void", data={"reason": "x"})
    check("F staff 作廢有出貨紀錄單 → 403", r.status_code == 403 and order(oid2).voided_at is None)
    det2 = sc.get(f"/orders/{oid2}").get_data(as_text=True)
    check("F 明細頁編輯按鈕為「僅收件/運費/備註」", "僅收件/運費/備註" in det2 and "edit-order-btn" not in det2)

    # =====================================================================
    # G. 入口
    # =====================================================================
    sc.post("/orders/new", data={
        "customer_id": str(cid), "recipient_name": "CR10客",
        "item_product_id": [str(pid)], "item_combo_code": ["LOOSE"],
        "item_qty": ["2"], "item_amount": ["100"],
    }, follow_redirects=True)
    oid3, ono3 = last_order_id()
    cr = sc.get(f"/customers/{cid}")
    cpage = cr.get_data(as_text=True)
    check("G 客戶頁訂單列含 /orders/<id>（未出貨 oid3 / 有出貨 oid2）", f"/orders/{oid3}" in cpage and f"/orders/{oid2}" in cpage,
          f"status={cr.status_code} oid2={oid2} oid3={oid3} lines={[l.strip()[:80] for l in cpage.splitlines() if '/orders/' in l][:8]}")
    check("G 客戶頁未出貨單有 /orders/<id>/edit 快捷", f'href="/orders/{oid3}/edit"' in cpage)
    check("G 客戶頁有出貨紀錄單無 edit 快捷", f'href="/orders/{oid2}/edit"' not in cpage)
    mpage = sc.get(f"/m/customers/{cid}").get_data(as_text=True)
    check("G 手機客戶頁訂單列含 /m/orders/<id>", f'href="/m/orders/{oid3}"' in mpage)
    lst = sc.get("/orders/").get_data(as_text=True)
    check("G 訂單列表單號連到明細", f'href="/orders/{oid3}"' in lst)
    vpage = vc.get(f"/customers/{cid}").get_data(as_text=True)
    check("G viewer 客戶頁無編輯快捷、仍可點明細", f"/orders/{oid3}/edit" not in vpage and f"/orders/{oid3}" in vpage)
    vdet = vc.get(f"/orders/{oid3}").get_data(as_text=True)
    check("G viewer 訂單明細無編輯按鈕", f"/orders/{oid3}/edit" not in vdet)
    check("G viewer GET /orders/<id>/edit → 403", vc.get(f"/orders/{oid3}/edit").status_code == 403)
    check("G viewer POST 編輯 → 403", vc.post(f"/orders/{oid3}/edit", data={"item_product_id": str(pid),
          "item_combo_code": "LOOSE", "item_qty": "9", "item_amount": "1"}).status_code == 403)
    vm = vc.get(f"/m/orders/{oid3}").get_data(as_text=True)
    check("G viewer 手機明細無編輯入口", f"/m/orders/{oid3}/edit" not in vm)

    # =====================================================================
    # H. 手機簡版編輯
    # =====================================================================
    mdet = sc.get(f"/m/orders/{oid3}").get_data(as_text=True)
    check("H 手機明細有編輯品項入口與桌面連結", f'href="/m/orders/{oid3}/edit"' in mdet and f'href="/orders/{oid3}/edit"' in mdet)
    g = sc.get(f"/m/orders/{oid3}/edit")
    gt = g.get_data(as_text=True)
    check("H 手機編輯頁 200、含新增一行／刪除按鈕、預填 片×2", g.status_code == 200 and "新增一行" in gt
          and "removeLine" in gt and 'value="2"' in gt)
    loose_h = bal(pid, "loose_piece")
    box_h = bal(pid, "boxed")
    r = sc.post(f"/m/orders/{oid3}/edit", data={
        "product_id": [str(pid), str(pid)], "combo_code": ["LOOSE", "BOX"],
        "qty": ["2", "1"], "amount": ["100", "300"], "discount": "0",
    }, follow_redirects=True)
    check("H 手機新增一行 盒×1 → 盒裝 −1、裸片不變、items 2", bal(pid, "boxed") == box_h - 1
          and bal(pid, "loose_piece") == loose_h and len(items(oid3)) == 2, str(items(oid3)))
    check("H total 400", float(order(oid3).total_amount) == 400.0)
    d = audit_last("order_edit", oid3)
    check("H audit via=mobile、lines_added 盒×1", d and d.get("via") == "mobile" and len(d["lines_added"]) == 1
          and d["lines_added"][0]["combo_code"] == "BOX")
    check("H summarize 含「手機」", "手機" in summarize("order_edit", d))
    r = sc.post(f"/m/orders/{oid3}/edit", data={
        "product_id": [str(pid)], "combo_code": ["LOOSE"], "qty": ["2"], "amount": ["100"], "discount": "0",
    }, follow_redirects=True)
    check("H 手機刪一行 → 盒裝回補", bal(pid, "boxed") == box_h and len(items(oid3)) == 1)
    r = sc.post(f"/m/orders/{oid3}/edit", data={
        "product_id": [str(pid)], "combo_code": ["LOOSE"], "qty": ["999999"], "amount": ["1"], "discount": "0",
    }, follow_redirects=True)
    check("H 手機缺貨 rollback", "庫存不足" in r.get_data(as_text=True) and items(oid3) == [("LOOSE", 2, 100.0)]
          and bal(pid, "loose_piece") == loose_h)
    r = sc.get(f"/m/orders/{oid2}/edit", follow_redirects=True)
    check("H 有出貨紀錄單手機編輯 → 導回明細並提示鎖定", "品項鎖定" in r.get_data(as_text=True))
    mdet2 = sc.get(f"/m/orders/{oid2}").get_data(as_text=True)
    check("H 有出貨紀錄單手機明細無「編輯品項」按鈕", f'href="/m/orders/{oid2}/edit"' not in mdet2)
    check("H viewer 手機編輯 403", vc.get(f"/m/orders/{oid3}/edit").status_code == 403)

    # =====================================================================
    # I. 操作紀錄頁
    # =====================================================================
    ap = oc.get("/admin/audit/?action=order_edit")
    at = ap.get_data(as_text=True)
    check("I /admin/audit 200 且含 order_edit 人話（新增行/刪除行/修改行）", ap.status_code == 200
          and ("新增行" in at or "刪除行" in at or "修改行" in at))

    print(f"\nPASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
