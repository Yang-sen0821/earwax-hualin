"""Flora Court 面膜 ERP — 庫存契約層（§4 / §7，模組層實作）

全案中樞：所有「改庫存量」的操作唯一經此層。對外契約函式簽章與地基階段空殼一致，
回傳輕量 Result 物件（dataclass）。

紅線 R1（全函式遵守）：
- 任何改 qty 的操作必在「同一原子 DB transaction」內（session 由呼叫端管理 commit/rollback），
  並同筆寫 inventory_movements（含 qty_before / qty_after），同生同滅。
- 多品項訂單任一缺貨 ⇒ 呼叫端整張 rollback（本層回 ok=false、不扣不留痕，§4.2）。
- 併發用 SELECT..FOR UPDATE 或條件式原子更新（§4.3，策略 = Config.CONCURRENCY_STRATEGY）。
- 預留(reserved)物理分列硬隔離：一般銷售只讀 normal，扣不到 reserved（§4.0 / §7.8）。
- 禁止任何路徑直接 UPDATE inventory_balances（§4.8 / 修正9）；唯一寫入口 = record_movement + _apply_delta。

設計：
- record_movement   — 唯一 movement 寫入口（建立一筆 InventoryMovement）。
- _apply_delta      — 唯一 balance 變動入口（鎖讀 → 套用 delta → 呼 record_movement）。
- 所有契約函式皆透過 _apply_delta，不自寫 balance / movement。
"""
from dataclasses import dataclass, field
from typing import List, Optional
import uuid

from sqlalchemy import text

from config import Config
from db import (
    InventoryBalance, InventoryMovement, InventoryThreshold,
    SalesPlan, SalesPlanBomItem, Product, Setting,
    MOVEMENT_TYPES, INVENTORY_POOLS, STOCK_CATEGORIES, COMBO_CODES,
)


# =========================================================================
# 回傳型別（輕量）
# =========================================================================
@dataclass
class Result:
    ok: bool
    qty_before: Optional[int] = None
    qty_after: Optional[int] = None
    remaining: Optional[int] = None
    low_water: bool = False
    threshold: Optional[int] = None
    movement_ids: List[int] = field(default_factory=list)
    error: Optional[str] = None


# DeductResult / SplitResult 對外同型別（共用 Result 欄位即可）
DeductResult = Result
SplitResult = Result


class InventoryError(Exception):
    """業務層可捕捉的庫存錯誤（缺貨/非法參數）；呼叫端據此 rollback。"""


# =========================================================================
# 池 → 單位（新建 InventoryBalance 列時自動帶入，避免 NULL unit；2026-08-31）
# =========================================================================
POOL_UNIT = {
    "boxed": "box",
    "loose_piece": "piece",
    "paper_bag": "bag",
}


# =========================================================================
# §4.1 銷售組合 → 池對應（鎖定不可改）
# =========================================================================
_COMBO_MAP = {
    "SINGLE": ("loose_piece", 1),
    "BOX1":   ("boxed", 1),
    "BOX3":   ("boxed", 3),
    "BOX10":  ("boxed", 10),
}

# 新模型「單位」→ 池對應（每單位扣 1，量由 order_qty 決定）：
#   LOOSE(片) → 裸片池、BOX(盒) → 盒裝池。
# 舊的 4-combo 倍率(BOX3=3/BOX10=10)在新模型下由「盒 × 數量」自然取代。
_UNIT_MAP = {
    "LOOSE": ("loose_piece", 1),
    "BOX":   ("boxed", 1),
    # 2026-09-01（森哥「訂單建立也要能選擇紙袋當品項」）：BAG(袋) → 紙袋池 × 數量，
    # 掛 sentinel 共用包材商品（PKG-SHARED-001）。走同一 SALE / SALE_REVERSAL 契約，
    # 故 CR-9 歷史輸入／已消耗會把紙袋銷售一併算成消耗；編輯／作廢的 reverse_sale 同樣回補。
    "BAG":   ("paper_bag", 1),
}


