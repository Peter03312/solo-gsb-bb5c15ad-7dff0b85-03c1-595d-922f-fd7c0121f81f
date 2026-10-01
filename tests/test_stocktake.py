"""库位盘点：提交、核对、核销、整单拒绝、查询、撤销联动、并发。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import func, select

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import DEFAULT_LOCATION_CODE, Batch, BatchLocation, Stocktake, StocktakeLine
from app.schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    StocktakeIn,
    StocktakeLineIn,
    SuspectIn,
    TransferIn,
)


# ------------------------------------------------------------ 辅助


def _q(v) -> Decimal:
    return Decimal(str(v))


def _raw(client, batch_no, qty, unit="kg", key=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty)},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    total = sum((Decimal(str(q)) for _, q, *_ in inputs), Decimal(0))
    normalized = []
    for b, q, *rest in inputs:
        loc = rest[0] if rest else None
        entry = {"batch_no": b, "quantity": str(q)}
        if loc is not None:
            entry["location_code"] = loc
        normalized.append(entry)
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": normalized,
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


def _stocktake(client, no, lines, key=None):
    return client.post(
        "/stocktakes",
        json={
            "stocktake_no": no,
            "lines": [
                {
                    "batch_no": b,
                    "location_code": loc,
                    "book_qty": str(book),
                    "actual_qty": str(actual),
                }
                for b, loc, book, actual in lines
            ],
        },
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _get_stocktake(client, no):
    return client.get(f"/stocktakes/{no}")


def _loc_qty(client, batch_no, location_code):
    body = client.get(f"/batches/{batch_no}/locations").json()
    for row in body["locations"]:
        if row["location_code"] == location_code:
            return Decimal(str(row["quantity"]))
    return None


def _remaining(client, batch_no):
    return Decimal(str(client.get(f"/batches/{batch_no}").json()["remaining_qty"]))


# ------------------------------------------------------------ 成功路径


def test_stocktake_happy_path_writes_off_shortage(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "97")])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stocktake_no"] == "ST-1"
    assert body["status"] == "POSTED"
    assert body["lines"] == [
        {
            "line_no": 0,
            "batch_no": "RM-1",
            "location_code": DEFAULT_LOCATION_CODE,
            "unit": "kg",
            "book_qty": 100.0,
            "actual_qty": 97.0,
            "shortage_qty": 3.0,
            "location_balance": 97.0,
            "batch_remaining_qty": 97.0,
        }
    ]
    # 库位层与全局层同步核减，且仍恒等
    assert _loc_qty(client, "RM-1", DEFAULT_LOCATION_CODE) == Decimal("97.0000")
    assert _remaining(client, "RM-1") == Decimal("97.0000")


def test_stocktake_multiple_lines_different_batches_and_locations(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "40")
    # 每行单位逐行取自批次
    client.post(
        "/raw-batches",
        json={"batch_no": "RM-3", "unit": "L", "quantity": "10"},
        headers={"Idempotency-Key": "key-RM-3"},
    )
    _transfer(client, "T2", "RM-3", DEFAULT_LOCATION_CODE, "A", "6", key="key-T2")

    r = _stocktake(
        client,
        "ST-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "60", "60"),  # 无盘亏行（允许）
            ("RM-1", "A", "40", "38"),                   # 盘亏 2
            ("RM-2", DEFAULT_LOCATION_CODE, "50", "49.5"),  # 盘亏 0.5
            ("RM-3", "A", "6", "0"),                     # 整库位盘空
        ],
    )
    assert r.status_code == 201, r.text
    lines = r.json()["lines"]
    assert [ln["line_no"] for ln in lines] == [0, 1, 2, 3]
    assert [ln["unit"] for ln in lines] == ["kg", "kg", "kg", "L"]
    assert [ln["shortage_qty"] for ln in lines] == [0.0, 2.0, 0.5, 6.0]

    assert _loc_qty(client, "RM-1", DEFAULT_LOCATION_CODE) == Decimal("60")
    assert _loc_qty(client, "RM-1", "A") == Decimal("38")
    assert _remaining(client, "RM-1") == Decimal("98")
    assert _remaining(client, "RM-2") == Decimal("49.5")
    assert _loc_qty(client, "RM-3", "A") == Decimal("0")
    assert _remaining(client, "RM-3") == Decimal("4")  # DEFAULT 仍有 4


def test_stocktake_zero_shortage_line_kept_and_query_returns_snapshot(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(
        client,
        "ST-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "100", "95"),
        ],
    )
    assert r.status_code == 201
    created_at = r.json()["created_at"]

    # 后续库存再变动，盘点单返回的仍是提交当时的不可修改快照
    _raw(client, "RM-2", "10")
    _produce(client, "FG-1", [("RM-1", "10", None)])

    g = _get_stocktake(client, "ST-1")
    assert g.status_code == 200
    body = g.json()
    assert body["status"] == "POSTED"
    assert body["created_at"] == created_at
    line = body["lines"][0]
    assert line["book_qty"] == 100.0
    assert line["actual_qty"] == 95.0
    assert line["shortage_qty"] == 5.0
    # 附随字段反映当前实存（快照之外的即时值）
    assert line["location_balance"] == 85.0
    assert line["batch_remaining_qty"] == 85.0


def test_get_unknown_stocktake_404(client):
    r = _get_stocktake(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "STOCKTAKE_NOT_FOUND"


# ------------------------------------------------------------ 输入校验


def test_stocktake_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/stocktakes",
        json={
            "stocktake_no": "ST-1",
            "lines": [
                {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                 "book_qty": "100", "actual_qty": "90"}
            ],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_stocktake_negative_qty_rejected_422(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "-1")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 失败不留记录
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_actual_exceeds_book_rejected_422(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "101")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCKTAKE_ACTUAL_EXCEEDS_BOOK"
    assert _remaining(client, "RM-1") == Decimal("100")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_all_zero_shortage_rejected_422(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "100")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCKTAKE_NO_SHORTAGE"
    assert _remaining(client, "RM-1") == Decimal("100")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_duplicate_line_rejected_422(client):
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "20")
    r = _stocktake(
        client,
        "ST-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "80", "79"),
            ("RM-1", DEFAULT_LOCATION_CODE, "80", "78"),
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_STOCKTAKE_LINE"
    assert _remaining(client, "RM-1") == Decimal("100")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_empty_lines_rejected_422(client):
    r = client.post(
        "/stocktakes",
        json={"stocktake_no": "ST-1", "lines": []},
        headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 422


def test_stocktake_blank_location_rejected_422(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/stocktakes",
        json={
            "stocktake_no": "ST-1",
            "lines": [
                {"batch_no": "RM-1", "location_code": "  ",
                 "book_qty": "100", "actual_qty": "90"}
            ],
        },
        headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------ 事务期核对


def test_stocktake_unknown_batch_404_and_no_record(client):
    _raw(client, "RM-1", "100")
    # 第一行合法、第二行批次不存在：整单拒绝
    r = _stocktake(
        client,
        "ST-1",
        [
            ("GHOST", DEFAULT_LOCATION_CODE, "5", "4"),
            ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),
        ],
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert _remaining(client, "RM-1") == Decimal("100")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_unknown_location_404_and_no_record(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "10")
    r = _stocktake(
        client,
        "ST-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),
            ("RM-2", "NOPE", "10", "9"),
        ],
    )
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "LOCATION_NOT_FOUND"
    assert err["details"] == {"batch_no": "RM-2", "location_code": "NOPE"}
    assert _remaining(client, "RM-1") == Decimal("100")
    assert _remaining(client, "RM-2") == Decimal("10")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_book_changed_rejected_409(client):
    _raw(client, "RM-1", "100")
    # 账面填 100，但当前实存已经是 70
    _produce(client, "FG-1", [("RM-1", "30", None)])
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "95")])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "STOCKTAKE_BOOK_MISMATCH"
    assert err["details"]["declared_book_qty"] == "100"
    assert err["details"]["current_book_qty"] == "70.0000"
    # 余额不变、不留记录
    assert _remaining(client, "RM-1") == Decimal("70")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_whole_order_atomic_when_later_line_mismatches(client):
    """第一行核对通过、第二行账面已变动：整单回滚，第一行也不核销。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _produce(client, "FG-9", [("RM-2", "20", None)], key="key-fg9")
    r = _stocktake(
        client,
        "ST-1",
        [
            ("RM-1", DEFAULT_LOCATION_CODE, "100", "90"),
            ("RM-2", DEFAULT_LOCATION_CODE, "100", "90"),  # 实存为 80
        ],
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STOCKTAKE_BOOK_MISMATCH"
    assert _remaining(client, "RM-1") == Decimal("100")
    assert _remaining(client, "RM-2") == Decimal("80")
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_no_reuse_409(client):
    _raw(client, "RM-1", "100")
    r1 = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "99")])
    assert r1.status_code == 201
    # 单号复用（即使账面已更新、内容合法）：拒绝（换用新幂等键以直达业务校验）
    r2 = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "99", "98")],
                    key="key-ST-1-retry")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "STOCKTAKE_NO_REUSED"
    # 仍是第一次核销后的余额
    assert _remaining(client, "RM-1") == Decimal("99")


