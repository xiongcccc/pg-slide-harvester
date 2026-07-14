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


class AdapterTests(unittest.TestCase):
    def test_eventyay_event_url_can_be_discovered_from_sponsorship_page(self):
        original_request_url = pgppt.request_url
        try:
            def fake_request(url):
                if url == "https://summit.fossasia.org/":
                    return FakeResponse(
                        b'<meta http-equiv="refresh" content="0; url=https://eventyay.com/e/88882f3e" />',
                        "text/html",
                    )
                return FakeResponse(b"", "text/html")

            pgppt.request_url = fake_request

            self.assertEqual(
                pgppt.eventyay_event_url("https://summit.fossasia.org/pgday-sponsorship"),
                "https://eventyay.com/ev/88882f3e/",
            )
        finally:
            pgppt.request_url = original_request_url

    def test_discover_eventyay_sessions_filters_pgday_track(self):
        original_request_url = pgppt.request_url
        try:
            payload = {
                "tracks": [
                    {"id": 562, "name": {"en": "PGDay"}},
                    {"id": 560, "name": {"en": "AI"}},
                ],
                "talks": [
                    {
                        "id": 1,
                        "code": "PG1",
                        "title": "PostgreSQL Backup Patterns",
                        "abstract": "<p>Backup and recovery.</p>",
                        "track": 562,
                    },
                    {
                        "id": 2,
                        "code": "AI1",
                        "title": "General AI Talk",
                        "abstract": "Not PG.",
                        "track": 560,
                    },
                ],
            }
            html = (
                '<script id="pretalx-schedule-data" type="application/json">'
                + __import__("json").dumps(payload)
                + "</script>"
            ).encode()
            pgppt.request_url = lambda url: FakeResponse(html, "text/html")

            sessions = pgppt.discover_eventyay_sessions("https://eventyay.com/ev/88882f3e/")

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["title"], "PostgreSQL Backup Patterns")
            self.assertEqual(sessions[0]["session_url"], "https://eventyay.com/ev/88882f3e/talk/PG1/")
        finally:
            pgppt.request_url = original_request_url

    def test_discover_wordpress_schedule_sessions_from_elementor_widgets(self):
        page = {
            "slug": "schedule",
            "title": "Schedule",
            "link": "https://2026.pghyd.in/schedule/",
            "content": """
                <div data-widget_type="heading.default"><h4>10:00 to 10:30 AM</h4></div>
                <div data-widget_type="text-editor.default"><p>Logical replication theory and concepts</p></div>
                <div data-widget_type="text-editor.default"><p>Tea Break</p></div>
                <div data-widget_type="html.default">Pinning the Plan That Works: pg_plan_advice in PostgreSQL 19</div>
            """,
        }

        sessions = pgppt.discover_wordpress_schedule_sessions(page)

        self.assertEqual([session["title"] for session in sessions], [
            "Logical replication theory and concepts",
            "Pinning the Plan That Works: pg_plan_advice in PostgreSQL 19",
        ])

    def test_postgresql_eu_schedule_url_can_be_discovered_from_event_site(self):
        original_request_url = pgppt.request_url
        try:
            html = b"""
            <html><body>
              <a href="https://www.postgresql.eu/events/schedule/fosdem2026/">Schedule</a>
            </body></html>
            """
            pgppt.request_url = lambda url: FakeResponse(html, "text/html")

            self.assertEqual(
                pgppt.postgresql_eu_schedule_url("https://2026.fosdempgday.org"),
                "https://www.postgresql.eu/events/fosdem2026/schedule/",
            )
        finally:
            pgppt.request_url = original_request_url

    def test_discover_postgresql_eu_sessions(self):
        original_request_url = pgppt.request_url
        try:
            html = b"""
            <html><body>
              <a href="session/7370-zero-downtime-upgrades-postgresql-and-osglibc-at-global-scale/">
                Zero-Downtime Upgrades: PostgreSQL and OS/glibc at Global Scale
              </a>
              <a href="../../speaker/459-alexander-sosna/">Speaker</a>
            </body></html>
            """
            pgppt.request_url = lambda url: FakeResponse(html, "text/html")

            sessions = pgppt.discover_postgresql_eu_sessions("https://www.postgresql.eu/events/fosdem2026/schedule/")

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0][0], "https://www.postgresql.eu/events/fosdem2026/schedule/session/7370-zero-downtime-upgrades-postgresql-and-osglibc-at-global-scale/")
            self.assertEqual(sessions[0][1], "Zero-Downtime Upgrades: PostgreSQL and OS/glibc at Global Scale")
        finally:
            pgppt.request_url = original_request_url


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

    def test_duplicate_content_across_sessions_does_not_mark_downloaded_without_asset(self):
        body = b"%PDF-1.4 shared deck\n"
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
                run_id = pgppt.begin_run(conn, 'crawl duplicate content')
                pgppt.ACTIVE_RUN_ID = run_id
                event_id = pgppt.upsert_event(conn, "CERN PGDay 2026")
                first_session_id = pgppt.upsert_session(conn, event_id, "Call for Abstracts")
                second_session_id = pgppt.upsert_session(conn, event_id, "Registration")

                ok1, msg1 = pgppt.download_asset(conn, first_session_id, "https://example.org/book.pdf", "CERN PGDay 2026", "Call for Abstracts")
                ok2, msg2 = pgppt.download_asset(conn, second_session_id, "https://example.org/registration-book.pdf", "CERN PGDay 2026", "Registration")
                pgppt.finish_run(conn, run_id, "ok", "\n".join([msg1, msg2]))

                second_session = conn.execute("select asset_status from sessions where id = ?", (second_session_id,)).fetchone()
                second_assets = conn.execute("select count(*) as count from assets where session_id = ?", (second_session_id,)).fetchone()
                _, csv_path, count = pgppt.run_report(conn, run_id)

                self.assertTrue(ok1, msg1)
                self.assertFalse(ok2, msg2)
                self.assertEqual(second_session["asset_status"], "duplicate_content")
                self.assertEqual(second_assets["count"], 0)
                self.assertEqual(count, 2)
                self.assertIn("duplicate_content", csv_path.read_text(encoding="utf-8"))
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
