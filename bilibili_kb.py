"""
B站视频内容知识库模块

功能：
- 将视频转写和AI总结切分为文本块
- 使用DashScope text-embedding-v1生成向量
- 使用FAISS构建本地向量库
- 支持基于视频内容的知识库问答

"""

import json
import re
import hashlib
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse
import numpy as np
import faiss
import dashscope
from dashscope import TextEmbedding


# ==================== 配置 ====================
# DashScope API Key（用于embedding，优先读环境变量）
from settings import DASHSCOPE_API_KEY, KB_DIR

# 文本切块参数
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100

# 每次向量化最大批次
EMBEDDING_BATCH_SIZE = 25

# 检索返回的最大块数
DEFAULT_TOP_K = 5


# ==================== 文本切分 ====================
def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """将长文本切分为固定大小的文本块，保留中文句子边界"""
    if not text or not text.strip():
        return []

    # 按段落初步切分
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    chunks = []
    current_chunk = ""

    for para in paragraphs:
        # 如果单个段落超过chunk_size，按句子再切
        if len(para) > chunk_size:
            sentences = re.split(r'([。！？.!?])', para)
            sentences = [s for s in sentences if s.strip()]
            # 把标点和前面句子合并
            merged = []
            i = 0
            while i < len(sentences):
                if re.match(r'[。！？.!?]', sentences[i]):
                    if merged:
                        merged[-1] += sentences[i]
                    i += 1
                else:
                    if i + 1 < len(sentences) and re.match(r'[。！？.!?]', sentences[i + 1]):
                        merged.append(sentences[i] + sentences[i + 1])
                        i += 2
                    else:
                        merged.append(sentences[i])
                        i += 1
            sentences = merged
        else:
            sentences = [para]

        for sent in sentences:
            if len(current_chunk) + len(sent) <= chunk_size:
                current_chunk += sent
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                # 保留重叠部分
                if overlap > 0 and current_chunk:
                    overlap_text = current_chunk[-overlap:]
                    current_chunk = overlap_text + sent
                else:
                    current_chunk = sent

                # 如果单句仍然超过chunk_size，强制截断
                while len(current_chunk) > chunk_size:
                    chunks.append(current_chunk[:chunk_size].strip())
                    current_chunk = current_chunk[chunk_size - overlap:chunk_size] + current_chunk[chunk_size:]

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


