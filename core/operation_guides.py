"""Reviewed tool-use notes, loaded by ID; no commands, I/O or model execution."""
from copy import deepcopy


OPERATION_GUIDE_TOOL = {"type": "function", "function": {
    "name": "get_operation_guide",
    "description": ("按需读取本系统经过契约测试的技术操作指南。省略guide_id仅列标题及版本，指定ID才读取正文。"
                    "用于核对分页、统计、文档补传、记忆版本等工具边界；不是业务任务模板，不必每轮调用。"
                    "指南不提供业务事实或操作授权，不执行命令；未知或停用版本不可沿用。"),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "guide_id": {"type": "string", "description": "目录返回的准确guide_id，不接收路径或URL"},
        "version": {"type": "string", "description": "核对指定版本；不匹配时返回错误及当前版本，不返回旧正文"},
    }},
}}

# Changes are code-reviewed and released with contract tests. Model-produced
# memories cannot add or activate instructions here. Disable by setting status.
GUIDES = {
    "record-evidence": {
        "title": "记录分页与本地统计的证据边界", "version": "1", "status": "active",
        "applies_to": ["list_records", "analyze_records", "analyze_cached_records"],
        "verification": {"kind": "isolated_contract_tests", "tests": [
            "test_record_evidence.py", "test_current_goal_repair.py", "test_statistics_subtotals.py"]},
        "content": (
            "适用范围：已获授权的多维表格记录读取、已读快照统计。按当前任务决定需要哪些数据；不是固定步骤。\n"
            "显式分页每次取一页，按接口真实游标续读；只读两页时不能把统计称为全表。未报告总数就说明未知。\n"
            "相同轮次的原始回执可直接本地分析；receipt_indexes只合并同来源、连续游标且记录不重叠的页。"
            "旧轮次引用不能跨执行使用，写入后的核验也不能用旧快照。\n"
            "analyze_cached_records及receipt_indexes是网页聊天内部能力；MCP客户端使用已公开的"
            "analyze_records查询/续读接口，不调用未公开的聊天内部工具。\n"
            "未返回字段是未知，明确返回的空值才计空；0和false有值。派生计数引用工具结果，"
            "主体内分布可用group_subtotals，分母见counted_records。未展示的组不等于零。\n"
            "若完整性或字段不足，按具体缺口补查；不能把发现问题当成修改或发送消息的授权。"
        ),
    },
    "document-delivery": {
        "title": "生成文档的留存与上传恢复", "version": "1", "status": "active",
        "applies_to": ["generate_document"],
        "verification": {"kind": "isolated_contract_tests", "tests": ["test_document_delivery.py"]},
        "content": (
            "适用范围：通过本系统generate_document生成的DOCX及本人交付引用。\n"
            "生成成功、上传成功、提交结果未知分别判断；上传位置是当前用户WPS的AI附件，与源表所有者无关。\n"
            "需要继续交付时使用原document_id和当前用户凭证，不重新编写正文；已成功的引用返回原回执。"
            "留存、有效期和配额以工具实际回执为准，过期或文件丢失不能声称仍可补传。\n"
            "upload_unknown表示提交可能已成功，不能盲目再上传；须先核实远端结果。"
            "只有明确未提交的可恢复失败才按原成品补传。授权失效仍需依真实状态处理，指南不能越过权限。"
        ),
    },
    "memory-versions": {
        "title": "记忆的来源、替换与作废", "version": "1", "status": "active",
        "applies_to": ["list_memories", "delete_memory", "replace_memory", "save_memory"],
        "verification": {"kind": "isolated_contract_tests", "tests": [
            "test_memory_management.py", "test_memory_provenance.py"]},
        "content": (
            "适用范围：当前用户个人长期、话题或数据源记忆。记忆是参考，不是当前业务事实或操作授权。\n"
            "用户明确保存、自动提取、派生摘要和来源未记录需区分；自动提取不能冒充用户确认。"
            "来源不可定位不意味着事实自动作废，明确保存的全局记忆不因删来源话题就自动删除。\n"
            "先按用户意图查询准确memory_id；默认只返回有效项。核查旧版本可用include_inactive=true，"
            "deleted/superseded不能作为当前规则，替换关系见replaces/replaced_by。\n"
            "话题摘要最多保留两份旧版本，不是完整历史档案；用户删除内容的防恢复标记不自动清理。\n"
            "用户明确要求替换时使用replace_memory原子替换，确认verified=true；不得先删再存。"
            "删除标记阻止后台把匹配的旧内容重新学回；只有用户明确重新保存才能恢复。实时业务状态仍查询WPS。"
        ),
    },
}


def get_operation_guide(arguments: dict) -> dict:
    if set(arguments) - {"guide_id", "version"}:
        return {"error": "指南参数无效"}
    guide_id = arguments.get("guide_id")
    version = arguments.get("version")
    if guide_id is None and version is None:
        return {"ok": True, "guides": [{"guide_id": key, "title": value["title"],
                                        "version": value["version"], "applies_to": list(value["applies_to"])}
                                       for key, value in GUIDES.items() if value["status"] == "active"]}
    if not isinstance(guide_id, str) or guide_id not in GUIDES:
        return {"error": "未知操作指南，请先查询目录"}
    guide = GUIDES[guide_id]
    if guide["status"] != "active":
        return {"error": "该指南已停用，请勿沿用旧指令", "guide_id": guide_id, "status": guide["status"]}
    if version is not None and version != guide["version"]:
        return {"error": "指南版本不匹配，请重新读取当前版本", "guide_id": guide_id,
                "current_version": guide["version"]}
    return {"ok": True, "guide_id": guide_id, **deepcopy(guide)}