# ------------------------------------------------------------ 幂等


def test_stocktake_idempotent_replay(client):
    _raw(client, "RM-1", "100")
    payload = {
        "stocktake_no": "ST-1",
        "lines": [
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_qty": "100", "actual_qty": "95"}
        ],
    }
    r1 = client.post("/stocktakes", json=payload, headers={"Idempotency-Key": "IK-1"})
    assert r1.status_code == 201
    r2 = client.post("/stocktakes", json=payload, headers={"Idempotency-Key": "IK-1"})
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    # 只核销一次
    assert _remaining(client, "RM-1") == Decimal("95")


def test_stocktake_idempotency_conflict_on_different_body(client):
    _raw(client, "RM-1", "100")
    base = {
        "stocktake_no": "ST-1",
        "lines": [
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_qty": "100", "actual_qty": "95"}
        ],
    }
    r1 = client.post("/stocktakes", json=base, headers={"Idempotency-Key": "IK-1"})
    assert r1.status_code == 201
    other = {
        "stocktake_no": "ST-2",
        "lines": [
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_qty": "95", "actual_qty": "90"}
        ],
    }
    r2 = client.post("/stocktakes", json=other, headers={"Idempotency-Key": "IK-1"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 第二单未发生
    assert _get_stocktake(client, "ST-2").status_code == 404


def test_failed_stocktake_not_persisted_and_key_reusable(client):
    """失败请求不固化幂等记录：修正请求后可用同一 key 重试成功。"""
    _raw(client, "RM-1", "100")
    bad = {
        "stocktake_no": "ST-1",
        "lines": [
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_qty": "100", "actual_qty": "100"}  # 无盘亏
        ],
    }
    r1 = client.post("/stocktakes", json=bad, headers={"Idempotency-Key": "IK-1"})
    assert r1.status_code == 422
    good = {
        "stocktake_no": "ST-1",
        "lines": [
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_qty": "100", "actual_qty": "96"}
        ],
    }
    r2 = client.post("/stocktakes", json=good, headers={"Idempotency-Key": "IK-1"})
    assert r2.status_code == 201, r2.text
    assert _remaining(client, "RM-1") == Decimal("96")


# ------------------------------------------------------------ 隔离 / 已撤销产出


def test_stocktake_allowed_on_quarantined_batch(client):
    _raw(client, "RM-1", "100")
    r = client.post(
        "/contamination",
        json={"batch_no": "RM-1", "note": "疑似"},
        headers={"Idempotency-Key": "cont-1"},
    )
    assert r.status_code == 200
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "100", "80")])
    assert r.status_code == 201, r.text
    assert _remaining(client, "RM-1") == Decimal("80")
    # 隔离状态不变
    batch = client.get("/batches/RM-1").json()
    assert batch["is_quarantined"] is True


