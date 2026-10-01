"""盘点并发：盘点 / 投料 / 分装 / 调拨 / 撤销交错时核对扣减原子化，
绝不超扣、不覆盖新余额，两层余额恒等。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import text

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import DEFAULT_LOCATION_CODE, Batch
from app.schemas import (
    ProductionIn,
    RawBatchIn,
    SplitIn,
    StocktakeIn,
    StocktakeLineIn,
    TransferIn,
)


def _q(v) -> Decimal:
    return Decimal(str(v))


def _register(no: str = "RM-1", qty: str = "100") -> None:
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no=no, unit="kg", quantity=_q(qty))
        )


def _stocktake_data(no: str, book: str, actual: str,
                    batch: str = "RM-1", loc: str = DEFAULT_LOCATION_CODE):
    return StocktakeIn(
        stocktake_no=no,
        items=[StocktakeLineIn(
            batch_no=batch, location_code=loc,
            book_quantity=_q(book), actual_quantity=_q(actual),
        )],
    )


def _totals(batch: str = "RM-1"):
    with session_scope() as s:
        global_qty = Decimal(s.get(Batch, batch).remaining_qty)
        loc_sum = Decimal(s.execute(
            text("SELECT COALESCE(SUM(qty), 0) FROM batch_locations WHERE batch_no=:b"),
            {"b": batch},
        ).scalar_one())
        return global_qty, loc_sum


def test_concurrent_stocktakes_same_location_one_ok_one_mismatch(engine):
    """两张盘点单并发核销同一库位（都按账面 100、实点 90）：
    先提交者成功扣 10，后提交者读到新账面 90 != 100 被整单拒绝。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(no: str) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(s, _stocktake_data(no, "100", "90"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ["CC-1", "CC-2"]))

    assert results.count("ok") == 1, results
    assert results.count("STOCKTAKE_BOOK_MISMATCH") == 1, results
    global_qty, loc_sum = _totals()
    assert global_qty == loc_sum == Decimal("90.0000")


def test_concurrent_stocktake_and_production_never_overuse(engine):
    """盘点核销 10 与投料消耗 95 并发（账面 100）：
    投料先 -> 账面变 5，盘点按 100 核对失败，最终 5；
    盘点先 -> 账面变 90，投料 95 不足失败，最终 90。两者绝不叠加成负余额。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(s, _stocktake_data("CC-1", "100", "90"))
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
                        output_batch_no="FG-1", unit="kg", output_quantity=_q("95"),
                        inputs=[{"batch_no": "RM-1", "quantity": _q("95"),
                                 "location_code": DEFAULT_LOCATION_CODE}],
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
    assert results["stocktake"] in ("ok", "STOCKTAKE_BOOK_MISMATCH"), results
    assert results["produce"] in ("ok", "INSUFFICIENT_BALANCE"), results
    # 不可能两笔都成功（10 + 95 > 100），恰一成一拒
    assert results["stocktake"] != results["produce"], results

    global_qty, loc_sum = _totals()
    assert global_qty == loc_sum
    assert global_qty >= 0
    if results["stocktake"] == "ok":
        # 盘点先：扣 10 到 90，投料 95 不足被拒
        assert results["produce"] == "INSUFFICIENT_BALANCE"
        assert global_qty == Decimal("90.0000")
    else:
        # 投料先：扣 95 到 5，盘点按旧账面 100 核对失败
        assert results["produce"] == "ok"
        assert global_qty == Decimal("5.0000")


def test_concurrent_stocktake_and_transfer_same_location(engine):
    """盘点核销与调拨并发竞争同一库位：任一先提交，另一个按新账面核对，
    不会超扣、两层恒等。调拨只移动分布不改全局；盘点改全局。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                # 按账面 100、实点 80 核销 20
                service.submit_stocktake(s, _stocktake_data("CC-1", "100", "80"))
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
                    TransferIn(transfer_no="T1", batch_no="RM-1",
                               source_location=DEFAULT_LOCATION_CODE,
                               target_location="A", quantity=_q("30")),
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
    assert results["stocktake"] in ("ok", "STOCKTAKE_BOOK_MISMATCH"), results

    global_qty, loc_sum = _totals()
    with session_scope() as s:
        rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no='RM-1'")
            ).all()
        }
    assert sum(rows.values()) == global_qty == loc_sum
    if results["stocktake"] == "ok":
        # 盘点先（DEFAULT 100->80）再调拨 30：DEFAULT=50,A=30，全局=80
        # 调拨先（DEFAULT 100->70,A=30）盘点按旧账面 100 核对失败：
        #   DEFAULT=70,A=30，全局=100
        assert global_qty in (Decimal("80.0000"), Decimal("100.0000")), rows
        assert rows["A"] == Decimal("30.0000"), rows


