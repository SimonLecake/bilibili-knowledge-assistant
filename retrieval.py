"""Pure retrieval utilities, independent of model APIs and the UI."""


def unique_queries(question, expanded):
    """Keep the original question first and embed every distinct query once."""
    return list(dict.fromkeys(q.strip() for q in [question, *expanded] if q and q.strip()))


def reciprocal_rank_fusion(result_lists, top_k, rank_constant=60):
    """Fuse ranked lists before truncation, using stable chunk IDs for deduplication.

    Scores are rank-based fusion weights, not calibrated confidence values.
    Ties preserve first-seen order, favoring the original query deterministically.
    """
    scores = {}
    chunks = {}
    for results in result_lists:
        seen = set()
        for rank, chunk in enumerate(results, 1):
            key = chunk.get("id")
            if key is None:
                key = (chunk.get("video_url"), chunk.get("source"), chunk["text"])
            if key in seen:
                continue
            seen.add(key)
            chunks.setdefault(key, chunk)
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank_constant + rank)
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [chunks[key] for key in ranked[:max(0, int(top_k))]]