# =========================================================================
# 唯一 movement 寫入口（§4.8）
# =========================================================================
def record_movement(session, product_id, inventory_pool, qty_delta,
                    qty_before, qty_after, movement_type, operator,
                    stock_category_from=None, stock_category_to=None,
                    ref_type=None, ref_id=None, movement_group_id=None,
                    reason=None, note="", created_by=None):
    """唯一 movement 寫入口。回傳 movement_id（flush 後可得）。

    禁止其他路徑自建 InventoryMovement；balance 變動一律配對一筆 movement。
    """
    if movement_type not in MOVEMENT_TYPES:
        raise InventoryError(f"非法 movement_type: {movement_type}")
    mv = InventoryMovement(
        product_id=product_id,
        inventory_pool=inventory_pool,
        stock_category_from=stock_category_from,
        stock_category_to=stock_category_to,
        qty_delta=qty_delta,
        qty_before=qty_before,
        qty_after=qty_after,
        movement_type=movement_type,
        ref_type=ref_type,
        ref_id=(str(ref_id) if ref_id is not None else None),
        movement_group_id=movement_group_id,
        reason=reason,
        note=note or "",
        operator=operator,
        created_by=created_by,
    )
    session.add(mv)
    session.flush()  # 取得 movement_id（同 tx 內，未 commit）
    return mv.movement_id


# =========================================================================
# 唯一 balance 變動入口（鎖讀 + 原子扣 + 留痕）
# =========================================================================
def _get_balance(session, product_id, pool, category):
    return session.query(InventoryBalance).filter_by(
        product_id=product_id, inventory_pool=pool, stock_category=category
    ).first()


def _lock_balance(session, product_id, pool, category):
    """併發鎖讀（for_update 策略）。回傳已鎖的 balance 列或 None。

    atomic_update 策略不在此鎖，改於 _apply_delta 用條件式 UPDATE。
    """
    q = session.query(InventoryBalance).filter_by(
        product_id=product_id, inventory_pool=pool, stock_category=category
    )
    if Config.CONCURRENCY_STRATEGY == "for_update":
        q = q.with_for_update()
    return q.first()


def _apply_delta(session, product_id, pool, category, delta,
                 movement_type, operator, *,
                 stock_category_from=None, stock_category_to=None,
                 ref_type=None, ref_id=None, movement_group_id=None,
                 reason=None, note="", created_by=None,
                 allow_create_on_increase=False):
    """唯一 balance 變動入口。

    delta>0 入庫 / delta<0 出庫。內含併發防超賣（§4.3）：
    - for_update：SELECT..FOR UPDATE 鎖列再判斷 qty。
    - atomic_update：UPDATE..SET qty=qty+delta WHERE qty+delta>=0 並檢查受影響列數。

    出庫缺貨 ⇒ raise InventoryError（不扣不留痕）。回 (qty_before, qty_after, movement_id)。
    """
    if delta == 0:
        raise InventoryError("delta 不可為 0")

    strategy = Config.CONCURRENCY_STRATEGY

    if strategy == "atomic_update":
        # 條件式原子更新（§4.3 b）。先讀 before（同 tx 一致），再條件 UPDATE。
        bal = _get_balance(session, product_id, pool, category)
        if bal is None:
            if delta > 0 and allow_create_on_increase:
                qty_before = 0
                bal = InventoryBalance(
                    product_id=product_id, inventory_pool=pool,
                    stock_category=category, qty=0, updated_by=created_by,
                    unit=POOL_UNIT.get(pool),
                )
                session.add(bal)
                session.flush()
            else:
                raise InventoryError(
                    f"庫存列不存在: product={product_id} pool={pool} cat={category}"
                )
        qty_before = bal.qty
        # 條件式原子更新：扣減時要求 qty+delta>=0，受影響列數=0 即缺貨
        upd = (
            session.query(InventoryBalance)
            .filter(
                InventoryBalance.product_id == product_id,
                InventoryBalance.inventory_pool == pool,
                InventoryBalance.stock_category == category,
                InventoryBalance.qty + delta >= 0,
            )
            .update(
                {InventoryBalance.qty: InventoryBalance.qty + delta},
                synchronize_session=False,
            )
        )
        if upd == 0:
            raise InventoryError(
                f"庫存不足/缺貨: pool={pool} cat={category} need={-delta} have={qty_before}"
            )
        session.expire(bal)
        qty_after = bal.qty
    else:
        # for_update 策略（§4.3 a）：鎖列 → 判斷 → 改值
        bal = _lock_balance(session, product_id, pool, category)
        if bal is None:
            if delta > 0 and allow_create_on_increase:
                bal = InventoryBalance(
                    product_id=product_id, inventory_pool=pool,
                    stock_category=category, qty=0, updated_by=created_by,
                    unit=POOL_UNIT.get(pool),
                )
                session.add(bal)
                session.flush()
            else:
                raise InventoryError(
                    f"庫存列不存在: product={product_id} pool={pool} cat={category}"
                )
        qty_before = bal.qty
        if bal.qty + delta < 0:
            raise InventoryError(
                f"庫存不足/缺貨: pool={pool} cat={category} need={-delta} have={qty_before}"
            )
        bal.qty = bal.qty + delta
        if created_by is not None:
            bal.updated_by = created_by
        session.flush()
        qty_after = bal.qty

    mv_id = record_movement(
        session, product_id=product_id, inventory_pool=pool,
        qty_delta=delta, qty_before=qty_before, qty_after=qty_after,
        movement_type=movement_type, operator=operator,
        stock_category_from=stock_category_from, stock_category_to=stock_category_to,
        ref_type=ref_type, ref_id=ref_id, movement_group_id=movement_group_id,
        reason=reason, note=note, created_by=created_by,
    )
    return qty_before, qty_after, mv_id


