import os
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite

import models


def q(shares: int | str) -> int:
    return models.quantity_to_units(shares)


class DatabaseInitTests(unittest.IsolatedAsyncioTestCase):
    def test_float_quantity_is_rejected_by_model_boundary(self) -> None:
        with self.assertRaises(TypeError):
            models.quantity_to_units(0.01)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            models.validate_qty_units(1.0)  # type: ignore[arg-type]

    async def test_init_creates_missing_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "nested", "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()

            self.assertTrue(os.path.isfile(db_path))
            async with aiosqlite.connect(db_path) as db:
                row = await (
                    await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
                    )
                ).fetchone()
            self.assertEqual(row, ("trades",))

    async def test_trade_ledger_can_represent_a_negative_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 10.0, q(100), "CNY", 1.0
                )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 8.0, q(40), "CNY", 1.0, 1
                )
                summary = await models.get_user_summary(1)

            self.assertEqual(summary, {"600000.A": -q(60)})

    async def test_fresh_database_stores_integer_hundredth_share_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                whole_trade = await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 10.0, q(1), "CNY", 1.0
                )
                fractional_trade = await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 10.0, q("0.01"), "CNY", 1.0, 1
                )
                summary = await models.get_user_summary(1)

            async with aiosqlite.connect(db_path) as db:
                rows = await (
                    await db.execute("SELECT qty, typeof(qty) FROM trades ORDER BY id")
                ).fetchall()
                version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
                with self.assertRaises(aiosqlite.IntegrityError):
                    await db.execute("UPDATE trades SET qty = 0.5 WHERE id = 1")
                with self.assertRaises(aiosqlite.IntegrityError):
                    await db.execute(
                        "UPDATE trades SET market = 'INVALID' WHERE id = 1"
                    )

            self.assertEqual(rows, [(100, "integer"), (1, "integer")])
            self.assertEqual(summary, {"600000.A": 101})
            self.assertIs(type(whole_trade.qty), int)
            self.assertIs(type(fractional_trade.qty), int)
            self.assertIs(type(summary["600000.A"]), int)
            self.assertEqual(version, models.DB_SCHEMA_VERSION)

    async def test_update_backfills_legacy_rows_once_and_preserves_visible_qty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """CREATE TABLE trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        username TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                        price REAL NOT NULL,
                        qty INTEGER NOT NULL,
                        currency TEXT NOT NULL DEFAULT 'CNY',
                        rate REAL NOT NULL DEFAULT 1.0,
                        trade_ts TEXT NOT NULL
                    )"""
                )
                await db.execute(
                    "INSERT INTO trades "
                    "(user_id, username, symbol, side, price, qty, currency, rate, trade_ts) "
                    "VALUES (1, 'tester', '600000', 'BUY', 10, 300, 'CNY', 1, '2026-01-01')"
                )
                await db.commit()

            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                # New code remains safe during the deployment window before
                # the operator invokes /update on this legacy database.
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 11.0, q("0.25"), "CNY", 1.0, 1
                )
                before = await models.get_user_summary(1)
                first = await models.update_database()
                after = await models.get_user_summary(1)
                second = await models.update_database()

            async with aiosqlite.connect(db_path) as db:
                raw_rows = await (
                    await db.execute(
                        "SELECT qty, leverage, market FROM trades ORDER BY id"
                    )
                ).fetchall()
                version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]

            self.assertEqual(before, {"600000.A": 30025})
            self.assertTrue(first.updated)
            self.assertEqual(first.rows_updated, 2)
            self.assertEqual(
                raw_rows,
                [(30000, 1.0, "A"), (25, 1.0, "A")],
            )
            self.assertEqual(after, {"600000.A": 30025})
            self.assertFalse(second.updated)
            self.assertEqual(version, 1)
            self.assertEqual(models.DB_SCHEMA_VERSION, 1)

    async def test_init_backfills_market_in_prerelease_v1_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """CREATE TABLE trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        username TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                        price REAL NOT NULL,
                        qty INTEGER NOT NULL,
                        leverage REAL NOT NULL DEFAULT 1.0,
                        currency TEXT NOT NULL DEFAULT 'CNY',
                        rate REAL NOT NULL DEFAULT 1.0,
                        trade_ts TEXT NOT NULL
                    )"""
                )
                await db.execute(
                    "INSERT INTO trades "
                    "(user_id, username, symbol, side, price, qty, trade_ts) "
                    "VALUES (1, 'tester', '002714', 'SELL', 45, 20000, "
                    "'2026-09-02')"
                )
                await db.execute("PRAGMA user_version = 1")
                await db.commit()

            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                entries = await models.get_position_entries(1)

            async with aiosqlite.connect(db_path) as db:
                market = await (
                    await db.execute("SELECT market FROM trades")
                ).fetchone()

            self.assertEqual(market, ("A",))
            self.assertEqual(
                entries,
                [models.PositionEntry("002714", 1.0, -20000, "A")],
            )

    async def test_fractional_short_and_close_remain_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 10.0, q("1.25"), "CNY", 1.0
                )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 8.0, q("0.5"), "CNY", 1.0, 1
                )
                before = await models.get_user_summary(1)
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", None, 8.0, "CNY", 1.0
                )
                after = await models.get_user_summary(1)

            async with aiosqlite.connect(db_path) as db:
                raw_qty = [
                    row[0]
                    for row in await (
                        await db.execute("SELECT qty FROM trades ORDER BY id")
                    ).fetchall()
                ]

            self.assertEqual(before, {"600000.A": -75})
            self.assertEqual(len(closing_trades), 1)
            self.assertEqual(closing_trades[0].qty, 75)
            self.assertEqual(raw_qty, [125, 50, 75])
            self.assertEqual(after, {})

    async def test_matching_leverage_bucket_survives_reversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                opened = await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 10.0, q(100), "CNY", 1.0, 5
                )
                added = await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 11.0, q(50), "CNY", 1.0, 5
                )
                reversed_trade = await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 12.0, q(200), "CNY", 1.0, 5
                )
                summary = await models.get_user_summary(1)

            self.assertEqual(opened.leverage, 5.0)
            self.assertEqual(added.leverage, 5.0)
            self.assertEqual(reversed_trade.leverage, 5.0)
            self.assertEqual(summary, {"600000.A": -q(50)})

    async def test_wrong_leverage_cannot_close_another_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY, 10.0, q(100), "CNY", 1.0, 5
                )
                with self.assertRaises(models.LeverageMismatchError):
                    await models.insert_trade(
                        1, "tester", "600000", models.Side.SELL,
                        12.0, q(200), "CNY", 1.0, 2,
                    )
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", None, 12.0, "CNY", 1.0
                )
                reopened = await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 12.0, q(50), "CNY", 1.0, 2
                )

            self.assertEqual(reopened.leverage, 2.0)
            self.assertEqual(len(closing_trades), 1)

    async def test_opposite_trade_requires_explicit_matching_leverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL,
                    10.0, q(100), "CNY", 1.0,
                )
                with self.assertRaises(models.LeverageMismatchError):
                    await models.insert_trade(
                        1, "tester", "600000", models.Side.BUY,
                        8.0, q(40), "CNY", 1.0,
                    )
                covered = await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY,
                    8.0, q(40), "CNY", 1.0, 1,
                )
                entries = await models.get_position_entries(1, "600000")

            self.assertEqual(covered.leverage, 1.0)
            self.assertEqual(entries, [models.PositionEntry("600000", 1.0, -q(60))])

    async def test_same_direction_different_leverage_creates_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY,
                    10.0, q(100), "CNY", 1.0, 5,
                )
                with self.assertRaises(models.LeverageMismatchError):
                    await models.insert_trade(
                        1, "tester", "600000", models.Side.BUY,
                        11.0, q(50), "CNY", 1.0,
                    )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY,
                    11.0, q(50), "CNY", 1.0, 2,
                )
                entries = await models.get_position_entries(1, "600000")

            self.assertEqual(
                entries,
                [
                    models.PositionEntry("600000", 5.0, q(100)),
                    models.PositionEntry("600000", 2.0, q(50)),
                ],
            )

    async def test_all_buy_atomically_closes_a_short_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 10.0, q(100), "CNY", 1.0
                )
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", models.Side.BUY, 8.0, "CNY", 1.0
                )
                summary = await models.get_user_summary(1)

            self.assertEqual(len(closing_trades), 1)
            self.assertEqual(closing_trades[0].qty, q(100))
            self.assertEqual(summary, {})

    async def test_all_closes_every_matching_leverage_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL,
                    10.0, q(100), "CNY", 1.0, 2,
                )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL,
                    11.0, q(50), "CNY", 1.0, 5,
                )
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", models.Side.BUY, 8.0, "CNY", 1.0
                )
                entries = await models.get_position_entries(1, "600000")

            self.assertEqual(
                [(trade.leverage, trade.qty) for trade in closing_trades],
                [(2.0, q(100)), (5.0, q(50))],
            )
            self.assertEqual(entries, [])

    async def test_all_with_leverage_closes_only_that_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL,
                    10.0, q(100), "CNY", 1.0, 2,
                )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL,
                    11.0, q(50), "CNY", 1.0, 5,
                )
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", models.Side.BUY,
                    8.0, "CNY", 1.0, 5,
                )
                entries = await models.get_position_entries(1, "600000")

            self.assertEqual(
                [(trade.leverage, trade.qty) for trade in closing_trades],
                [(5.0, q(50))],
            )
            self.assertEqual(
                entries,
                [models.PositionEntry("600000", 2.0, -q(100))],
            )

    async def test_close_closes_mixed_direction_buckets_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY,
                    10.0, q(100), "CNY", 1.0, 5,
                )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY,
                    10.0, q(100), "CNY", 1.0, 2,
                )
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL,
                    12.0, q(150), "CNY", 1.0, 5,
                )
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", None, 11.0, "CNY", 1.0
                )
                entries = await models.get_position_entries(1, "600000")

            self.assertEqual(
                [(trade.leverage, trade.side, trade.qty) for trade in closing_trades],
                [
                    (2.0, models.Side.SELL, q(100)),
                    (5.0, models.Side.BUY, q(50)),
                ],
            )
            self.assertEqual(entries, [])

    async def test_all_rejects_a_trade_in_the_same_direction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 10.0, q(100), "CNY", 1.0
                )
                closing_trades = await models.insert_close_trades(
                    1, "tester", "600000", models.Side.SELL, 8.0, "CNY", 1.0
                )
                summary = await models.get_user_summary(1)

            self.assertEqual(closing_trades, [])
            self.assertEqual(summary, {"600000.A": -q(100)})

    async def test_close_automatically_chooses_side_for_long_and_short(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "600000", models.Side.SELL, 10.0, q(100), "CNY", 1.0
                )
                await models.insert_trade(
                    1, "tester", "00700", models.Side.BUY, 400.0, q(80), "HKD", 0.9
                )
                short_close = await models.insert_close_trades(
                    1, "tester", "600000", None, 8.0, "CNY", 1.0
                )
                long_close = await models.insert_close_trades(
                    1, "tester", "00700", None, 450.0, "HKD", 0.9
                )
                summary = await models.get_user_summary(1)

            self.assertEqual(short_close[0].side, models.Side.BUY)
            self.assertEqual(long_close[0].side, models.Side.SELL)
            self.assertEqual(summary, {})

    async def test_same_code_markets_are_stored_and_managed_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                stock = await models.insert_trade(
                    1, "tester", "002714", models.Side.BUY,
                    40.0, q(100), "CNY", 1.0, market="A",
                )
                fund = await models.insert_trade(
                    1, "tester", "002714", models.Side.BUY,
                    1.2, q(20), "CNY", 1.0, market="FUND",
                )
                entries = await models.get_position_entries(1, "002714")
                await models.insert_close_trades(
                    1, "tester", "002714", None, 42.0, "CNY", 1.0,
                    market="A",
                )
                remaining = await models.get_position_entries(1, "002714")

            async with aiosqlite.connect(db_path) as db:
                stored_markets = await (
                    await db.execute(
                        "SELECT market FROM trades ORDER BY id"
                    )
                ).fetchall()

            self.assertEqual(stock.market, "A")
            self.assertEqual(fund.market, "FUND")
            self.assertEqual(
                {(entry.market, entry.qty) for entry in entries},
                {("A", q(100)), ("FUND", q(20))},
            )
            self.assertEqual(
                remaining,
                [models.PositionEntry("002714", 1.0, q(20), "FUND")],
            )
            self.assertEqual(stored_markets, [("A",), ("FUND",), ("A",)])

    async def test_delete_trades_deletes_owned_ids_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                first = await models.insert_trade(
                    1, "tester", "600000", models.Side.BUY,
                    10.0, q(10), "CNY", 1.0,
                )
                second = await models.insert_trade(
                    1, "tester", "00700", models.Side.BUY,
                    400.0, q(10), "HKD", 0.9,
                )
                other_user = await models.insert_trade(
                    2, "other", "AAPL", models.Side.BUY,
                    200.0, q(1), "USD", 7.0,
                )

                deleted = await models.delete_trades(
                    [first.id, second.id, other_user.id, 999], 1
                )
                own_remaining = await models.get_trades(1)
                other_remaining = await models.get_trades(2)

            self.assertEqual(deleted, [first.id, second.id])
            self.assertEqual(own_remaining, [])
            self.assertEqual([trade.id for trade in other_remaining], [other_user.id])

    async def test_delete_symbol_leverage_targets_exact_market_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "trades.db")
            with patch.object(models.config, "DB_PATH", db_path):
                await models.init_db()
                await models.insert_trade(
                    1, "tester", "002714", models.Side.BUY,
                    40.0, q(10), "CNY", 1.0, 5, market="A",
                )
                await models.insert_trade(
                    1, "tester", "002714", models.Side.BUY,
                    41.0, q(5), "CNY", 1.0, 5, market="A",
                )
                await models.insert_trade(
                    1, "tester", "002714", models.Side.BUY,
                    39.0, q(3), "CNY", 1.0, 2, market="A",
                )
                await models.insert_trade(
                    1, "tester", "002714", models.Side.BUY,
                    1.2, q(20), "CNY", 1.0, 5, market="FUND",
                )

                deleted_count = await models.delete_trades_by_symbol_leverage(
                    1, "002714", 5
                )
                remaining = await models.get_trades(1)

            self.assertEqual(deleted_count, 2)
            self.assertEqual(
                {(trade.market, trade.leverage) for trade in remaining},
                {("A", 2.0), ("FUND", 5.0)},
            )


if __name__ == "__main__":
    unittest.main()