def test_concurrent_stocktakes_different_batches_both_succeed(engine):
    """两张盘点单分别盘点不同批次：行锁不互斥，两笔都成功。"""
    _register("RM-1", "100")
    _register("RM-2", "50")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(no: str, batch: str, book: str, actual: str) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(s, _stocktake_data(no, book, actual, batch=batch))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda f: f(),
            [lambda: worker("CC-1", "RM-1", "100", "90"),
             lambda: worker("CC-2", "RM-2", "50", "40")],
        ))

    assert results == ["ok", "ok"], results
    assert _totals("RM-1")[0] == Decimal("90.0000")
    assert _totals("RM-2")[0] == Decimal("40.0000")


def test_concurrent_duplicate_stocktake_no_only_one_succeeds(engine):
    """同一盘点单号并发提交：唯一约束兜底，恰好一笔成功。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(actual: str) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(s, _stocktake_data("CC-DUP", "100", actual))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ["90", "80"]))

    assert results.count("ok") == 1, results
    # 败者可能先撞到单号唯一约束，也可能在唯一约束前读到胜者新账面而账面不符
    assert results.count("STOCKTAKE_NO_REUSED") + results.count(
        "STOCKTAKE_BOOK_MISMATCH") == 1, results
    global_qty, loc_sum = _totals()
    # 两笔内容不同（亏 10 或亏 20），胜者恰核销一笔，败者整单回滚：
    # 最终全局余量等于胜者核销后的 90 或 80，绝不会扣两次。
    assert global_qty == loc_sum
    assert global_qty in (Decimal("90.0000"), Decimal("80.0000")), results


def test_concurrent_stocktake_and_cancel_output(engine):
    """盘点产出批次 FG-1 与撤销其生产单并发：
    盘点先成功 -> 撤销被 OUTPUT_HAS_STOCKTAKE_LOSS 拒绝；
    撤销先成功 -> 盘点按 FG-1 默认库位旧账面核对（撤销不动产出余额）仍成功。"""
    _register(qty="100")
    with session_scope() as s:
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="FG-1", unit="kg", output_quantity=_q("40"),
                inputs=[{"batch_no": "RM-1", "quantity": _q("40")}],
            ),
        )
    # 盘点前：RM-1=60, FG-1=40

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    StocktakeIn(
                        stocktake_no="CC-1",
                        items=[StocktakeLineIn(
                            batch_no="FG-1", location_code=DEFAULT_LOCATION_CODE,
                            book_quantity=_q("40"), actual_quantity=_q("35"),
                        )],
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
            from app.schemas import CancelIn

            with session_scope() as s:
                service.cancel_production(s, "FG-1", CancelIn(cancel_reason="录入错误"))
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, cancel_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["stocktake"] == "ok", results  # 撤销不动 FG-1 余额，盘点恒成功
    assert results["cancel"] in ("ok", "OUTPUT_HAS_STOCKTAKE_LOSS"), results

    rm_global, rm_loc = _totals("RM-1")
    fg_global, fg_loc = _totals("FG-1")
    if results["cancel"] == "ok":
        # 撤销先：RM-1 收回 40 -> 100；FG-1 随后盘亏 5 -> 35（已撤销产出仍核销）
        assert rm_global == rm_loc == Decimal("100.0000"), results
        assert fg_global == fg_loc == Decimal("35.0000"), results
    else:
        # 盘点先：撤销被拒，RM-1=60，FG-1=35
        assert rm_global == rm_loc == Decimal("60.0000"), results
        assert fg_global == fg_loc == Decimal("35.0000"), results


def test_concurrent_stocktake_and_split(engine):
    """盘点核销 20 与分装 15 并发（RM-1 默认库位 100，未指定库位）：
    一成一拒或两成（15+20<=100 时可能两成），任何结局不超扣、两层恒等。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                # 按提交时刻的账面核对：分装先提交则账面变 85，本单按 100 核对失败
                service.submit_stocktake(s, _stocktake_data("CC-1", "100", "80"))
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def split_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.split_batch(
                    s,
                    "RM-1",
                    SplitIn(splits=[{"batch_no": "RM-1-S", "quantity": _q("15")}]),
                )
            results["split"] = "ok"
        except AppError as exc:
            results["split"] = exc.code
        except Exception as exc:
            results["split"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, split_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["split"] == "ok", results
    assert results["stocktake"] in ("ok", "STOCKTAKE_BOOK_MISMATCH"), results
    global_qty, loc_sum = _totals("RM-1")
    # 分装：父批次 -15 并新建子批次（默认库位 15）；盘点：-20
    child_global, child_loc = _totals("RM-1-S")
    assert child_global == child_loc == Decimal("15.0000"), results
    if results["stocktake"] == "ok":
        # 盘点先（100->80），分装再扣 15：父=65
        assert global_qty == loc_sum == Decimal("65.0000"), results
    else:
        # 分装先（100->85），盘点按旧账面 100 核对失败：父=85
        assert global_qty == loc_sum == Decimal("85.0000"), results