def _new_group_id():
    return uuid.uuid4().hex


# =========================================================================
# §4.1 銷售組合扣減
# =========================================================================
def deduct_for_sale(session, product_id, combo_code, order_qty,
                    operator, order_ref, note=""):
    """銷售扣減（§4.1）。

    新模型「單位 + 數量」（建單只用這兩個）：
      LOOSE(片) → 扣裸片池 × order_qty、BOX(盒) → 扣盒裝池 × order_qty、
      BAG(袋) → 扣紙袋池 × order_qty（product 為 sentinel 共用包材商品）。
    舊 4-combo 相容（歷史 / 既有呼叫）：
      SINGLE→(loose_piece,1)/BOX1→(boxed,1)/BOX3→(boxed,3)/BOX10→(boxed,10)。
    只讀 normal，永不觸 reserved/pr/trial/scrap；兩池不互換（R1 紅線不變）。
    need = per_unit(combo/unit) * order_qty；缺貨 ⇒ ok=false、不扣不留痕（呼叫端 rollback）。
    """
    if combo_code in _UNIT_MAP:
        pool, per_unit = _UNIT_MAP[combo_code]
    elif combo_code in _COMBO_MAP:
        pool, per_unit = _COMBO_MAP[combo_code]
    else:
        return Result(ok=False, error=f"非法 combo_code: {combo_code}")
    if order_qty is None or order_qty <= 0:
        return Result(ok=False, error="order_qty 必須 > 0")
    if not operator:
        return Result(ok=False, error="operator 不可空")

    need = per_unit * order_qty
    category = "normal"  # 永遠只讀 normal（reserved 硬隔離）

    try:
        qb, qa, mv_id = _apply_delta(
            session, product_id, pool, category, -need,
            movement_type="SALE", operator=operator,
            stock_category_from=category, stock_category_to=None,
            ref_type="order", ref_id=order_ref, note=note,
        )
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                  movement_ids=[mv_id])


# =========================================================================
# §4.4 紙袋出貨扣減
# =========================================================================
def _get_setting_int(session, key, default):
    s = session.query(Setting).filter_by(key=key).first()
    if s is None or s.value is None:
        return default
    try:
        return int(s.value)
    except (TypeError, ValueError):
        return default


def _packaging_product_id(session):
    """sentinel 共用包材商品（紙袋掛此）。優先 is_packaging，退而求 sku。"""
    p = session.query(Product).filter_by(is_packaging=True).first()
    if p is None:
        p = session.query(Product).filter_by(sku="PKG-SHARED-001").first()
    if p is None:
        raise InventoryError("找不到 sentinel 共用包材商品（PKG-SHARED-001）")
    return p.id


