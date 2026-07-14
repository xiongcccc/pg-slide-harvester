import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pgppt  # noqa: E402


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
            "Semi-Joins in Postgres planner optimizer",
        )
        self.assertEqual(pgppt.safe_filename_stem("PostgreSQL 18.0"), "PostgreSQL 18.0")

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

    def test_upsert_session_can_backfill_abstract(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
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
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
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
                self.assertEqual(row["local_path"], "archive/by_topic/uncategorized/Readable Talk Title.pdf")
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
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
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
                self.assertEqual(row["local_path"], "archive/by_topic/optimizer/Semi Joins in Postgres.pdf")
                self.assertTrue((pgppt.ROOT / row["local_path"]).exists())
        finally:
            pgppt.ROOT = original_root
            pgppt.request_url = original_request_url


if __name__ == "__main__":
    unittest.main()
