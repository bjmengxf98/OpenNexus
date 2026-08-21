---
name: opennexus-dashboard-analysis
description: >
  使用 OpenNexus MCP 获取部门驾驶舱、每日进展、任务分析、项目分析和整体情况的指标、历史日期及快照，并由 WorkBuddy 做进一步解读。用户提到“驾驶舱”“仪表盘”“看某天数据”“整体情况”“任务/项目分析”时使用。
---

# OpenNexus 驾驶舱分析

## 获取数据

1. 调用 `get_current_user` 和 `list_wps_files` 确定用户与业务文件。
2. 历史日期先调用 `list_dashboard_dates` 获取可用日期，不猜测快照是否存在。
3. 调用 `get_dashboard`：
   - `view=daily`：每日进展
   - `view=tasks`：任务分析
   - `view=projects`：项目分析
   - `view=overview`：整体情况
4. 默认显式设置 `ai_summary=false`，由 WorkBuddy 当前模型分析返回的结构化结果，避免第二次大模型消耗。

## 数据新鲜度

- 历史日期优先使用已保存快照。
- 用户明确要求“刷新数据/最新情况”时才请求刷新或实时同步；说明刷新可能需要访问 WPS 并耗时。
- WPS 暂时不可用时，可展示已有缓存，但必须标注缓存时间，不能称为实时数据。

## 解读

- 先给关键数字和变化，再给风险、异常及建议。
- 区分业务事实、规则计算结果和模型推断。
- 不把“未填报”直接解释为“未工作”。
- 需要追溯明细时，再用 `get_schema` 与 `list_records` 查询原始记录验证。