def deduct_for_shipment(session, order_id, operator):
    """紙袋出貨扣減（§4.4）。

    出貨同 tx 內執行；扣 sentinel (paper_bag, normal)；數量讀
    settings.paperbag_qty_per_shipment（預設 1）；寫 PAPERBAG_OUT，ref_type=shipment。
    不足 ⇒ ok=false（呼叫端擋出貨 rollback）。
    """
    if not operator:
        return Result(ok=False, error="operator 不可空")
    try:
        pkg_pid = _packaging_product_id(session)
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    qty = _get_setting_int(session, "paperbag_qty_per_shipment", 1)
    if qty <= 0:
        # 設為 0 即不扣紙袋（仍視為成功，不留痕）
        return Result(ok=True, qty_before=None, qty_after=None, movement_ids=[])

    try:
        qb, qa, mv_id = _apply_delta(
            session, pkg_pid, "paper_bag", "normal", -qty,
            movement_type="PAPERBAG_OUT", operator=operator,
            stock_category_from="normal", stock_category_to=None,
            ref_type="shipment", ref_id=order_id,
            note=f"出貨扣紙袋 x{qty}",
        )
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                  movement_ids=[mv_id])


# =========================================================================
# §4.5 手動拆盒調撥（2 筆 movement 同 group_id）
# =========================================================================
def split_box(session, product_id, box_qty, operator, note=""):
    """拆盒調撥（§4.5）。loose_added = box_qty*5。

    單一原子 tx 寫 2 筆 SPLIT_BOX 共用 movement_group_id：
      #1 (boxed,normal) -= box_qty
      #2 (loose_piece,normal) += box_qty*5
    任一失敗全 rollback（拋 InventoryError → 上層 rollback）。
    """
    if box_qty is None or box_qty < 1:
        return Result(ok=False, error="box_qty 必須 >= 1")
    if not operator:
        return Result(ok=False, error="operator 不可空")

    loose_added = box_qty * 5
    group_id = _new_group_id()
    mv_ids = []
    try:
        # #1 BOX 端扣減（前置：(boxed,normal).qty >= box_qty 由 _apply_delta 防超賣保證）
        qb_box, qa_box, mv1 = _apply_delta(
            session, product_id, "boxed", "normal", -box_qty,
            movement_type="SPLIT_BOX", operator=operator,
            stock_category_from="normal", stock_category_to=None,
            ref_type="split", ref_id=group_id, movement_group_id=group_id,
            reason="拆盒-盒端", note=note,
        )
        mv_ids.append(mv1)
        # #2 LOOSE 端增加（不存在則建列）
        qb_lp, qa_lp, mv2 = _apply_delta(
            session, product_id, "loose_piece", "normal", +loose_added,
            movement_type="SPLIT_BOX", operator=operator,
            stock_category_from=None, stock_category_to="normal",
            ref_type="split", ref_id=group_id, movement_group_id=group_id,
            reason="拆盒-片端", note=note,
            allow_create_on_increase=True,
        )
        mv_ids.append(mv2)
    except InventoryError as e:
        return Result(ok=False, error=str(e), movement_ids=mv_ids)

    # remaining 回報 boxed 池（§5 低水位檢查 boxed）
    return Result(ok=True, qty_before=qb_box, qty_after=qa_box,
                  remaining=qa_box, movement_ids=mv_ids)


# =========================================================================
# §4.6 通用出庫
# =========================================================================
_OUT_TYPES = {"GIFT", "TRIAL", "PR", "KOL_SAMPLE", "STAFF_USE",
              "INSTORE_USE", "SCRAP_LOSS", "SCRAP", "SALE"}


