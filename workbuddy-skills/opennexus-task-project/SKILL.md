---
name: opennexus-task-project
description: >
  使用 OpenNexus MCP 创建、查询、更新和分析部门任务或项目，包括负责人、优先级、期限、状态、每日进展以及任务与项目的关联。用户要求“新建任务”“分配负责人”“项目到哪了”“哪些任务逾期”“把任务关联到项目”时使用。
---

# OpenNexus 任务与项目

## 查询

1. 调用 `get_current_user`、`list_wps_files` 和 `get_schema` 确定身份、业务文件与实时结构。
2. 用 `list_records` 查询任务、项目、人员和进展。涉及关联字段时，继续读取关联表并翻译名称。
3. 判断逾期必须同时检查截止日期和完成状态，不能只看日期。

## 新建与更新

1. 从用户表述提取名称、负责人、优先级、期限、状态、所属项目和说明。
2. 负责人、项目等关联对象必须先查询真实记录：
   - Link 字段使用对应记录 ID 数组。
   - Contact 字段使用 WPS 数字 `account_id`。
3. 缺少负责人、任务名称等必填信息时先询问；不得填入虚构占位值。
4. 新建调用 `create_records`，始终追加新记录，不填补历史空行。
5. 更新调用 `update_records`；批量更新前先展示影响范围并确认。
6. 写入后用 `list_records` 按记录 ID 回读验证。

## 通知协同

- 用户明确要求通知相关人员时才发送消息。
- WPS 字母数字 open_id 仅用于 `send_wps_bot_message`。
- 企业微信用 `send_wecom_message`；个人微信用 `send_weixin_message`；通用渠道选择可用 `send_notification`。
- 消息中写清事项、责任、期限和数据来源，不把发送失败描述成成功。

## 删除与结构操作

- 删除任务或项目必须先查询并列出名称、记录 ID 和可能受影响的关联，再获得明确确认后调用 `delete_records`。
- 本技能不擅自修改表结构；结构调整交给 `opennexus-wps-maintenance`。
