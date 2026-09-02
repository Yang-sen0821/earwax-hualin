# -*- coding: utf-8 -*-
"""訂單搜尋修正驗證（2026-09-01 客戶 bug：搜客戶名找不到收件人空白的單）。
throwaway sqlite 放 %TEMP%；env 在 import app 前設定。
"""
import os, tempfile, uuid

_TMP_DB = os.path.join(tempfile.gettempdir(), f"fc_search_{uuid.uuid4().hex}.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP_DB.replace("\\", "/")
os.environ.setdefault("SECRET_KEY", "test")

results = []
def check(name, ok, note=""):
    results.append((name, bool(ok), note))
    print(("[PASS] " if ok else "[FAIL] ") + name + ((" | " + note) if note and not ok else ""))

from app import app  # noqa: E402
from db import SessionLocal, Base, engine, User, Customer, Order, OrderItem, Product  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="owner", password_hash=generate_password_hash("owner123"), role="owner"))
p = Product(name="測試面膜", sku="T-1", category="面膜"); db.add(p); db.flush()
c1 = Customer(name="陳筠涵", phone="0912345678"); db.add(c1); db.flush()
# 單 A：收件人空白、綁客戶 陳筠涵（重現 FC20260831041 型態）
oa = Order(order_no="FC90000001", customer_id=c1.id, total_amount=2580, payment_status="paid", shipping_status="delivered")
# 單 B：無客戶、收件人 王小明
ob = Order(order_no="FC90000002", recipient_name="王小明", recipient_phone="0987000111", total_amount=100, payment_status="unpaid", shipping_status="pending")
db.add_all([oa, ob]); db.flush()
db.add_all([OrderItem(order_id=oa.id, product_id=p.id, combo_code="BOX", qty=3, unit_price=2580, subtotal=2580),
            OrderItem(order_id=ob.id, product_id=p.id, combo_code="BOX", qty=1, unit_price=100, subtotal=100)])
db.commit(); db.close()

cl = app.test_client()
cl.post("/login", data={"username": "owner", "password": "owner123"})

def hits(qs):
    r = cl.get(f"/orders/?q={qs}")
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)

h = hits("陳筠涵")
check("1 搜客戶名找到收件人空白的單", "FC90000001" in h)
check("2 搜客戶名不誤含他單", "FC90000002" not in h)
h = hits("0912345678")
check("3 搜客戶電話找到", "FC90000001" in h)
h = hits("王小明")
check("4 搜收件人仍可找到", "FC90000002" in h)
h = hits("fc90000002")
check("5 訂單編號忽略大小寫", "FC90000002" in h)
h = hits("%E3%80%80%E9%99%B3%E7%AD%A0%E6%B6%B5%E3%80%80")  # 全形空白包住
check("6 全形空白去除後仍可找到", "FC90000001" in h)
h = hits("")
check("7 空字串列出全部", ("FC90000001" in h) and ("FC90000002" in h))

fails = [n for n, ok, _ in results if not ok]
print(f"PASS: {len(results)-len(fails)}  FAIL: {len(fails)}")
if fails:
    raise SystemExit(1)
print("ALL GREEN")
