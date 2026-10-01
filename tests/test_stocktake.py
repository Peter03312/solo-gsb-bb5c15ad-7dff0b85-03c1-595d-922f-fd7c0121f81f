"""批次库位盘点：多行盘亏核销、整单拒绝、快照查询、幂等、与撤销/隔离交互。"""
from __future__ import annotations

from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import DEFAULT_LOCATION_CODE
from app.schemas import StocktakeIn


# ------------------------------------------------------------ 辅助


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty), "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    total = sum((Decimal(str(q)) for _, q, *_ in inputs), Decimal(0))
    input_list = []
    for item in inputs:
        entry = {"batch_no": item[0], "quantity": str(item[1])}
        if len(item) > 2 and item[2] is not None:
            entry["location_code"] = item[2]
        input_list.append(entry)
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": input_list,
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _transfer(client, no, batch, src, dst, qty, key=None):
    return client.post(
        "/transfers",
        json={
            "transfer_no": no, "batch_no": batch,
            "source_location": src, "target_location": dst, "quantity": str(qty),
        },
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _cancel(client, output_no, reason, key):
    return client.post(
        f"/productions/{output_no}/cancellation",
        json={"cancel_reason": reason},
        headers={"Idempotency-Key": key},
    )


def _stocktake(client, no, items, key=None):
    return client.post(
        "/stocktakes",
        json={
            "stocktake_no": no,
            "items": [
                {
                    "batch_no": b, "location_code": loc,
                    "book_quantity": str(book), "actual_quantity": str(actual),
                }
                for b, loc, book, actual in items
            ],
        },
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _get_stocktake(client, no):
    return client.get(f"/stocktakes/{no}")


def _loc_map(client, batch_no):
    body = client.get(f"/batches/{batch_no}/locations").json()
    return {row["location_code"]: Decimal(str(row["quantity"]))
            for row in body["locations"]}, Decimal(str(body["remaining_qty"]))


# ------------------------------------------------------------ 成功路径


def test_stocktake_single_line_deducts_location_and_global(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "92")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stocktake_no"] == "CC-1"
    assert body["status"] == "COMMITTED"
    assert body["created_at"]
    assert body["items"] == [
        {
            "line_no": 1,
            "batch_no": "RM-1",
            "location_code": DEFAULT_LOCATION_CODE,
            "unit": "kg",
            "book_quantity": 100.0,
            "actual_quantity": 92.0,
            "variance_quantity": 8.0,
        }
    ]
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("92")}
    assert global_qty == Decimal("92")
    # GET 返回同样的状态与逐行原值
    got = _get_stocktake(client, "CC-1")
    assert got.status_code == 200
    assert got.json() == body


def test_stocktake_multiple_lines_batches_and_locations(client):
    """整单多行：不同批次、同批次不同库位，逐行扣减、按批次扣全局余量。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    # RM-1: DEFAULT=70, A=30；RM-2: DEFAULT=50
    r = _stocktake(
        client,
        "CC-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "70", "65"),  # 亏 5
            ("RM-1", "A", "30", "30"),                   # 相符行
            ("RM-2", DEFAULT_LOCATION_CODE, "50", "40"),  # 亏 10
        ],
    )
    assert r.status_code == 201, r.text
    assert [it["variance_quantity"] for it in r.json()["items"]] == [5.0, 0.0, 10.0]

    locs1, g1 = _loc_map(client, "RM-1")
    assert locs1 == {DEFAULT_LOCATION_CODE: Decimal("65"), "A": Decimal("30")}
    assert g1 == Decimal("95")
    assert sum(locs1.values()) == g1
    locs2, g2 = _loc_map(client, "RM-2")
    assert locs2 == {DEFAULT_LOCATION_CODE: Decimal("40")}
    assert g2 == Decimal("40")


def test_stocktake_line_order_preserved_on_query(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    r = _stocktake(
        client,
        "CC-1",
        [
            ("RM-2", DEFAULT_LOCATION_CODE, "100", "90"),
            ("RM-1", DEFAULT_LOCATION_CODE, "100", "100"),
        ],
    )
    assert r.status_code == 201, r.text
    # 仅一行盘亏即可；行序按提交顺序（与批次锁定顺序无关）
    assert [it["line_no"] for it in r.json()["items"]] == [1, 2]
    assert [it["batch_no"] for it in r.json()["items"]] == ["RM-2", "RM-1"]


def test_stocktake_zero_actual_allowed_wipes_location(client):
    """实点量可以为 0：该库位与全局余量按账面量全额核销。"""
    _raw(client, "RM-1", "30")
    r = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "30", "0")])
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["variance_quantity"] == 30.0
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("0")}
    assert global_qty == 0


def test_stocktake_allowed_for_quarantined_batch(client):
    """隔离批次仍可盘点核销。"""
    _raw(client, "RM-1", "100")
    r = client.post(
        "/contamination",
        json={"batch_no": "RM-1", "note": "疑似"},
        headers={"Idempotency-Key": "flag-1"},
    )
    assert r.status_code == 200
    r = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "80")])
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1")[1] == Decimal("80")
    # 隔离状态不变
    assert client.get("/batches/RM-1").json()["is_quarantined"] is True


def test_stocktake_allowed_for_cancelled_output(client):
    """已撤销生产产出仍可盘点核销。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    assert _cancel(client, "MIX-1", "录入错误", "c1").status_code == 200
    r = _stocktake(client, "CC-1", [("MIX-1", DEFAULT_LOCATION_CODE, "40", "35")])
    assert r.status_code == 201, r.text
    assert _loc_map(client, "MIX-1")[1] == Decimal("35")


def test_stocktake_after_transfer_uses_new_location_balance(client):
    """调拨改变库位分布后，按调拨后的账面量核对并核销。"""
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = _stocktake(
        client,
        "CC-1",
        [("RM-1", "A", "30", "20"), ("RM-1", DEFAULT_LOCATION_CODE, "70", "70")],
    )
    assert r.status_code == 201, r.text
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("20")}
    assert global_qty == Decimal("90")