def deduct_out(session, product_id, inventory_pool, stock_category,
               movement_type, qty, operator, note="", ref=""):
    """通用出庫（§4.6）。對 (product_id,pool,category) 扣量並留痕。

    movement_type 限業務出庫類（GIFT/TRIAL/PR/KOL_SAMPLE/STAFF_USE/INSTORE_USE/
    SCRAP_LOSS/SCRAP）。觸發 §5 低水位（由呼叫端 commit 後查）。
    """
    if movement_type not in _OUT_TYPES:
        return Result(ok=False, error=f"非出庫類 movement_type: {movement_type}")
    if inventory_pool not in INVENTORY_POOLS:
        return Result(ok=False, error=f"非法 inventory_pool: {inventory_pool}")
    if stock_category not in STOCK_CATEGORIES:
        return Result(ok=False, error=f"非法 stock_category: {stock_category}")
    if qty is None or qty <= 0:
        return Result(ok=False, error="qty 必須 > 0")
    if not operator:
        return Result(ok=False, error="operator 不可空")

    try:
        qb, qa, mv_id = _apply_delta(
            session, product_id, inventory_pool, stock_category, -qty,
            movement_type=movement_type, operator=operator,
            stock_category_from=stock_category, stock_category_to=None,
            ref_type="manual", ref_id=(ref or None), note=note,
        )
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                  movement_ids=[mv_id])


# =========================================================================
# §4.7 入庫
# =========================================================================
def restock(session, product_id, inventory_pool, stock_category,
            movement_type, qty, operator, note="", ref=""):
    """入庫（§4.7）。movement_type ∈ {PURCHASE, RESTOCK}。

    qty += 量 + 同 tx 寫 1 筆 movement。入庫不觸發低水位（補貨只解除警示）。
    """
    if movement_type not in ("PURCHASE", "RESTOCK"):
        return Result(ok=False, error=f"入庫類僅限 PURCHASE/RESTOCK: {movement_type}")
    if inventory_pool not in INVENTORY_POOLS:
        return Result(ok=False, error=f"非法 inventory_pool: {inventory_pool}")
    if stock_category not in STOCK_CATEGORIES:
        return Result(ok=False, error=f"非法 stock_category: {stock_category}")
    if qty is None or qty <= 0:
        return Result(ok=False, error="qty 必須 > 0")
    if not operator:
        return Result(ok=False, error="operator 不可空")

    try:
        qb, qa, mv_id = _apply_delta(
            session, product_id, inventory_pool, stock_category, +qty,
            movement_type=movement_type, operator=operator,
            stock_category_from=None, stock_category_to=stock_category,
            ref_type="restock", ref_id=(ref or None), note=note,
            allow_create_on_increase=True,
        )
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                  movement_ids=[mv_id])


# =========================================================================
# §7.8 預留釋放 / 取消回 normal
# =========================================================================
def release_reserved(session, product_id, qty, operator,
                     order_ref, note="", mode="pickup"):
    """預留生命週期（§7.8）。reserved 物理分列硬隔離。

    mode='pickup'：(boxed,reserved) -= qty，RELEASE_RESERVE，ref_type=order，出庫，不經 normal。
    mode='cancel'：reserved -= qty、normal += qty（from=reserved→to=normal），
                   reason=cancel，2 筆同 movement_group_id 綁定，方向寫死不得反向。
    """
    if qty is None or qty <= 0:
        return Result(ok=False, error="qty 必須 > 0")
    if not operator:
        return Result(ok=False, error="operator 不可空")
    if mode not in ("pickup", "cancel"):
        return Result(ok=False, error=f"非法 mode: {mode}")

    if mode == "pickup":
        try:
            qb, qa, mv_id = _apply_delta(
                session, product_id, "boxed", "reserved", -qty,
                movement_type="RELEASE_RESERVE", operator=operator,
                stock_category_from="reserved", stock_category_to=None,
                ref_type="order", ref_id=order_ref,
                reason="pickup", note=note,
            )
        except InventoryError as e:
            return Result(ok=False, error=str(e))
        return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                      movement_ids=[mv_id])

    # mode == "cancel"：reserved -> normal（方向寫死）
    group_id = _new_group_id()
    mv_ids = []
    try:
        # #1 reserved 端扣
        qb_r, qa_r, mv1 = _apply_delta(
            session, product_id, "boxed", "reserved", -qty,
            movement_type="RELEASE_RESERVE", operator=operator,
            stock_category_from="reserved", stock_category_to="normal",
            ref_type="order", ref_id=order_ref, movement_group_id=group_id,
            reason="cancel", note=note,
        )
        mv_ids.append(mv1)
        # #2 normal 端加（轉回可售）
        qb_n, qa_n, mv2 = _apply_delta(
            session, product_id, "boxed", "normal", +qty,
            movement_type="RELEASE_RESERVE", operator=operator,
            stock_category_from="reserved", stock_category_to="normal",
            ref_type="order", ref_id=order_ref, movement_group_id=group_id,
            reason="cancel", note=note,
            allow_create_on_increase=True,
        )
        mv_ids.append(mv2)
    except InventoryError as e:
        return Result(ok=False, error=str(e), movement_ids=mv_ids)

    return Result(ok=True, qty_before=qb_n, qty_after=qa_n, remaining=qa_n,
                  movement_ids=mv_ids)


