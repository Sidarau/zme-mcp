import unittest
from unittest.mock import patch

from zme_mcp.backend import (SEARCHABLE_TYPES, ZME, _decorate, _eligible,
                             coverage_gaps, diversify)


class MetadataNormalizationTests(unittest.TestCase):
    def test_video_is_an_explicitly_searchable_type(self):
        self.assertIn("video", SEARCHABLE_TYPES)

    def test_youtube_channel_metadata_becomes_source_alias(self):
        row = _decorate({}, {"metadata": {"channel_name": "Forward Guidance"}})
        self.assertIn("forward_guidance", row["_source_aliases"])

    def test_x_bundle_gets_date_and_source_from_canonical_url(self):
        row = _decorate({"source_url": "x://FinanceLancelot/2026-05-09"})
        self.assertEqual(row["published_at"], "2026-05-09T00:00:00+00:00")
        self.assertEqual(row["source_key"], "financelancelot")

    def test_ingestion_time_is_not_used_as_publication_time(self):
        row = _decorate({"created_at": "2026-08-16T00:00:00Z"})
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["ingested_at"], "2026-08-16T00:00:00+00:00")

    def test_unknown_publication_date_fails_closed(self):
        self.assertFalse(_eligible({}, "2026-07-17T00:00:00Z", None, None))

    def test_old_record_is_excluded_even_if_semantically_ranked(self):
        row = _decorate({"source_url": "x://FinanceLancelot/2026-05-09"})
        self.assertFalse(_eligible(row, "2026-07-17T00:00:00Z", "2026-08-16T23:59:59Z", None))

    def test_source_alias_is_enforced(self):
        row = _decorate({"source_url": "https://bravosresearch.com/story"})
        self.assertTrue(_eligible(row, None, None, ["bravosresearch"]))
        self.assertFalse(_eligible(row, None, None, ["reuters"]))


class BackendSearchTests(unittest.TestCase):
    def client(self):
        with patch.object(ZME, "__init__", lambda self: None):
            client = ZME()
        client.nv_key = None
        return client

    def test_filtered_search_fetches_metadata_and_never_backfills_old_rows(self):
        client = self.client()
        calls = []
        client.rpc = lambda name, args: calls.append((name, args)) or [
            {"chunk_id": "old", "source_id": "1", "rrf_score": 1.0},
            {"chunk_id": "new", "source_id": "2", "rrf_score": 0.5},
        ]
        client.get = lambda *_: [
            {"id": "1", "url": "x://FinanceLancelot/2026-05-09", "source_type": "social"},
            {"id": "2", "url": "https://reuters.com/story", "source_key": "reuters",
             "published_at": "2026-08-15T00:00:00Z", "source_type": "article"},
        ]
        result = client.search("markets", published_after="2026-07-17T00:00:00Z",
                               published_before="2026-08-16T23:59:59Z")
        self.assertEqual([row["chunk_id"] for row in result], ["new"])
        self.assertEqual(calls[0][0], "zme_search_filtered")
        self.assertEqual(calls[0][1]["p_published_after"], "2026-07-17T00:00:00Z")

    def test_explicit_video_search_hydrates_source_metadata(self):
        client = self.client()
        client.rpc = lambda *_: [{"chunk_id": "v", "source_id": "1"}]
        client.get = lambda *_: [{
            "id": "1", "source_type": "video",
            "metadata": {"channel_name": "Forward Guidance"},
        }]
        result = client.search("markets", k=1, source_types=["video"])
        self.assertEqual(result[0]["source_key"], "forward_guidance")

    def test_source_filter_rejects_unrelated_high_rank_result(self):
        client = self.client()
        client.rpc = lambda *_: [
            {"chunk_id": "local", "source_id": "1", "rrf_score": 1.0},
            {"chunk_id": "wanted", "source_id": "2", "rrf_score": 0.5},
        ]
        client.get = lambda *_: [
            {"id": "1", "url": "file:///tmp/family-law.md", "source_type": "note"},
            {"id": "2", "url": "https://reuters.com/story", "source_key": "reuters",
             "source_type": "article"},
        ]
        result = client.search("markets", source_keys=["reuters"])
        self.assertEqual([row["chunk_id"] for row in result], ["wanted"])

    def test_invalid_date_rejected_even_with_no_candidates(self):
        client = self.client()
        client.rpc = lambda *_: []
        with self.assertRaisesRegex(ValueError, "invalid published_after"):
            client.search("markets", published_after="last month-ish")


