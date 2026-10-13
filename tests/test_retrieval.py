import unittest

from retrieval import reciprocal_rank_fusion, unique_queries


def chunk(key):
    return {"id": key, "text": key, "title": key, "video_url": "demo://" + key}


class RetrievalTests(unittest.TestCase):
    def test_original_question_is_embedded_only_once(self):
        self.assertEqual(unique_queries("original", ["original", " extra ", "extra", ""]),
                         ["original", "extra"])

    def test_expanded_query_evidence_survives_context_limit(self):
        original = [chunk("a"), chunk("b"), chunk("c")]
        expanded = [chunk("x"), chunk("y"), chunk("z")]
        result = reciprocal_rank_fusion([original, expanded], top_k=3)
        self.assertEqual([c["id"] for c in result], ["a", "x", "b"])

    def test_consensus_across_queries_improves_rank(self):
        result = reciprocal_rank_fusion(
            [[chunk("a"), chunk("shared")], [chunk("b"), chunk("shared")]], 3
        )
        self.assertEqual(result[0]["id"], "shared")
        self.assertEqual(len({c["id"] for c in result}), len(result))

    def test_duplicate_within_one_list_does_not_inflate_score(self):
        result = reciprocal_rank_fusion(
            [[chunk("a"), chunk("a"), chunk("b")], [chunk("b")]], 2
        )
        self.assertEqual(result[0]["id"], "b")

    def test_empty_results(self):
        self.assertEqual(reciprocal_rank_fusion([[], []], 5), [])


if __name__ == "__main__":
    unittest.main()
