# -*- coding: utf-8 -*-
"""CR-2 驗收：客戶交易紀錄（桌面 /customers/<id> + 手機 /m/customers/<id>）— 2026-08-31。

throwaway sqlite 放 %TEMP%，不污染交付庫；env 在 import 任何 app 模組前先設定。
驗證項目：
  A 桌面明細列出 日期 / 品項（商品名 片/盒×qty）/ 運費；作廢單預設不列、?show_voided=1 灰字列出
  B 客戶小計：訂單數（非作廢）、累計金額（排除作廢與已退款、不含運費）、最近購買日
  C staff 看不到金額欄 / 運費欄 / 累計金額，仍看到訂單數與最近購買；owner/accounting 看得到
  D 手機 /m/customers/<id> 200、含訂單與品項；/m/customers 列表每列有連結；staff 手機亦不見累計金額
  E viewer 可讀（桌面 + 手機 200）；不存在的客戶 404
"""
import os
import sys
import tempfile
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_TMP_DB = os.path.join(tempfile.gettempdir(), "flora_court_custdetail_test.db")
if os.path.exists(_TMP_DB):
    os.remove(_TMP_DB)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ["ENABLE_MOBILE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import init_db  # noqa: E402
from db import get_session, Order, OrderItem, Product, Customer  # noqa: E402
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

    # ---- 樣本：1 客戶 4 單（1 paid、1 unpaid、1 refunded、1 voided）----
    db = get_session()
    mask = db.query(Product).filter_by(sku=init_db.MASK_SKU).first()
    mask_name = mask.name
    cust = Customer(name="交易紀錄客", phone="0900111222")
    db.add(cust)
    db.flush()

    def mk(no, created, pay, items, fee=0, voided=False):
        o = Order(order_no=no, customer_id=cust.id, recipient_name="收件人",
                  total_amount=0, shipping_fee=fee, payment_status=pay,
                  shipping_status="pending", created_at=created)
        if voided:
            o.voided_at = datetime(2026, 8, 30, 12, 0, 0)
            o.void_reason = "測試作廢"
        db.add(o)
        db.flush()
        total = 0
        for combo, qty, price in items:
            db.add(OrderItem(order_id=o.id, product_id=mask.id, combo_code=combo,
                             qty=qty, unit_price=price, subtotal=price * qty))
            total += price * qty
        o.total_amount = total
        db.flush()
        return o.id

    id_paid = mk("CD-PAID", datetime(2026, 8, 1, 10, 0), "paid",
                 [("BOX", 2, 500), ("LOOSE", 3, 120)], fee=60)          # 1360
    id_unpaid = mk("CD-UNPAID", datetime(2026, 8, 10, 10, 0), "unpaid",
                   [("BOX", 1, 500)], fee=0)                            # 500
    id_refund = mk("CD-REFUND", datetime(2026, 8, 20, 10, 0), "refunded",
                   [("BOX", 4, 500)], fee=60)                           # 2000 → 不計
    id_void = mk("CD-VOID", datetime(2026, 8, 25, 10, 0), "unpaid",
                 [("BOX", 9, 500)], fee=60, voided=True)                # 4500 → 不列不計
    db.commit()
    cid = cust.id
    db.close()

    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()

    # =====================================================================
    # A / B：owner 桌面明細
    # =====================================================================
    login(c, "owner", "owner123")
    r = c.get(f"/customers/{cid}")
    html = r.get_data(as_text=True)
    check("A owner /customers/<id> 200", r.status_code == 200, f"got {r.status_code}")
    check("A 列出日期 2026-08-01", "2026-08-01" in html)
    check("A 列出品項「商品名 盒×2」", f"{mask_name} 盒×2" in html)
    check("A 列出品項「商品名 片×3」（多品項換行）", f"{mask_name} 片×3" in html)
    check("A 有運費欄（表頭 + $60）", ">運費<" in html and "$60" in html)
    check("A 作廢單預設不列", "CD-VOID" not in html)
    check("A 有效單皆列出", all(n in html for n in ("CD-PAID", "CD-UNPAID", "CD-REFUND")))
    # 小計：訂單數 3（非作廢）；累計 1360+500=1860（排除 refunded 2000、voided 4500、不含運費）
    check("B 小計訂單數 3（非作廢，含已退款單）", ">3<" in html.replace(" ", ""))
    check("B 累計金額 $1860（排除作廢與已退款、不含運費）", "$1860" in html)
    check("B 累計金額不含已退款 2000 / 作廢 4500", "$3860" not in html and "$6360" not in html)
    check("B 最近購買 = 2026-08-20（不看作廢單 08-25）",
          "2026-08-20" in html and "2026-08-25" not in html)

    r2 = c.get(f"/customers/{cid}?show_voided=1")
    html2 = r2.get_data(as_text=True)
    check("A ?show_voided=1 列出作廢單並標「已作廢」", "CD-VOID" in html2 and "已作廢" in html2)
    check("A ?show_voided=1 小計不變（仍 $1860、訂單數 3）",
          "$1860" in html2 and ">3<" in html2.replace(" ", ""))

    # =====================================================================
    # C：staff 不見金額；accounting 見金額
    # =====================================================================
    login(c, "staff", "staff123")
    r = c.get(f"/customers/{cid}")
    html = r.get_data(as_text=True)
    check("C staff /customers/<id> 200", r.status_code == 200, f"got {r.status_code}")
    check("C staff 看不到累計金額", "累計金額" not in html)
    check("C staff 看不到金額 / 運費欄", ">金額<" not in html and ">運費<" not in html
          and "$1860" not in html and "$1360" not in html)
    check("C staff 仍看到訂單數與最近購買", "訂單數" in html and "最近購買" in html
          and "2026-08-20" in html)
    check("C staff 仍看到日期 / 品項", "2026-08-01" in html and f"{mask_name} 盒×2" in html)

    login(c, "accounting", "accounting123")
    html = c.get(f"/customers/{cid}").get_data(as_text=True)
    check("C accounting 看得到累計金額 $1860", "累計金額" in html and "$1860" in html)

    # =====================================================================
    # D：手機明細
    # =====================================================================
    login(c, "owner", "owner123")
    lst = c.get("/m/customers").get_data(as_text=True)
    check("D /m/customers 每列連到明細", f'href="/m/customers/{cid}"' in lst)
    r = c.get(f"/m/customers/{cid}")
    html = r.get_data(as_text=True)
    check("D owner /m/customers/<id> 200", r.status_code == 200, f"got {r.status_code}")
    check("D 手機含客戶電話與訂單編號", "0900111222" in html and "CD-PAID" in html)
    check("D 手機含品項與日期", f"{mask_name} 盒×2" in html and "2026-08-01" in html)
    check("D 手機作廢單不列", "CD-VOID" not in html)
    check("D 手機 owner 見累計金額 $1860", "$1860" in html)
    login(c, "staff", "staff123")
    html = c.get(f"/m/customers/{cid}").get_data(as_text=True)
    check("D 手機 staff 不見累計金額 / 單筆金額", "累計金額" not in html and "$1360" not in html)
    check("D 手機 staff 仍見訂單數與品項", "訂單數" in html and f"{mask_name} 盒×2" in html)

    # =====================================================================
    # E：viewer 可讀；404
    # =====================================================================
    login(c, "viewer", "viewer123")
    check("E viewer 桌面客戶明細 200", c.get(f"/customers/{cid}").status_code == 200)
    check("E viewer 手機客戶明細 200", c.get(f"/m/customers/{cid}").status_code == 200)
    check("E 不存在客戶 404（桌面）", c.get("/customers/999999").status_code == 404)
    check("E 不存在客戶 404（手機）", c.get("/m/customers/999999").status_code == 404)

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