# ==================== 向量获取 ====================
def get_embeddings(texts: List[str]) -> np.ndarray:
    """调用DashScope text-embedding-v1获取文本向量"""
    if not DASHSCOPE_API_KEY:
        raise ValueError("未配置DASHSCOPE_API_KEY，无法生成embedding。请在 .env 中配置。")

    dashscope.api_key = DASHSCOPE_API_KEY

    valid_texts = [t for t in texts if t and t.strip()]
    if not valid_texts:
        raise ValueError("输入文本全部为空，无法生成embedding。")

    embeddings = []
    for i in range(0, len(valid_texts), EMBEDDING_BATCH_SIZE):
        batch = valid_texts[i:i + EMBEDDING_BATCH_SIZE]
        resp = TextEmbedding.call(
            model=TextEmbedding.Models.text_embedding_v1,
            input=batch
        )

        if not resp:
            raise RuntimeError(f"DashScope embedding返回异常: {resp}")

        # 兼容对象和字典两种返回格式
        if isinstance(resp, dict):
            output = resp.get('output')
            if output is None:
                raise RuntimeError(f"DashScope embedding返回异常: {resp}")
            embedding_list = output.get('embeddings', [])
            for emb in embedding_list:
                emb_vec = emb.get('embedding')
                if emb_vec is None or len(emb_vec) == 0:
                    raise RuntimeError("DashScope返回了空embedding。")
                embeddings.append(emb_vec)
        else:
            if not hasattr(resp, 'output') or resp.output is None:
                raise RuntimeError(f"DashScope embedding返回异常: {resp}")
            for emb in resp.output.embeddings:
                if emb.embedding is None or len(emb.embedding) == 0:
                    raise RuntimeError("DashScope返回了空embedding。")
                embeddings.append(emb.embedding)

    embeddings_array = np.array(embeddings, dtype=np.float32)
    # 归一化，使内积等价于余弦相似度
    norms = np.linalg.norm(embeddings_array, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embeddings_array = embeddings_array / norms
    return embeddings_array


# ==================== 知识库类 ====================
class KnowledgeBase:
    """B站视频内容知识库"""

    def __init__(self, kb_dir: str = KB_DIR):
        self.kb_dir = Path(kb_dir)
        self.docs_dir = self.kb_dir / "documents"
        self.index_path = self.kb_dir / "vector_db.faiss"
        self.chunks_path = self.kb_dir / "chunks.json"

        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        self.chunks: List[Dict] = []
        self.index: Optional[faiss.IndexFlatIP] = None
        self._load()

    def _load(self):
        """加载已有知识库"""
        if self.chunks_path.exists():
            try:
                with open(self.chunks_path, 'r', encoding='utf-8') as f:
                    self.chunks = json.load(f)
            except Exception as e:
                print(f"加载chunks.json失败: {e}")
                self.chunks = []

        if self.index_path.exists():
            try:
                self.index = faiss.read_index(str(self.index_path))
            except Exception as e:
                print(f"加载FAISS索引失败: {e}")
                self.index = None

    def _save(self):
        """保存知识库"""
        with open(self.chunks_path, 'w', encoding='utf-8') as f:
            json.dump(self.chunks, f, ensure_ascii=False, indent=2)
        if self.index is not None:
            faiss.write_index(self.index, str(self.index_path))

    @staticmethod
    def _canonical_source(video_url: str) -> str:
        """Normalize Bilibili BV links while retaining multipart page identity."""
        from urllib.parse import parse_qs

        parsed = urlparse(video_url)
        host = (parsed.hostname or "").lower()
        match = re.search(r"/video/(BV[0-9A-Za-z]+)", parsed.path)
        if (host == "bilibili.com" or host.endswith(".bilibili.com")) and match:
            page = parse_qs(parsed.query).get("p", ["1"])[0]
            suffix = f"?p={int(page)}" if page.isdigit() and int(page) > 1 else ""
            return f"https://www.bilibili.com/video/{match.group(1)}{suffix}"
        return video_url.strip()

    @staticmethod
    def _video_id_from_url(video_url: str) -> str:
        """从视频URL生成文档ID"""
        canonical = KnowledgeBase._canonical_source(video_url)
        return hashlib.sha1(canonical.encode('utf-8')).hexdigest()

    def ingest(self, video_url: str, title: str, transcript: str, summary: str, metadata: dict = None):
        """
        将单次视频分析结果沉淀到知识库
        参数：
            video_url: 视频链接
            title: 视频标题
            transcript: 完整转写文本
            summary: AI总结文本
            metadata: 视频元数据（uploader/duration/description等）
        """
        if not DASHSCOPE_API_KEY:
            print("[知识库] 未配置DASHSCOPE_API_KEY，跳过沉淀。")
            return False

        video_url = self._canonical_source(video_url)
        doc_id = self._video_id_from_url(video_url)
        metadata = metadata or {}
        uploader = metadata.get("uploader", "未知UP主")
        duration = metadata.get("duration", 0)
        description = metadata.get("description", "")

        # 构建文本块
        chunks = []

        # Metadata文本块（标题、UP主、时长、简介）
        meta_text = f"视频标题：{title}\nUP主：{uploader}\n视频时长：{duration}秒"
        if description and description.strip():
            meta_text += f"\n视频简介：{description.strip()[:500]}"
        chunks.append({
            "id": f"{doc_id}_m_0",
            "text": meta_text,
            "source": "metadata",
            "video_url": video_url,
            "title": title,
            "created_at": datetime.now().isoformat()
        })

        # 转写文本切块
        for idx, text in enumerate(split_text(transcript)):
            chunks.append({
                "id": f"{doc_id}_t_{idx}",
                "text": text,
                "source": "transcript",
                "video_url": video_url,
                "title": title,
                "created_at": datetime.now().isoformat()
            })

        # AI总结切块（通常较短，保持原样或简单切分）
        for idx, text in enumerate(split_text(summary)):
            chunks.append({
                "id": f"{doc_id}_s_{idx}",
                "text": text,
                "source": "summary",
                "video_url": video_url,
                "title": title,
                "created_at": datetime.now().isoformat()
            })

        if not chunks:
            print("[知识库] 没有可沉淀的内容。")
            return False

        # Obtain new embeddings before changing the existing in-memory index.
        embeddings = get_embeddings([c["text"] for c in chunks])
        if embeddings.shape[0] != len(chunks):
            raise ValueError("Embedding 数量与文本块数量不一致，未更新知识库。")
        if self.chunks and (self.index is None or self.index.ntotal != len(self.chunks)):
            raise ValueError("索引与文本块不一致，请从原始资料重建知识库。")
        if self.index is not None and self.index.d != embeddings.shape[1]:
            raise ValueError("Embedding 维度发生变化，请使用新的知识库目录。")

        # Replace this source, retaining existing vectors for other documents.
        # Rebuild IndexFlatIP because vector positions must match chunk indices.
        kept = [
            i for i, c in enumerate(self.chunks)
            if self._canonical_source(c["video_url"]) != video_url
        ]
        new_index = faiss.IndexFlatIP(embeddings.shape[1])
        if kept:
            retained_vectors = np.vstack([self.index.reconstruct(i) for i in kept])
            new_index.add(retained_vectors.astype(np.float32))
        new_index.add(embeddings)
        self.chunks = [self.chunks[i] for i in kept] + chunks
        for i, chunk in enumerate(self.chunks):
            chunk["index"] = i
        self.index = new_index

        self._save()

        # 保存单份文档（兼容RAG-Challenge格式）
        doc = {
            "metainfo": {
                "sha1_name": doc_id,
                "video_url": video_url,
                "title": title,
                "uploader": uploader,
                "duration": duration,
                "description": description,
                "created_at": datetime.now().isoformat()
            },
            "content": {
                "chunks": chunks
            }
        }
        doc_path = self.docs_dir / f"{doc_id}.json"
        with open(doc_path, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

        print(f"[知识库] 已沉淀 {len(chunks)} 个文本块，文档ID: {doc_id}")
        return True

    def query_batch(self, questions: List[str], top_k: int = DEFAULT_TOP_K) -> List[List[Dict]]:
        """
        批量查询：一次 embedding 调用完成多个问题的向量检索，减少 API 往返。
        返回与 questions 等长的列表，每项是对应问题的检索结果。
        """
        if not DASHSCOPE_API_KEY:
            return [[] for _ in questions]

        if self.index is None or len(self.chunks) == 0:
            return [[] for _ in questions]

        valid_questions = [q.strip() for q in questions if q and q.strip()]
        if not valid_questions or top_k <= 0:
            return [[] for _ in questions]
        q_embs = get_embeddings(valid_questions)
        if not q_embs.shape[0]:
            return [[] for _ in questions]

        actual_top_k = min(top_k, len(self.chunks))
        results = []
        emb_idx = 0
        for q in questions:
            if q and q.strip():
                q_emb = q_embs[emb_idx]
                emb_idx += 1
                _, indices = self.index.search(q_emb.reshape(1, -1), actual_top_k)
                retrieved = []
                for idx in indices[0]:
                    if 0 <= idx < len(self.chunks):
                        retrieved.append(self.chunks[idx])
                results.append(retrieved)
            else:
                results.append([])
        return results

    def query(self, question: str, top_k: int = DEFAULT_TOP_K) -> Tuple[str, List[Dict]]:
        """
        基于知识库回答问题
        返回：(答案, 检索到的相关块列表)
        """
        if not DASHSCOPE_API_KEY:
            return "[未配置DASHSCOPE_API_KEY，无法检索知识库]", []

        if self.index is None or len(self.chunks) == 0:
            return "[知识库为空，请先分析视频并沉淀内容]", []

        retrieved = self.query_batch([question], top_k=top_k)[0]
        return "", retrieved

    def get_stats(self) -> Dict:
        """获取知识库统计信息"""
        doc_count = len(list(self.docs_dir.glob("*.json")))
        chunk_count = len(self.chunks)
        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "kb_dir": str(self.kb_dir)
        }