# =========================================================================
# CR-4（2026-08-31）訂單銷售回補：SALE 反向 +qty，型別 SALE_REVERSAL
# =========================================================================
def reverse_sale(session, order_id, operator, actor_id=None, reason=""):
    """整單銷售回補（訂單編輯前的反沖 / 未出貨作廢）。

    以該單 ref_type='order' AND ref_id=str(order_id) 的 SALE 與 SALE_REVERSAL movement 為準，
    逐 (product_id, pool, category) 計淨額（SALE 為負、REVERSAL 為正）：
    - 全部淨額為 0 ⇒ 已回補過，回 ok=False「已回補過」拒絕重複（冪等，Codex M1）。
    - 完全沒有 SALE 紀錄 ⇒ 無事可做，回 ok=True、movement_ids=[]（歷史直建單相容）。
    - 否則對每個淨額 <0 的鍵 _apply_delta(+|淨額|, SALE_REVERSAL)，同一新 movement_group_id，
      note 記原 SALE movement_id 清單，reason 由呼叫端標明 order_edit / order_void。
    R1：唯一經 _apply_delta，同 tx 寫 movement；session 由呼叫端 commit/rollback。
    寫法範本：release_reserved(mode='cancel')。
    """
    if not operator:
        return Result(ok=False, error="operator 不可空")
    ref_id = str(order_id)
    mvs = (
        session.query(InventoryMovement)
        .filter(
            InventoryMovement.ref_type == "order",
            InventoryMovement.ref_id == ref_id,
            InventoryMovement.movement_type.in_(("SALE", "SALE_REVERSAL")),
        )
        .order_by(InventoryMovement.movement_id.asc())
        .all()
    )
    if not mvs:
        return Result(ok=True, movement_ids=[])

    net = {}          # (pid, pool, cat) -> 淨額
    src_ids = {}      # (pid, pool, cat) -> 原 SALE movement_id 清單
    for m in mvs:
        if m.movement_type == "SALE":
            cat = m.stock_category_from or "normal"
        else:
            cat = m.stock_category_to or "normal"
        key = (m.product_id, m.inventory_pool, cat)
        net[key] = net.get(key, 0) + int(m.qty_delta or 0)
        if m.movement_type == "SALE":
            src_ids.setdefault(key, []).append(m.movement_id)

    todo = [(k, v) for k, v in net.items() if v < 0]
    if not todo:
        return Result(ok=False, error="已回補過（該單 SALE 已全數對沖，拒絕重複回補）")

    group_id = _new_group_id()
    mv_ids = []
    try:
        for (pid, pool, cat), v in todo:
            qb, qa, mv_id = _apply_delta(
                session, pid, pool, cat, +abs(v),
                movement_type="SALE_REVERSAL", operator=operator,
                stock_category_from=None, stock_category_to=cat,
                ref_type="order", ref_id=ref_id, movement_group_id=group_id,
                reason=(reason or "reverse_sale"),
                note=f"回補訂單 {ref_id}；原 SALE movement_id: {src_ids.get((pid, pool, cat), [])}",
                created_by=actor_id,
                allow_create_on_increase=True,
            )
            mv_ids.append(mv_id)
    except InventoryError as e:
        return Result(ok=False, error=str(e), movement_ids=mv_ids)

    return Result(ok=True, movement_ids=mv_ids)