class InventoryTests(unittest.TestCase):
    def client(self, sources):
        with patch.object(ZME, "__init__", lambda self: None):
            client = ZME()
        client.rpc = lambda *_: (_ for _ in ()).throw(RuntimeError("rpc -> 404 missing"))
        client.get = lambda *_: sources
        return client

    def test_inventory_distinguishes_current_stale_unknown_and_absent(self):
        client = self.client([
            {"id": "1", "url": "https://reuters.com/a", "source_key": "reuters",
             "published_at": "2026-08-15T00:00:00Z", "created_at": "2026-08-15T01:00:00Z",
             "source_type": "article", "title": "Current"},
            {"id": "2", "url": "x://FinanceLancelot/2026-05-09", "source_type": "social",
             "created_at": "2026-08-15T01:00:00Z", "title": "Old"},
            {"id": "3", "url": "https://bravosresearch.com/undated", "source_type": "article",
             "created_at": "2026-08-15T01:00:00Z", "title": "Undated"},
        ])
        rows = client.inventory(
            source_keys=["reuters", "financelancelot", "bravosresearch", "macrovoices"],
            published_after="2026-07-17T00:00:00Z", as_of="2026-08-16T23:59:59Z")
        states = {row["source_key"]: row["state"] for row in rows}
        self.assertEqual(states, {
            "macrovoices": "not_present",
            "bravosresearch": "unknown_date",
            "financelancelot": "content_stale",
            "reuters": "content_current",
        })
        self.assertTrue(all(row["collector_health"] == "not_exposed" for row in rows))

    def test_inventory_never_counts_ingestion_time_as_publication(self):
        client = self.client([{
            "id": "1", "url": "https://example.com/a", "source_key": "example",
            "created_at": "2026-08-15T00:00:00Z", "source_type": "article",
        }])
        row = client.inventory(published_after="2026-07-17T00:00:00Z",
                               as_of="2026-08-16T23:59:59Z")[0]
        self.assertEqual(row["state"], "unknown_date")
        self.assertEqual(row["documents_in_window"], 0)

    def test_native_inventory_rpc_is_preferred_when_deployed(self):
        client = self.client([])
        expected = [{"source_key": "reuters", "state": "content_current"}]
        client.rpc = lambda *_: expected
        client.get = lambda *_: self.fail("legacy source scan should not run")
        self.assertEqual(client.inventory(as_of="2026-08-16T00:00:00Z"), expected)


class DiversityTests(unittest.TestCase):
    def test_canonical_publisher_cap_not_only_document_deduplication(self):
        rows = [
            {"source_id": str(index), "source_key": "bravos", "chunk_id": f"b{index}"}
            for index in range(10)
        ] + [{"source_id": "reuters-doc", "source_key": "reuters", "chunk_id": "r1"}]
        selected = diversify(rows, limit=8, per_source_cap=3)
        self.assertEqual(sum(row["source_key"] == "bravos" for row in selected), 3)
        self.assertIn("reuters", {row["source_key"] for row in selected})

    def test_duplicate_chunks_from_one_document_collapse(self):
        rows = [
            {"source_id": "same", "source_key": "reuters", "chunk_id": "a"},
            {"source_id": "same", "source_key": "reuters", "chunk_id": "b"},
        ]
        self.assertEqual([row["chunk_id"] for row in diversify(rows)], ["a"])


class CoverageGateTests(unittest.TestCase):
    def test_required_source_needs_current_full_text_and_healthy_collector(self):
        rows = [{
            "source_key": "forward_guidance", "display_name": "Forward Guidance",
            "required": True, "state": "content_current",
            "documents_in_window": 7, "full_text_in_window": 0,
            "collector_health": "outcome_not_exposed",
        }, {
            "source_key": "wealthion", "display_name": "Wealthion",
            "required": False, "state": "content_current",
            "documents_in_window": 10, "full_text_in_window": 2,
            "collector_health": "outcome_not_exposed",
        }]
        self.assertEqual(coverage_gaps(rows), [{
            "source_key": "forward_guidance",
            "display_name": "Forward Guidance",
            "reasons": ["no_searchable_full_text", "collector_outcome_not_exposed"],
        }])

    def test_complete_required_source_has_no_gap(self):
        self.assertEqual(coverage_gaps([{
            "source_key": "reuters", "required": True,
            "state": "content_current", "documents_in_window": 5,
            "full_text_in_window": 5, "collector_health": "healthy",
        }]), [])


if __name__ == "__main__":
    unittest.main()
