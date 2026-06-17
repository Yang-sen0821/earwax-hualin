"""Flora Court ERP — 本機端到端驗證腳本（D17）

在獨立 throwaway SQLite DB 上跑（不污染 flora_court.db），逐項驗證：
 1) seed 數字（1271/35/1825/932）
 2) deduct_for_sale 四組合各扣對應池正確、扣不到 reserved
 3) split_box 1盒→盒-1片+5、寫 2 筆同 group movement
 4) deduct_for_shipment 扣 1 紙袋並寫 PAPERBAG_OUT
 5) 多品項缺貨整張 rollback
 6) manual_adjust 留痕
 7) check_low_water 觸發
 8) 財務毛利/淨利公式
 9) 權限：staff 擋 owner 後台

執行：python e2e_test.py  （需先設 DATABASE_URL 指向 throwaway db；本腳本自動設）
輸出：每項 [PASS]/[FAIL] + 關鍵數字；結尾總結。
"""
import io
import os
import sys
import tempfile

# ---- 強制走 throwaway SQLite，避免動到已 seed 的 flora_court.db ----
_TMP = os.path.join(tempfile.gettempdir(), "flora_court_e2e.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DATABASE_URL"] = "sqlite:///" + _TMP
os.environ["CONCURRENCY_STRATEGY"] = "atomic_update"

import init_db
from db import (
    get_session, Product, SalesPlan, InventoryBalance, InventoryMovement,
    FinancialEntry, ExtraExpense, User, Order, OrderItem,
    HistoricalOrderImport, HistoricalOrderImportRow,
)
import inventory_service as inv

RESULTS = []  # (name, ok, detail)


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}  {detail}")
    return ok


def bal(s, pid, pool, cat):
    b = s.query(InventoryBalance).filter_by(
        product_id=pid, inventory_pool=pool, stock_category=cat).first()
    return b.qty if b else None


