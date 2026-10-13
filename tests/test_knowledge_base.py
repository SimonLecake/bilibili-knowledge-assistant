import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import bilibili_kb


def fake_embeddings(texts):
    vectors = []
    for text in texts:
        vector = np.array([1.0, float(len(text) % 11), float(sum(map(ord, text)) % 13)], dtype=np.float32)
        vectors.append(vector / np.linalg.norm(vector))
    return np.asarray(vectors, dtype=np.float32)


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = patch.object(bilibili_kb, "DASHSCOPE_API_KEY", "offline-test")
        self.key.start()
        self.addCleanup(self.key.stop)
        self.embedding = patch.object(bilibili_kb, "get_embeddings", side_effect=fake_embeddings)
        self.embedding.start()
        self.addCleanup(self.embedding.stop)
        self.kb = bilibili_kb.KnowledgeBase(self.temp.name)

    def test_reingestion_replaces_source_and_preserves_other_vectors(self):
        self.kb.ingest("demo://a", "A", "old text", "")
        self.kb.ingest("demo://b", "B", "retained text", "")
        expected = {c["id"]: self.kb.index.reconstruct(i).copy()
                    for i, c in enumerate(self.kb.chunks) if c["video_url"] == "demo://b"}
        self.kb.ingest("demo://a", "A", "updated text", "")
        self.assertEqual(len(self.kb.chunks), 4)
        self.assertFalse(any(c["text"] == "old text" for c in self.kb.chunks))
        reloaded = bilibili_kb.KnowledgeBase(self.temp.name)
        self.assertEqual(reloaded.index.ntotal, len(reloaded.chunks))
        self.assertEqual(reloaded.get_stats()["documents"], 2)
        for i, c in enumerate(reloaded.chunks):
            self.assertEqual(c["index"], i)
            if c["id"] in expected:
                np.testing.assert_array_equal(reloaded.index.reconstruct(i), expected[c["id"]])

    def test_tracking_parameters_do_not_create_duplicate_documents(self):
        self.kb.ingest("https://www.bilibili.com/video/BV1Example?spm_id_from=test", "A", "first", "")
        self.kb.ingest("https://www.bilibili.com/video/BV1Example/?vd_source=test", "A", "second", "")
        self.assertEqual(self.kb.get_stats(), {"documents": 1, "chunks": 2, "kb_dir": self.temp.name})

    def test_multipart_identity_is_preserved(self):
        canonical = self.kb._canonical_source
        self.assertNotEqual(canonical("https://www.bilibili.com/video/BV1Example?p=2"),
                            canonical("https://www.bilibili.com/video/BV1Example?p=3"))

    def test_embedding_failure_leaves_persisted_corpus_unchanged(self):
        self.kb.ingest("demo://a", "A", "original", "")
        before = self.kb.chunks_path.read_bytes()
        with patch.object(bilibili_kb, "get_embeddings", side_effect=RuntimeError("offline failure")):
            with self.assertRaises(RuntimeError):
                self.kb.ingest("demo://a", "A", "replacement", "")
        self.assertEqual(self.kb.chunks_path.read_bytes(), before)
        self.assertEqual(self.kb.index.ntotal, 2)

    def test_query_preserves_positions_of_empty_questions(self):
        self.kb.ingest("demo://a", "A", "sample", "")
        results = self.kb.query_batch(["", "sample", " "], top_k=1)
        self.assertEqual([len(r) for r in results], [0, 1, 0])
        self.assertEqual(self.kb.query_batch(["", " "], top_k=1), [[], []])

    def test_long_sentence_is_bounded_and_has_overlap(self):
        chunks = bilibili_kb.split_text("a" * 1200)
        self.assertTrue(all(0 < len(c) <= 500 for c in chunks))
        self.assertEqual([len(c) for c in chunks], [500, 500, 400])


if __name__ == "__main__":
    unittest.main()
