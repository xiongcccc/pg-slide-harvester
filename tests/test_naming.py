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


class NamingTests(unittest.TestCase):
    def test_safe_filename_stem_keeps_readable_title(self):
        self.assertEqual(
            pgppt.safe_filename_stem("Semi-Joins in Postgres: planner/optimizer?"),
            "Semi Joins in Postgres planner optimizer",
        )
        self.assertEqual(pgppt.safe_filename_stem("PostgreSQL 18.0"), "PostgreSQL 18.0")
        self.assertEqual(
            pgppt.safe_filename_stem("Update-on-index-prefetching.pdf"),
            "Update on index prefetching",
        )

    def test_asset_title_uses_page_title_for_single_asset(self):
        title = pgppt.asset_title_from_context(
            "Semi Joins in Postgres",
            "slides.pdf",
            "https://example.org/sjp.pdf",
            1,
        )
        self.assertEqual(title, "Semi Joins in Postgres")

    def test_asset_title_adds_specific_label_for_multiple_assets(self):
        title = pgppt.asset_title_from_context(
            "PostgreSQL Backup Patterns",
            "Demo Notes",
            "https://example.org/demo.pdf",
            2,
        )
        self.assertEqual(title, "PostgreSQL Backup Patterns - Demo Notes")

    def test_book_of_abstracts_is_not_treated_as_slide_asset(self):
        self.assertFalse(
            pgppt.is_probably_slide_asset(
                "https://indico.example.org/event/1/book-of-abstracts.pdf",
                "Book of abstracts",
            )
        )
        self.assertTrue(
            pgppt.is_probably_slide_asset(
                "https://indico.example.org/event/1/contributions/2/slides.pdf",
                "Slides",
            )
        )

    def test_upsert_session_can_backfill_abstract(self):
        conn = memory_conn(self)
        pgppt.init_db(conn)
        event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
        session_id = pgppt.upsert_session(conn, event_id, "Query Planning")
        same_session_id = pgppt.upsert_session(
            conn,
            event_id,
            "Query Planning",
            abstract="Planner statistics and optimizer costing.",
        )
        row = conn.execute("select abstract from sessions where id = ?", (session_id,)).fetchone()
        self.assertEqual(same_session_id, session_id)
        self.assertEqual(row["abstract"], "Planner statistics and optimizer costing.")

    def test_missing_existing_file_is_downloaded_again_with_readable_name(self):
        body = b"%PDF-1.4 test\n"
        original_root = pgppt.ROOT
        original_request_url = pgppt.request_url

        try:
            with tempfile.TemporaryDirectory() as tmp:
                pgppt.ROOT = Path(tmp)
                pgppt.request_url = lambda url: FakeResponse(body)
                conn = memory_conn(self)
                pgppt.init_db(conn)
                pgppt.ensure_tags(conn)
                event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
                session_id = pgppt.upsert_session(conn, event_id, "Readable Talk Title")
                conn.execute(
                    """
                    insert into assets(
                        session_id, file_url, local_path, file_type, sha256,
                        size_bytes, downloaded_at, created_at
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        "https://example.org/short.pdf",
                        "archive/by_event/PGConf.dev-2026/old-short.pdf",
                        "pdf",
                        "oldsha",
                        1,
                        pgppt.utcnow(),
                        pgppt.utcnow(),
                    ),
                )
                conn.commit()

                ok, msg = pgppt.download_asset(
                    conn,
                    session_id,
                    "https://example.org/short.pdf",
                    "PGConf.dev 2026",
                    "Readable Talk Title",
                )

                self.assertTrue(ok, msg)
                row = conn.execute("select local_path from assets").fetchone()
                self.assertEqual(row["local_path"], "archive/uncategorized/Readable Talk Title.pdf")
                self.assertTrue((pgppt.ROOT / row["local_path"]).exists())
        finally:
            pgppt.ROOT = original_root
            pgppt.request_url = original_request_url

    def test_download_uses_abstract_based_topic_directory(self):
        body = b"%PDF-1.4 optimizer test\n"
        original_root = pgppt.ROOT
        original_request_url = pgppt.request_url

        try:
            with tempfile.TemporaryDirectory() as tmp:
                pgppt.ROOT = Path(tmp)
                pgppt.request_url = lambda url: FakeResponse(body)
                conn = memory_conn(self)
                pgppt.init_db(conn)
                pgppt.ensure_tags(conn)
                event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
                session_id = pgppt.upsert_session(
                    conn,
                    event_id,
                    "Semi Joins in Postgres",
                    abstract="This talk explains planner selectivity, optimizer costing, and join order.",
                )

                ok, msg = pgppt.download_asset(
                    conn,
                    session_id,
                    "https://example.org/sjp.pdf",
                    "PGConf.dev 2026",
                    "Semi Joins in Postgres",
                )

                self.assertTrue(ok, msg)
                row = conn.execute("select local_path from assets").fetchone()
                self.assertEqual(row["local_path"], "archive/optimizer/Semi Joins in Postgres.pdf")
                self.assertTrue((pgppt.ROOT / row["local_path"]).exists())
        finally:
            pgppt.ROOT = original_root
            pgppt.request_url = original_request_url

    def test_organize_archive_flattens_topic_directory_and_normalizes_filename(self):
        original_root = pgppt.ROOT

        try:
            with tempfile.TemporaryDirectory() as tmp:
                pgppt.ROOT = Path(tmp)
                old_path = pgppt.ROOT / "archive/by_topic/optimizer/Update-on-index-prefetching.pdf"
                old_path.parent.mkdir(parents=True)
                old_path.write_bytes(b"%PDF-1.4 old path\n")

                conn = memory_conn(self)
                pgppt.init_db(conn)
                pgppt.ensure_tags(conn)
                event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
                session_id = pgppt.upsert_session(
                    conn,
                    event_id,
                    "Update-on-index-prefetching",
                    abstract="This talk covers planner statistics and optimizer prefetching.",
                )
                conn.execute(
                    """
                    insert into assets(
                        session_id, file_url, local_path, file_type, sha256,
                        size_bytes, downloaded_at, created_at
                    )
                    values(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        "https://example.org/Update-on-index-prefetching.pdf",
                        "archive/by_topic/optimizer/Update-on-index-prefetching.pdf",
                        "pdf",
                        "oldsha",
                        old_path.stat().st_size,
                        pgppt.utcnow(),
                        pgppt.utcnow(),
                    ),
                )
                conn.commit()

                messages = pgppt.organize_archive_by_topic(conn)

                row = conn.execute("select local_path from assets").fetchone()
                self.assertEqual(row["local_path"], "archive/optimizer/Update on index prefetching.pdf")
                self.assertTrue((pgppt.ROOT / row["local_path"]).exists(), messages)
                self.assertFalse(old_path.exists())
        finally:
            pgppt.ROOT = original_root

    def test_run_report_records_assets_for_one_download_run(self):
        body = b"%PDF-1.4 run report\n"
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
                run_id = pgppt.begin_run(conn, 'download-event "PGConf.dev 2026"')
                pgppt.ACTIVE_RUN_ID = run_id
                event_id = pgppt.upsert_event(conn, "PGConf.dev 2026")
                session_id = pgppt.upsert_session(
                    conn,
                    event_id,
                    "Update-on-index-prefetching",
                    abstract="This talk covers planner statistics and optimizer prefetching.",
                )

                ok, msg = pgppt.download_asset(
                    conn,
                    session_id,
                    "https://example.org/Update-on-index-prefetching.pdf",
                    "PGConf.dev 2026",
                    "Update-on-index-prefetching",
                )
                pgppt.finish_run(conn, run_id, "ok", msg)
                html_path, csv_path, count = pgppt.run_report(conn, run_id)

                self.assertTrue(ok, msg)
                self.assertEqual(count, 1)
                self.assertTrue(html_path.exists())
                self.assertTrue(csv_path.exists())
                self.assertIn("reports/runs/", str(html_path))
                csv_text = csv_path.read_text(encoding="utf-8")
                self.assertIn("downloaded", csv_text)
                self.assertIn("archive/optimizer/Update on index prefetching.pdf", csv_text)
        finally:
            pgppt.ROOT = original_root
            pgppt.request_url = original_request_url
            pgppt.ACTIVE_RUN_ID = original_active_run_id


if __name__ == "__main__":
    unittest.main()
