---
name: opennexus-wps-maintenance
description: >
  使用 OpenNexus MCP 管理 WPS 多维表格的表、字段、视图、表单、父子表、自动化 Hook、传统表格工作表与单元格范围。仅在用户明确要求建表、改字段、调整视图或表单、配置 Hook、维护表结构时使用；不用于普通业务记录填报。
---

# OpenNexus WPS 维护

## 通用规则

1. 调用 `get_current_user`、`list_wps_files` 和 `get_schema` 获取实时对象与 ID。
2. 任何结构变更先展示拟操作对象、变更内容和影响，再获得明确确认。
3. 不猜测字段类型、选项 ID、视图 ID、表单字段 ID 或 Hook ID。
4. 操作后重新调用 `get_schema` 或对应查询工具验证。

## 多维表结构

- 表：`create_sheet`、`delete_sheet`
- 字段：`create_fields`、`update_fields`、`delete_fields`
- 视图：`create_view`、`delete_view`
- 仪表盘：`list_dashboards`、`copy_dashboard`
- 表单：`get_form_meta`、`update_form_meta`、`list_form_fields`、`update_form_field`
- 父子表：`get_parent_status`、`enable_parent`、`disable_parent`、`list_children`、`bind_children`、`unbind_children`
- Hook：`list_hooks`、`create_hook`、`delete_hook`

删除表、字段、视图、Hook 或解除父子绑定属于高风险操作，必须二次确认。

## 传统 WPS 表格

- 文件创建能力 `sheets_create_file` 当前禁用，不要尝试调用。
- 工作表：`sheets_list_worksheets`、`sheets_create_worksheet`、`sheets_update_worksheet`、`sheets_delete_worksheets`、`sheets_copy_worksheet`
- 单元格：`sheets_get_range`、`sheets_update_range`、`sheets_delete_range`、`sheets_find_range`

更新或清空单元格前先读取目标范围并展示预期变更；批量操作需确认范围。

## 审计

- 需要追踪表格变更时调用 `get_change_log`。
- 需要追踪外部 MCP 客户端调用时调用 `get_mcp_audit_log`。
- 审计输出不得暴露访问令牌、API Key 或不必要的个人身份标识。
