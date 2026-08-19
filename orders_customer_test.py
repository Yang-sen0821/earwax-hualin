# -*- coding: utf-8 -*-
"""建單客戶欄「打字過濾 ＋ 找不到就當場新增」handler 級測試。

對應森哥 2026-08-19 第 1 項：訂單輸入時要能打字過濾客戶名單，不要純下拉；
名字不在名單中時，可直接新增到客戶名單，不用重複作業。

執行：python orders_customer_test.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = os.path.join(tempfile.gettempdir(), "flora_court_ordercust_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"

import init_db  # noqa: E402
init_db.main()

from db import get_session, Product, Customer, Order  # noqa: E402
from app import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {}  {}".format("PASS" if ok else "FAIL", name, detail))


def fresh():
    s = get_session()
    s.expire_all()
    return s


PID = fresh().query(Product).filter_by(active=True, is_packaging=False).first().id


def cust_count():
    return fresh().query(Customer).count()


def cust_by_name(n):
    return fresh().query(Customer).filter_by(name=n).first()


def cust_id(n):
    """回純 int，避免 ORM 物件跨 session 失效。"""
    row = fresh().query(Customer.id).filter(Customer.name == n).first()
    return row[0] if row else None


def last_order_customer_id():
    row = (fresh().query(Order.customer_id).order_by(Order.id.desc()).first())
    return row[0] if row else None


c = app.test_client()
c.post("/login", data={"username": "staff", "password": "staff123"}, follow_redirects=True)

# =====================================================================
# 1. 畫面：不再是純下拉
# =====================================================================
html = c.get("/orders/new").get_data(as_text=True)
check("1a) 桌面建單頁客戶欄不再是 select（純下拉已移除）",
      '<select name="customer_id"' not in html)
check("1b) 桌面建單頁有可打字的客戶輸入框與過濾腳本",
      'id="cust-input"' in html and 'id="cust-data"' in html
      and 'name="new_customer_name"' in html)
mhtml = c.get("/m/orders/new").get_data(as_text=True)
check("1c) 手機建單頁同樣改為可打字過濾",
      '<select name="customer_id"' not in mhtml and 'id="cust-input"' in mhtml
      and 'name="new_customer_name"' in mhtml)

# =====================================================================
# 2. 打的名字不在名單 → 當場新增（桌面）
# =====================================================================
before = cust_count()
r = c.post("/orders/new", data={
    "new_customer_name": "測試新客甲",
    "recipient_name": "測試新客甲", "recipient_phone": "0912345678",
    "item_product_id": str(PID), "item_combo_code": "BOX",
    "item_qty": "1", "item_amount": "300",
}, follow_redirects=True)
newc_id = cust_id("測試新客甲")
newc_phone = (fresh().query(Customer.phone)
              .filter(Customer.name == "測試新客甲").first() or [None])[0]
check("2a) 桌面：名單裡沒有的名字 → 訂單建立且客戶同時被新增",
      r.status_code == 200 and cust_count() == before + 1 and newc_id is not None,
      "status={} 客戶數 {}->{}".format(r.status_code, before, cust_count()))
check("2b) 新客戶自動綁到這張訂單上", last_order_customer_id() == newc_id)
check("2c) 新客戶沿用收件電話（不用再補一次）",
      newc_phone == "0912345678", "phone={}".format(newc_phone))
check("2d) 成功訊息有告知客戶已新增",
      "同時新增客戶" in r.get_data(as_text=True))

# =====================================================================
# 3. 已從清單選定客戶 → 以選定者為準，不會多開一筆
# =====================================================================
before = cust_count()
r = c.post("/orders/new", data={
    "customer_id": str(newc_id), "new_customer_name": "不該被建立的名字",
    "item_product_id": str(PID), "item_combo_code": "BOX",
    "item_qty": "1", "item_amount": "300",
}, follow_redirects=True)
check("3a) 已選既有客戶時，忽略打字內容，不會誤建新客戶",
      cust_count() == before and cust_by_name("不該被建立的名字") is None)
check("3b) 訂單綁的是選定的既有客戶", last_order_customer_id() == newc_id)

# =====================================================================
# 4. 打的名字剛好與既有客戶同名 → 沿用，不建重複的
# =====================================================================
before = cust_count()
r = c.post("/orders/new", data={
    "new_customer_name": "測試新客甲",
    "item_product_id": str(PID), "item_combo_code": "BOX",
    "item_qty": "1", "item_amount": "300",
}, follow_redirects=True)
check("4) 打的名字與既有客戶同名 → 沿用既有，不建重複",
      cust_count() == before and last_order_customer_id() == newc_id)

# =====================================================================
# 5. 訂單失敗 → 客戶不留下半筆（同一 transaction）
# =====================================================================
before = cust_count()
r = c.post("/orders/new", data={
    "new_customer_name": "庫存不足不該留下",
    "item_product_id": str(PID), "item_combo_code": "BOX",
    "item_qty": "999999", "item_amount": "300",
}, follow_redirects=True)
check("5) 庫存不足整張訂單不成立時，剛打的新客戶也不會留下",
      cust_count() == before and cust_by_name("庫存不足不該留下") is None
      and "庫存不足" in r.get_data(as_text=True))

# =====================================================================
# 6. 姓名長度上限
# =====================================================================
before = cust_count()
r = c.post("/orders/new", data={
    "new_customer_name": "長" * 65,
    "item_product_id": str(PID), "item_combo_code": "BOX",
    "item_qty": "1", "item_amount": "300",
}, follow_redirects=True)
check("6) 姓名超過 64 字 → 擋下且不建客戶",
      cust_count() == before and "過長" in r.get_data(as_text=True))

# =====================================================================
# 7. 手機版同行為
# =====================================================================
before = cust_count()
r = c.post("/m/orders/new", data={
    "new_customer_name": "測試新客乙",
    "recipient_phone": "0955000111",
    "product_id": str(PID), "combo_code": "BOX", "qty": "1", "amount": "300",
}, follow_redirects=True)
lb_id = cust_id("測試新客乙")
check("7a) 手機：打的名字不在名單 → 訂單建立且客戶同時被新增",
      cust_count() == before + 1 and lb_id is not None and last_order_customer_id() == lb_id,
      "客戶數 {}->{}".format(before, cust_count()))
check("7b) 手機成功訊息有告知客戶已新增",
      "同時新增客戶" in r.get_data(as_text=True))

before = cust_count()
r = c.post("/m/orders/new", data={
    "customer_id": str(lb_id), "new_customer_name": "手機不該建立",
    "product_id": str(PID), "combo_code": "BOX", "qty": "1", "amount": "300",
}, follow_redirects=True)
check("7c) 手機：已選既有客戶時不會誤建新客戶",
      cust_count() == before and cust_by_name("手機不該建立") is None)

# =====================================================================
# 8. 不綁客戶仍可建單（原行為未回歸）
# =====================================================================
before = cust_count()
r = c.post("/orders/new", data={
    "item_product_id": str(PID), "item_combo_code": "BOX",
    "item_qty": "1", "item_amount": "300",
}, follow_redirects=True)
check("8) 客戶欄留空仍可建單且不新增客戶（原行為保留）",
      cust_count() == before and last_order_customer_id() is None
      and "已建立" in r.get_data(as_text=True))

print("\n" + "=" * 60)
print("通過 {}/{}".format(sum(RESULTS), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
