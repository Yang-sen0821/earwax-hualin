# -*- coding: utf-8 -*-
"""庫存頁直接編輯（quick_edit）＋ 分頁列角色顯示 ＋ 中文錯誤頁 handler 級測試。

對應森哥 2026-08-19 三項指示中的第 2、3 項：
- 第 2 項：庫存頁分頁列按角色顯示；沒權限時看到中文說明頁而非英文 Forbidden。
- 第 3 項（乙案）：品名與各格數量直接改，不必人工填理由，系統自動留痕；員工也能編輯。

執行：python quick_edit_test.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = os.path.join(tempfile.gettempdir(), "flora_court_quickedit_test.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"

import init_db  # noqa: E402
init_db.main()

from db import get_session, Product, InventoryBalance, InventoryMovement, AuditLog  # noqa: E402
from app import app  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {}  {}".format("PASS" if ok else "FAIL", name, detail))


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=True)


def fresh_session():
    s = get_session()
    s.expire_all()
    return s


PID = fresh_session().query(Product).order_by(Product.id).first().id
ORIG_NAME = fresh_session().query(Product).filter_by(id=PID).first().name


def qty_of(pool, cat):
    b = (fresh_session().query(InventoryBalance)
         .filter_by(product_id=PID, inventory_pool=pool, stock_category=cat).first())
    return b.qty if b else 0


def mv_count():
    return fresh_session().query(InventoryMovement).count()


QE = "/inventory/quick-edit/" + str(PID)

# =====================================================================
# 1. 分頁列按角色顯示（第 2 項）
# =====================================================================
c = app.test_client()
login(c, "staff", "staff123")
html = c.get("/inventory/").get_data(as_text=True)
check("1a) 員工看不到『盤點調整』（限 owner；以前看得到、點了噴錯）", "盤點調整" not in html)
check("1b) 員工看得到『補貨入庫』（森哥已授權員工可編輯庫存）", "補貨入庫" in html)

cv = app.test_client()
login(cv, "viewer", "viewer123")
html_v = cv.get("/inventory/").get_data(as_text=True)
check("1c) 檢視者看不到補貨入庫／通用出庫／拆盒調撥／盤點調整",
      all(x not in html_v for x in ("補貨入庫", "通用出庫", "拆盒調撥", "盤點調整")))
check("1d) 檢視者仍看得到總覽／異動紀錄／低水位警示",
      all(x in html_v for x in ("總覽", "異動紀錄", "低水位警示")))

co = app.test_client()
login(co, "owner", "owner123")
html_o = co.get("/inventory/").get_data(as_text=True)
check("1e) owner 七個分頁按鈕都在",
      all(x in html_o for x in ("總覽", "補貨入庫", "通用出庫", "拆盒調撥",
                                "盤點調整", "異動紀錄", "低水位警示")))

# =====================================================================
# 2. 中文錯誤頁（第 2 項）
# =====================================================================
r = c.get("/inventory/adjust")
body = r.get_data(as_text=True)
check("2a) 員工直接打網址進盤點調整仍被擋（403）", r.status_code == 403,
      "status=" + str(r.status_code))
check("2b) 403 是中文說明頁，不是英文 Forbidden",
      "沒有權限" in body and "Forbidden" not in body)
r404 = c.get("/inventory/no-such-page")
check("2c) 404 也是中文頁",
      r404.status_code == 404 and "找不到這一頁" in r404.get_data(as_text=True))

# =====================================================================
# 3. 員工直接改（第 3 項乙案：不填理由、自動留痕）
# =====================================================================
before_qty = qty_of("boxed", "normal")
before_mv = mv_count()
r = c.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": str(before_qty + 7)},
           follow_redirects=True)
after_qty = qty_of("boxed", "normal")
check("3a) 員工直接改盒裝/正常數量成功（不需填理由）",
      r.status_code == 200 and after_qty == before_qty + 7,
      str(before_qty) + " -> " + str(after_qty))

mv = fresh_session().query(InventoryMovement).order_by(InventoryMovement.movement_id.desc()).first()
check("3b) 系統自動留痕：ADJUSTMENT + ref_type=quick_edit + 記錄改前改後",
      (mv_count() == before_mv + 1 and mv.movement_type == "ADJUSTMENT"
       and mv.ref_type == "quick_edit" and mv.qty_before == before_qty
       and mv.qty_after == before_qty + 7 and mv.reason == "庫存頁直接修改"),
      "type={} ref={} {}->{} reason={}".format(mv.movement_type, mv.ref_type,
                                               mv.qty_before, mv.qty_after, mv.reason))

b_loose = qty_of("loose_piece", "normal")
b_pr = qty_of("boxed", "pr")
before_mv = mv_count()
c.post(QE, data={"name": ORIG_NAME,
                 "qty_loose_piece_normal": str(b_loose + 3),
                 "qty_boxed_pr": str(b_pr + 2)}, follow_redirects=True)
check("3c) 一次改多格，兩格都生效且各留一筆紀錄",
      qty_of("loose_piece", "normal") == b_loose + 3
      and qty_of("boxed", "pr") == b_pr + 2 and mv_count() == before_mv + 2)

c.post(QE, data={"name": ORIG_NAME + "改"}, follow_redirects=True)
nm = fresh_session().query(Product).filter_by(id=PID).first().name
audit = (fresh_session().query(AuditLog).filter_by(action="product_name_edit")
         .order_by(AuditLog.id.desc()).first())
check("3d) 員工改品名成功且寫入 audit_logs（含改前改後）",
      nm == ORIG_NAME + "改" and audit is not None
      and ORIG_NAME in audit.detail and (ORIG_NAME + "改") in audit.detail,
      "name=" + str(nm))
c.post(QE, data={"name": ORIG_NAME}, follow_redirects=True)

before_mv = mv_count()
cur = qty_of("boxed", "normal")
c.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": str(cur)}, follow_redirects=True)
check("3e) 數字沒改就不產生異動紀錄（不灌水）", mv_count() == before_mv)

# =====================================================================
# 4. 驗證與整筆 rollback
# =====================================================================
cur = qty_of("boxed", "normal")
before_mv = mv_count()
r = c.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": str(cur + 5),
                     "qty_boxed_pr": "-1"}, follow_redirects=True)
check("4a) 有一格填負數 → 整筆不存（同批合法那格也不生效）",
      qty_of("boxed", "normal") == cur and mv_count() == before_mv
      and "不可為負" in r.get_data(as_text=True))

r = c.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": "abc"}, follow_redirects=True)
check("4b) 數量填非數字 → 擋下且資料未變",
      qty_of("boxed", "normal") == cur and "格式錯誤" in r.get_data(as_text=True))

r = c.post(QE, data={"name": "  "}, follow_redirects=True)
check("4c) 品名清空 → 擋下",
      fresh_session().query(Product).filter_by(id=PID).first().name == ORIG_NAME
      and "品名不可空白" in r.get_data(as_text=True))

r = c.post("/inventory/quick-edit/999999", data={"name": "x"}, follow_redirects=True)
check("4d) 不存在的商品 → 中文提示，不是錯誤頁",
      r.status_code == 200 and "找不到該商品" in r.get_data(as_text=True))

# =====================================================================
# 5. 權限邊界
# =====================================================================
before_qty = qty_of("boxed", "normal")
r = cv.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": str(before_qty + 99)})
check("5a) 檢視者無法直接編輯（403）且資料未變",
      r.status_code == 403 and qty_of("boxed", "normal") == before_qty,
      "status=" + str(r.status_code))
check("5b) 檢視者看不到編輯輸入框（庫存頁維持唯讀）",
      'name="qty_boxed_normal"' not in cv.get("/inventory/").get_data(as_text=True))
check("5c) 員工看得到編輯輸入框",
      'name="qty_boxed_normal"' in c.get("/inventory/").get_data(as_text=True))

ca = app.test_client()
login(ca, "accounting", "accounting123")
r = ca.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": "1"})
check("5d) 會計無庫存寫入權（403）", r.status_code == 403, "status=" + str(r.status_code))

r = c.get("/inventory/restock")
check("5e) 員工現在可進補貨入庫（森哥授權放寬）", r.status_code == 200,
      "status=" + str(r.status_code))

cw = app.test_client()
login(cw, "warehouse", "warehouse123")
wq = qty_of("boxed", "normal")
r = cw.post(QE, data={"name": ORIG_NAME, "qty_boxed_normal": str(wq + 1)},
            follow_redirects=True)
check("5f) 倉管仍可編輯（未回歸）",
      r.status_code == 200 and qty_of("boxed", "normal") == wq + 1)

print("\n" + "=" * 60)
print("通過 {}/{}".format(sum(RESULTS), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
