"""
知识库 RAG（检索增强生成）核心模块

职责：
- 文本分块
- 调用 embedding API 生成向量
- 对单篇文档进行分块+嵌入+存储

数据存取使用 auth.db 中的 knowledge_chunks 相关函数。
此模块不直接依赖 app.py，可独立测试。
"""

from __future__ import annotations

import json
import math


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 100) -> list[str]:
    """将长文本切分为重叠块（按字符数）"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


async def embed_texts(
    texts: list[str],
    api_key: str,
    base_url: str,
    model: str,
) -> list[list[float]]:
    """调用 OpenAI 兼容 embedding API，返回按输入顺序排列的向量列表"""
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    resp = await client.embeddings.create(model=model, input=texts)
    ordered = sorted(resp.data, key=lambda x: x.index)
    return [item.embedding for item in ordered]


async def embed_knowledge_doc(kid: int, content: str, title: str) -> int:
    """
    对单篇知识文档切块 + 向量化，结果存入 knowledge_chunks。

    返回值：
        >= 0  实际存储的块数
        -1    未配置嵌入 API 或调用失败
    """
    from auth import db

    cfg = db.get_embed_config()
    if not cfg:
        return -1
    try:
        chunks = chunk_text(content)
        if not chunks:
            return 0
        # 首块在标题前缀，增强语义关联
        chunks[0] = f"{title}\n{chunks[0]}"
        vectors = await embed_texts(chunks, cfg["api_key"], cfg["base_url"], cfg["model"])
        rows = [
            (i, chunks[i], json.dumps(vectors[i], ensure_ascii=False))
            for i in range(len(chunks))
        ]
        db.save_knowledge_chunks(kid, rows)
        print(f"[RAG] doc {kid} 《{title}》: {len(rows)} chunks embedded")
        return len(rows)
    except Exception as ex:
        print(f"[RAG] embed_knowledge_doc({kid}) failed: {ex}")
        return -1


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0
