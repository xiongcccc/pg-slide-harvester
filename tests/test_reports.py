import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pgppt  # noqa: E402


def memory_conn(test_case):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    test_case.addCleanup(conn.close)
    return conn


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/pdf"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class ReportTests(unittest.TestCase):
    def test_duplicate_content_records_second_source_url(self):
        body = b"%PDF-1.4 same content\n"
        original_root = pgppt.ROOT
        original_request_url = pgppt.request_url
        original_active_run_id = pgppt.ACTIVE_RUN_ID

        try:
            with tempfile.TemporaryDirectory() as tmp:
                pgppt.ROOT = Path(tmp)
                pgppt.request_url = lambda url: FakeResponse(body)
                conn = memory_conn(self)
                pgppt.init_db(conn)
                pgppt.ensure_tags(conn)
                run_id = pgppt.begin_run(conn, 'ingest duplicate sources')
                pgppt.ACTIVE_RUN_ID = run_id
                event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
                session_id = pgppt.upsert_session(
                    conn,
                    event_id,
                    "Duplicate Content",
                    abstract="Planner statistics and optimizer costing.",
                )

                ok1, msg1 = pgppt.download_asset(conn, session_id, "https://example.org/one.pdf", "PGConf.dev 2026", "Duplicate Content")
                ok2, msg2 = pgppt.download_asset(conn, session_id, "https://mirror.example.org/one.pdf", "PGConf.dev 2026", "Duplicate Content Mirror")
                pgppt.finish_run(conn, run_id, "ok", "\n".join([msg1, msg2]))
                _, csv_path, count = pgppt.run_report(conn, run_id)

                self.assertTrue(ok1, msg1)
                self.assertFalse(ok2, msg2)
                self.assertEqual(count, 2)
                source_count = conn.execute("select count(*) as count from asset_sources").fetchone()["count"]
                self.assertEqual(source_count, 2)
                csv_text = csv_path.read_text(encoding="utf-8")
                self.assertIn("duplicate_content", csv_text)
                self.assertIn("https://mirror.example.org/one.pdf", csv_text)
        finally:
            pgppt.ROOT = original_root
            pgppt.request_url = original_request_url
            pgppt.ACTIVE_RUN_ID = original_active_run_id

    def test_run_report_includes_missing_session_without_asset(self):
        original_root = pgppt.ROOT
        original_active_run_id = pgppt.ACTIVE_RUN_ID

        try:
            with tempfile.TemporaryDirectory() as tmp:
                pgppt.ROOT = Path(tmp)
                conn = memory_conn(self)
                pgppt.init_db(conn)
                pgppt.ensure_tags(conn)
                run_id = pgppt.begin_run(conn, 'tick --limit 1')
                pgppt.ACTIVE_RUN_ID = run_id
                event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
                session_id = pgppt.upsert_session(
                    conn,
                    event_id,
                    "Slides Not Published Yet",
                    session_url="https://example.org/session/1",
                    asset_status="missing",
                )

                pgppt.mark_session_checked(conn, session_id, "missing")
                pgppt.finish_run(conn, run_id, "ok", "missing")
                html_path, csv_path, count = pgppt.run_report(conn, run_id)

                self.assertEqual(count, 1)
                self.assertTrue(html_path.exists())
                csv_text = csv_path.read_text(encoding="utf-8")
                self.assertIn("missing", csv_text)
                self.assertIn("Slides Not Published Yet", csv_text)
                self.assertIn("https://example.org/session/1", csv_text)
        finally:
            pgppt.ROOT = original_root
            pgppt.ACTIVE_RUN_ID = original_active_run_id


if __name__ == "__main__":
    unittest.main()
