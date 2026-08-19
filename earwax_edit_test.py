# -*- coding: utf-8 -*-
"""愛啪啪庫存編輯功能 handler 級測試（throwaway sqlite + EARWAX_TABLE 同構表覆寫）。

驗：顯示、編輯寫入、新增、驗證擋壞資料、權限 403、audit_logs 留痕。
執行：python earwax_edit_test.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = os.path.join(tempfile.gettempdir(), "flora_court_earwax_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"
os.environ["EARWAX_TABLE"] = "test_earwax_consumables"  # 同構表（無 schema 前綴）

import init_db  # noqa: E402
init_db.main()  # 建 20 表 + seed
from sqlalchemy import text  # noqa: E402
from db import get_session, AuditLog  # noqa: E402
from app import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


# ---- 建同構表 + seed 一列 ----
s = get_session()
s.execute(text(
    "CREATE TABLE test_earwax_consumables ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " category VARCHAR(20) NOT NULL DEFAULT 'consumable',"
    " name VARCHAR(100) NOT NULL,"
    " qty_on_hand INTEGER DEFAULT 0,"
    " unit_cost FLOAT DEFAULT 0,"
    " note TEXT DEFAULT '',"
    " created_at TIMESTAMP)"))
s.execute(text(
    "INSERT INTO test_earwax_consumables (category, name, qty_on_hand, unit_cost, note) "
    "VALUES ('consumable', '銀', 1, 0, '盤點')"))
s.commit()

c = app.test_client()

# 1) owner 顯示
c.post("/login", data={"username": "owner", "password": "owner123"})
r = c.get("/inventory/", follow_redirects=True)
html = r.get_data(as_text=True)
check("1) owner 看到愛啪啪區與品項", r.status_code == 200 and "銀" in html and "愛啪啪" in html,
      f"status={r.status_code}")

# 2) owner 編輯（改名/量/成本/備註/類別）
r = c.post("/inventory/earwax/1/edit", data={
    "name": "銀安瓶", "category": "consumable", "qty_on_hand": "5",
    "unit_cost": "12.5", "note": "改測"}, follow_redirects=True)
row = s.execute(text("SELECT name, qty_on_hand, unit_cost, note FROM test_earwax_consumables WHERE id=1")).first()
check("2) 編輯寫入生效", r.status_code == 200 and row[0] == "銀安瓶" and row[1] == 5
      and abs(row[2] - 12.5) < 1e-9 and row[3] == "改測", f"row={tuple(row)}")

# 3) audit 留痕（edit）
n_edit = s.query(AuditLog).filter_by(action="earwax_consumable_edit", target_id="1").count()
check("3) audit_logs 留痕 edit", n_edit == 1, f"count={n_edit}")

# 4) 新增品項
r = c.post("/inventory/earwax/new", data={
    "name": "藍安瓶", "category": "consumable", "qty_on_hand": "2",
    "unit_cost": "0", "note": ""}, follow_redirects=True)
n_rows = s.execute(text("SELECT COUNT(*) FROM test_earwax_consumables")).scalar()
n_create = s.query(AuditLog).filter_by(action="earwax_consumable_create").count()
check("4) 新增品項 + audit 留痕", r.status_code == 200 and n_rows == 2 and n_create == 1,
      f"rows={n_rows} create_audit={n_create}")

# 5) 驗證擋壞資料：負數量、空品名（值不應變動）
c.post("/inventory/earwax/1/edit", data={"name": "X", "category": "consumable",
                                          "qty_on_hand": "-3", "unit_cost": "0", "note": ""})
c.post("/inventory/earwax/1/edit", data={"name": "  ", "category": "consumable",
                                          "qty_on_hand": "9", "unit_cost": "0", "note": ""})
row = s.execute(text("SELECT name, qty_on_hand FROM test_earwax_consumables WHERE id=1")).first()
check("5) 負數量/空品名被擋、原值不動", row[0] == "銀安瓶" and row[1] == 5, f"row={tuple(row)}")

# 6) 權限（森哥 2026-08-19 授權：staff 也可編輯庫存，故此案由「被擋」改為「可寫」）
c.get("/logout", follow_redirects=True)
c.post("/login", data={"username": "staff", "password": "staff123"})
r = c.post("/inventory/earwax/1/edit", data={"name": "銀安瓶", "category": "consumable",
                                              "qty_on_hand": "9", "unit_cost": "0", "note": ""})
s.expire_all()
row = s.execute(text("SELECT name, qty_on_hand FROM test_earwax_consumables WHERE id=1")).first()
check("6) staff 可編輯愛啪啪品項（2026-08-19 放寬）", r.status_code in (200, 302) and row[1] == 9,
      f"status={r.status_code} row={tuple(row)}")

# 6b) 權限下界：viewer 仍被擋（403）且值不動
c.get("/logout", follow_redirects=True)
c.post("/login", data={"username": "viewer", "password": "viewer123"})
r = c.post("/inventory/earwax/1/edit", data={"name": "駭", "category": "consumable",
                                              "qty_on_hand": "99", "unit_cost": "0", "note": ""})
s.expire_all()
row = s.execute(text("SELECT name, qty_on_hand FROM test_earwax_consumables WHERE id=1")).first()
check("6b) viewer 編輯被擋(403)且值不動", r.status_code == 403 and row[0] == "銀安瓶" and row[1] == 9,
      f"status={r.status_code} row={tuple(row)}")

print("=" * 50)
print(f"結果：{sum(RESULTS)}/{len(RESULTS)} PASS")
sys.exit(0 if all(RESULTS) else 1)
