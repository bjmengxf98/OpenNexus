"""
WPS MCP 客户端 - 封装多维表格操作
"""
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "https://openapi.wps.cn/mcp/v2/kso-dbsheet/message"


async def _call(access_token: str, tool_name: str, arguments: dict):
    async with streamablehttp_client(
        url=MCP_URL,
        headers={"Authorization": f"Bearer {access_token}"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            if result.content:
                return result.content[0].text
            return ""


async def list_tools(access_token: str) -> list:
    async with streamablehttp_client(
        url=MCP_URL,
        headers={"Authorization": f"Bearer {access_token}"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema,
                    }
                }
                for t in resp.tools
            ]


# ── 便捷封装 ───────────────────────────────────────────────

async def get_file_schema(access_token: str, keyword: str = None, file_url: str = None):
    args = {}
    if keyword:
        args["keyword"] = keyword
    if file_url:
        args["fileURL"] = file_url
    return await _call(access_token, "kso_dbsheet_get_file_schema", args)


async def create_sheet(access_token: str, sheet_name: str, field_names: list,
                       file_url: str = None, keyword: str = None):
    args = {"sheetName": sheet_name, "fieldNames": field_names}
    if file_url:
        args["fileURL"] = file_url
    if keyword:
        args["keyword"] = keyword
    return await _call(access_token, "kso_dbsheet_create_sheet", args)


async def list_records(access_token: str, sheet_name: str = None,
                       file_url: str = None, page_size: int = 100):
    args = {"pageSize": page_size}
    if sheet_name:
        args["sheetName"] = sheet_name
    if file_url:
        args["fileURL"] = file_url
    return await _call(access_token, "kso_dbsheet_list_sheet_records", args)


async def create_records(access_token: str, records: list,
                         sheet_name: str = None, file_url: str = None):
    args = {"records": records}
    if sheet_name:
        args["sheetName"] = sheet_name
    if file_url:
        args["fileURL"] = file_url
    return await _call(access_token, "kso_dbsheet_create_sheet_records", args)


async def update_records(access_token: str, records: list,
                         sheet_name: str = None, file_url: str = None):
    args = {"records": records}
    if sheet_name:
        args["sheetName"] = sheet_name
    if file_url:
        args["fileURL"] = file_url
    return await _call(access_token, "kso_dbsheet_update_sheet_records", args)
