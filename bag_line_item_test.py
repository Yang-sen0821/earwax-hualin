# -*- coding: utf-8 -*-
"""紙袋當品項驗收（森哥 2026-09-01「訂單建立也要能選擇紙袋當品項」）。

throwaway sqlite 放 %TEMP%；env 在 import 任何 app 模組前先設定。
驗證項目：
  A 建單頁（桌面／手機）商品下拉含「包材-共用」、單位含 BAG(袋)
  B staff 建單 BOX 2 + BAG 2（0 元）→ 面膜 boxed −2、包材 paper_bag −2、total 只含面膜金額；明細顯示「袋」
  C BAG 填 50 元 → total 含 50
  D 作廢 → 兩者皆回補
  E 紙袋缺貨 → 整張 rollback（面膜、紙袋餘量與訂單數不變）
  F 單位錯配後端擋：包材配 BOX／面膜配 BAG 皆拒
  G 手機 /m/orders/new 建 BOX 1 + BAG 1 → 兩池各扣
  H 編輯（CR-10 整合）：既有單新增 BAG 一行 → paper_bag 再 −1；刪掉 → 回補
  I 報表排行含「袋」列、Excel 匯出 200；庫存頁「已消耗」含紙袋銷售
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_TMP_DB = os.path.join(tempfile.gettempdir(), "flora_court_bagline_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
from db import (  # noqa: E402
    get_session, Order, OrderItem, InventoryBalance, InventoryMovement, Product,
)
import inventory_service  # noqa: E402
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


def last_order():
    db = fresh()
    o = db.query(Order).order_by(Order.id.desc()).first()
    out = (o.id, o.order_no, float(o.total_amount or 0)) if o else (None, None, None)
    db.close()
    return out


def order_count():
    db = fresh()
    n = db.query(Order).count()
    db.close()
    return n


def items(oid):
    db = fresh()
    rows = db.query(OrderItem).filter_by(order_id=oid).order_by(OrderItem.id).all()
    out = [(r.product_id, r.combo_code, r.qty, float(r.subtotal or 0)) for r in rows]
    db.close()
    return out


def mv_count(oid):
    db = fresh()
    n = db.query(InventoryMovement).filter_by(ref_type="order", ref_id=str(oid)).count()
    db.close()
    return n


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

    db = get_session()
    mask = db.query(Product).filter_by(sku="FC-MASK-001").first()
    pkg = db.query(Product).filter_by(sku="PKG-SHARED-001").first()
    pid, kid, kname = mask.id, pkg.id, pkg.name
    db.close()
    box0 = bal(pid, "boxed")
    bag0 = bal(kid, "paper_bag")
    print(f"初始：面膜盒裝={box0} 紙袋={bag0}（包材商品 #{kid} {kname}）")

    check("UNIT_MAP 含 BAG→paper_bag", inventory_service._UNIT_MAP.get("BAG") == ("paper_bag", 1))

    # A. 下拉
    g = sc.get("/orders/new").get_data(as_text=True)
    check("A 桌面建單頁商品下拉含包材商品且 pkg:true", kname in g and "pkg:true" in g and '"BAG"' in g)
    gm = sc.get("/m/orders/new").get_data(as_text=True)
    check("A 手機建單頁含包材商品 data-pkg=1 與 BAG 選項", 'data-pkg="1"' in gm and 'value="BAG"' in gm and "袋" in gm)

    # B. 建單 BOX 2 + BAG 2（0 元）
    r = sc.post("/orders/new", data={
        "new_customer_name": "紙袋客", "recipient_name": "紙袋客",
        "item_product_id": [str(pid), str(kid)], "item_combo_code": ["BOX", "BAG"],
        "item_qty": ["2", "2"], "item_amount": ["600", "0"],
    }, follow_redirects=True)
    oid, ono, total = last_order()
    check("B staff 建單成功", r.status_code == 200 and "已建立" in r.get_data(as_text=True), r.get_data(as_text=True)[:200])
    check("B 面膜 boxed −2", bal(pid, "boxed") == box0 - 2, f"now={bal(pid, 'boxed')}")
    check("B 紙袋 paper_bag −2", bal(kid, "paper_bag") == bag0 - 2, f"now={bal(kid, 'paper_bag')}")
    check("B total 只含面膜 600（袋 0 元）", total == 600.0, f"total={total}")
    check("B items 2 列（BOX / BAG）", items(oid) == [(pid, "BOX", 2, 600.0), (kid, "BAG", 2, 0.0)], str(items(oid)))
    check("B 2 筆 SALE movement", mv_count(oid) == 2)
    det = sc.get(f"/orders/{oid}").get_data(as_text=True)
    check("B 明細頁顯示「袋」與包材商品名", "袋" in det and kname in det)
    lst = sc.get("/orders/").get_data(as_text=True)
    check("B 訂單列表 200 含該單", ono in lst)
    db = fresh()
    from db import Customer
    cid = db.query(Customer).filter_by(name="紙袋客").first().id
    db.close()
    cp = sc.get(f"/customers/{cid}").get_data(as_text=True)
    check("B 客戶頁品項欄顯示 袋×2", "袋×2" in cp, cp[cp.find("袋") - 30: cp.find("袋") + 10] if "袋" in cp else "no 袋")

    # C. BAG 50 元
    sc.post("/orders/new", data={
        "customer_id": str(cid), "recipient_name": "紙袋客",
        "item_product_id": [str(pid), str(kid)], "item_combo_code": ["BOX", "BAG"],
        "item_qty": ["1", "1"], "item_amount": ["300", "50"],
    }, follow_redirects=True)
    oid_c, _, total_c = last_order()
    check("C 袋填 50 → total 350", total_c == 350.0 and oid_c != oid, f"total={total_c}")
    check("C 紙袋 −3 累計", bal(kid, "paper_bag") == bag0 - 3)

    # D. 作廢 B 單 → 兩者回補
    r = sc.post(f"/orders/{oid}/void", data={"reason": "紙袋測試作廢"}, follow_redirects=True)
    check("D 作廢成功且回補訊息", "庫存已回補" in r.get_data(as_text=True))
    check("D 面膜回補（初始−1，僅 C 單）", bal(pid, "boxed") == box0 - 1, f"now={bal(pid, 'boxed')}")
    check("D 紙袋回補（初始−1，僅 C 單）", bal(kid, "paper_bag") == bag0 - 1, f"now={bal(kid, 'paper_bag')}")
    check("D movement 2 SALE + 2 SALE_REVERSAL", mv_count(oid) == 4)

    # E. 紙袋缺貨 rollback
    n0 = order_count()
    r = sc.post("/orders/new", data={
        "customer_id": str(cid), "recipient_name": "紙袋客",
        "item_product_id": [str(pid), str(kid)], "item_combo_code": ["BOX", "BAG"],
        "item_qty": ["1", "999999"], "item_amount": ["300", "0"],
    }, follow_redirects=True)
    check("E 紙袋缺貨提示", "庫存不足" in r.get_data(as_text=True))
    check("E 整張 rollback：訂單數不變、面膜/紙袋餘量不變", order_count() == n0
          and bal(pid, "boxed") == box0 - 1 and bal(kid, "paper_bag") == bag0 - 1)

    # F. 單位錯配
    r = sc.post("/orders/new", data={
        "customer_id": str(cid), "item_product_id": [str(kid)], "item_combo_code": ["BOX"],
        "item_qty": ["1"], "item_amount": ["0"],
    }, follow_redirects=True)
    check("F 包材配 BOX 被拒", "只能選「袋」" in r.get_data(as_text=True) and order_count() == n0)
    r = sc.post("/orders/new", data={
        "customer_id": str(cid), "item_product_id": [str(pid)], "item_combo_code": ["BAG"],
        "item_qty": ["1"], "item_amount": ["0"],
    }, follow_redirects=True)
    check("F 面膜配 BAG 被拒", "不能以「袋」" in r.get_data(as_text=True) and order_count() == n0
          and bal(kid, "paper_bag") == bag0 - 1)
    r = sc.post("/m/orders/new", data={
        "product_id": [str(kid)], "combo_code": ["BOX"], "qty": ["1"], "amount": ["0"],
    }, follow_redirects=True)
    check("F 手機包材配 BOX 被拒", "只能選「袋」" in r.get_data(as_text=True) and order_count() == n0)

    # G. 手機建單 BOX 1 + BAG 1
    r = sc.post("/m/orders/new", data={
        "customer_id": str(cid), "recipient_name": "紙袋客",
        "product_id": [str(pid), str(kid)], "combo_code": ["BOX", "BAG"],
        "qty": ["1", "1"], "amount": ["300", "0"],
    }, follow_redirects=True)
    oid_g, _, total_g = last_order()
    check("G 手機建單成功 total 300", order_count() == n0 + 1 and total_g == 300.0, r.get_data(as_text=True)[:200])
    check("G 手機建單：面膜 −1、紙袋 −1", bal(pid, "boxed") == box0 - 2 and bal(kid, "paper_bag") == bag0 - 2)
    mdet = sc.get(f"/m/orders/{oid_g}").get_data(as_text=True)
    check("G 手機明細顯示 袋 × 1", "袋 × 1" in mdet)

    # H. 編輯整合：C 單新增 BAG 一行
    r = sc.post(f"/orders/{oid_c}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid), str(kid), str(kid)], "item_combo_code": ["BOX", "BAG", "BAG"],
        "item_qty": ["1", "1", "1"], "item_amount": ["300", "50", "0"],
    }, follow_redirects=True)
    check("H 編輯新增 BAG 一行 → 紙袋再 −1", bal(kid, "paper_bag") == bag0 - 3 and len(items(oid_c)) == 3,
          f"bag={bal(kid, 'paper_bag')} items={items(oid_c)}")
    r = sc.post(f"/orders/{oid_c}/edit", data={
        "discount": "0",
        "item_product_id": [str(pid)], "item_combo_code": ["BOX"],
        "item_qty": ["1"], "item_amount": ["300"],
    }, follow_redirects=True)
    db = fresh()
    total_h = float(db.query(Order).filter_by(id=oid_c).first().total_amount or 0)
    db.close()
    check("H 編輯刪掉 BAG 兩行 → total 300、items 1", total_h == 300.0 and len(items(oid_c)) == 1, f"total={total_h}")
    check("H 紙袋餘量回補 = 初始−1（僅 G 單）", bal(kid, "paper_bag") == bag0 - 1, f"now={bal(kid, 'paper_bag')}")
    g = sc.get(f"/orders/{oid_g}/edit").get_data(as_text=True)
    check("H 編輯頁預填 BAG 列", '"combo_code": "BAG"' in g)
    r = sc.post(f"/m/orders/{oid_g}/edit", data={
        "product_id": [str(pid), str(kid)], "combo_code": ["BOX", "BAG"],
        "qty": ["1", "3"], "amount": ["300", "0"], "discount": "0",
    }, follow_redirects=True)
    check("H 手機編輯 BAG 1→3 → 紙袋 −3", bal(kid, "paper_bag") == bag0 - 3, f"now={bal(kid, 'paper_bag')}")

    # I. 報表 / Excel / 庫存頁
    rp = oc.get("/reports/")
    rt = rp.get_data(as_text=True)
    check("I 報表首頁 200 排行含「袋」列", rp.status_code == 200 and "袋" in rt and "BAG" in rt)
    rs = oc.get("/reports/sales")
    check("I 銷售報表 200", rs.status_code == 200)
    xl = oc.get("/reports/export.xlsx")
    check("I Excel 匯出 200 且為 xlsx", xl.status_code == 200 and "sheet" in (xl.content_type or "")
          and len(xl.data) > 1000, f"status={xl.status_code} ct={xl.content_type}")
    inv = oc.get("/inventory/")
    check("I 庫存頁 200", inv.status_code == 200)
    db = fresh()
    consumed = inventory_service.consumed_by_pool(db)
    db.close()
    check("I 已消耗含紙袋銷售（paper_bag 消耗 = 3）", consumed.get((kid, "paper_bag")) == 3,
          f"consumed={consumed.get((kid, 'paper_bag'))}")

    print(f"\nPASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
