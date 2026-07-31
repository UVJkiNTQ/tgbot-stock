import os
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite

import models


class DatabaseInitTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