def test_stocktake_allowed_on_cancelled_output(client):
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "60", None)])
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200
    # MIX-1 已撤销（DEFAULT 仍保留 60），仍可盘点核销
    r = _stocktake(client, "ST-1", [("MIX-1", DEFAULT_LOCATION_CODE, "60", "55")])
    assert r.status_code == 201, r.text
    assert _remaining(client, "MIX-1") == Decimal("55")
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is True


# ------------------------------------------------------------ 撤销联动


def test_cancel_rejected_after_shortage_stocktake_on_output(client):
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "60", None)])
    r = _stocktake(client, "ST-1", [("MIX-1", DEFAULT_LOCATION_CODE, "60", "50")])
    assert r.status_code == 201

    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "OUTPUT_ALREADY_STOCKTAKEN"
    assert err["details"]["stocktake_nos"] == ["ST-1"]
    # 未返还：RM-1 仍为 40，MIX-1 仍为盘点后的 50
    assert _remaining(client, "RM-1") == Decimal("40")
    assert _remaining(client, "MIX-1") == Decimal("50")
    # 谱系与投料记录不改
    lineage = client.get("/batches/RM-1/lineage").json()
    assert [n["batch_no"] for n in lineage["downstream"]] == ["MIX-1"]
    assert client.get("/productions/MIX-1").json()["is_cancelled"] is False


