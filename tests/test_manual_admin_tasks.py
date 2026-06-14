import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main


class ManualAdminTaskTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = main.DB_PATH
        self.original_run_task_sequence = main.run_task_sequence
        self.tmpdir = tempfile.TemporaryDirectory()
        main.DB_PATH = str(Path(self.tmpdir.name) / "data.db")
        main.init_db()

        async def fake_run_task_sequence(*args, **kwargs):
            return None

        main.run_task_sequence = fake_run_task_sequence
        self.client = TestClient(main.app)

    def tearDown(self):
        main.run_task_sequence = self.original_run_task_sequence
        main.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    def test_parse_single_concurrency(self):
        self.assertEqual(main.parse_concurrency_setting("30", stepped=False), [30])

    def test_parse_custom_step_concurrency(self):
        self.assertEqual(main.parse_concurrency_setting("10, 20,50", stepped=True), [10, 20, 50])

    def test_single_value_step_uses_default_levels_up_to_target(self):
        self.assertEqual(main.parse_concurrency_setting("100", stepped=True), [20, 50, 100])

    def test_single_value_step_includes_non_default_target(self):
        self.assertEqual(main.parse_concurrency_setting("80", stepped=True), [20, 50, 80])

    def test_rejects_empty_concurrency_setting(self):
        with self.assertRaises(ValueError):
            main.parse_concurrency_setting("", stepped=False)

    def test_create_manual_test_records_marks_source_and_mode(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        main.init_db(conn)

        supplier_id, test_ids = main.create_manual_test_records(
            conn,
            name="管理员创建",
            phone="",
            url="https://api.example.com",
            api_key="sk-test",
            model="gpt-image-2",
            custom_url=False,
            concurrency_levels=[20, 50],
            mode="step",
        )

        supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        tasks = conn.execute(
            "SELECT * FROM stress_tests WHERE supplier_id=? ORDER BY concurrency",
            (supplier_id,),
        ).fetchall()

        self.assertEqual(supplier["source"], "admin")
        self.assertEqual(supplier["test_mode"], "step")
        self.assertEqual(test_ids, [tasks[0]["id"], tasks[1]["id"]])
        self.assertEqual([task["concurrency"] for task in tasks], [20, 50])
        self.assertTrue(all(task["source"] == "admin" for task in tasks))
        self.assertTrue(all(task["test_mode"] == "step" for task in tasks))

    def test_admin_create_task_requires_login(self):
        resp = self.client.post("/api/admin/tasks", json={
            "name": "管理员任务",
            "url": "https://api.example.com",
            "api_key": "sk-test",
            "concurrency": "20",
            "stepped": False,
        })

        self.assertEqual(resp.status_code, 401)

    def test_admin_create_task_creates_manual_single_task(self):
        self.client.cookies.set(main.ADMIN_COOKIE_NAME, main.ADMIN_COOKIE_VALUE)

        resp = self.client.post("/api/admin/tasks", json={
            "name": "管理员任务",
            "phone": "18800000000",
            "url": "https://api.example.com",
            "api_key": "sk-test",
            "model": "gpt-image-2",
            "custom_url": False,
            "concurrency": "80",
            "stepped": False,
        })

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["source"], "admin")
        self.assertEqual(data["mode"], "single")
        self.assertEqual(data["concurrency_levels"], [80])

        conn = main.get_db()
        supplier = conn.execute(
            "SELECT source, test_mode FROM suppliers WHERE id=?",
            (data["supplier_id"],),
        ).fetchone()
        tasks = conn.execute(
            "SELECT concurrency, source, test_mode FROM stress_tests WHERE supplier_id=?",
            (data["supplier_id"],),
        ).fetchall()
        conn.close()

        self.assertEqual(supplier["source"], "admin")
        self.assertEqual(supplier["test_mode"], "single")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["concurrency"], 80)
        self.assertEqual(tasks[0]["source"], "admin")
        self.assertEqual(tasks[0]["test_mode"], "single")


if __name__ == "__main__":
    unittest.main()
