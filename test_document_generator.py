import asyncio
import io
import json
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from docx import Document

from core.document_generator import (
    DocumentStructureError,
    _normalize_document_structure,
    generate_and_upload_document,
)


def _sample_structure():
    return {
        "sections": [
            {"type": "title", "text": "测试报告"},
            {"type": "paragraph", "text": "这是正文。"},
        ]
    }


def _fake_wps_client():
    module = types.ModuleType("agent.wps_client")
    module.upload_to_drive = AsyncMock(return_value={"id": "cloud-file-1"})
    return module


@pytest.mark.parametrize(
    "content",
    [
        _sample_structure(),
        json.dumps(_sample_structure(), ensure_ascii=False),
        "```json\n" + json.dumps(_sample_structure(), ensure_ascii=False) + "\n```",
        json.dumps(json.dumps(_sample_structure(), ensure_ascii=False), ensure_ascii=False),
        "以下是文档结构：\n" + json.dumps(_sample_structure(), ensure_ascii=False),
        '{"sections":[{"type":"paragraph","text":"第一行\n第二行"}]}',
    ],
)
def test_normalize_document_structure_accepts_compatible_inputs(content):
    result = _normalize_document_structure(content)

    assert result["sections"][0]["type"] in {"title", "paragraph"}
    assert any(section.get("text") for section in result["sections"])


@pytest.mark.parametrize(
    "content",
    [
        '{"sections":[{"type":"paragraph","text":"说"生成文档"自动"}]}',
        {"sections": []},
        {"sections": [{"type": "table", "text": "不支持的结构"}]},
        {
            "sections": [
                {
                    "type": "paragraph",
                    "text": '{"sections":[{"type":"title","text":"源码"}]}',
                }
            ]
        },
    ],
)
def test_normalize_document_structure_rejects_unsafe_inputs(content):
    with pytest.raises(DocumentStructureError):
        _normalize_document_structure(content)


def test_malformed_structure_never_uploads_raw_json():
    fake_wps = _fake_wps_client()
    malformed = '{"sections":[{"type":"paragraph","text":"说"生成文档"自动"}]}'

    with patch.dict(sys.modules, {"agent.wps_client": fake_wps}):
        result = asyncio.run(
            generate_and_upload_document(
                access_token="token",
                title="错误文档",
                content=malformed,
                dbsheet_file_id="file-id",
            )
        )

    assert result["ok"] is False
    assert result["code"] == "invalid_document_structure"
    assert result["retryable"] is True
    assert result["uploaded"] is False
    assert "未生成或上传任何文件" in result["error"]
    fake_wps.upload_to_drive.assert_not_awaited()


def test_valid_structure_renders_and_uploads_docx_without_raw_json():
    fake_wps = _fake_wps_client()

    with patch.dict(sys.modules, {"agent.wps_client": fake_wps}):
        result = asyncio.run(
            generate_and_upload_document(
                access_token="token",
                title="测试报告",
                content=_sample_structure(),
                dbsheet_file_id="file-id",
                doc_type="report",
            )
        )

    assert result["ok"] is True
    fake_wps.upload_to_drive.assert_awaited_once()
    uploaded_bytes = fake_wps.upload_to_drive.await_args.args[3]
    document = Document(io.BytesIO(uploaded_bytes))
    rendered_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "测试报告" in rendered_text
    assert "这是正文。" in rendered_text
    assert '{"sections"' not in rendered_text