# =========================================================================
# §7.9 手動調整 / 盤點（ADJUSTMENT + reason 必填 + owner 核可）
# =========================================================================
def adjust_inventory(session, product_id, inventory_pool, stock_category,
                     target_qty, reason, operator, approved_by, note=""):
    """調整單（§7.9）。經契約在 tx 內改 + 寫 movement(ADJUSTMENT)，含 qty_before/after。

    禁直改 inventory_balances；reason 必填；approved_by 須為 owner（角色檢查在 bp 層 +
    此處要求非空）。delta = target_qty - 現值；可正可負；列不存在且 target>0 則建列。
    """
    if inventory_pool not in INVENTORY_POOLS:
        return Result(ok=False, error=f"非法 inventory_pool: {inventory_pool}")
    if stock_category not in STOCK_CATEGORIES:
        return Result(ok=False, error=f"非法 stock_category: {stock_category}")
    if target_qty is None or target_qty < 0:
        return Result(ok=False, error="target_qty 必須 >= 0")
    if not reason:
        return Result(ok=False, error="reason 必填（§7.9）")
    if not operator:
        return Result(ok=False, error="operator 不可空")
    if approved_by is None:
        return Result(ok=False, error="approved_by 必填（owner 核可，§7.9）")

    bal = _get_balance(session, product_id, inventory_pool, stock_category)
    current = bal.qty if bal else 0
    delta = target_qty - current
    if delta == 0:
        # 無變化：不留痕、視為成功
        return Result(ok=True, qty_before=current, qty_after=current,
                      remaining=current, movement_ids=[])

    try:
        qb, qa, mv_id = _apply_delta(
            session, product_id, inventory_pool, stock_category, delta,
            movement_type="ADJUSTMENT", operator=operator,
            stock_category_from=(stock_category if delta < 0 else None),
            stock_category_to=(stock_category if delta > 0 else None),
            ref_type="adjustment", ref_id=str(approved_by),
            reason=reason,
            note=(note + f"（核可者 user_id={approved_by}）").strip(),
            created_by=approved_by,
            allow_create_on_increase=True,
        )
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                  movement_ids=[mv_id])


def quick_adjust(session, product_id, inventory_pool, stock_category,
                 target_qty, operator, actor_id, note=""):
    """庫存頁「直接改數字」路徑（森哥 2026-08-19 乙案）。

    與 adjust_inventory（§7.9 調整單）的差別，兩者不可混用：
    - adjust_inventory：owner 核可、reason 人工必填、ref_type='adjustment'。
    - quick_adjust：具庫存寫入權者當場直改、不必填 reason，系統自動留痕，
      ref_type='quick_edit'、reason 固定為「庫存頁直接修改」、ref_id/created_by = 操作者。

    仍守 R1：唯一經 _apply_delta，同 tx 寫 inventory_movements（含 qty_before/after）。
    delta=0 不留痕。
    """
    if inventory_pool not in INVENTORY_POOLS:
        return Result(ok=False, error=f"非法 inventory_pool: {inventory_pool}")
    if stock_category not in STOCK_CATEGORIES:
        return Result(ok=False, error=f"非法 stock_category: {stock_category}")
    if target_qty is None or target_qty < 0:
        return Result(ok=False, error="數量必須 >= 0")
    if not operator:
        return Result(ok=False, error="operator 不可空")
    if actor_id is None:
        return Result(ok=False, error="actor_id 必填（留痕需記操作者）")

    bal = _get_balance(session, product_id, inventory_pool, stock_category)
    current = bal.qty if bal else 0
    delta = target_qty - current
    if delta == 0:
        return Result(ok=True, qty_before=current, qty_after=current,
                      remaining=current, movement_ids=[])

    try:
        qb, qa, mv_id = _apply_delta(
            session, product_id, inventory_pool, stock_category, delta,
            movement_type="ADJUSTMENT", operator=operator,
            stock_category_from=(stock_category if delta < 0 else None),
            stock_category_to=(stock_category if delta > 0 else None),
            ref_type="quick_edit", ref_id=str(actor_id),
            reason="庫存頁直接修改",
            note=(note + f"（操作者 user_id={actor_id}）").strip(),
            created_by=actor_id,
            allow_create_on_increase=True,
        )
    except InventoryError as e:
        return Result(ok=False, error=str(e))

    return Result(ok=True, qty_before=qb, qty_after=qa, remaining=qa,
                  movement_ids=[mv_id])


