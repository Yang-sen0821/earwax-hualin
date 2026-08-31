# -*- coding: utf-8 -*-
"""CR-7 驗收：客戶明細頁「＋ 新增訂單」入口（桌面 + 手機，預填客戶）— 2026-08-31。

throwaway sqlite 放 %TEMP%，不污染交付庫；env 在 import 任何 app 模組前先設定。
驗證項目：
  A 桌面 GET /orders/new?customer_id=X 預填：hidden customer_id=X、可見輸入框 value=「姓名（電話）」
    不存在 id / 非數字 id ⇒ 200、不預填、不炸
  B 手機 GET /m/orders/new?customer_id=X 同上；不存在 id 不炸
  C 客戶明細頁（桌面 /customers/<id> + 手機 /m/customers/<id>）：
    staff / owner 看得到「＋ 新增訂單」連結且帶 customer_id；viewer / accounting 看不到
  D POST 建單帶預填 customer_id ⇒ 訂單綁定該客戶、不新建客戶（桌面 + 手機），導向訂單明細照舊
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_TMP_DB = os.path.join(tempfile.gettempdir(), "flora_court_custneworder_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
from db import get_session, Order, Product, Customer  # noqa: E402
from app import create_app  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def login(client, username, password):
    client.get("/logout")
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def main():
    print(f"=== throwaway DB: {_TMP_DB} ===")
    init_db.create_all()
    db0 = get_session()
    init_db.seed(db0)
    db0.close()

    db = get_session()
    mask = db.query(Product).filter_by(sku=init_db.MASK_SKU).first()
    pid = mask.id
    cust = Customer(name="預填客戶甲", phone="0911222333")
    cust_nophone = Customer(name="無電話客戶乙", phone=None)
    db.add_all([cust, cust_nophone])
    db.commit()
    cid, cid2 = cust.id, cust_nophone.id
    db.close()

    def cust_count():
        s = get_session()
        try:
            return s.query(Customer).count()
        finally:
            s.close()

    def last_order():
        s = get_session()
        try:
            return s.query(Order).order_by(Order.id.desc()).first()
        finally:
            s.close()

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()

    disp = "預填客戶甲（0911222333）"
    empty_id = 'id="cust-id" value=""'

    # =====================================================================
    # A：桌面建單頁預填
    # =====================================================================
    login(c, "staff", "staff123")
    r = c.get(f"/orders/new?customer_id={cid}")
    html = r.get_data(as_text=True)
    check("A staff GET /orders/new?customer_id 200", r.status_code == 200, f"got {r.status_code}")
    check("A hidden customer_id 預填",
          f'name="customer_id" id="cust-id" value="{cid}"' in html)
    check("A 可見輸入框預填「姓名（電話）」", f'value="{disp}"' in html)
    check("A hidden customer_display 預填同格式", f'id="cust-display" value="{disp}"' in html)
    check("A new_customer_name 保持空白", 'id="cust-new" value=""' in html)

    html = c.get(f"/orders/new?customer_id={cid2}").get_data(as_text=True)
    check("A 無電話客戶顯示只有姓名", 'value="無電話客戶乙"' in html
          and f'id="cust-id" value="{cid2}"' in html)

    r = c.get("/orders/new?customer_id=999999")
    html = r.get_data(as_text=True)
    check("A 不存在 id ⇒ 200 不炸", r.status_code == 200, f"got {r.status_code}")
    check("A 不存在 id ⇒ 不預填（cust-id 空）", empty_id in html)
    r = c.get("/orders/new?customer_id=abc")
    check("A 非數字 id ⇒ 200 不炸、不預填", r.status_code == 200
          and empty_id in r.get_data(as_text=True))
    r = c.get("/orders/new")
    check("A 無參數 ⇒ 200、照常空白", r.status_code == 200
          and empty_id in r.get_data(as_text=True))

    # =====================================================================
    # B：手機建單頁預填
    # =====================================================================
    r = c.get(f"/m/orders/new?customer_id={cid}")
    html = r.get_data(as_text=True)
    check("B staff GET /m/orders/new?customer_id 200", r.status_code == 200, f"got {r.status_code}")
    check("B 手機 hidden customer_id 預填",
          f'name="customer_id" id="cust-id" value="{cid}"' in html)
    check("B 手機可見輸入框預填「姓名（電話）」", f'value="{disp}"' in html)
    check("B 手機 new_customer_name 空白", 'id="cust-new" value=""' in html)
    r = c.get("/m/orders/new?customer_id=999999")
    check("B 手機不存在 id ⇒ 200 不炸、不預填", r.status_code == 200
          and empty_id in r.get_data(as_text=True))
    r = c.get("/m/orders/new")
    check("B 手機無參數 ⇒ 200、照常空白", r.status_code == 200
          and empty_id in r.get_data(as_text=True))

    # =====================================================================
    # C：客戶明細頁按鈕（權限比照建單）
    # =====================================================================
    desk_link = f'href="/orders/new?customer_id={cid}"'
    mob_link = f'href="/m/orders/new?customer_id={cid}"'
    label = "＋ 新增訂單"
    for user, pw, expect in (("staff", "staff123", True), ("owner", "owner123", True),
                             ("viewer", "viewer123", False), ("accounting", "accounting123", False)):
        login(c, user, pw)
        vis = "看得到" if expect else "看不到"
        r = c.get(f"/customers/{cid}")
        html = r.get_data(as_text=True)
        check(f"C {user} 桌面客戶明細 200", r.status_code == 200, f"got {r.status_code}")
        check(f"C {user} 桌面{vis}「{label}」連結", (desk_link in html and label in html) == expect)
        r = c.get(f"/m/customers/{cid}")
        html = r.get_data(as_text=True)
        check(f"C {user} 手機客戶明細 200", r.status_code == 200, f"got {r.status_code}")
        check(f"C {user} 手機{vis}「{label}」連結", (mob_link in html and label in html) == expect)

    # =====================================================================
    # D：POST 建單帶預填 customer_id ⇒ 綁定該客戶
    # =====================================================================
    login(c, "staff", "staff123")
    before = cust_count()
    r = c.post("/orders/new", data={
        "customer_id": str(cid), "customer_display": disp, "new_customer_name": "",
        "recipient_name": "收件人甲", "recipient_phone": "0911222333",
        "item_product_id": str(pid), "item_combo_code": "BOX",
        "item_qty": "1", "item_amount": "300",
    })
    o = last_order()
    loc = r.headers.get("Location") or ""
    check("D 桌面 POST 建單 302 導向訂單明細", r.status_code == 302 and o is not None
          and f"/orders/{o.id}" in loc, f"status={r.status_code} loc={loc}")
    check("D 桌面訂單綁定預填客戶", o is not None and o.customer_id == cid,
          f"customer_id={getattr(o, 'customer_id', None)}")
    check("D 桌面不新建客戶", cust_count() == before)

    before = cust_count()
    r = c.post("/m/orders/new", data={
        "customer_id": str(cid), "new_customer_name": "",
        "recipient_name": "收件人甲", "recipient_phone": "0911222333",
        "product_id": [str(pid), "", ""], "combo_code": ["BOX", "BOX", "BOX"],
        "qty": ["1", "", ""], "amount": ["300", "", ""],
    })
    o2 = last_order()
    loc = r.headers.get("Location") or ""
    check("D 手機 POST 建單 302 導向訂單明細", r.status_code == 302 and o2 is not None
          and f"/m/orders/{o2.id}" in loc, f"status={r.status_code} loc={loc}")
    check("D 手機訂單綁定預填客戶", o2 is not None and o is not None and o2.id != o.id
          and o2.customer_id == cid, f"customer_id={getattr(o2, 'customer_id', None)}")
    check("D 手機不新建客戶", cust_count() == before)

    print("\n=== 結果 ===")
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