def test_stocktake_does_not_change_lineage_or_history(client):
    """盘点不改历史投料与谱系：只扣余量。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    assert _stocktake(
        client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "60", "50")]
    ).status_code == 201
    lineage = client.get("/batches/RM-1/lineage").json()
    assert lineage["downstream"][0]["batch_no"] == "MIX-1"
    # 原生产单据不变
    prod = client.get("/productions/MIX-1").json()
    assert prod["inputs"] == [{"batch_no": "RM-1", "quantity": 40.0}]


# ------------------------------------------------------------ 输入/业务校验拒绝


def test_stocktake_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/stocktakes",
        json={"stocktake_no": "CC-1",
              "items": [{"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                         "book_quantity": "100", "actual_quantity": "90"}]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_stocktake_empty_items_rejected(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/stocktakes",
        json={"stocktake_no": "CC-1", "items": []},
        headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 不留盘点记录
    assert _get_stocktake(client, "CC-1").status_code == 404


def test_stocktake_negative_quantity_rejected(client):
    _raw(client, "RM-1", "100")
    for book, actual in [("-1", "0"), ("100", "-5")]:
        r = client.post(
            "/stocktakes",
            json={"stocktake_no": f"CC-{book}-{actual}",
                  "items": [{"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                             "book_quantity": book, "actual_quantity": actual}]},
            headers={"Idempotency-Key": f"k-{book}-{actual}"},
        )
        assert r.status_code == 422, (book, actual, r.text)


def test_stocktake_actual_above_book_rejected(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "101")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _loc_map(client, "RM-1")[1] == Decimal("100")


def test_stocktake_without_any_loss_rejected(client):
    """整单没有任何盘亏（全部账实相符）：422 STOCKTAKE_WITHOUT_LOSS。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    r = _stocktake(
        client,
        "CC-1",
        [("RM-1", DEFAULT_LOCATION_CODE, "100", "100"),
         ("RM-2", DEFAULT_LOCATION_CODE, "50", "50")],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCKTAKE_WITHOUT_LOSS"
    # 余额不变、不留记录
    assert _loc_map(client, "RM-1")[1] == Decimal("100")
    assert _get_stocktake(client, "CC-1").status_code == 404


def test_stocktake_duplicate_line_rejected(client):
    """同单内（批次, 库位）重复：422 DUPLICATE_STOCKTAKE_LINE。"""
    _raw(client, "RM-1", "100")
    r = _stocktake(
        client,
        "CC-1",
        [("RM-1", DEFAULT_LOCATION_CODE, "100", "95"),
         ("RM-1", DEFAULT_LOCATION_CODE, "100", "90")],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_STOCKTAKE_LINE"
    assert _loc_map(client, "RM-1")[1] == Decimal("100")
    assert _get_stocktake(client, "CC-1").status_code == 404


def test_stocktake_unknown_batch_rejected(client):
    r = _stocktake(client, "CC-1", [("GHOST", DEFAULT_LOCATION_CODE, "10", "9")])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert _get_stocktake(client, "CC-1").status_code == 404


def test_stocktake_unknown_location_rejected(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "CC-1", [("RM-1", "NOPE", "10", "9")])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LOCATION_NOT_FOUND"
    body = r.json()
    assert body["error"]["details"] == {"batch_no": "RM-1", "location_code": "NOPE"}
    assert _loc_map(client, "RM-1")[1] == Decimal("100")


def test_stocktake_book_mismatch_entire_order_rejected(client):
    """账面已变动：多行中任一行实存不等于所填账面量，整单拒绝、全行不扣。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    r = _stocktake(
        client,
        "CC-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "100", "90"),   # 与实存相符
            ("RM-2", DEFAULT_LOCATION_CODE, "99", "80"),    # 实存 100 != 99
        ],
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "STOCKTAKE_BOOK_MISMATCH"
    assert body["error"]["details"]["batch_no"] == "RM-2"
    assert body["error"]["details"]["submitted_book_quantity"] == "99"
    assert body["error"]["details"]["current_quantity"] == "100.0000"
    # 全部余额不变、不留盘点记录
    assert _loc_map(client, "RM-1")[1] == Decimal("100")
    assert _loc_map(client, "RM-2")[1] == Decimal("100")
    assert _get_stocktake(client, "CC-1").status_code == 404


def test_stocktake_book_changed_after_consumption_rejected(client):
    """盘点核对的是提交时的当前库位实存：先按旧账面准备，被投料改变后拒绝。"""
    _raw(client, "RM-1", "100")
    _produce(client, "FG-1", [("RM-1", "20", DEFAULT_LOCATION_CODE)], out_qty=20)
    # 当前 DEFAULT=80，却仍按旧账面 100 填报
    r = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "90")])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STOCKTAKE_BOOK_MISMATCH"
    assert _loc_map(client, "RM-1")[1] == Decimal("80")


def test_stocktake_no_reuse_rejected(client):
    _raw(client, "RM-1", "100")
    r1 = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "90")])
    assert r1.status_code == 201
    # 单号复用（即便换幂等键、换内容）
    r2 = client.post(
        "/stocktakes",
        json={"stocktake_no": "CC-1",
              "items": [{"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                         "book_quantity": "90", "actual_quantity": "80"}]},
        headers={"Idempotency-Key": "another-key"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "STOCKTAKE_NO_REUSED"
    # 只核销一次
    assert _loc_map(client, "RM-1")[1] == Decimal("90")
    # 原单内容不变
    got = _get_stocktake(client, "CC-1").json()
    assert got["items"][0]["actual_quantity"] == 90.0


def test_get_unknown_stocktake_404(client):
    r = _get_stocktake(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "STOCKTAKE_NOT_FOUND"


# ------------------------------------------------------------ 幂等


def test_stocktake_idempotent_replay_returns_first_result(client):
    _raw(client, "RM-1", "100")
    payload = {"stocktake_no": "CC-1",
               "items": [{"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                          "book_quantity": "100", "actual_quantity": "90"}]}
    r1 = client.post("/stocktakes", json=payload, headers={"Idempotency-Key": "k1"})
    r2 = client.post("/stocktakes", json=payload, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    # 只核销一次
    assert _loc_map(client, "RM-1")[1] == Decimal("90")


def test_stocktake_idempotency_conflict_on_different_body(client):
    _raw(client, "RM-1", "100")
    r1 = client.post(
        "/stocktakes",
        json={"stocktake_no": "CC-1",
              "items": [{"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                         "book_quantity": "100", "actual_quantity": "90"}]},
        headers={"Idempotency-Key": "shared"},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/stocktakes",
        json={"stocktake_no": "CC-2",
              "items": [{"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                         "book_quantity": "90", "actual_quantity": "80"}]},
        headers={"Idempotency-Key": "shared"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 第二笔未生效
    assert _loc_map(client, "RM-1")[1] == Decimal("90")


def test_stocktake_failure_not_stored_can_retry_same_key(client):
    """失败请求不落幂等记录：修正账面后可用同 key 重试成功。"""
    _raw(client, "RM-1", "100")
    r1 = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "99", "90")],
                    key="retry-key")
    assert r1.status_code == 409
    assert r1.json()["error"]["code"] == "STOCKTAKE_BOOK_MISMATCH"
    r2 = _stocktake(client, "CC-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "90")],
                    key="retry-key")
    assert r2.status_code == 201, r2.text
    assert _loc_map(client, "RM-1")[1] == Decimal("90")


# ------------------------------------------------------------ 与生产撤销交互


def test_cancel_rejected_after_stocktake_loss_on_output(client):
    """发生盘亏的生产产出不得撤销原生产单：409 OUTPUT_HAS_STOCKTAKE_LOSS。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    assert _stocktake(
        client, "CC-1", [("MIX-1", DEFAULT_LOCATION_CODE, "40", "35")]
    ).status_code == 201
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "OUTPUT_HAS_STOCKTAKE_LOSS"
    assert body["error"]["details"]["stocktake_nos"] == ["CC-1"]
    # 生产单未撤销、来源余量未返还
    assert client.get("/productions/MIX-1").json()["is_cancelled"] is False
    assert _loc_map(client, "RM-1")[1] == Decimal("60")


def test_cancel_still_blocked_after_loss_when_stock_later_moves(client):
    """曾在产出上盘亏：其后即使库位再经调拨移动，撤销仍被拒绝。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    assert _stocktake(
        client, "CC-1", [("MIX-1", DEFAULT_LOCATION_CODE, "40", "35")]
    ).status_code == 201
    # 盘亏后把剩余 35 中的 5 调拨到 A（隔离批次可调拨，普通批次更可调拨）
    assert _transfer(
        client, "T1", "MIX-1", DEFAULT_LOCATION_CODE, "A", "5"
    ).status_code == 201
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_HAS_STOCKTAKE_LOSS"
    # 余额未因撤销变化：DEFAULT=30, A=5
    assert _loc_map(client, "MIX-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("30"), "A": Decimal("5")}


def test_zero_variance_line_on_output_does_not_block_cancel(client):
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    # 盘亏单包含 MIX-1 的相符行（差额 0），另有来源盘亏
    assert _stocktake(
        client, "CC-1", [("MIX-1", DEFAULT_LOCATION_CODE, "40", "40"),
                         ("RM-1", DEFAULT_LOCATION_CODE, "60", "55")]
    ).status_code == 201
    # MIX-1 未发生盘亏，仍可撤销（返还 RM-1；盘点核销不影响撤销路径）
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 200, r.text
    # RM-1: 100 -40(投料) -5(盘亏) +40(撤销返还) = 95
    assert _loc_map(client, "RM-1")[1] == Decimal("95")


# ------------------------------------------------------------ 服务层直接校验


def test_service_submit_stocktake_atomic_on_mismatch(engine):
    """服务层：多行中一行账面不符，整单回滚，无盘点单/行、库存不变。"""
    from sqlalchemy import select

    from app.models import Stocktake
    from app.schemas import RawBatchIn

    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity=Decimal("100"))
        )
    data = StocktakeIn(
        stocktake_no="CC-1",
        items=[
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_quantity": Decimal("100"), "actual_quantity": Decimal("90")},
        ],
    )
    # 先把账面改掉（模拟库位实存已变）；同步改全局余量保持两层恒等
    with session_scope() as s:
        from app.models import Batch, BatchLocation

        s.get(Batch, "RM-1").remaining_qty = Decimal("99")
        row = s.execute(
            select(BatchLocation).where(
                BatchLocation.batch_no == "RM-1",
                BatchLocation.location_code == DEFAULT_LOCATION_CODE,
            )
        ).scalar_one()
        row.qty = Decimal("99")

    with session_scope() as s:
        try:
            service.submit_stocktake(s, data)
            assert False, "应抛出 STOCKTAKE_BOOK_MISMATCH"
        except AppError as exc:
            assert exc.code == "STOCKTAKE_BOOK_MISMATCH"

    with session_scope() as s:
        assert s.execute(select(Stocktake)).scalars().all() == []
        from app.models import Batch

        assert s.get(Batch, "RM-1").remaining_qty == Decimal("99")
