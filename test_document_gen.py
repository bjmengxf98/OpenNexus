#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Document generation test - local file creation only
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

async def test_document_generation():
    from core.document_generator import generate_and_upload_document

    # Test with dummy token (will fail on WPS upload, but local file should be created)
    access_token = "test_token_12345"
    dbsheet_file_id = "test_file_id"

    result = await generate_and_upload_document(
        access_token=access_token,
        dbsheet_file_id=dbsheet_file_id,
        title="Test Report",
        content="# Title\n\nThis is a test document.\n\n- Item 1\n- Item 2",
        doc_type="report",
        metadata={
            "author": "Test User",
            "department": "Test Dept",
            "date": "2026-04-07"
        }
    )

    print("=== Document Generation Result ===")
    print(f"OK: {result.get('ok')}")
    print(f"Message: {result.get('message')}")
    if result.get('local_path'):
        print(f"Local path: {result.get('local_path')}")
        local_file = Path(result.get('local_path'))
        if local_file.exists():
            print(f"File size: {local_file.stat().st_size} bytes")
            print("SUCCESS: Local file created!")
        else:
            print("ERROR: File not created!")
    if result.get('error'):
        print(f"Error: {result.get('error')}")
    if result.get('upload_error'):
        print(f"Upload error (expected): {result.get('upload_error')}")

if __name__ == "__main__":
    asyncio.run(test_document_generation())