def main():
    # ---------- 建表 + seed ----------
    init_db.create_all()
    s = get_session()
    init_db.seed(s)

    mask = s.query(Product).filter_by(sku="FC-MASK-001").first()
    pkg = s.query(Product).filter_by(sku="PKG-SHARED-001").first()
    owner = s.query(User).filter_by(username="owner").first()
    staff = s.query(User).filter_by(username="staff").first()
    MID = mask.id

    # ================= 1) seed 數字 =================
    b_boxed_n = bal(s, MID, "boxed", "normal")
    b_boxed_r = bal(s, MID, "boxed", "reserved")
    b_loose_n = bal(s, MID, "loose_piece", "normal")
    b_paper_n = bal(s, pkg.id, "paper_bag", "normal")
    check("1) seed 數字 1271/35/1825/932",
          b_boxed_n == 1271 and b_boxed_r == 35 and b_loose_n == 1825 and b_paper_n == 932,
          f"boxed.normal={b_boxed_n} boxed.reserved={b_boxed_r} loose.normal={b_loose_n} paper.normal={b_paper_n}")

    # ================= 2) deduct_for_sale 四組合 + 扣不到 reserved =================
    # SINGLE→loose_piece -1；BOX1→boxed -1；BOX3→boxed -3；BOX10→boxed -10
    before_box = bal(s, MID, "boxed", "normal")
    before_loose = bal(s, MID, "loose_piece", "normal")
    before_reserved = bal(s, MID, "boxed", "reserved")

    r_single = inv.deduct_for_sale(s, MID, "SINGLE", 2, "tester", order_ref="o1")  # loose -2
    r_box1 = inv.deduct_for_sale(s, MID, "BOX1", 1, "tester", order_ref="o1")      # boxed -1
    r_box3 = inv.deduct_for_sale(s, MID, "BOX3", 1, "tester", order_ref="o1")      # boxed -3
    r_box10 = inv.deduct_for_sale(s, MID, "BOX10", 1, "tester", order_ref="o1")    # boxed -10
    s.commit()

    after_box = bal(s, MID, "boxed", "normal")
    after_loose = bal(s, MID, "loose_piece", "normal")
    after_reserved = bal(s, MID, "boxed", "reserved")

    boxed_drop = before_box - after_box           # 應 = 1+3+10 = 14
    loose_drop = before_loose - after_loose       # 應 = 2
    reserved_untouched = (before_reserved == after_reserved)  # reserved 永不被一般銷售動到
    all_ok = all(r.ok for r in (r_single, r_box1, r_box3, r_box10))
    check("2a) 四組合扣對應池（boxed-14 / loose-2）",
          all_ok and boxed_drop == 14 and loose_drop == 2,
          f"boxed_drop={boxed_drop} loose_drop={loose_drop}")
    check("2b) 一般銷售扣不到 reserved（硬隔離）",
          reserved_untouched,
          f"reserved {before_reserved}->{after_reserved}")

    # 額外證明：把 boxed.normal 調到 0，仍不能用 reserved 出貨
    # 直接嘗試扣超過 normal 的量，應缺貨（不會借 reserved）
    huge = after_box + 1
    r_over = inv.deduct_for_sale(s, MID, "BOX1", huge, "tester", order_ref="o1")
    s.rollback()
    check("2c) normal 不足時不借 reserved（缺貨擋下）",
          (not r_over.ok),
          f"嘗試扣 {huge} 盒 → ok={r_over.ok} err={r_over.error}")

    # ================= 3) split_box 1盒→盒-1片+5、2 筆同 group =================
    pre_box = bal(s, MID, "boxed", "normal")
    pre_loose = bal(s, MID, "loose_piece", "normal")
    r_split = inv.split_box(s, MID, 1, "tester", note="拆盒測試")
    s.commit()
    post_box = bal(s, MID, "boxed", "normal")
    post_loose = bal(s, MID, "loose_piece", "normal")
    # 查這次拆盒的 2 筆 movement
    split_mvs = s.query(InventoryMovement).filter(
        InventoryMovement.movement_id.in_(r_split.movement_ids)).all()
    same_group = (len({m.movement_group_id for m in split_mvs}) == 1
                  and split_mvs[0].movement_group_id is not None)
    both_split_type = all(m.movement_type == "SPLIT_BOX" for m in split_mvs)
    check("3) split_box 盒-1/片+5 且 2 筆同 group movement",
          r_split.ok and (pre_box - post_box) == 1 and (post_loose - pre_loose) == 5
          and len(split_mvs) == 2 and same_group and both_split_type,
          f"boxed {pre_box}->{post_box} loose {pre_loose}->{post_loose} "
          f"mv_count={len(split_mvs)} same_group={same_group} group={split_mvs[0].movement_group_id if split_mvs else None}")

    # ================= 4) deduct_for_shipment 扣 1 紙袋 + PAPERBAG_OUT =================
    pre_paper = bal(s, pkg.id, "paper_bag", "normal")
    r_ship = inv.deduct_for_shipment(s, order_id=12345, operator="tester")
    s.commit()
    post_paper = bal(s, pkg.id, "paper_bag", "normal")
    ship_mv = s.query(InventoryMovement).filter_by(
        movement_type="PAPERBAG_OUT").order_by(InventoryMovement.movement_id.desc()).first()
    check("4) deduct_for_shipment 扣 1 紙袋 + 寫 PAPERBAG_OUT",
          r_ship.ok and (pre_paper - post_paper) == 1
          and ship_mv is not None and ship_mv.qty_delta == -1
          and ship_mv.inventory_pool == "paper_bag" and ship_mv.ref_type == "shipment",
          f"paper {pre_paper}->{post_paper} mv_type={ship_mv.movement_type if ship_mv else None} "
          f"delta={ship_mv.qty_delta if ship_mv else None} ref={ship_mv.ref_type if ship_mv else None}")

    # ================= 5) 多品項缺貨整張 rollback =================
    # 模擬 orders._create_order 的 §4.2：第一項成功扣、第二項缺貨 → 整張 rollback
    snap_box = bal(s, MID, "boxed", "normal")
    snap_loose = bal(s, MID, "loose_piece", "normal")
    mv_count_before = s.query(InventoryMovement).count()
    rolled_back = False
    try:
        rA = inv.deduct_for_sale(s, MID, "BOX1", 1, "tester", order_ref="multi")     # ok
        rB = inv.deduct_for_sale(s, MID, "BOX10", 99999, "tester", order_ref="multi")  # 缺貨
        if not rA.ok or not rB.ok:
            s.rollback()
            rolled_back = True
    except Exception:
        s.rollback()
        rolled_back = True
    after_box5 = bal(s, MID, "boxed", "normal")
    after_loose5 = bal(s, MID, "loose_piece", "normal")
    mv_count_after = s.query(InventoryMovement).count()
    check("5) 多品項任一缺貨 → 整張 rollback（不扣不留痕）",
          rolled_back and after_box5 == snap_box and after_loose5 == snap_loose
          and mv_count_after == mv_count_before,
          f"rolled_back={rolled_back} boxed={snap_box}->{after_box5} "
          f"movement筆數 {mv_count_before}->{mv_count_after}（應不變）")

    # ================= 6) manual_adjust 留痕 =================
    pre_adj = bal(s, MID, "boxed", "normal")
    target = pre_adj - 5
    r_adj = inv.manual_adjust(s, MID, "boxed", "normal", target_qty=target,
                              reason="盤點短少", operator="tester",
                              approved_by=owner.id, note="盤點")
    s.commit()
    post_adj = bal(s, MID, "boxed", "normal")
    adj_mv = s.query(InventoryMovement).filter(
        InventoryMovement.movement_id.in_(r_adj.movement_ids)).first()
    check("6) manual_adjust（ADJUSTMENT）留痕 + reason",
          r_adj.ok and post_adj == target and adj_mv is not None
          and adj_mv.movement_type == "ADJUSTMENT" and adj_mv.reason == "盤點短少"
          and adj_mv.qty_before == pre_adj and adj_mv.qty_after == target,
          f"boxed {pre_adj}->{post_adj} mv_type={adj_mv.movement_type if adj_mv else None} "
          f"reason={adj_mv.reason if adj_mv else None}")

    # ================= 7) check_low_water 觸發 =================
    # 門檻 boxed=50（seed）。先看現值，再調到門檻以下，確認 low_water=True
    r_lw_before = inv.check_low_water(s, MID, "boxed", "normal")
    # 調到 49（<50）
    inv.manual_adjust(s, MID, "boxed", "normal", target_qty=49,
                      reason="壓到低水位測試", operator="tester", approved_by=owner.id)
    s.commit()
    r_lw_after = inv.check_low_water(s, MID, "boxed", "normal")
    check("7) check_low_water 觸發（remaining<=threshold）",
          r_lw_after.low_water is True and r_lw_after.threshold == 50
          and r_lw_after.remaining == 49,
          f"threshold={r_lw_after.threshold} remaining={r_lw_after.remaining} "
          f"low_water={r_lw_after.low_water}（壓到 49 < 50）")

    # ================= 8) 財務毛利/淨利公式 =================
    # 毛利 = 銷售 - 商品成本；淨利 = 毛利 - 包材 - 行銷 - 其他支出
    s.add_all([
        FinancialEntry(entry_type="sale", amount=10000),
        FinancialEntry(entry_type="product_cost", amount=3000),
        FinancialEntry(entry_type="packaging", amount=500),
        FinancialEntry(entry_type="marketing", amount=1500),
    ])
    s.add(ExtraExpense(name="版模費", amount=800))
    s.commit()
    # 直接用 reports 的公式函式驗證
    from blueprints.reports import _financial_report
    fin = _financial_report(s)
    expect_gross = 10000 - 3000          # 7000
    expect_net = 7000 - 500 - 1500 - 800  # 4200
    check("8) 財務毛利/淨利公式",
          abs(fin["gross_profit"] - expect_gross) < 1e-6
          and abs(fin["net_profit"] - expect_net) < 1e-6,
          f"gross={fin['gross_profit']}(期望{expect_gross}) net={fin['net_profit']}(期望{expect_net})")

    # ================= 9) 權限：staff 擋 owner 後台 =================
    # 用 Flask test client：staff 登入後存取 owner-only 路由（/inventory/adjust 限 owner）應 403
    from app import app as flask_app
    flask_app.config["TESTING"] = True
    perm_ok = True
    detail9 = ""
    with flask_app.test_client() as c:
        # staff 登入
        rlogin = c.post("/login", data={"username": "staff", "password": "staff123"},
                        follow_redirects=False)
        # /inventory/adjust 限 owner（role_required('owner')）→ staff 應 403
        r_adjpage = c.get("/inventory/adjust")
        staff_blocked = (r_adjpage.status_code == 403)
        detail9 += f"staff→/inventory/adjust={r_adjpage.status_code}(應403) "
        # owner 登入後同路由應可進（200）
        c.get("/logout")
        c.post("/login", data={"username": "owner", "password": "owner123"})
        r_owner = c.get("/inventory/adjust")
        owner_allowed = (r_owner.status_code == 200)
        detail9 += f"owner→/inventory/adjust={r_owner.status_code}(應200)"
        perm_ok = staff_blocked and owner_allowed
    check("9) 權限：staff 擋 owner 後台、owner 可進", perm_ok, detail9)

    # ================= 案 A) D16 手機後台寫入權限 =================
    # viewer/accounting 打 /m/orders/new 與出貨寫入 → 403；owner/staff → 可進/成功。
    # 需有一張待出貨訂單供出貨路由測試。
    a_detail = ""
    a_ok = True
    # 準備一張 pending 訂單（直接落地，不經權限路由）
    ship_order_obj = Order(
        order_no="FC-E2E-SHIP-1", payment_status="paid",
        shipping_status="pending", total_amount=0,
    )
    s.add(ship_order_obj)
    s.commit()
    ship_oid = ship_order_obj.id

    def _login(c, uname, pw):
        c.get("/logout")
        return c.post("/login", data={"username": uname, "password": pw},
                      follow_redirects=False)

    with flask_app.test_client() as c:
        # --- viewer：建單頁 GET 應 403、出貨 POST 應 403 ---
        _login(c, "viewer", "viewer123")
        v_new = c.get("/m/orders/new")
        v_ship = c.post(f"/m/shipments/{ship_oid}/ship", data={})
        a_detail += f"viewer:new={v_new.status_code}(403) ship={v_ship.status_code}(403) "
        if v_new.status_code != 403 or v_ship.status_code != 403:
            a_ok = False

        # --- accounting：同樣 403（orders/shipment 對 accounting 唯讀）---
        _login(c, "accounting", "accounting123")
        ac_new = c.get("/m/orders/new")
        ac_ship = c.post(f"/m/shipments/{ship_oid}/ship", data={})
        a_detail += f"acct:new={ac_new.status_code}(403) ship={ac_ship.status_code}(403) "
        if ac_new.status_code != 403 or ac_ship.status_code != 403:
            a_ok = False

        # --- staff：建單頁 GET 應 200（可寫）---
        _login(c, "staff", "staff123")
        st_new = c.get("/m/orders/new")
        a_detail += f"staff:new={st_new.status_code}(200) "
        if st_new.status_code != 200:
            a_ok = False

        # --- owner：出貨 POST 應成功（302 轉址，非 403）並真正出貨 ---
        _login(c, "owner", "owner123")
        ow_ship = c.post(f"/m/shipments/{ship_oid}/ship",
                         data={"carrier": "黑貓", "tracking_no": "T1"})
        a_detail += f"owner:ship={ow_ship.status_code}(非403) "
        if ow_ship.status_code == 403:
            a_ok = False
    # 驗證 owner 出貨確實生效（出貨狀態變 shipped）
    s.expire_all()
    shipped = s.query(Order).filter_by(id=ship_oid).first()
    a_detail += f"order.shipping={shipped.shipping_status}(shipped)"
    if shipped.shipping_status != "shipped":
        a_ok = False
    check("A) D16 手機寫入權限：viewer/accounting→403、owner/staff→可寫", a_ok, a_detail)

    # ================= 案 B) D14 歷史訂單匯入 =================
    # 匯入小樣本 CSV → 批次 + rows 落地、統計正確、未動任何 inventory_balances/movements。
    inv_bal_before = s.query(InventoryBalance).count()
    inv_mv_before = s.query(InventoryMovement).count()
    # 用各池 qty 快照確認匯入完全不碰庫存量
    bal_snapshot_before = {
        (b.product_id, b.inventory_pool, b.stock_category): b.qty
        for b in s.query(InventoryBalance).all()
    }

    sample_csv = (
        "訂單編號,姓名,電話,訂購日期,商品名稱,購買數量,單價,訂單金額,付款狀態,出貨狀態,備註\n"
        "H001,王小明,0911000111,2025-01-05,植萃面膜,2,500,1000,paid,shipped,VIP\n"
        "H002,陳美玲,0922000222,2025-01-06,植萃面膜,1,500,500,unpaid,pending,\n"
        "H003,,0933,2025-01-07,,abc,xx,,paid,pending,壞列測試\n"  # 缺單號+缺商品+數量非法 → 失敗列
    )
    b_detail = ""
    b_ok = True
    with flask_app.test_client() as c:
        # 權限：viewer 打匯入頁應 403
        _login(c, "viewer", "viewer123")
        imp_forbidden = c.get("/orders/import")
        b_detail += f"viewer→import={imp_forbidden.status_code}(403) "
        if imp_forbidden.status_code != 403:
            b_ok = False

        # accounting（D14 允許）上傳 CSV
        _login(c, "accounting", "accounting123")
        data = {
            "batch_name": "E2E 樣本",
            "file": (io.BytesIO(sample_csv.encode("utf-8")), "sample.csv"),
        }
        r_imp = c.post("/orders/import", data=data,
                       content_type="multipart/form-data",
                       follow_redirects=False)
        b_detail += f"acct→import={r_imp.status_code}(302) "
        if r_imp.status_code not in (302, 303):
            b_ok = False

    # 驗證落地
    s.expire_all()
    batch = (s.query(HistoricalOrderImport)
             .order_by(HistoricalOrderImport.id.desc()).first())
    rows = (s.query(HistoricalOrderImportRow)
            .filter_by(import_id=batch.id).all() if batch else [])
    batch_ok = (batch is not None and batch.imported_rows == 3
                and batch.success_rows == 2 and batch.failed_rows == 1
                and len(rows) == 3)
    b_detail += (f"batch(imported={batch.imported_rows if batch else None},"
                 f"success={batch.success_rows if batch else None},"
                 f"failed={batch.failed_rows if batch else None},rows={len(rows)}) ")
    if not batch_ok:
        b_ok = False
    # 失敗列須有 error_message、成功列須無
    fail_rows = [r for r in rows if r.error_message]
    ok_rows = [r for r in rows if not r.error_message]
    raw_ok = all(r.raw_json for r in rows)
    b_detail += f"fail_rows={len(fail_rows)}(1) raw保留={raw_ok} "
    if len(fail_rows) != 1 or len(ok_rows) != 2 or not raw_ok:
        b_ok = False

    # 庫存完全未動（balances 列數、movements 筆數、各池 qty 皆不變）
    inv_bal_after = s.query(InventoryBalance).count()
    inv_mv_after = s.query(InventoryMovement).count()
    bal_snapshot_after = {
        (b.product_id, b.inventory_pool, b.stock_category): b.qty
        for b in s.query(InventoryBalance).all()
    }
    inv_untouched = (inv_bal_after == inv_bal_before
                     and inv_mv_after == inv_mv_before
                     and bal_snapshot_after == bal_snapshot_before)
    b_detail += (f"庫存balances {inv_bal_before}->{inv_bal_after} "
                 f"movements {inv_mv_before}->{inv_mv_after} 不變={inv_untouched}")
    if not inv_untouched:
        b_ok = False
    check("B) D14 歷史匯入：批次+rows落地、統計正確、未動庫存", b_ok, b_detail)

    s.close()

    # ---------- 總結 ----------
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 60)
    print(f"端到端結果：{passed}/{total} PASS")
    for name, ok, _ in RESULTS:
        if not ok:
            print(f"  FAIL → {name}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
