# OpenNexus WorkBuddy 业务技能包

这套技能让 WorkBuddy 直接调用 OpenNexus MCP 的业务工具。数据读取、写入、提醒、通知和文档生成由 OpenNexus 完成；业务理解和最终文字由 WorkBuddy 当前模型完成，默认不会再次调用 OpenNexus 的聊天模型。

## 包含的技能

- `opennexus-daily-progress`：查询、填报和修改每日进展
- `opennexus-work-summary`：生成日报、周报和部门工作总结
- `opennexus-task-project`：任务、项目及关联关系管理
- `opennexus-people-leave`：人员、休假和工作衔接查询
- `opennexus-smart-reminders`：智能规划、创建和管理提醒
- `opennexus-knowledge-documents`：知识检索和正式文档生成
- `opennexus-dashboard-analysis`：驾驶舱和历史快照分析（整体视图参数为 `overview`）
- `opennexus-wps-maintenance`：多维表结构、视图、表单和 Hook 管理

## 安装

1. 先在 WorkBuddy 中启用 OpenNexus MCP，并确认能看到 `opennexus-local` 工具。
2. 将需要的技能文件夹复制到：

   `C:\Users\<用户名>\.workbuddy\skills\`

3. 重启 WorkBuddy，或在技能管理页重新加载技能。

建议八个技能一起安装。技能只描述调用策略，不包含令牌、WPS 凭证、个人数据或服务器地址。

## 设计原则

- WorkBuddy 负责理解和写作，OpenNexus MCP 负责确定性业务操作。
- 每次操作先获取当前用户、可用文件和实时表结构，不猜测表 ID、字段 ID 或记录 ID。
- 写入后必须回读验证；删除、批量修改和结构变更必须先确认。
- 新增业务记录使用追加方式，不复用历史空行。
- 关联字段写真实记录 ID 数组；联系人字段写 WPS 数字 `account_id`。
- 驾驶舱默认使用 `ai_summary=false`，避免第二次大模型消耗。