def test_zero_shortage_stocktake_does_not_block_cancel(client):
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "60", None)])
    # 整单至少一条盘亏：对另一批次盘亏，MIX-1 仅零差额
    _raw(client, "RM-2", "10", key="key-rm2")
    r = _stocktake(
        client,
        "ST-1",
        [
            ("MIX-1", DEFAULT_LOCATION_CODE, "60", "60"),
            ("RM-2", DEFAULT_LOCATION_CODE, "10", "9"),
        ],
    )
    assert r.status_code == 201
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text
    assert _remaining(client, "RM-1") == Decimal("100")


def test_stocktake_on_source_batch_does_not_block_cancel(client):
    """盘亏发生在来源批次（而非产出）上，不阻止撤销产出生产单。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "60", None)])
    r = _stocktake(client, "ST-1", [("RM-1", DEFAULT_LOCATION_CODE, "40", "30")])
    assert r.status_code == 201
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 200, r.text
    # 返还原投料 60：30 + 60 = 90，不超过初始 100
    assert _remaining(client, "RM-1") == Decimal("90")


# ------------------------------------------------------------ 服务层直接用例


def test_service_stocktake_writes_both_layers(engine):
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        result = service.submit_stocktake(
            s,
            StocktakeIn(
                stocktake_no="ST-1",
                lines=[
                    StocktakeLineIn(
                        batch_no="RM-1",
                        location_code=DEFAULT_LOCATION_CODE,
                        book_qty=_q("100"),
                        actual_qty=_q("88.5"),
                    )
                ],
            ),
        )
    assert result["lines"][0]["shortage_qty"] == 11.5
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("88.5000")
        assert s.get(Stocktake, "ST-1").status == "POSTED"
        line = s.execute(
            StocktakeLine.__table__.select().where(
                StocktakeLine.stocktake_no == "ST-1"
            )
        ).mappings().one()
        assert line["book_qty"] == Decimal("100.0000")
        assert line["actual_qty"] == Decimal("88.5000")
        assert line["shortage_qty"] == Decimal("11.5000")
        assert line["unit"] == "kg"


# ------------------------------------------------------------ 并发


def _register(no="RM-1", qty="100"):
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no=no, unit="kg", quantity=_q(qty))
        )


def test_concurrent_stocktakes_never_overdeduct(engine):
    """6 个盘点单并发核销同一库位各 5（账面必须恰等于实存）：
    恰好第一个成功，其余以账面已变动拒绝；不会超扣、不留失败单。"""
    _register(qty="100")
    barrier = threading.Barrier(6)
    results: list[str] = []

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                # 每行都按“账面 100、实点 95”申报：只有首个提交者核对通过
                service.submit_stocktake(
                    s,
                    StocktakeIn(
                        stocktake_no=f"ST-{idx}",
                        lines=[
                            StocktakeLineIn(
                                batch_no="RM-1",
                                location_code=DEFAULT_LOCATION_CODE,
                                book_qty=_q("100"),
                                actual_qty=_q("95"),
                            )
                        ],
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert results.count("ok") == 1, results
    assert results.count("STOCKTAKE_BOOK_MISMATCH") == 5, results
    assert not [r for r in results if r.startswith("ERROR")], results
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("95.0000")
        orders = s.execute(Stocktake.__table__.select()).all()
        assert len(orders) == 1


def test_concurrent_stocktake_and_production_equivalent_to_serial(engine):
    """盘点 RM-1（账面 100、核销 10）与投料消耗 RM-1 20 并发：
    两种串行结局都自洽，绝不超扣、两层恒等。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                # 盘点按“账面 100、实点 90”申报
                service.submit_stocktake(
                    s,
                    StocktakeIn(
                        stocktake_no="ST-1",
                        lines=[
                            StocktakeLineIn(
                                batch_no="RM-1",
                                location_code=DEFAULT_LOCATION_CODE,
                                book_qty=_q("100"),
                                actual_qty=_q("90"),
                            )
                        ],
                    ),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=_q("20"),
                        inputs=[{"batch_no": "RM-1", "quantity": _q("20")}],  # type: ignore[list-item]
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, produce_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    with session_scope() as s:
        remaining = s.get(Batch, "RM-1").remaining_qty
        loc_sum = s.execute(
            select(func.sum(BatchLocation.qty)).where(BatchLocation.batch_no == "RM-1")
        ).scalar_one()
        fg1 = s.get(Batch, "FG-1")

    if results["stocktake"] == "ok":
        # 盘点先提交：投料后 90 - 20 = 70
        assert results["produce"] == "ok", results
        assert remaining == Decimal("70.0000")
        assert Decimal(loc_sum) == remaining
        assert fg1 is not None
    else:
        # 投料先提交：盘点账面核对失败（100 != 80）
        assert results["stocktake"] == "STOCKTAKE_BOOK_MISMATCH", results
        assert results["produce"] == "ok", results
        assert remaining == Decimal("80.0000")
        assert Decimal(loc_sum) == remaining


def test_concurrent_stocktake_and_transfer_equivalent_to_serial(engine):
    """盘点与调拨并发同一库位：要么盘点先（调拨随后成功），
    要么调拨先（盘点账面不符被拒）；余额恒为合法结局。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    StocktakeIn(
                        stocktake_no="ST-1",
                        lines=[
                            StocktakeLineIn(
                                batch_no="RM-1",
                                location_code=DEFAULT_LOCATION_CODE,
                                book_qty=_q("100"),
                                actual_qty=_q("90"),
                            )
                        ],
                    ),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def transfer_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(
                        transfer_no="T1",
                        batch_no="RM-1",
                        source_location=DEFAULT_LOCATION_CODE,
                        target_location="A",
                        quantity=_q("30"),
                    ),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, transfer_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["transfer"] == "ok", results
    with session_scope() as s:
        remaining = s.get(Batch, "RM-1").remaining_qty
        default_qty = Decimal(
            s.execute(
                BatchLocation.__table__.select()
                .where(
                    BatchLocation.batch_no == "RM-1",
                    BatchLocation.location_code == DEFAULT_LOCATION_CODE,
                )
            ).mappings().one()["qty"]
        )
        a_qty = Decimal(
            s.execute(
                BatchLocation.__table__.select()
                .where(BatchLocation.batch_no == "RM-1", BatchLocation.location_code == "A")
            ).mappings().one()["qty"]
        )
    # 调拨不改变全局余量；盘点核销 10 与否只影响结局 A
    if results["stocktake"] == "ok":
        # 盘点先：DEFAULT 100->90，再调拨 30：DEFAULT=60, A=30, 全局=90
        assert remaining == Decimal("90.0000")
        assert default_qty == Decimal("60") and a_qty == Decimal("30")
    else:
        # 调拨先：DEFAULT=70 != 账面 100，盘点拒绝；全局仍 100
        assert results["stocktake"] == "STOCKTAKE_BOOK_MISMATCH", results
        assert remaining == Decimal("100.0000")
        assert default_qty == Decimal("70") and a_qty == Decimal("30")


def test_concurrent_stocktake_and_cancel_equivalent_to_serial(engine):
    """盘点 MIX-1（盘亏）与撤销 MIX-1 并发：
    结局 A：盘点先 -> 撤销以 OUTPUT_ALREADY_STOCKTAKEN 拒绝，来源不返还；
    结局 B：撤销先 -> 盘点核对 MIX-1 默认库位（产出行作废但仍保留 60）成功。
    """
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=_q("60"),
                inputs=[{"batch_no": "RM-1", "quantity": _q("60")}],  # type: ignore[list-item]
            ),
        )

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    StocktakeIn(
                        stocktake_no="ST-1",
                        lines=[
                            StocktakeLineIn(
                                batch_no="MIX-1",
                                location_code=DEFAULT_LOCATION_CODE,
                                book_qty=_q("60"),
                                actual_qty=_q("50"),
                            )
                        ],
                    ),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def cancel_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.cancel_production(s, "MIX-1", CancelIn(cancel_reason="录入错误"))
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, cancel_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1").remaining_qty
        mix1 = s.get(Batch, "MIX-1").remaining_qty
        cancelled = service.get_production_record(s, "MIX-1").is_cancelled

    if results["cancel"] == "ok":
        # 结局 B：撤销先 -> 产出行作废但默认库位仍保留 60，盘点随后成功核销
        assert results["stocktake"] == "ok", results
        assert rm1 == Decimal("100.0000")
        assert mix1 == Decimal("50.0000")
        assert cancelled is True
    else:
        # 结局 A：盘点先 -> 撤销以 OUTPUT_ALREADY_STOCKTAKEN 拒绝，来源不返还
        assert results["cancel"] == "OUTPUT_ALREADY_STOCKTAKEN", results
        assert results["stocktake"] == "ok", results
        assert rm1 == Decimal("40.0000")      # 未返还
        assert mix1 == Decimal("50.0000")     # 盘点核销后
        assert cancelled is False


def test_concurrent_duplicate_stocktake_no_only_one_succeeds(engine):
    """两个相同盘点单号并发提交：恰好一笔成功，另一笔单号复用拒绝。"""
    _register(qty="100")

    def payload():
        return StocktakeIn(
            stocktake_no="ST-DUP",
            lines=[
                StocktakeLineIn(
                    batch_no="RM-1",
                    location_code=DEFAULT_LOCATION_CODE,
                    book_qty=_q("100"),
                    actual_qty=_q("95"),
                )
            ],
        )

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(s, payload())
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: worker(), range(2)))

    assert results.count("ok") == 1, results
    assert set(results) == {"ok", "STOCKTAKE_NO_REUSED"}, results
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("95.0000")


def test_stocktake_on_suspect_flagged_batch_concurrent_marker_still_quarantined(engine):
    """隔离批次可盘点：盘点不改变 is_suspect/is_quarantined 标志。"""
    _register(qty="100")
    with session_scope() as s:
        service.mark_suspect(s, SuspectIn(batch_no="RM-1"))
    with session_scope() as s:
        service.submit_stocktake(
            s,
            StocktakeIn(
                stocktake_no="ST-1",
                lines=[
                    StocktakeLineIn(
                        batch_no="RM-1",
                        location_code=DEFAULT_LOCATION_CODE,
                        book_qty=_q("100"),
                        actual_qty=_q("0"),
                    )
                ],
            ),
        )
    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        assert batch.remaining_qty == 0
        assert batch.is_suspect is True
        assert batch.is_quarantined is True