# 別名：接案任務以 manual_adjust 稱呼，地基契約簽章為 adjust_inventory，兩者同一實作
manual_adjust = adjust_inventory


# =========================================================================
# §7.10 低水位檢查（commit 後查；門檻讀 inventory_thresholds）
# =========================================================================
def check_low_water(session, product_id, inventory_pool, stock_category="normal"):
    """低水位（§7.10）。回 Result(low_water, remaining, threshold)。

    remaining = 當前 (pool, category).qty；threshold 讀 inventory_thresholds(product_id, pool)。
    remaining <= threshold ⇒ low_water=true。無門檻設定 ⇒ low_water=false。
    扣減成功 commit 後由呼叫端呼叫。
    """
    bal = _get_balance(session, product_id, inventory_pool, stock_category)
    remaining = bal.qty if bal else 0
    thr_row = session.query(InventoryThreshold).filter_by(
        product_id=product_id, inventory_pool=inventory_pool
    ).first()
    if thr_row is None:
        return Result(ok=True, remaining=remaining, threshold=None, low_water=False)
    threshold = thr_row.threshold_qty
    return Result(ok=True, remaining=remaining, threshold=threshold,
                  low_water=(remaining <= threshold))


# =========================================================================
# CR-9（2026-08-31）：歷史輸入 ＝ 現有合計 ＋ 累計消耗（唯讀彙整，不改庫存）
# =========================================================================
# 累計消耗計入的 movement_type（只讀 inventory_movements，不含轉移／校正類）：
#   - SALE + SALE_REVERSAL：qty_delta 淨額取絕對值（作廢／編輯回補後自動扣回）
#   - 業務出庫 8 類 + PAPERBAG_OUT：|負 qty_delta| 加總
# 不計入：SPLIT_BOX / RELEASE_RESERVE（池內、分類轉移）、ADJUSTMENT / SEED /
#   PURCHASE / RESTOCK / IMPORT（已反映在現有合計，不重複加）。
CONSUMED_SALE_TYPES = ("SALE", "SALE_REVERSAL")
CONSUMED_OUT_TYPES = ("GIFT", "TRIAL", "PR", "KOL_SAMPLE", "STAFF_USE",
                      "INSTORE_USE", "SCRAP", "SCRAP_LOSS", "PAPERBAG_OUT")


def consumed_by_pool(session):
    """回 {(product_id, inventory_pool): 累計消耗(int ≥ 0)}，一次聚合查詢。

    歷史輸入(product, pool) = 五分類現有合計 + consumed_by_pool()[(product, pool)]。
    """
    from sqlalchemy import func, case
    sale_net = func.coalesce(func.sum(case(
        (InventoryMovement.movement_type.in_(CONSUMED_SALE_TYPES), InventoryMovement.qty_delta),
        else_=0)), 0)
    out_neg = func.coalesce(func.sum(case(
        (InventoryMovement.movement_type.in_(CONSUMED_OUT_TYPES) & (InventoryMovement.qty_delta < 0),
         -InventoryMovement.qty_delta),
        else_=0)), 0)
    rows = (
        session.query(InventoryMovement.product_id, InventoryMovement.inventory_pool,
                      sale_net, out_neg)
        .filter(InventoryMovement.movement_type.in_(CONSUMED_SALE_TYPES + CONSUMED_OUT_TYPES))
        .group_by(InventoryMovement.product_id, InventoryMovement.inventory_pool)
        .all()
    )
    result = {}
    for pid, pool, s_net, o_neg in rows:
        consumed = max(0, -int(s_net or 0)) + int(o_neg or 0)
        result[(pid, pool)] = consumed
    return result
