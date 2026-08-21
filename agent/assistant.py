"""
智能体核心 - 多模型支持 + WPS REST API 全量工具调用
"""
import json
import mimetypes
from pathlib import Path
from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, RateLimitError
from agent.wps_client import (
    get_schema,
    list_records, create_records, update_records, delete_records,
    create_sheet, delete_sheet,
    create_fields, update_fields, delete_fields,
    create_view, delete_view,
    upload_attachment, upload_to_drive, _post,
    get_form_meta, update_form_meta, list_form_fields, update_form_field,
    list_dashboards, copy_dashboard,
    get_parent_status, enable_parent, disable_parent,
    list_children, bind_children, unbind_children,
    list_hooks, create_hook, delete_hook,
    sheets_list_worksheets, sheets_create_worksheet, sheets_update_worksheet,
    sheets_delete_worksheets, sheets_copy_worksheet,
    sheets_get_range, sheets_update_range, sheets_delete_range,
    sheets_find_range, sheets_create_file,
)

from auth.db import (search_knowledge as _db_search_knowledge,
                     search_knowledge_rag as _db_search_knowledge_rag,
                     get_embed_config as _db_get_embed_config,
                     get_change_log as _db_get_change_log,
                     get_change_log_last_seen as _db_get_last_seen,
                     set_change_log_last_seen as _db_set_last_seen,
                     save_user_memory as _db_save_user_memory,
                     get_user_memory as _db_get_user_memory,
                     get_wps_user_id_by_name as _db_get_wps_uid,
                     list_users_for_ai as _db_list_users_for_ai,
                     add_wps_file as _db_add_wps_file,
                     add_reminder as _db_add_reminder,
                     list_reminders as _db_list_reminders,
                     cancel_reminder as _db_cancel_reminder)
from auth.email_sender import send_email as _send_email
from agent.wps_client import create_record_comment as _wps_comment
from agent.wps_client import list_contacts as _wps_list_contacts
from agent.wps_client import send_bot_message as _wps_send_bot_message
from agent.wecom_client import send_wecom_webhook as _wecom_send
from auth.db import get_system_config as _db_get_system_config
from auth.db import get_wecom_userid as _db_get_wecom_userid
from core.document_generator import generate_and_upload_document as _generate_document

LLM_PRESETS = {
    "scnet": {
        "name": "超算SCNet",
        "base_url": "https://api.scnet.cn/api/llm/v1",
        "model": "DeepSeek-V4-Flash",
        "models": [
            # ── DeepSeek ──────────────────────────────────────
            {"id": "DeepSeek-V4-Flash",            "name": "V4-Flash · 标准模式（推荐）"},
            {"id": "DeepSeek-V4-Flash-Reasoning",  "name": "V4-Flash · 思考模式"},
            {"id": "DeepSeek-V4-Pro",              "name": "V4-Pro · 标准模式"},
            {"id": "DeepSeek-V4-Pro-Reasoning",    "name": "V4-Pro · 思考模式"},
            {"id": "DeepSeek-V3.2",                "name": "V3.2 · 通用"},
            {"id": "DeepSeek-R1-0528",             "name": "R1-0528 · 最新推理"},
            {"id": "DeepSeek-R1-Distill-Qwen-32B", "name": "R1-Distill-32B · 中量推理"},
            {"id": "DeepSeek-R1-Distill-Llama-70B", "name": "R1-Distill-Llama-70B · 推理"},
            {"id": "DeepSeek-R1-Distill-Qwen-7B",  "name": "R1-Distill-7B · 轻量推理"},
            # ── Qwen ──────────────────────────────────────────
            {"id": "Qwen3.6-Flash",                "name": "Qwen3.6-Flash · 快速"},
            {"id": "Qwen3.6-Plus",                 "name": "Qwen3.6-Plus · 均衡"},
            {"id": "Qwen3.6-Max",                  "name": "Qwen3.6-Max · 旗舰"},
            {"id": "Qwen3-30B-A3B",                "name": "Qwen3-30B · MoE"},
            {"id": "Qwen3-235B-A22B",              "name": "Qwen3-235B · 超大MoE"},
            {"id": "Qwen3-235B-A22B-Thinking-2507","name": "Qwen3-235B · 思考模式"},
            {"id": "QwQ-32B",                      "name": "QwQ-32B · 推理"},
            # ── GLM ───────────────────────────────────────────
            {"id": "GLM-5.2",                      "name": "GLM-5.2 · 最新旗舰"},
            {"id": "GLM-5.1",                      "name": "GLM-5.1 · 旗舰"},
            {"id": "GLM-5",                        "name": "GLM-5 · 通用"},
            # ── Kimi ──────────────────────────────────────────
            {"id": "Kimi-K2.7-Code",               "name": "Kimi-K2.7-Code · 代码"},
            {"id": "Kimi-K2.6",                    "name": "Kimi-K2.6 · 通用"},
            {"id": "Kimi-K2.5",                    "name": "Kimi-K2.5 · 轻量"},
            # ── MiniMax ───────────────────────────────────────
            {"id": "MiniMax-M3",                   "name": "MiniMax-M3 · 最新"},
            {"id": "MiniMax-M2.7",                 "name": "MiniMax-M2.7 · 通用"},
            {"id": "MiniMax-M2.5",                 "name": "MiniMax-M2.5 · 百万上下文"},
            # ── MiMo ──────────────────────────────────────────
            {"id": "MiMo-V2.5-Pro",                "name": "MiMo-V2.5-Pro · 推理"},
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "models": [
            {"id": "deepseek-v4-flash",           "name": "V4 Flash · 标准模式（推荐）"},
            {"id": "deepseek-v4-flash-reasoning", "name": "V4 Flash · 思考模式"},
            {"id": "deepseek-v4-pro",             "name": "V4 Pro · 标准模式"},
            {"id": "deepseek-v4-pro-reasoning",   "name": "V4 Pro · 思考模式"},
        ],
    },
    "claude": {
        "name": "Claude",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "models": [
            {"id": "claude-haiku-4-5-20251001", "name": "Haiku · 快速轻量"},
            {"id": "claude-sonnet-4-6",         "name": "Sonnet · 均衡（推荐）"},
            {"id": "claude-opus-4-6",           "name": "Opus · 旗舰最强"},
        ],
    },
    "gemini": {
        "name": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash",
        "models": [
            {"id": "gemini-2.0-flash",              "name": "2.0 Flash · 快速（推荐）"},
            {"id": "gemini-2.0-flash-thinking-exp", "name": "2.0 Flash Thinking · 推理"},
            {"id": "gemini-1.5-pro",                "name": "1.5 Pro · 长文本"},
        ],
    },
    "qwen": {
        "name": "千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": [
            {"id": "qwen-turbo",   "name": "Turbo · 快速"},
            {"id": "qwen-plus",    "name": "Plus · 均衡（推荐）"},
            {"id": "qwen-max",     "name": "Max · 旗舰"},
            {"id": "qwen3.6-plus", "name": "3.6 Plus · 深度思考"},
            {"id": "qwen-vl-plus", "name": "VL-Plus · 视觉理解"},
            {"id": "qwen-vl-max",  "name": "VL-Max · 视觉旗舰"},
        ],
    },
    "doubao": {
        "name": "豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
        "models": [
            {"id": "doubao-lite-32k",       "name": "Lite 32K · 轻量"},
            {"id": "doubao-pro-32k",        "name": "Pro 32K · 专业（推荐）"},
            {"id": "doubao-pro-128k",       "name": "Pro 128K · 长文本"},
            {"id": "doubao-vision-pro-32k", "name": "Vision Pro · 视觉"},
        ],
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "models": [
            {"id": "gpt-4o-mini", "name": "GPT-4o mini · 快速轻量"},
            {"id": "gpt-4o",      "name": "GPT-4o · 均衡（推荐）"},
            {"id": "o1-mini",     "name": "o1-mini · 推理"},
            {"id": "o3-mini",     "name": "o3-mini · 推理旗舰"},
        ],
    },
    "glm": {
        "name": "智谱GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": [
            {"id": "glm-5.1",        "name": "GLM-5.1 · 旗舰最新"},
            {"id": "glm-5",          "name": "GLM-5 · 旗舰"},
            {"id": "glm-5-turbo",    "name": "GLM-5-Turbo · 旗舰精选"},
            {"id": "glm-4.7-flash",  "name": "GLM-4.7-Flash · 免费"},
            {"id": "glm-4-flash",    "name": "GLM-4-Flash · 免费"},
            {"id": "glm-4-plus",     "name": "GLM-4-Plus · 均衡"},
            {"id": "glm-4-air",      "name": "GLM-4-Air · 轻量"},
            {"id": "glm-4.6v-flash", "name": "GLM-4.6V-Flash · 视觉免费"},
        ],
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "models": [
            {"id": "deepseek-ai/DeepSeek-V3",           "name": "DeepSeek-V3 · 免费额度"},
            {"id": "Qwen/Qwen2.5-72B-Instruct",         "name": "Qwen2.5-72B · 免费额度"},
            {"id": "Qwen/Qwen2.5-7B-Instruct",          "name": "Qwen2.5-7B · 免费"},
            {"id": "THUDM/glm-4-9b-chat",               "name": "GLM-4-9B · 免费"},
            {"id": "meta-llama/Llama-3.3-70B-Instruct", "name": "Llama-3.3-70B · 免费额度"},
            {"id": "THUDM/GLM-5",                       "name": "GLM-5 · 付费"},
            {"id": "moonshot-ai/Kimi-K2.5",             "name": "Kimi-K2.5 · 付费"},
            {"id": "MiniMax-AI/MiniMax-M2.5",           "name": "MiniMax-M2.5 · 付费"},
            {"id": "Pro/THUDM/glm-4.7",                 "name": "GLM-4.7 Pro · 付费"},
        ],
    },
    "agnes": {
        "name": "Agnes AI",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "model": "agnes-2.0-flash",
        "models": [
            {"id": "agnes-2.0-flash", "name": "Agnes 2.0 Flash · 免费（推荐）"},
        ],
    },
}

SYSTEM_PROMPT_TEMPLATE = """你是 {username} 的全能工作助手，同时也是业务管理专家。

## 【首要规则】每次响应前先判断话题
收到用户消息后，**第一步**先判断：当前消息是上一个话题的延续，还是一个全新的独立指令？

**判断方法**：将当前消息与上一条用户消息对比——
- 当前消息涉及的对象、动作、目标与上一条**明显不同** → **新话题**，忽略上文已执行的操作，只响应当前消息
- 当前消息是对上一条的追问、补充、确认、修正 → **延续话题**，结合上文理解

**典型新话题信号**（出现以下任一，视为新话题）：
- 换了人物（上文说张三，现在说李四）
- 换了动作（上文在操作表格，现在在问管理方法）
- 换了表格或项目（上文在归档，现在说切换到另一个表）
- 疑问句且与上文无关联（上文在执行操作，现在问"应该怎么管理"）

**禁止**：把上一个话题中已完成的操作结果，当作当前话题的回答内容。

**主业**：通过 WPS 多维表格管理部门事务（查询录入数据、建表设计、统计分析）
**兼业**：日常秘书与助理工作，包括：
- 起草、修改文件、通知、邮件、报告、总结、方案
- 分析上传的 Excel/Word/PDF，提炼要点、做数据摘要
- 整理会议纪要、梳理待办事项、安排日程
- 回答工作问题、提供专业建议
- 查阅并解读本单位规章制度，指导按规办事

当前用户：{username}（{role}）
{file_hint}

## 重要：文件读取能力
系统已将用户上传的 Word/PDF/Excel/图片解析为纯文本，附在消息正文中，格式：
  【文件：文件名】
  <内容>
内容就在消息里，直接阅读并处理。**如果看到 [PDF内容为空] [解析失败] 等提示，如实告知用户解析失败原因，绝对不要编造文件内容。**

## 文档生成
用户说「生成报告」「写通知」「生成纪要」「导出文档」「生成word」「生成文档」「写文档」「导出word」等时：
**必须调用 generate_document 工具**，不得只回复文字。

### ⚠️ doc_type 选择规则（重要）
**关键区分**：
- **工作报告/总结/计划** → 用 `"report"`（普通文档，无发文字号，标题用黑体）
- **通知/纪要** → 用 `"notice"` 或 `"minutes"`（普通文档，无发文字号）
- **正式公文（需要发文字号）** → 用 `"official"` 或 `"official_red"`（有发文字号，标题用方正小标宋）

**判断标准**：只有明确需要发文字号的正式公文才用 official/official_red，其他都用 report/notice/minutes。

### ⚠️ content 参数格式（重要）
content 参数是一个 JSON 对象（不是字符串），包含 sections 数组。每个 section 有 type 和 text 字段。直接传递对象，不需要将其序列化为字符串。

**关键规则**：
1. **单位名称**：必须是"民航机场规划设计研究总院有限公司"（绝对不能用"中国民航机场建设集团公司"）
2. **发文字号**：格式"规划总院〔2026〕XX号"（不是部门名如"科技质量部〔2026〕XX号"）
3. **条款间距**：条款之间只需一个 {{"type": "space"}}，不要连续多个空行
4. **称谓行顶格**：通知/公文中"各部门：""各位同事："等称谓行必须用 `{{"type": "recipient"}}` 类型，不能用 paragraph
5. **通知必须有称谓行**：生成通知类文档时，标题后必须紧跟称谓行，禁止省略
6. **结尾语**：通知结尾的"特此通知""请遵照执行"等必须用 `{{"type": "ending"}}` 类型（顶格，段前自动空一行）；落款（signature）段前也会自动空一行

**红头公文模板**（doc_type="official_red"，用于正式发文，标题用方正小标宋简体）：
```json
{{"sections": [
  {{"type": "org_header", "text": "民航机场规划设计研究总院有限公司"}},
  {{"type": "space"}},
  {{"type": "space"}},
  {{"type": "doc_number", "text": "规划总院〔2026〕15号"}},
  {{"type": "red_line"}},
  {{"type": "space"}},
  {{"type": "space"}},
  {{"type": "title", "text": "标题"}},
  {{"type": "space"}},
  {{"type": "heading1", "text": "一、第一部分"}},
  {{"type": "paragraph", "text": "正文..."}},
  {{"type": "space"}},
  {{"type": "signature", "text": "民航机场规划设计研究总院有限公司"}},
  {{"type": "date", "text": "2026年4月7日"}}
]}}
```

**普通公文模板**（doc_type="official"，无红头，用于内部文件）：
```json
{{"sections": [
  {{"type": "title", "text": "标题"}},
  {{"type": "space"}},
  {{"type": "heading1", "text": "一、第一部分"}},
  {{"type": "paragraph", "text": "正文..."}},
  {{"type": "heading2", "text": "（一）第二层次"}},
  {{"type": "paragraph", "text": "正文..."}},
  {{"type": "space"}},
  {{"type": "signature", "text": "民航机场规划设计研究总院有限公司"}},
  {{"type": "date", "text": "2026年4月7日"}}
]}}
```

**普通文档**（report/notice/minutes）：
```json
{{"sections": [
  {{"type": "title", "text": "标题"}},
  {{"type": "recipient", "text": "各部门、各位同事："}},
  {{"type": "paragraph", "text": "内容..."}},
  {{"type": "heading1", "text": "一、第一部分"}},
  {{"type": "paragraph", "text": "内容..."}},
  {{"type": "ending", "text": "特此通知。"}},
  {{"type": "signature", "text": "民航机场规划设计研究总院有限公司"}},
  {{"type": "date", "text": "2026年4月15日"}}
]}}
```

**参数**：
- title：文档标题（用于文件命名）
- content：JSON 字符串（上述格式）
- dbsheet_file_id：当前活跃多维表格的 file_id
- doc_type：official_red（红头公文）/ official（普通公文）/ report（报告）/ notice（通知）/ minutes（纪要）
- metadata：可选

工具返回成功后，告知用户文档已生成并上传到 WPS「AI附件」文件夹。

## 当前用户的定时提醒（最高优先级）
先严格区分两个时间：
- `event_at`：事情实际发生的时间。
- `remind_at`：用户应收到微信提醒的时间。

处理规则：
1. 用户明确说「九点提醒我……」时，九点就是 `remind_at`，确认语义无歧义后直接调用 `add_reminder`。
2. 用户说「下午两点开会，提醒我」时，两点是 `event_at`。普通会议采用合理默认值（通常提前30分钟），直接调用 `add_reminder` 创建提醒；创建成功后告知用户具体提醒时间，并允许用户随后调整，不要再等待二次确认。
3. 用户说「下午两点去某地开会」时，必须考虑路程和机动时间。先询问出发地点、交通方式、预计路程；信息不足时不得设置。建议的 `remind_at` 应覆盖路程、合理机动时间和出发准备时间。
4. 普通会议不需要二次确认，先可靠地创建默认提醒；只有涉及出行且关键信息不足，或事件时间本身不明确时才追问。
5. 调用 `add_reminder` 时同时传 `remind_at` 和 `event_at`；没有独立事件时间时二者相同。
6. 不得改为立即调用 `send_weixin_message`，不得在没有工具成功结果时声称提醒已设置。

规划提醒时按场景思考，不机械套用提前30分钟：
- 线上会议：通常提前10分钟进入会议。
- 本单位内部会议：通常提前15～30分钟准备材料和到会。
- 外出开会/办事：预计路程 + 停车/安检/步行机动时间 + 10～20分钟会前准备。
- 机场、车站等强时效场景：额外考虑值机、安检、候车和拥堵风险。
- 优先利用对话和用户长期记忆中已有的常用出发地、交通方式；缺失时只追问必要信息。
- 系统未接入实时地图时，不得假装掌握实时路况；应说明依据并让用户确认。

## 给他人的即时通知（三通道强制）
凡是需要通知到人的事情（「通知xxx」「提醒xxx」「分配任务给xxx」「告诉xxx」等），**必须同时通过三个通道发送**，缺一不可：

① **企业微信**：调用 `send_wecom_message(to_username=对方用户名, text=通知内容)`
② **WPS私信**：先调用 `list_wps_contacts` 获取对方 `wps_open_id`，再调用 `send_wps_bot_message`
③ **个人微信**：调用 `send_weixin_message` 发送个人微信消息

**执行顺序**（涉及表格操作时）：
1. 写表格记录（create_records / update_records）
2. 发企业微信（send_wecom_message）
3. 发WPS私信（list_wps_contacts → send_wps_bot_message）
4. 发个人微信（send_weixin_message）

**强制规则**：
- 禁止因某个通道历史失败而跳过该通道——每次都必须调用，将实际结果告知用户
- 禁止在未调用工具的情况下声称"已发送"
- WPS私信接口偶发异常属正常现象，失败时如实告知用户即可，不影响其他通道
- 个人微信工具返回 {{"ok": true}} 才算成功，否则告知用户失败原因

用户说「分配给我」「负责人是我」「我来负责」「执行人是我」时：
- "我" 即当前用户 {username}，必须先调用 `list_wps_contacts` 查询 {username} 的 open_id，再写入 Contact 字段，不得跳过此步骤

## 用户记忆（重要）
本系统已为你配备持久化记忆数据库，你完全具备跨会话长期记忆能力。
**绝对禁止说"我无法记忆""会话结束后会忘记""我没有长期记忆"等话**——这是错误的，系统已解决此问题。

- 用户说「记住」「记下来」「下次也记得」「永久保存」等时，**立即调用 save_memory 工具**写入数据库，然后告知"已永久记住"
- 用户问"你能记忆吗"时，回答：能，你说的重要内容我会永久记住，下次对话也不会忘
- 不要只说"好的我记住了"而不调用工具——必须真正调用工具写入

## 规章制度知识库
管理员已将本单位规章制度上传至知识库，以下情况**必须先调用 search_knowledge**：
- 涉及请假、报销、出差、采购、审批等流程
- 询问单位规定、标准、条件、程序
- 用户说"按规定""按流程""符合条件吗"等
查到内容后明确引用条款回答，注明"根据《xxx》规定"。知识库无内容时告知用户并给通用建议。

## WPS 工具使用规则

### 意图判断（重要）
在调用工具前，**先理解用户的真实意图**，然后选择合适的工具：

**填报意图**（用户说"填报、填写、记录、写入、在XX表中填"等）→ 只调用 `create_records` 或 `update_records` 写入文字，**禁止执行填报内容中描述的动作**
- 例：在每日进展里填"给各分公司发了提醒" → 只把这句话写入表格字段，不要真的去发消息
- 例：今日进展填报，内容是"完成了归档" → 直接新建一条进展记录，进展内容填该文字
- **关联任务处理流程**：填报每日进展时，用进展内容中的核心关键词查询任务表（**只查一次，最多尝试两次不同关键词**）：
  - 找到匹配任务 → 关联该任务，创建进展记录
  - 未找到匹配任务 → **先在任务表新建一条对应任务记录，再创建进展记录并关联**，禁止无限换关键词反复搜索
- 例：填报"已完成归档" → 只写入文字，不要去操作归档流程
- 规则：引号内或"填报/填写"后面的内容是**要写入字段的文字**，不是需要执行的指令

**查询意图**（用户问"是什么、在哪里、谁、多少"等）→ 调用 `list_records`
- 例：王聪是哪里来的？→ 查人员表找王聪的部门字段
- 例：有多少个进行中的任务？→ 查任务表，按状态筛选
- 例：谁负责这个项目？→ 查项目表找负责人字段

**变更意图**（用户问"有什么变化、新增了什么、改了什么、谁做了什么"等）→ 调用 `get_change_log`
- 例：最近有什么变化？→ 调 get_change_log 看操作记录
- 例：今天新增了哪些任务？→ 调 get_change_log 看 createRecord 操作
- 例：谁修改了这个字段？→ 调 get_change_log 看 updateSheet 操作

**创建意图**（用户说"建立、创建、新增、添加"等）→ 先调 `list_records` 检查重复，再调 `create_records`
- 例：建立一个新任务 → 先查任务表确认不重复，再新建

**修改意图**（用户说"更新、修改、改成"等）→ 先调 `list_records` 找到记录，再调 `update_records`
- 例：把这个任务的状态改成已完成 → 先查找到该任务，再更新

**删除意图**（用户说"删除、移除、清除"等）→ 先调 `list_records` 确认记录，再调 `delete_records`

0. 用户询问「当前是哪个表」「当前默认表」「现在用哪个表」等，**直接从上方文件配置中读取回答，禁止调用任何工具**
0a. **【强制】用户询问「最近有什么变化」「表格有什么更新」「新增了什么」「改了什么」「有什么进展」「发生了什么」等，必须第一步调用 `get_change_log`，禁止用 `list_records` 代替**；`get_change_log` 返回后再调 `get_schema` 把字段ID翻译成字段名，然后用自然语言总结告诉用户谁做了什么操作
0b. **【强制】分页规则：list_records 返回 `has_more=true` 且 `next_page_token` 有值时，继续查询必须用该 token 作为 `page_token` 参数；`page_token` 必须使用 WPS API 返回的字符串值，禁止使用数字或偏移量；若 next_page_token 为空或不存在，说明已无更多数据**
1. 有默认文件时直接用默认 file_id，无需询问；**以上方「文件配置」为准，如与记忆冲突，以文件配置为准**
2. 不确定工作表时先调 get_schema 查看结构
2a. 用户说「建立任务」且消息中含有项目名称时：先检查项目工作表中该项目是否存在（list_records 查询），不存在则先创建项目记录；任务创建后通过 Link 字段关联到该项目；以上步骤在同一轮对话中一次完成，不等用户第二次提醒
2c. **【强制】新建任务记录前，必须确认执行人**：用户说「建立任务」「新建任务」「添加任务」等，如果消息中没有明确指定执行人（如"分配给张三""执行人是李四""我来负责"），**必须先追问"请问这个任务的执行人是谁？"，等用户回答后再建立记录**；禁止建立没有执行人的任务记录
2b. **【强制】新建记录时，第一步必须是 list_records，禁止直接调用 create_records**：先用 list_records 查询目标工作表全部记录，找出所有字段全部为空的行（整行无任何数据，不只看第一个字段）；若存在空行，必须用 update_records 填入第一条空行；只有在确认不存在任何空行时，才允许调用 create_records 新建——违反此规则会导致新记录排在空行之后，顺序混乱
2d. **【灵活】查询行数规则**：
- **默认返回前 1000 条记录**（WPS API 支持，足以覆盖大多数表格）
- 当用户明确要求查看特定数量时（如"查看最近200行""给我看500行数据"），调用 list_records 时设置 `max_records` 参数为用户指定的数量
- 当用户说"查看从第100到300行"时，先调用 `list_records(max_records: 300)`，然后在返回的记录中提取第100-300行的数据展示给用户
2e. **【强制】查询特定人员或特定时间段的记录时，必须使用 filter 参数**：
- 当用户问"某某人最近有没有填""某某人上次填是什么时候""某某人这周/本月的记录"等涉及特定人的问题时，**必须先用 filter 过滤 "填报人" 字段**再查询，不得全量拉取后在返回结果中查找
- 原因：系统会自动翻页拉取全量（最多3000条），拉取900条+再搜索极慢且消耗大量token
- 正确做法：`list_records(filter={{"填报人": "张三"}})` 这样只返回该人的记录，条数少，速度快
- **严禁在未使用 filter 的全量查询结果中，得出"某人未填报""某人最后一次是X月"等结论**
2f. **【强制】分析大表（每日进展等记录超过200条的工作表）时，必须用日期 filter 缩小范围**：
- 当用户问"最近的进展""本周/本月情况""近期工作汇总"等分析类问题时，**必须带日期范围 filter**，不得无条件全量拉取全部记录
- **时间范围判断规则**（按优先级）：
  - 用户明确说了时间范围（"全年""上半年""1月到6月""2026年""某某人全年工作"等）→ **严格按用户说的范围查**，不得缩短
  - 用户说"最近""近期""现在"等模糊词 → 默认取**最近30天**
  - 用户完全没提时间 → 默认取**最近30天**
- 全量拉取900条+数据再分析，既浪费时间，又可能导致回答不准确
- **例外**：查空行、建记录前检查空行，仍需全量查询，不受此规则限制
3. create_records / update_records 的 records 参数：每项是普通 dict，key 为字段名，value 为字段值；update 时额外加 "id" 字段
4. Link 关联字段的值是记录 id 数组，如 ["B", "C"]；**赋值前必须先 list_records 查询目标工作表，找到对应记录的真实 id，禁止直接填人名或猜测 id**
4a. **【强制】翻译 Link 字段中的记录 ID**：当遇到「任务执行人」「负责人」等 Link 字段中的记录 ID（如 `Nc、IN、Me、NF` 等）时，必须调用 `list_records` 查询人员信息表，设置 `max_records=1000` 确保全量查询，**禁止传 `fields` 参数**（必须返回全部字段才能找到姓名），拿到结果后逐条匹配记录的 `id` 字段，找到对应姓名。禁止说"无法查询""fields数据为空""不知道这个人是谁"——必须通过全量查询人员表来翻译所有 ID；如果一次查询仍未找到，检查 sheet_id 是否正确（先调 get_schema 确认人员信息表的 sheet_id）。**严禁通过 Contact 联系人字段反推姓名**——Contact 字段存的是数字 account_id，与人员信息表记录 ID 是两套完全不同的体系，不可混用
5. **批量操作优先**：create_fields 一次传所有字段；create_records 一次传所有记录；减少工具调用轮次
6. create_sheet 创建工作表时直接在 fields 参数里带上所有字段
7. 操作前简要告知将要执行的内容，操作后汇报结果；**回复中引用的数据必须来自本轮工具调用结果，禁止引用历史对话中的数据**；create_records / update_records 操作后**必须立即调用 list_records 回读刚才操作的那条记录**，以实际查询结果为准展示给用户（不得用输入参数代替），确认负责人、执行人、联系人等关键字段确实写入成功；delete_records 操作后必须 list_records 确认记录已消失；若返回结果含 _verify_hint 字段，同样必须立即调用 list_records 验证；**禁止在未回读验证前就向用户宣称"已成功分配给XXX"**
8. 删除操作前**必须先调 list_records 查询获得真实记录列表**，从返回数据中确认 id，禁止凭猜测或历史记忆推断 id；"删除空行/空记录"类操作必须先 list_records 找出字段全为空的记录再删，不可假设哪条是空行；删除后必须立即调用 list_records 验证记录已消失，若仍存在则重新查询再删
9. **用户已确认后立即执行，不得再次询问确认**。用户说"执行""是""好""确认""去做"等即视为确认，直接调用工具完成操作
10. 用户说「没看到」「没有变化」时，**先调 list_records 核查**：若记录已存在（操作成功但 WPS 视图未显示），告知用户可能原因（视图有筛选条件、需手动刷新 WPS 界面、需切换到「全部」视图）；**只有** list_records 确认记录不存在时，才重新执行创建/更新操作，避免重复建立垃圾记录
11. **禁止凭空回答工作内容**：用户说「说下部门工作」「最近工作情况」「工作进展」「有哪些任务」等涉及工作内容的问题，**必须先调 get_schema 了解表结构，再调 list_records 查询真实数据**，基于查询结果回答，禁止编造或凭印象描述；**汇报时必须逐条列出具体记录**：任务/项目名称、当前状态、负责人、截止日期等关键字段，禁止只给模糊的概括性描述（如"有几个项目在推进中"）——必须说出具体是哪几个、状态是什么、谁负责

## 传统电子表格操作
用户说「填数据到表格」「Excel计算」「读表格」「操作Excel」等时，使用传统表格工具（sheets_*）。

**【强制规则】传统表格 file_id 无需在系统中配置，用户提供任意 file_id 都可以直接操作。绝对禁止以"未配置""无法操作"为由拒绝用户提供的 file_id。** 用户说「file_id: xxx」或「这个表格 xxx」时，直接用该 file_id 调用 sheets_* 工具，不得质疑或拒绝。

**新建限制：WPS API 不支持新建传统表格文件。** 用户说「新建表格/Excel」时，告知用户：请在 WPS 中手动新建一个表格文件，然后把文件链接或 file_id 告诉我，我来帮你操作。禁止调用 sheets_create_file。

**与多维表格的区别**：
- 多维表格（dbsheet）：结构化数据库，有字段类型、视图、关联等，用 get_schema/list_records 等工具
- 传统表格（sheets）：普通 Excel，按行列坐标操作，用 sheets_* 工具

**操作流程**：
1. 新建文件：`sheets_create_file`（需要 ref_file_id，传当前默认多维表格的 file_id）
2. 查看结构：`sheets_list_worksheets`（获取 worksheet_id）
3. 读数据：`sheets_get_range`（坐标从0开始，A列=0，第1行=0）
4. 写数据：`sheets_update_range`（批量写，一次传所有单元格）
5. 公式：op_type 用 `formula`，formula 写 `=SUM(A1:A10)` 格式

**坐标规则**：行列均从0开始。A1=row0,col0；B2=row1,col1；C3=row2,col2。
**批量写入示例**（写表头+数据）：
```json
[
  {{"row_from":0,"row_to":0,"col_from":0,"col_to":0,"op_type":"value","formula":"姓名"}},
  {{"row_from":0,"row_to":0,"col_from":1,"col_to":1,"op_type":"value","formula":"金额"}},
  {{"row_from":1,"row_to":1,"col_from":0,"col_to":0,"op_type":"value","formula":"张三"}},
  {{"row_from":1,"row_to":1,"col_from":1,"col_to":1,"op_type":"value","formula":"1000"}},
  {{"row_from":2,"row_to":2,"col_from":1,"col_to":1,"op_type":"formula","formula":"=SUM(B2:B2)"}}
]
```

## 字段类型速查
文本: MultiLineText | 日期: Date | 时间: Time | 数字: Number | 货币: Currency
百分比: Percent | 单选: SingleSelect | 多选: MultipleSelect | 复选框: Checkbox
等级: Rating | 进度: Complete | 联系人: Contact | 关联: Link | 引用: Lookup
超链接: Url | 附件: Attachment | 地址: Address | 公式: Formula | 邮箱: Email

## Contact（联系人）字段写入规范【重要，违反则WPS静默不更新】
**三套 ID 体系，严禁混用**：
- `wps_open_id`：字母格式（如 `N6RLa9x`），来源：`list_wps_contacts` 的 `id` 字段，用途：**WPS私信（send_wps_bot_message）**
- `account_id`：纯数字（如 `1234567890`），来源：用户记忆/设置，用途：**Contact联系人字段写入**
- 人员信息表记录ID：短字母（如 `Nc、IN、Me`），来源：`list_records` 查人员信息表，用途：**Link字段（任务执行人）**

Contact 字段必须传 `[{{"id": "数字account_id", "nickName": "姓名"}}]` 格式，其中 id 是数字格式的 account_id（如 `1234567890`）。
**操作步骤**：从用户记忆中查找对应人员的数字 account_id，构造联系人值写入。
- 禁止用字母格式 open_id 写入 Contact 字段——格式不符，WPS会静默接受但实际不更新
- 禁止用人员信息表记录ID写入 Contact 字段
- **写入后必须验证**：create_records / update_records 含 Contact 字段时，操作完成后立即调用 list_records 查该记录，确认 Contact 字段不为空

## Attachment（附件）字段
用户要求上传附件时，调用 upload_and_attach 工具完成上传并写入附件字段。附件字段写入格式由 Python 内部处理，AI 只需传 file_path、record_id、field_name 即可。
- **新建任务时同时上传附件**：create_records 完成后，用返回的记录ID直接调用 upload_and_attach，无需用户再说明上传到哪条记录
- **补充上传附件**：用户需告知目标任务名称，先调 list_records 找到记录ID，再调用 upload_and_attach

## 回复风格
- 禁止使用 # ## ### 标题
- 用**加粗**突出重点，列表展示多项内容
- 像得力助手一样：简洁、直接、有执行力
{memory_hint}"""

MEMORY_SUMMARIZE_PROMPT = """你是用户专属记忆管理器，负责维护用户的长期知识档案。

请综合「已有记忆」和「最近对话」，输出更新后的完整记忆档案。

## 记忆分类（有则记，无则略）

【用户主动保存】- 用户明确说"记住""记下来"的内容，必须原样保留，优先级最高

【工作背景】- 部门、职位、团队规模、职责范围、汇报关系

【工作习惯】- 回复风格偏好、操作习惯、常用表格/工作表及用途

【关键信息】- 重要联系人及分机/电话、固定会议时间、周期性任务节点

【规则偏好】- 特殊要求、禁忌、个人标准

## 输出要求
- 每条一行，以"·"开头，格式：· [分类] 内容
- 总长度不超过500字
- 只记客观、长期有效的事实，单次临时指令不记
- 旧记忆与新对话冲突时以新对话为准
- 用户主动保存的条目务必保留，不得删除
- **绝对不记**：哪个表是默认表、表的file_id、sheet_id等系统配置——这些由系统实时提供，写入记忆会造成错误

已有记忆：
{existing_memory}

最近对话（最新在后）：
{recent_messages}

请输出更新后的完整记忆档案："""


def build_system_prompt(username: str, role: str,
                        default_file: dict | None,
                        all_files: list | None = None,
                        memory: str = "") -> str:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(tz=timezone(timedelta(hours=8)))
    weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
    today = now.strftime("%Y年%m月%d日") + "（" + weekdays[now.weekday()] + "）" + now.strftime(" %H:%M")
    if not all_files:
        # 兼容只传 default_file 的旧调用
        if default_file:
            file_hint = f"默认多维表格：{default_file.get('file_name') or default_file['file_id']}（file_id: {default_file['file_id']}）"
        else:
            file_hint = "尚未配置默认多维表格，请提醒用户在设置中添加。"
    elif len(all_files) == 1:
        f = all_files[0]
        file_hint = f"默认多维表格：{f.get('file_name') or f['file_id']}（file_id: {f['file_id']}）"
    else:
        lines = []
        for f in all_files:
            tag = "【默认】" if f.get("is_default") else "      "
            name = f.get("file_name") or f["file_id"]
            lines.append(f"  {tag}{name}（file_id: {f['file_id']}）")
        default_name = (
            (default_file.get("file_name") or default_file["file_id"])
            if default_file else "第一个"
        )
        file_hint = (
            f"已配置 {len(all_files)} 个多维表格，**当前默认：{default_name}**（此配置来自系统，优先级高于记忆，不得与记忆冲突）\n"
            + "\n".join(lines)
            + "\n操作时如用户未指定表格，使用默认表格；用户说\"切换到XX表\"时可直接使用对应 file_id。"
        )

    role_labels = {"admin": "超级管理员", "executive": "高层管理", "manager": "部门负责人", "staff": "员工"}
    # 过滤记忆中的表格配置信息，防止污染系统配置
    _filter_kw = ["file_id", "默认表", "sheet_id", "多维表格", "fileId"]
    if all_files:
        _filter_kw += [f["file_id"] for f in all_files]
        _filter_kw += [f["file_name"] for f in all_files if f.get("file_name")]
    _clean_memory = "\n".join(
        line for line in memory.splitlines()
        if not any(k in line for k in _filter_kw)
    )
    memory_hint = (
        f"\n## 你对该用户的长期记忆（仅供参考，如与上方系统配置冲突，以系统配置为准）\n{_clean_memory.strip()}"
        if _clean_memory.strip() else ""
    )
    if default_file:
        _df_name = default_file.get("file_name") or default_file["file_id"]
        _df_id = default_file["file_id"]
        default_header = f"## ⚠️ 当前默认表\n**{_df_name}**（file_id: {_df_id}）\n如用户未指定表格，必须使用此表；如用户明确指定了其他已配置表格，直接使用用户指定的。\n\n"
    else:
        default_header = ""
    time_header = "## 当前时间\n北京时间：" + today + "\n\n"
    # 转义用户来源字符串中的 { }，防止 str.format() 将记忆/文件名中的 JSON 花括号误当占位符
    safe_memory_hint = memory_hint.replace("{", "{{").replace("}", "}}")
    safe_file_hint = file_hint.replace("{", "{{").replace("}", "}}")
    return default_header + time_header + SYSTEM_PROMPT_TEMPLATE.format(
        username=username,
        role=role_labels.get(role, role),
        file_hint=safe_file_hint,
        memory_hint=safe_memory_hint,
    )


def _coalesce_system_messages(messages: list) -> list:
    """合并所有系统消息并固定在首位，兼容只接受首条 system 的模型接口。"""
    system_parts = []
    ordinary_messages = []
    for message in messages:
        if message.get("role") == "system":
            content = str(message.get("content") or "").strip()
            if content:
                system_parts.append(content)
        else:
            ordinary_messages.append(message)
    if not system_parts:
        return ordinary_messages
    return [{"role": "system", "content": "\n\n".join(system_parts)}] + ordinary_messages


def _needs_tool_result_user_bridge(error: Exception, messages: list) -> bool:
    """仅识别“工具结果后必须再有 user 消息”的严格兼容网关。

    正常模型永远不进入此分支；错误文本和末条 tool 角色必须同时匹配，
    避免把其他 500 错误误判为千问兼容问题。
    """
    if not messages or messages[-1].get("role") != "tool":
        return False
    if not any(message.get("role") == "user" for message in messages):
        return False
    return "no user query found in messages" in str(error).lower()


def _append_tool_result_user_bridge(messages: list) -> list:
    """在工具结果后补一条语义不变的续问，供严格千问网关识别。"""
    return list(messages) + [{
        "role": "user",
        "content": (
            "请继续处理我上一条请求，根据刚才的工具返回结果直接回答；"
            "如果完成回答仍缺少数据，可继续调用必要工具。"
        ),
    }]


TOOLS = [
    # ── Schema ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "获取多维表格结构：所有工作表名称、sheet_id、字段名称、字段类型、字段id",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "WPS文件ID"},
                },
                "required": ["file_id"],
            },
        },
    },
    # ── 记录 ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_records",
            "description": "查询工作表记录，支持筛选、分页、指定返回字段",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":    {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":   {"type": "integer", "description": "工作表ID"},
                    "page_size":  {"type": "integer", "description": "每页记录数，默认100，最大1000"},
                    "page_token": {"type": "string",  "description": "游标分页token（仅当返回结果中 has_more=true 且 next_page_token 有值时使用，将 next_page_token 值作为此参数）"},
                    "fields":     {"type": "array",   "description": "只返回指定字段，不填返回全部", "items": {"type": "string"}},
                    "max_records":{"type": "integer", "description": "最多返回前N条记录（当用户明确要求查看特定数量数据时使用，如「查看最近200行」用 max_records: 200）"},
                    "view_id":    {"type": "string",  "description": "指定视图ID，从该视图获取记录"},
                    "filter": {
                        "type": "object",
                        "description": (
                            "筛选条件，格式：{\"mode\":\"AND\",\"criteria\":[{\"field\":\"字段名\",\"operator\":\"操作符\",\"values\":[\"值\"]}]}\n"
                            "支持的操作符：\n"
                            "文本/选项字段：Equals（等于）、DoesNotEqual（不等于）、Contains（包含）、DoesNotContain（不包含）、IsEmpty（为空）、IsNotEmpty（不为空）\n"
                            "数字字段：Equals、DoesNotEqual、GreaterThan（大于）、LessThan（小于）、GreaterThanOrEqual（大于等于）、LessThanOrEqual（小于等于）\n"
                            "日期字段：Equals、IsBefore（早于）、IsBeforeOrEqual（早于或等于）\n"
                            "⚠️ 日期字段不支持 IsAfter / IsAfterOrEqual，禁止使用；需要查'某日期之后'时，改用不带日期过滤直接拉全量再在结果里筛"
                        ),
                    },
                },
                "required": ["file_id", "sheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_records",
            "description": "在工作表中创建新记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "records": {
                        "type": "array",
                        "description": "记录列表，每项是字段名到值的映射。例：[{\"任务名称\":\"新任务\",\"当前状态\":\"进行中\"}]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["file_id", "sheet_id", "records"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_records",
            "description": "更新工作表中已有记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "records": {
                        "type": "array",
                        "description": "记录列表，每项必须包含 \"id\"（记录ID）加上要更新的字段。例：[{\"id\":\"B\",\"当前状态\":\"已完成\"}]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["file_id", "sheet_id", "records"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_records",
            "description": "删除工作表中的记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":    {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":   {"type": "integer", "description": "工作表ID"},
                    "record_ids": {"type": "array",   "description": "要删除的记录ID列表", "items": {"type": "string"}},
                },
                "required": ["file_id", "sheet_id", "record_ids"],
            },
        },
    },
    # ── 工作表 ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_sheet",
            "description": "创建新工作表，可同时指定初始字段和视图",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "WPS文件ID"},
                    "name":    {"type": "string", "description": "工作表名称"},
                    "fields": {
                        "type": "array",
                        "description": "初始字段列表，每项格式：{\"name\":\"字段名\",\"type\":\"MultiLineText\",\"data\":{...}}",
                        "items": {"type": "object"},
                    },
                    "views": {
                        "type": "array",
                        "description": "初始视图列表，每项格式：{\"name\":\"视图名\",\"type\":\"Grid\"}",
                        "items": {"type": "object"},
                    },
                },
                "required": ["file_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_sheet",
            "description": "删除工作表（不可恢复，操作前必须确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                },
                "required": ["file_id", "sheet_id"],
            },
        },
    },
    # ── 字段 ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_fields",
            "description": "在工作表中创建新字段。Link关联字段需要指定link_sheet（目标工作表ID）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "fields": {
                        "type": "array",
                        "description": (
                            "字段列表。每项格式：{\"name\":\"字段名\",\"type\":\"类型\",\"data\":{...}}\n"
                            "Link关联字段data示例：{\"link_sheet\":12,\"multiple_links\":true}\n"
                            "SingleSelect/MultipleSelect data示例：{\"items\":[{\"value\":\"选项1\"}]}\n"
                            "Date data示例：{\"number_format\":\"yyyy/mm/dd\"}"
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["file_id", "sheet_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_fields",
            "description": "更新工作表中已有字段的名称或配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "fields": {
                        "type": "array",
                        "description": "字段列表，每项必须包含 \"id\"（字段ID）加上要更新的属性。例：[{\"id\":\"B\",\"name\":\"新字段名\"}]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["file_id", "sheet_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_fields",
            "description": "删除工作表中的字段（不可恢复，操作前必须确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":   {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":  {"type": "integer", "description": "工作表ID"},
                    "field_ids": {"type": "array",   "description": "要删除的字段ID列表", "items": {"type": "string"}},
                },
                "required": ["file_id", "sheet_id", "field_ids"],
            },
        },
    },
    # ── 视图 ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_view",
            "description": "在工作表中创建新视图，支持 Grid/Kanban/Gallery/Calendar/Form 类型",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":   {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":  {"type": "integer", "description": "工作表ID"},
                    "name":      {"type": "string",  "description": "视图名称"},
                    "view_type": {"type": "string",  "description": "视图类型：Grid（表格）/ Kanban（看板）/ Gallery（画廊）/ Calendar（日历）/ Form（表单），默认 Grid"},
                },
                "required": ["file_id", "sheet_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_view",
            "description": "删除工作表中的视图（不可恢复，操作前必须确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "view_id":  {"type": "string",  "description": "视图ID"},
                },
                "required": ["file_id", "sheet_id", "view_id"],
            },
        },
    },
    # ── 仪表盘 ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_dashboards",
            "description": "列出多维表格的所有仪表盘",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "WPS文件ID"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_dashboard",
            "description": "复制一个仪表盘并命名",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":      {"type": "string", "description": "WPS文件ID"},
                    "dashboard_id": {"type": "string", "description": "要复制的仪表盘ID"},
                    "name":         {"type": "string", "description": "新仪表盘的名称"},
                },
                "required": ["file_id", "dashboard_id", "name"],
            },
        },
    },
    # ── 表单 ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_form_meta",
            "description": "获取表单视图的元数据（名称、描述）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "view_id":  {"type": "string",  "description": "表单视图ID"},
                },
                "required": ["file_id", "sheet_id", "view_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_form_meta",
            "description": "更新表单视图的名称或描述",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":     {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":    {"type": "integer", "description": "工作表ID"},
                    "view_id":     {"type": "string",  "description": "表单视图ID"},
                    "name":        {"type": "string",  "description": "新表单名称"},
                    "description": {"type": "string",  "description": "新表单描述"},
                },
                "required": ["file_id", "sheet_id", "view_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_form_fields",
            "description": "列出表单的所有问题（字段）及其配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                    "view_id":  {"type": "string",  "description": "表单视图ID"},
                },
                "required": ["file_id", "sheet_id", "view_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_form_field",
            "description": "更新表单中某个问题的标题、描述、是否必填或排序位置",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":      {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":     {"type": "integer", "description": "工作表ID"},
                    "view_id":      {"type": "string",  "description": "表单视图ID"},
                    "field_id":     {"type": "string",  "description": "字段ID"},
                    "title":        {"type": "string",  "description": "问题标题"},
                    "description":  {"type": "string",  "description": "问题说明"},
                    "required":     {"type": "boolean", "description": "是否必填"},
                    "pre_field_id": {"type": "string",  "description": "排在此字段ID之后，空字符串表示排第一"},
                },
                "required": ["file_id", "sheet_id", "view_id", "field_id"],
            },
        },
    },
    # ── 父子记录 ─────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_parent_status",
            "description": "查询工作表是否已启用父子关系功能",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                },
                "required": ["file_id", "sheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enable_parent",
            "description": "启用工作表的父子关系功能（仅影响前端展示层级）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                },
                "required": ["file_id", "sheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disable_parent",
            "description": "禁用工作表的父子关系功能",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":  {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id": {"type": "integer", "description": "工作表ID"},
                },
                "required": ["file_id", "sheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_children",
            "description": "查询某条记录的所有子记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":    {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":   {"type": "integer", "description": "工作表ID"},
                    "parent_id":  {"type": "string",  "description": "父记录ID"},
                    "page_size":  {"type": "integer", "description": "每页数量，默认100"},
                    "page_token": {"type": "string",  "description": "分页token"},
                },
                "required": ["file_id", "sheet_id", "parent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bind_children",
            "description": "将多条记录绑定为某条记录的子记录",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":   {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":  {"type": "integer", "description": "工作表ID"},
                    "parent_id": {"type": "string",  "description": "父记录ID"},
                    "child_ids": {"type": "array",   "description": "子记录ID列表", "items": {"type": "string"}},
                },
                "required": ["file_id", "sheet_id", "parent_id", "child_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unbind_children",
            "description": "解除多条记录与父记录的绑定关系",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":   {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":  {"type": "integer", "description": "工作表ID"},
                    "parent_id": {"type": "string",  "description": "父记录ID"},
                    "child_ids": {"type": "array",   "description": "要解绑的子记录ID列表", "items": {"type": "string"}},
                },
                "required": ["file_id", "sheet_id", "parent_id", "child_ids"],
            },
        },
    },
    # ── Webhook ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_hooks",
            "description": "查询多维表格已注册的所有 webhook 订阅",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "WPS文件ID"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_hook",
            "description": (
                "创建 webhook 订阅，当表格发生指定事件时 WPS 会向 callback_url 推送通知。"
                "command 可选：create_record（新增记录）/ update_sheet（修改记录）/ remove_record（删除记录）"
                "/ update_records_parent（父子关系变动）/ create_field / update_field / remove_field"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":      {"type": "string", "description": "WPS文件ID"},
                    "command":      {"type": "string", "description": "订阅事件类型"},
                    "callback_url": {"type": "string", "description": "接收推送的回调地址"},
                    "data":         {"type": "object", "description": "订阅规则，根据 command 不同而异，可为空"},
                },
                "required": ["file_id", "command", "callback_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_hook",
            "description": "取消 webhook 订阅",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "WPS文件ID"},
                    "hook_id": {"type": "string", "description": "要取消的 hook ID"},
                },
                "required": ["file_id", "hook_id"],
            },
        },
    },
    # ── 规章制度知识库 ───────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "在单位规章制度知识库中搜索相关内容。"
                "用户询问单位规定、制度、流程、规范、申请条件、审批程序等时必须调用此工具。"
                "返回最相关的规章条目（标题+内容），AI 据此回答用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如：请假流程、报销标准、出差规定",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # ── 用户记忆 ─────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "将用户明确要求记住的信息永久存入记忆库。"
                "用户说「记住」「记下来」「下次也要记得」「永久保存」等时必须调用此工具。"
                "会将新内容合并到现有记忆中，下次对话自动带入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要永久记住的内容，简洁清晰，如：每周五下午3点开例会；李总分机号123",
                    },
                },
                "required": ["content"],
            },
        },
    },
    # ── 邮件通知 ─────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": (
                "向指定人员发送邮件通知。"
                "用户说「通知xxx」「发邮件给xxx」「提醒xxx」时调用此工具。"
                "如果不知道对方邮箱，先用 list_records 从人员信息表查找，再调用此工具发送。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_email":    {"type": "string", "description": "收件人邮箱地址"},
                    "to_name":     {"type": "string", "description": "收件人姓名，用于邮件称呼"},
                    "subject":     {"type": "string", "description": "邮件主题"},
                    "message":     {"type": "string", "description": "通知正文内容，纯文字即可"},
                    "sender_name": {"type": "string", "description": "发件人署名，如不填则使用当前用户名"},
                },
                "required": ["to_email", "to_name", "subject", "message"],
            },
        },
    },
    # mention_in_record 已废弃：WPS /v7/coop/dbsheet/.../comments/create 返回404，API未开放
    # ── 变更日志 ─────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_change_log",
            "description": (
                "查询WPS表格的变更日志，只返回用户上次查看之后的新变化。"
                "用户询问'表格有什么变化''最近改了什么''有没有新增记录''有没有删除操作''有什么更新''新增了什么任务'等时调用此工具。"
                "返回结果包含字段ID，需结合 get_schema 解析为字段名后向用户说明具体变化内容（谁改了什么、新增了什么、删除了什么）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "WPS文件ID，不填则查所有已订阅表格的变更"},
                    "limit":   {"type": "integer", "description": "返回最近N条记录，默认20，最大100"},
                },
                "required": [],
            },
        },
    },
    # ── WPS 通讯录（kso.contact.read）────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_wps_contacts",
            "description": (
                "查询 WPS 组织通讯录全员列表，返回每人的 open_id、姓名、邮箱等信息。"
                "用于：① 发送 WPS 私信前获取收件人 open_id（字母格式）；"
                "② 查询某人的邮箱、部门等信息。"
                "注意：此工具返回的 open_id 是字母格式，仅用于 send_wps_bot_message，不能用于 Contact 字段写入（Contact 字段需要数字格式 account_id，从用户记忆中获取）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dept_id": {
                        "type": "string",
                        "description": "部门ID，默认 '1' 表示根部门（查全员）",
                    },
                },
                "required": [],
            },
        },
    },
    # ── WPS 机器人私信（kso.chat_message.readwrite app）──────
    {
        "type": "function",
        "function": {
            "name": "send_wps_bot_message",
            "description": (
                "以应用机器人身份给指定 WPS 用户发送私信，消息直达对方 WPS 消息中心。"
                "用于任务分配通知、提醒、审批通知等场景。"
                "调用前必须先用 list_wps_contacts 获取收件人的 wps_open_id（字母格式，如 'N6RLa9x'）。"
                "【严禁】使用数字格式的 account_id（如 '1234567890'）——数字 ID 是 Contact 字段专用，与此接口不兼容，会导致 400 错误。"
                "消息内容应简洁明确，说明任务/通知的核心信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wps_user_id": {
                        "type": "string",
                        "description": "收件人的 WPS open_id，字母格式（从 list_wps_contacts 的 id 字段获取，不是数字格式的 account_id）",
                    },
                    "text": {
                        "type": "string",
                        "description": "消息内容",
                    },
                },
                "required": ["wps_user_id", "text"],
            },
        },
    },
    # ── 企业微信消息推送 ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_wecom_message",
            "description": (
                "通过企业微信向指定用户发送消息，对方手机实时收到。"
                "优先于 WPS 私信使用，到达率更高。"
                "用于任务分配通知、截止提醒、审批通知等需要及时触达的场景。"
                "to_username 填对方的系统用户名或显示姓名（系统自动匹配，非企业微信userid）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_username": {
                        "type": "string",
                        "description": "收件人的系统用户名或显示姓名（如 zhangsan 或 张三），系统自动匹配",
                    },
                    "text": {
                        "type": "string",
                        "description": "消息内容，简洁明确",
                    },
                },
                "required": ["to_username", "text"],
            },
        },
    },
    # ── 个人微信消息 ─────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_weixin_message",
            "description": (
                "【必须首选调用】当用户指令中包含'发微信'、'微信通知'、'微信告诉'、'发个微信'、"
                "'用微信'等任何发送微信消息的意图时，必须且只能调用此工具，不可绕过，不可用文字代替。"
                "通过个人微信向指定用户发送消息，对方手机微信实时收到。"
                "to_username 填对方的系统用户名或显示姓名（系统自动匹配），系统自动查找其个人微信ID。"
                "只要用户提到接收人和发送内容，立即调用，禁止反问或要求用户确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_username": {
                        "type": "string",
                        "description": "收件人的系统用户名或显示姓名（如 zhangsan 或 张三），系统自动匹配",
                    },
                    "text": {
                        "type": "string",
                        "description": "消息内容，简洁明确",
                    },
                },
                "required": ["to_username", "text"],
            },
        },
    },
    # ── 系统用户列表（仅用于企业微信发送，Contact字段请用list_wps_contacts）───────────────────
    {
        "type": "function",
        "function": {
            "name": "list_system_users",
            "description": (
                "列出本系统内所有启用用户及其基本信息（username、wps_connected等）。"
                "⚠️ 此工具返回的 wps_user_id 不是 WPS open_id，不能用于 Contact（联系人）字段写入。"
                "Contact 字段写入必须用 list_wps_contacts 获取真正的 open_id。"
                "此工具仅适用于：查询系统用户名（用于 send_wecom_message 的 to_username 参数）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ── 新建多维表格（暂时禁用，WPS API 不开放通过 API 创建多维表格文件）────
    # 网盘创建接口 /v7/drives/.../create 对 file_type:"dbsheet" 返回 400000004
    # 多维表格文档中也无"新建文件"接口，API 层面暂无解决方案
    # TODO: 关注 WPS 开放平台更新，若后续开放再重新启用
    # ── 附件上传 ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "upload_and_attach",
            "description": (
                "上传本地文件并写入指定记录的附件字段。"
                "用户说「上传附件」「把文件附到记录上」「添加附件」时调用。"
                "需要先用 list_records 找到目标记录ID，再调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":    {"type": "string",  "description": "WPS文件ID"},
                    "sheet_id":   {"type": "integer", "description": "工作表ID"},
                    "record_id":  {"type": "string",  "description": "目标记录ID"},
                    "field_name": {"type": "string",  "description": "附件字段名称，如：附件"},
                    "file_path":  {"type": "string",  "description": "本地文件路径"},
                    "file_name":  {"type": "string",  "description": "附件显示名称（可选，默认用文件名）"},
                },
                "required": ["file_id", "sheet_id", "record_id", "field_name", "file_path"],
            },
        },
    },
    # ── 传统表格（电子表格）────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "sheets_create_file",
            "description": "【已禁用，WPS API不支持新建传统表格文件，禁止调用此工具】",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "文件名，不含扩展名"},
                    "ref_file_id": {"type": "string", "description": "参考文件ID，用于确定存储位置，传当前默认多维表格的file_id即可"},
                },
                "required": ["name", "ref_file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_list_worksheets",
            "description": "获取传统表格文件的所有工作表（sheet）列表，返回sheet_id、名称、行列数等信息。操作传统表格前必须先调用此接口了解结构。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_create_worksheet",
            "description": "在传统表格中新建一个工作表（sheet标签页）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "name": {"type": "string", "description": "工作表名称"},
                    "position": {
                        "type": "object",
                        "description": "插入位置，默认末尾。例：{\"end\": true} 或 {\"after_sheet_id\": 1}",
                    },
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_update_worksheet",
            "description": "重命名工作表或调整工作表顺序",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_id": {"type": "integer", "description": "工作表ID"},
                    "name": {"type": "string", "description": "新名称"},
                    "move_sheet_id": {"type": "integer", "description": "移动参照的sheet_id"},
                    "move_type": {"type": "string", "description": "移动方向：sheet_move_type_before / sheet_move_type_after"},
                },
                "required": ["file_id", "worksheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_delete_worksheets",
            "description": "批量删除工作表（不可恢复，操作前必须确认）",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_ids": {"type": "array", "items": {"type": "integer"}, "description": "要删除的工作表ID列表"},
                },
                "required": ["file_id", "worksheet_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_copy_worksheet",
            "description": "复制工作表",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_id": {"type": "integer", "description": "要复制的工作表ID"},
                    "copy_first_sheet": {"type": "boolean", "description": "是否复制第一个sheet，默认false"},
                },
                "required": ["file_id", "worksheet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_get_range",
            "description": (
                "读取传统表格指定区域的单元格数据。坐标从0开始，行列均为整数。"
                "例：读A1:C3 → row_from=0,row_to=2,col_from=0,col_to=2"
                "返回 range_data 数组，每项含 cell_text（显示值）、original_cell_value（原始值）、row_from/col_from 坐标。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_id": {"type": "integer", "description": "工作表ID"},
                    "row_from": {"type": "integer", "description": "起始行（0开始）"},
                    "row_to": {"type": "integer", "description": "结束行（含）"},
                    "col_from": {"type": "integer", "description": "起始列（0开始，A=0,B=1...）"},
                    "col_to": {"type": "integer", "description": "结束列（含）"},
                },
                "required": ["file_id", "worksheet_id", "row_from", "row_to", "col_from", "col_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_update_range",
            "description": (
                "写入/更新传统表格区域数据，支持普通值和公式。"
                "range_data 每项格式：{\"row_from\":行, \"row_to\":行, \"col_from\":列, \"col_to\":列, "
                "\"op_type\":\"value\", \"formula\":\"内容\"}"
                "公式示例：\"formula\": \"=SUM(A1:A10)\"，op_type 改为 \"formula\"。"
                "批量写入时一次传入所有单元格，减少调用次数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_id": {"type": "integer", "description": "工作表ID"},
                    "range_data": {
                        "type": "array",
                        "description": "单元格操作数组，每项包含坐标、op_type、formula",
                        "items": {"type": "object"},
                    },
                },
                "required": ["file_id", "worksheet_id", "range_data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_delete_range",
            "description": "删除传统表格指定区域的数据，并将下方/右侧单元格移动填补空缺",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_id": {"type": "integer", "description": "工作表ID"},
                    "range_data": {
                        "type": "array",
                        "description": "要删除的区域列表，每项：{\"row_from\":0,\"row_to\":0,\"col_from\":0,\"col_to\":0}",
                        "items": {"type": "object"},
                    },
                    "shift_type": {"type": "string", "description": "移动方式：shift_up（上移，默认）/ shift_left（左移）"},
                },
                "required": ["file_id", "worksheet_id", "range_data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sheets_find_range",
            "description": (
                "在传统表格中查找/筛选数据。"
                "filter 支持 search（关键词搜索）和 condition（条件筛选）。"
                "示例：搜索A列含\"张三\"的行 → filter={\"search\":[{\"col\":0,\"value\":[\"张三\"]}]}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "传统表格文件ID"},
                    "worksheet_id": {"type": "integer", "description": "工作表ID"},
                    "range": {
                        "type": "object",
                        "description": "搜索区域：{\"row_from\":0,\"row_to\":1000,\"col_from\":0,\"col_to\":20}",
                    },
                    "filter": {
                        "type": "object",
                        "description": "筛选条件，支持 search（关键词）和 condition（条件）",
                    },
                    "page": {
                        "type": "object",
                        "description": "分页：{\"page\":0,\"page_size\":100}",
                    },
                    "show_total": {"type": "boolean", "description": "是否返回总数"},
                },
                "required": ["file_id", "worksheet_id", "range", "filter"],
            },
        },
    },
    # ── 文档生成 ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "generate_document",
            "description": (
                "生成 Word 文档（.docx）并上传到 WPS 网盘。"
                "用户说「生成报告」「写通知」「生成纪要」「导出文档」「生成word」「生成文档」「写文档」「导出word」时调用此工具。"
                "\n\n【content 参数是 JSON 对象，不是字符串】直接传递对象，无需序列化。"
                "\n\n【支持的 section 类型】"
                "\n- org_header: 发文机关标志（红色大字，仅红头公文使用）"
                "\n- doc_number: 发文字号"
                "\n- red_line: 红色分隔线（仅红头公文使用）"
                "\n- title: 文档标题"
                "\n- heading1: 第一层次标题（一、二、三、）"
                "\n- heading2: 第二层次标题（（一）（二）（三））"
                "\n- heading3: 第三层次标题"
                "\n- paragraph: 正文段落"
                "\n- signature: 发文机关署名"
                "\n- date: 成文日期"
                "\n- space: 空行"
                "\n\n【正确的 JSON 格式示例】"
                "\n{"
                '\n  "sections": ['
                '\n    {"type": "title", "text": "2026年4月第4周工作周报"},'
                '\n    {"type": "space"},'
                '\n    {"type": "paragraph", "text": "报告周期：2026年4月20日（周一）—4月24日（周五）"},'
                '\n    {"type": "heading1", "text": "一、本周重点工作进展"},'
                '\n    {"type": "paragraph", "text": "1. 项目名称：完成情况\\n   执行人：张三\\n   进度：进行中"}'
                '\n  ]'
                '\n}'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "文档标题（用于文件命名）"
                    },
                    "content": {
                        "type": "object",
                        "description": "文档结构对象，包含 sections 数组。直接传递对象，不要序列化为字符串。红头公文：使用 official_red 模板；普通公文：使用 official 模板（无红头）；普通文档：只用基础元素。",
                        "properties": {
                            "sections": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "type": {"type": "string"},
                                        "text": {"type": "string"}
                                    },
                                    "required": ["type"]
                                }
                            }
                        },
                        "required": ["sections"]
                    },
                    "dbsheet_file_id": {
                        "type": "string",
                        "description": "WPS 多维表格 ID（可选）"
                    },
                    "doc_type": {
                        "type": "string",
                        "description": "文档类型：official_red（红头公文）/ official（普通公文，无红头）/ report（报告）/ notice（通知）/ minutes（纪要）/ other（其他）",
                        "enum": ["official_red", "official", "report", "notice", "minutes", "other"]
                    },
                    "metadata": {
                        "type": "object",
                        "description": "元数据（可选）",
                        "properties": {
                            "author": {"type": "string"},
                            "department": {"type": "string"},
                            "date": {"type": "string"},
                            "doc_number": {"type": "string"}
                        }
                    }
                },
                "required": ["title", "content"]
            }
        }
    },
    # ── 提醒 ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": (
                "为当前用户设置一个定时提醒。到时间后系统自动推送微信消息，确认送达后才删除；失败会自动重试。"
                "用户说「提醒我」「帮我记一下」「到时候提醒」「X点提醒我」「明天提醒」时调用此工具。"
                "remind_at 必须是完整的日期时间，格式 'YYYY-MM-DD HH:MM'。"
                "如果用户只说了时间没说日期，结合当前日期推算；如果说「明天」则是今天日期+1。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "提醒内容，用用户自己的语言描述，简洁明了"
                    },
                    "remind_at": {
                        "type": "string",
                        "description": "触发时间，格式 'YYYY-MM-DD HH:MM'，本地时间"
                    },
                    "event_at": {
                        "type": "string",
                        "description": (
                            "事件实际发生时间，格式 'YYYY-MM-DD HH:MM'。"
                            "如果用户明确指定的就是提醒时刻且没有独立事件时间，则与 remind_at 相同"
                        )
                    },
                },
                "required": ["content", "remind_at", "event_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "查看当前用户的所有待触发提醒列表。用户说「我有哪些提醒」「查看提醒」「提醒列表」时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "取消（删除）指定提醒。用户说「取消提醒」「删除提醒」时调用，需先用 list_reminders 获取 id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "要取消的提醒 id，从 list_reminders 返回结果中获取"
                    },
                },
                "required": ["reminder_id"],
            },
        },
    },
]

TOOL_MAP = {
    "get_schema":    lambda at, a: get_schema(at, a["file_id"]),
    "list_records":  lambda at, a: list_records(at, a["file_id"], a["sheet_id"],
                                                a.get("page_size", 1000),
                                                a.get("page_token") if isinstance(a.get("page_token"), str) and not a.get("page_token", "").isdigit() else None,
                                                a.get("fields"), a.get("filter"),
                                                a.get("view_id"), a.get("max_records")),
    "create_records": lambda at, a: create_records(at, a["file_id"], a["sheet_id"], a["records"]),
    "update_records": lambda at, a: update_records(at, a["file_id"], a["sheet_id"], a["records"]),
    "delete_records": lambda at, a: delete_records(at, a["file_id"], a["sheet_id"], a["record_ids"]),
    "create_sheet":  lambda at, a: create_sheet(at, a["file_id"], a.get("name"),
                                                a.get("fields"), a.get("views")),
    "delete_sheet":  lambda at, a: delete_sheet(at, a["file_id"], a["sheet_id"]),
    "create_fields": lambda at, a: create_fields(at, a["file_id"], a["sheet_id"], a["fields"]),
    "update_fields": lambda at, a: update_fields(at, a["file_id"], a["sheet_id"], a["fields"]),
    "delete_fields": lambda at, a: delete_fields(at, a["file_id"], a["sheet_id"], a["field_ids"]),
    "create_view":   lambda at, a: create_view(at, a["file_id"], a["sheet_id"],
                                               a["name"], a.get("view_type", "Grid")),
    "delete_view":   lambda at, a: delete_view(at, a["file_id"], a["sheet_id"], a["view_id"]),
    # "upload_and_attach": 暂时禁用，MCP V2.0 后重新启用
    "upload_and_attach":  lambda at, a: _upload_and_attach(at, a["file_id"], a["sheet_id"],
                                                            a["record_id"], a["field_name"],
                                                            a["file_path"], a.get("file_name")),
    "search_knowledge":   lambda at, a: _search_knowledge(a["query"]),
    "list_system_users":  lambda at, a: {"users": _db_list_users_for_ai()},
    "send_notification":  lambda at, a: a,  # 占位，实际在chat()中处理
    "list_wps_contacts":  lambda at, a: _wps_list_contacts(at, a.get("dept_id", "1")),
    "send_wps_bot_message": lambda at, a: _wps_send_bot_message(a["wps_user_id"], a["text"]),
    "send_wecom_message":   lambda at, a: a,  # 占位，实际在 chat() 中处理
    "generate_document": lambda at, a: _generate_document(
        at, a["title"], a["content"],
        a.get("dbsheet_file_id"), a.get("doc_type", "report"),
        a.get("metadata")
    ),  # 返回 coroutine，由 chat() 中的 await 处理
    # 仪表盘
    "list_dashboards":   lambda at, a: list_dashboards(at, a["file_id"]),
    "copy_dashboard":    lambda at, a: copy_dashboard(at, a["file_id"], a["dashboard_id"], a["name"]),
    # 表单
    "get_form_meta":     lambda at, a: get_form_meta(at, a["file_id"], a["sheet_id"], a["view_id"]),
    "update_form_meta":  lambda at, a: update_form_meta(at, a["file_id"], a["sheet_id"], a["view_id"],
                                                        a.get("name"), a.get("description")),
    "list_form_fields":  lambda at, a: list_form_fields(at, a["file_id"], a["sheet_id"], a["view_id"]),
    "update_form_field": lambda at, a: update_form_field(at, a["file_id"], a["sheet_id"], a["view_id"],
                                                         a["field_id"], a.get("title"), a.get("description"),
                                                         a.get("required"), a.get("pre_field_id")),
    # 父子记录
    "get_parent_status": lambda at, a: get_parent_status(at, a["file_id"], a["sheet_id"]),
    "enable_parent":     lambda at, a: enable_parent(at, a["file_id"], a["sheet_id"]),
    "disable_parent":    lambda at, a: disable_parent(at, a["file_id"], a["sheet_id"]),
    "list_children":     lambda at, a: list_children(at, a["file_id"], a["sheet_id"], a["parent_id"],
                                                     a.get("page_size", 100),
                                                     a.get("page_token") if isinstance(a.get("page_token"), str) and not a.get("page_token", "").isdigit() else None),
    "bind_children":     lambda at, a: bind_children(at, a["file_id"], a["sheet_id"], a["parent_id"], a["child_ids"]),
    "unbind_children":   lambda at, a: unbind_children(at, a["file_id"], a["sheet_id"], a["parent_id"], a["child_ids"]),
    # Webhook
    "list_hooks":        lambda at, a: list_hooks(at, a["file_id"]),
    "create_hook":       lambda at, a: create_hook(at, a["file_id"], a["command"], a["callback_url"], a.get("data")),
    "delete_hook":       lambda at, a: delete_hook(at, a["file_id"], a["hook_id"]),
    # 传统表格
    "sheets_create_file":      lambda at, a: sheets_create_file(at, a["name"], a["ref_file_id"]),
    "sheets_list_worksheets":  lambda at, a: sheets_list_worksheets(at, a["file_id"]),
    "sheets_create_worksheet": lambda at, a: sheets_create_worksheet(at, a["file_id"], a.get("name"), a.get("position")),
    "sheets_update_worksheet": lambda at, a: sheets_update_worksheet(at, a["file_id"], a["worksheet_id"],
                                                                      a.get("name"), a.get("move_sheet_id"), a.get("move_type")),
    "sheets_delete_worksheets": lambda at, a: sheets_delete_worksheets(at, a["file_id"], a["worksheet_ids"]),
    "sheets_copy_worksheet":   lambda at, a: sheets_copy_worksheet(at, a["file_id"], a["worksheet_id"],
                                                                    a.get("copy_first_sheet", False)),
    "sheets_get_range":        lambda at, a: sheets_get_range(at, a["file_id"], a["worksheet_id"],
                                                               a["row_from"], a["row_to"], a["col_from"], a["col_to"]),
    "sheets_update_range":     lambda at, a: sheets_update_range(at, a["file_id"], a["worksheet_id"], a["range_data"]),
    "sheets_delete_range":     lambda at, a: sheets_delete_range(at, a["file_id"], a["worksheet_id"],
                                                                  a["range_data"], a.get("shift_type", "shift_up")),
    "sheets_find_range":       lambda at, a: sheets_find_range(at, a["file_id"], a["worksheet_id"],
                                                                a["range"], a["filter"],
                                                                a.get("page"), a.get("show_total", False)),
    # 新建多维表格（实际在 chat() 特殊处理，此处占位避免"未知工具"）
    "create_dbsheet":    lambda at, a: {"error": "此工具需在 chat() 中特殊处理"},
    # 提醒（实际在 chat() 中处理，此处占位）
    "add_reminder":      lambda at, a: {"error": "此工具需在 chat() 中特殊处理"},
    "list_reminders":    lambda at, a: {"error": "此工具需在 chat() 中特殊处理"},
    "cancel_reminder":   lambda at, a: {"error": "此工具需在 chat() 中特殊处理"},
}


_COMMAND_LABELS = {
    "create_record":         "新增记录",
    "update_sheet":          "修改记录",
    "remove_record":         "删除记录",
    "update_records_parent": "父子关系变动",
    "create_field":          "新增字段",
    "update_field":          "修改字段",
    "remove_field":          "删除字段",
}


def _get_change_log(file_id: str = None, limit: int = 20, uid: int = None) -> dict:
    """查询本地变更日志，不过滤 file_id（webhook用数字ID，配置用字母ID，同一文件两种ID都要查到）"""
    rows = _db_get_change_log(None, min(int(limit or 20) * 5, 500))  # 多取一些用于去重
    if not rows:
        return {
            "found": False,
            "message": "暂无变更记录，表格有变化时会自动记录。",
        }

    action_label = {
        "updateSheet": "修改了记录",
        "createRecord": "新增了记录",
        "removeRecord": "删除了记录",
    }

    # 去重：同一时间同一操作同一记录ID只保留一条
    seen = set()
    items = []
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            data = {}
        records = data.get("records", [])
        operator = data.get("operator", "")
        details = []
        for rec in records:
            rec_id = rec.get("id", "")
            dedup_key = (r["received_at"][:16], r["command"], rec_id)  # 精确到分钟去重
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            fields = rec.get("fields", {})
            origin = rec.get("originFields", {})
            changed = []
            for fname, val in fields.items():
                old = origin.get(fname, "")
                if val != old:
                    changed.append(f"{fname}: {old or '(空)'} → {val}")
                elif not origin and val:
                    changed.append(f"{fname}: {val}")
            if changed:
                details.append("；".join(changed))
        if not details:
            continue
        item = {
            "时间": r["received_at"],
            "操作": action_label.get(r["command"], r["command"]),
            "变更详情": details,
        }
        if operator:
            item["操作人"] = operator
        items.append(item)
        if len(items) >= int(limit or 20):
            break

    if not items:
        return {
            "found": False,
            "message": "暂无有效变更记录。",
            "_fallback_hint": "本地 webhook 日志为空，无法从变更日志获取信息。请立即改用 list_records 工具直接查询工作表记录，用日期字段或创建时间过滤今天的数据，向用户展示实际内容。",
        }

    # 检查最新一条日志是否超过3天——说明 webhook 可能中断，日志不完整
    from datetime import datetime, timezone, timedelta
    try:
        latest_time_str = items[0]["时间"]
        latest_time = datetime.fromisoformat(latest_time_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        days_gap = (now_utc - latest_time).days
    except Exception:
        days_gap = 0

    result = {
        "found": True,
        "count": len(items),
        "records": items,
    }
    if days_gap >= 3:
        result["_fallback_hint"] = (
            f"⚠️ 注意：本地变更日志最新记录距今已有 {days_gap} 天（{items[0]['时间']}），"
            "说明 webhook 可能在此期间中断，近期操作未被记录到本地日志。"
            "请同时调用 list_records 直接查询工作表，按日期字段过滤今天或近期的记录，"
            "以获取完整的当前数据，不要仅凭本日志向用户报告'没有变化'。"
        )
    return result


def _send_notification(args: dict, sender_name: str = "管理助手") -> dict:
    to_email = args.get("to_email", "").strip()
    to_name  = args.get("to_name", "您")
    subject  = args.get("subject", "工作通知")
    message  = args.get("message", "")
    sig      = args.get("sender_name") or sender_name
    if not to_email:
        return {"error": "缺少收件人邮箱"}
    html = f"""
    <div style="font-family:'Microsoft YaHei',sans-serif;max-width:560px;margin:0 auto;padding:32px">
      <h2 style="color:#0055ff;margin-bottom:4px">部门管理助手</h2>
      <p style="color:#999;font-size:13px;margin-bottom:24px">工作通知</p>
      <p>{to_name}，您好：</p>
      <div style="background:#f5f7ff;border-left:4px solid #0055ff;padding:16px 20px;
                  border-radius:4px;margin:16px 0;white-space:pre-wrap;line-height:1.8">
{message}
      </div>
      <p style="color:#999;font-size:12px;margin-top:24px">— {sig}</p>
    </div>"""
    ok, err = _send_email(to_email, f"【工作通知】{subject}", html)
    if ok:
        return {"ok": True, "message": f"已成功发送邮件通知给 {to_name}（{to_email}）"}
    return {"error": f"发送失败：{err}"}


async def _search_knowledge(query: str) -> dict:
    """查询本地规章制度知识库，优先 RAG 语义检索，降级 LIKE 关键词匹配"""
    # RAG 语义检索
    cfg = _db_get_embed_config()
    if cfg:
        try:
            from core.knowledge_rag import embed_texts
            vecs = await embed_texts([query], cfg["api_key"], cfg["base_url"], cfg["model"])
            rag_results = _db_search_knowledge_rag(vecs[0], limit=5)
            if rag_results:
                items = []
                for r in rag_results:
                    content = r["content"]
                    if len(content) > 2000:
                        content = content[:2000] + "\n…（内容较长，已截取前2000字）"
                    items.append({"title": r["title"], "content": content})
                return {"found": True, "count": len(items), "items": items}
        except Exception as _e:
            print(f"[KNOWLEDGE RAG] chat search failed: {_e}, fallback to LIKE")
    # 降级 LIKE 关键词匹配
    results = _db_search_knowledge(query, limit=5)
    if not results:
        return {"found": False, "message": "知识库中未找到相关规章制度，请联系管理员补充。"}
    items = []
    for r in results:
        content = r["content"]
        if len(content) > 2000:
            content = content[:2000] + "\n…（内容较长，已截取前2000字）"
        items.append({"title": r["title"], "content": content})
    return {"found": True, "count": len(items), "items": items}


async def _upload_attachment_from_path(access_token: str, file_id: str,
                                       file_path: str, file_name: str = None,
                                       content_type: str = None) -> dict:
    """从本地路径读取文件，通过三步上传接口写入附件。
    返回可直接写入 Attachment 字段的完整对象（已格式化，调用方无需处理）。
    """
    p = Path(file_path)
    if not p.exists():
        return {"error": f"文件不存在: {file_path}"}
    data = p.read_bytes()
    name = file_name or p.name
    ct = content_type or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    result = await upload_attachment(access_token, file_id, name, data, ct)
    return result


async def _upload_and_attach(access_token: str, file_id: str, sheet_id: int,
                              record_id: str, field_name: str,
                              file_path: str, file_name: str = None) -> dict:
    """上传文件并直接写入指定记录的附件字段（一步完成，Python 内部处理所有格式）。
    无需 AI 手动构造 Attachment 字段值。
    """
    # Step 1: 上传文件，拿到附件对象
    upload_result = await _upload_attachment_from_path(
        access_token, file_id, file_path, file_name)
    if "error" in upload_result:
        return upload_result

    # Step 2: Python 直接构造正确的 Attachment 字段值 —— [完整对象]
    # 去掉内部调试字段，只保留 WPS 需要的字段
    attach_obj = {k: v for k, v in upload_result.items() if not k.startswith("_")}
    print(f"[ATTACH] writing to field '{field_name}': {json.dumps([attach_obj], ensure_ascii=False)}")

    # Step 3: 写入记录
    api_records = [{"id": record_id, "fields_value": json.dumps(
        {field_name: [attach_obj]}, ensure_ascii=False)}]
    result = await _post(access_token,
                         f"/v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/update",
                         {"records": api_records})
    code = result.get("code", -1)
    mode = upload_result.get("_upload_mode", "unknown")
    if code == 0:
        return {
            "ok": True,
            "record_id": record_id,
            "field_name": field_name,
            "upload_mode": mode,
            "attachment": attach_obj,
            "message": (
                f"已上传文件并写入「{field_name}」字段（record_id={record_id}）。"
                f"上传模式：{'云文档（W/P/E图标）' if mode == 'cloud' else '普通附件（?图标）'}。"
                "请刷新 WPS 查看。"
            ),
        }
    return {"ok": False, "error": f"文件上传成功但写入记录失败，API 返回 code={code}", "detail": result}


def _compress_tool_result(name: str, result: dict) -> str:
    """
    处理工具返回结果。
    list_records 已在 wps_client 层自动翻页拉取全量数据，
    此处只计算空行 ID 方便 AI 优先复用空行，不再截断记录数量。
    """
    if name == "list_records":
        records = result.get("records", [])
        total = result.get("total", len(records))
        fetched = result.get("fetched", len(records))

        # 代码层计算空行，AI 直接用，无需自己遍历
        empty_row_ids = []
        for r in records:
            fields = r.get("fields") or {}
            if isinstance(fields, dict):
                is_empty = all(
                    v is None or v == "" or v == [] or v == {}
                    for v in fields.values()
                )
                if is_empty:
                    empty_row_ids.append(r["id"])

        result["empty_row_ids"] = empty_row_ids
        result["empty_row_count"] = len(empty_row_ids)
        if empty_row_ids:
            result["_note"] = (
                f"已返回全部 {fetched} 条记录（共 {total} 条）。"
                f"空行ID列表：{empty_row_ids}，新建记录前请优先用 update_records 填入空行。"
            )

    return json.dumps(result, ensure_ascii=False)


_GUIDE_DAILY = """[填报知识导航 - 每日进展]
请在回复用户前，先给出以下填报规范提示（简洁、友好，不超过150字）：

推荐格式：今日动作 + 今日成果 + 当前问题/下一步
- 今日动作：说明做了什么（如：核对数据并完成线上填报）
- 今日成果：说明形成了什么结果/数量（如：已导出PDF材料1份）【必填，不能只写"推进中""整理资料"】
- 当前问题/下一步：若有卡点或等待，填写原因和计划

每条进展必须关联一个主任务。如果任务不存在，先帮用户创建任务再关联。
如果用户提供的内容缺少"今日成果"，请提醒补充后再写入。"""

_GUIDE_TASK = """[填报知识导航 - 任务填报]
请在回复用户前，先给出以下填报规范提示（简洁、友好，不超过150字）：

任务命名格式：动词 + 对象 + 成果/节点
- 正确：完成集团课题季度资料收集与上报
- 错误：课题日常管理工作

必填字段：所属项目、任务名称、执行人、优先级（P0-P3）、计划完成日期、完成标准
任务必须关联一个项目。如果对应项目不存在，需先判断是否应建项目（满足2条以上：有明确目标、跨2周、需拆分、涉及多人）。
执行周期一般为0.5天至20个工作日，超过1个月且含多个成果节点的应升格为项目。"""

_GUIDE_PROJECT = """[填报知识导航 - 项目填报]
请在回复用户前，先给出以下填报规范提示（简洁、友好，不超过150字）：

项目命名格式：年度 + 对象/主题 + 工作目标
- 正确：2026年三体系内部审核
- 错误：三体系工作任务

建项条件（满足2条以上才建项目）：
① 有明确目标和期限  ② 需拆分为2个以上任务  ③ 涉及多人/跨部门
④ 持续超过两周  ⑤ 对考核/审计/复盘有价值

必填字段：项目名称、项目类型（专项/年度工作包/临时专项/协同支撑）、负责人、项目目标、交付成果、启动和完成日期、当前状态"""

_GUIDE_LEVEL = """[填报知识导航 - 层级判定]
请在回复用户前，用"四问判定法"帮用户判断应填哪一层（简洁友好）：

① 这是岗位长期稳定承担、不会因完成一次而消失的职责吗？→ 岗位职责（不新建，关联已有）
② 有明确目标/期限，需要拆成多步完成吗？→ 项目
③ 可以由个人独立完成、验收并关闭的一项工作吗？→ 任务
④ 只是今天针对某任务实际开展的动作或形成的成果吗？→ 每日进展

常见误区：单次报销/会费/采购 → 不建项目，归入"年度综合事务"下的任务"""


def _detect_filing_intent(user_msg: str) -> str:
    """
    检测用户消息是否处于填报场景，返回对应的填报规范指引。
    返回空字符串表示无需注入。
    """
    if not user_msg or len(user_msg.strip()) < 4:
        return ""

    msg = user_msg.lower()

    # ── 每日进展填报信号 ──────────────────────────────────
    _DAILY_KW = [
        "填进展", "填报进展", "写进展", "记录进展", "填每日", "今日进展",
        "今天的进展", "今日工作", "今天工作", "今日成果", "今天完成",
        "日报", "填日报", "写日报", "记日报", "工作进展",
        "进展填", "进展记录", "填写进展",
    ]
    if any(kw in msg for kw in _DAILY_KW):
        return _GUIDE_DAILY

    # ── 任务填报信号 ──────────────────────────────────────
    _TASK_KW = [
        "建任务", "新建任务", "建立任务", "创建任务", "添加任务",
        "填任务", "填写任务", "录入任务", "新增任务",
        "建一个任务", "建个任务", "加一个任务", "加个任务",
        "任务名称", "任务执行人", "完成标准", "任务类型",
    ]
    if any(kw in msg for kw in _TASK_KW):
        return _GUIDE_TASK

    # ── 项目填报信号 ──────────────────────────────────────
    _PROJECT_KW = [
        "建项目", "新建项目", "建立项目", "创建项目", "添加项目",
        "填项目", "新增项目", "录入项目",
        "建一个项目", "建个项目", "加一个项目", "加个项目",
        "项目名称", "项目目标", "项目负责人", "项目类型", "交付成果",
    ]
    if any(kw in msg for kw in _PROJECT_KW):
        return _GUIDE_PROJECT

    # ── 层级判定困惑信号 ──────────────────────────────────
    _LEVEL_KW = [
        "应该填哪", "填哪里", "填哪个", "建项目还是", "建任务还是",
        "算项目吗", "算任务吗", "算项目还是", "算任务还是",
        "是项目还是", "是任务还是", "是建项目", "是建任务",
        "该建项目", "该建任务", "需要建项目", "需要建任务",
        "层级怎么", "填哪一层", "哪一层", "放哪里", "归到哪",
        "项目还是任务", "任务还是项目",
    ]
    if any(kw in msg for kw in _LEVEL_KW):
        return _GUIDE_LEVEL

    # ── 宽泛的填报动作词（降低优先级，避免误触发）─────────
    # 只有在同时提到"表格/多维/进展/任务/项目"等业务词时才触发
    _FILL_VERBS = ["填报", "填写", "录入", "写入"]
    _BIZ_WORDS = ["进展", "任务", "项目", "表格", "多维", "成果", "工作记录"]
    _has_fill_verb = any(kw in msg for kw in _FILL_VERBS)
    _has_biz_word = any(kw in msg for kw in _BIZ_WORDS)
    if _has_fill_verb and _has_biz_word:
        # 进一步细分
        if any(w in msg for w in ["进展", "日报", "今日", "今天"]):
            return _GUIDE_DAILY
        if any(w in msg for w in ["任务"]):
            return _GUIDE_TASK
        if any(w in msg for w in ["项目"]):
            return _GUIDE_PROJECT

    return ""


def _detect_personal_reminder_intent(user_msg: str) -> tuple[bool, bool]:
    """返回（是否为当前用户的定时提醒，是否包含可执行的具体时刻）。"""
    import re

    text = (user_msg or "").strip()
    personal_intent = any(phrase in text for phrase in (
        "提醒我", "通知我", "微信提醒我", "到时提醒", "到时候提醒",
        "到点提醒", "记得提醒", "帮我记一下", "安排提醒",
        "设置提醒", "设个提醒",
    ))
    if not personal_intent:
        return False, False

    # 支持「九点」「9:30」「半小时后」「2小时后」等明确触发时刻。
    has_clock = bool(re.search(
        r"(?:\d{1,2}\s*[:：]\s*\d{1,2})"
        r"|(?:[零〇一二两三四五六七八九十\d]{1,3}\s*(?:点|时)"
        r"(?:半|[零〇一二两三四五六七八九十\d]{1,3}\s*分?)?)"
        r"|(?:[零〇一二两三四五六七八九十百\d]+\s*(?:分钟|小时)后)"
        r"|(?:提前\s*[零〇一二两三四五六七八九十百\d]+\s*(?:分钟|小时))",
        text,
    ))
    return True, has_clock


def _detect_event_reminder_scene(user_msg: str) -> str:
    """
    区分用户说的是提醒时刻，还是事件发生时刻。

    返回 "event"（普通事件）、"travel"（需要出行）或空字符串（明确提醒时刻）。
    """
    import re

    text = (user_msg or "").strip()
    time_expr = (
        r"(?:\d{1,2}\s*[:：]\s*\d{1,2})"
        r"|(?:[零〇一二两三四五六七八九十\d]{1,3}\s*(?:点|时)"
        r"(?:半|[零〇一二两三四五六七八九十\d]{1,3}\s*分?)?)"
    )
    event_after_time = re.search(
        time_expr
        + r".{0,6}(?:开会|会议开始|参加会议|出发|前往|去往|去|到达|赶到|拜访|登机|乘车)",
        text,
    )
    if not event_after_time:
        return ""

    travel_words = (
        "出发", "前往", "去往", "去", "到达", "赶到", "拜访",
        "登机", "乘车", "开车", "打车", "机场", "车站",
    )
    return "travel" if any(word in text for word in travel_words) else "event"


class Assistant:
    def __init__(self, api_key: str, provider: str = "deepseek",
                 base_url: str = None, model: str = None,
                 advanced: dict | None = None):
        preset = LLM_PRESETS.get(provider, LLM_PRESETS["deepseek"])
        self.provider = provider  # 保存 provider
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or preset["base_url"],
        )
        self.model = model or preset["model"]
        self.advanced = advanced or {}
        self.supports_tools = bool(self.advanced.get("supports_tools", True))
        self.supports_vision = bool(self.advanced.get("supports_vision", False))
        self.reasoning_mode = str(self.advanced.get("reasoning_mode") or "auto").lower()
        self.reasoning_effort = str(self.advanced.get("reasoning_effort") or "auto").lower()
        try:
            self.context_window = int(self.advanced.get("context_window") or 0)
        except (TypeError, ValueError):
            self.context_window = 0
        try:
            self.max_output_tokens = int(self.advanced.get("max_output_tokens") or 8192)
        except (TypeError, ValueError):
            self.max_output_tokens = 8192
        self.max_output_tokens = max(128, min(self.max_output_tokens, 262_144))

    async def _auto_learn(self, user_msg: str, ai_reply: str, uid: int, actual_model: str):
        """
        后台自动学习（不阻塞主响应）：
        第一层 — 自动识别对话中的长期价值信息并存入记忆
        第二层 — 检测纠错意图，自动保存正确做法
        含去重：与现有记忆对比，已有类似内容则跳过。
        """
        if not uid or not user_msg:
            return
        # 过滤问候/确认等无意义短消息
        _SKIP_KW = ["在吗", "你好", "hi", "hello", "好的", "谢谢", "ok", "嗯", "好", "收到"]
        if len(user_msg) <= 8 and any(kw in user_msg.lower() for kw in _SKIP_KW):
            return

        existing_memory = _db_get_user_memory(uid) or ""

        # ── 第二层：纠错记忆（优先）────────────────────────
        _CORRECTION_KW = ["不对", "不是", "错了", "不叫", "应该是", "不应该",
                          "你搞错了", "纠正", "更正", "实际上", "其实", "正确的是",
                          "不能这样", "是错的", "写错了", "说错了"]
        if any(kw in user_msg for kw in _CORRECTION_KW):
            try:
                resp = await self.client.chat.completions.create(
                    model=actual_model,
                    messages=[{"role": "user", "content":
                        f"用户在纠正AI的错误。请提炼出需要永久记住的正确事实或规则。\n\n"
                        f"用户说：{user_msg}\n"
                        f"AI原回复：{ai_reply[:200]}\n\n"
                        f"已有记忆（避免重复）：\n{existing_memory[:500] or '（空）'}\n\n"
                        f"要求：\n"
                        f"1. 提炼成一句简洁的话，格式：[纠错] 正确做法\n"
                        f"2. 如果已有记忆中已有类似内容，只输出：重复\n"
                        f"3. 如果无法提炼出有价值的纠错信息，只输出：无"}],
                    max_tokens=100,
                )
                item = resp.choices[0].message.content.strip()
                if item and item not in ("无", "重复") and item.startswith("[纠错]"):
                    merged = (existing_memory + "\n- " + item) if existing_memory else "- " + item
                    _db_save_user_memory(uid, merged)
                    print(f"[AUTO_LEARN] 纠错记忆已保存: {item}")
                    return  # 本轮只写一条，纠错优先
            except Exception as e:
                print(f"[AUTO_LEARN] 纠错检测失败: {e}")

        # ── 第一层：自动记忆识别 ────────────────────────────
        try:
            resp = await self.client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content":
                    f"分析下面这段对话，判断是否包含值得长期记忆的信息。\n\n"
                    f"用户说：{user_msg}\n"
                    f"AI回复：{ai_reply[:300]}\n\n"
                    f"已有记忆（避免重复）：\n{existing_memory[:500] or '（空）'}\n\n"
                    f"【值得记忆的信息类型】\n"
                    f"✅ 工作习惯（如：每周五下午开例会）\n"
                    f"✅ 人员信息（如：李总分机123、张三负责采购）\n"
                    f"✅ 固定规则（如：报销必须先审批、公文字号格式）\n"
                    f"✅ 业务术语（如：公司全称、项目代号含义）\n"
                    f"✅ 个人偏好（如：喜欢简洁回复、不用表格展示）\n\n"
                    f"【不值得记忆】\n"
                    f"❌ 一次性查询或操作（查数据、建记录、发通知等）\n"
                    f"❌ 已有记忆中已有的类似内容\n"
                    f"❌ 闲聊、问候、确认\n\n"
                    f"如果有值得记忆的内容，提炼成一句话，格式：[自动] 内容\n"
                    f"否则只输出：无"}],
                max_tokens=100,
            )
            item = resp.choices[0].message.content.strip()
            if item and item != "无" and item.startswith("[自动]"):
                merged = (existing_memory + "\n- " + item) if existing_memory else "- " + item
                _db_save_user_memory(uid, merged)
                print(f"[AUTO_LEARN] 自动记忆已保存: {item}")
        except Exception as e:
            print(f"[AUTO_LEARN] 自动记忆识别失败: {e}")

    async def summarize_memory(self, recent_messages: list, existing_memory: str) -> str:
        """根据最近对话更新用户记忆，返回新的记忆文本。"""
        # 代码层保护用户主动保存的条目，不交给 LLM 判断是否保留
        protected_lines = []
        summarize_lines = []
        for line in (existing_memory or "").splitlines():
            if "用户主动保存" in line and line.strip():
                protected_lines.append(line.strip())
            elif line.strip():
                summarize_lines.append(line.strip())

        conv_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}：{m['content'][:300]}"
            for m in recent_messages
            if m["role"] in ("user", "assistant")
        )
        prompt = MEMORY_SUMMARIZE_PROMPT.format(
            existing_memory="\n".join(summarize_lines) or "（暂无）",
            recent_messages=conv_text,
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
            )
            summarized = resp.choices[0].message.content.strip()
        except Exception:
            summarized = "\n".join(summarize_lines)  # 失败时保留原非保护内容

        # 把受保护条目无条件拼回最前面
        parts = []
        if protected_lines:
            parts.extend(protected_lines)
        if summarized:
            parts.append(summarized)
        return "\n".join(parts)

    async def chat(self, messages: list, access_token: str,
                   on_tool_call=None, username: str = "用户",
                   role: str = "staff", default_file: dict = None,
                   all_files: list = None, memory: str = "",
                   uid: int = 0) -> str:
        # 兼容旧测试、插件或外部代码通过 Assistant.__new__ 构造的实例；
        # 正常运行时这些值均由 __init__ 从高级配置写入。
        if not hasattr(self, "supports_tools"):
            self.supports_tools = True
        if not hasattr(self, "supports_vision"):
            self.supports_vision = False
        if not hasattr(self, "reasoning_mode"):
            self.reasoning_mode = "auto"
        if not hasattr(self, "reasoning_effort"):
            self.reasoning_effort = "auto"
        if not hasattr(self, "context_window"):
            self.context_window = 0
        if not hasattr(self, "max_output_tokens"):
            self.max_output_tokens = 8192
        from datetime import datetime, timezone, timedelta
        _now = datetime.now(tz=timezone(timedelta(hours=8)))
        _wd  = ["周一","周二","周三","周四","周五","周六","周日"][_now.weekday()]
        _today = _now.strftime("%Y年%m月%d日") + "(" + _wd + ")" + _now.strftime(" %H:%M")
        system_prompt = build_system_prompt(username, role, default_file, all_files, memory)

        # 部分 OpenAI 兼容接口（尤其部分千问网关）要求 system 只能出现在
        # 消息数组首位。实时配置仍保持系统级权限，并放在系统提示末尾以维持
        # 对 DeepSeek 等原有模型的近因强调效果，不降级成用户消息。
        if default_file:
            _rt_name = default_file.get("file_name") or default_file["file_id"]
            _rt_id = default_file["file_id"]
            system_prompt += (
                "\n\n## 本轮最高优先级实时状态\n"
                f"当前默认表格：**{_rt_name}**（file_id: {_rt_id}）。"
                "如用户未指定表格，必须使用此 file_id；"
                "如用户明确指定了其他已配置表格，直接使用用户指定的表格。"
            )

        # ── 历史清洗 ──────────────────────────────────────────
        # 去掉传给 API 的 tool_calls / tool 消息，断掉模型对历史成功结果的"幻觉依赖"
        # 保留：用户消息 + 最后一条 AI 回复（无工具调用），保持对话连贯性但断掉工具结果依赖
        def _flatten(msgs):
            # 保留原始 user/assistant 交替顺序，只过滤 tool_calls 消息
            # 旧实现把所有 user 堆前、单条 assistant 追末尾，导致 AI 重复执行历史请求
            result = []
            for m in msgs:
                role = m.get("role")
                if role in ("user", "assistant") and not m.get("tool_calls"):
                    result.append({"role": role, "content": m.get("content", "")})
            return result

        # 取最后一条用户消息内容，供后续意图判断
        _last_user_content = ""
        for _m in reversed(messages):
            if _m.get("role") == "user":
                _last_user_content = str(_m.get("content", ""))
                break

        # ── 主动填报知识导航注入 ──────────────────────────────
        # 识别用户当前是否处于填报场景，若是则将匹配的规范指引注入到用户消息末尾
        # 对用户不可见，只进入 AI 上下文，让 AI 自然地给出填报建议
        _filing_guide = _detect_filing_intent(_last_user_content)

        # 把当前时间注入到最后一条用户消息
        injected = list(_flatten(messages))
        if injected and injected[-1].get("role") == "user":
            injected[-1] = dict(injected[-1])
            _base = f"[当前北京时间：{_today}]\n{injected[-1]['content']}"
            if _filing_guide:
                _base += f"\n\n{_filing_guide}"
            injected[-1]["content"] = _base

        # 自定义模型可声明上下文窗口。用保守字符估算裁剪最旧历史，始终保留
        # 系统提示和当前用户消息；未配置时沿用路由层最近 20 条的默认策略。
        if self.context_window and len(injected) > 1:
            reserve_chars = self.max_output_tokens * 2
            history_budget = max(2000, self.context_window * 2 - len(system_prompt) - reserve_chars)
            while len(injected) > 1 and sum(len(str(m.get("content", ""))) for m in injected) > history_budget:
                injected.pop(0)

        full_messages = [{"role": "system", "content": system_prompt}] + injected

        def _append_system_instruction(instruction: str):
            """追加系统级规则，但始终保持消息数组只有首条 system。"""
            nonlocal full_messages
            full_messages = _coalesce_system_messages(
                full_messages + [{"role": "system", "content": instruction}]
            )

        # 合法 file_id 集合：用户已配置的所有文件，AI 可以自由在这些文件间切换
        _valid_file_ids = {f["file_id"] for f in (all_files or [])} if all_files else set()
        if default_file:
            _valid_file_ids.add(default_file["file_id"])
        _active_file_id = default_file["file_id"] if default_file else None

        # schema 缓存：get_schema 调用成功后记录 file_id → {sheet_id: sheet_name}
        # 用于后续工具调用时 sheet_id 缺失/为0 时自动补齐
        _schema_cache: dict[str, dict] = {}  # {file_id: {sheet_id: int, sheets: list}}

        # 问候/闲聊检测 → 直接返回，不调工具，不受历史上下文影响
        _GREETING_KW = ["在吗", "你好", "hi", "hello", "在不在", "有人吗", "在线吗", "忙吗", "打扰一下"]
        _is_greeting = (
            len(_last_user_content.strip()) <= 10
            and any(kw in _last_user_content.lower() for kw in _GREETING_KW)
        )
        if _is_greeting:
            return "在的，有什么可以帮您？"

        # 检测用户是否发出操作指令 → 第一轮强制调用工具，防止 AI 直接复用历史结果
        # ⚠️ 大幅收紧：只在非常明确的操作意图时才强制，避免误触发
        _ACTION_KW = ["建立任务", "新建任务", "添加任务", "创建任务",
                      "建立项目", "新建项目", "创建项目",
                      "删除", "移除", "清除"]
        _force_tool_first = any(kw in _last_user_content for kw in _ACTION_KW)

        # 当前用户的未来提醒优先级高于“立即发微信”。
        _reminder_intent, _reminder_has_time = _detect_personal_reminder_intent(
            _last_user_content
        )
        _event_reminder_scene = (
            _detect_event_reminder_scene(_last_user_content)
            if _reminder_intent else ""
        )

        # 查找本轮用户消息之前的最近一条助手回复，用自然语言前缀维持规划状态。
        _previous_assistant = ""
        _seen_current_user = False
        for _history_msg in reversed(messages):
            if not _seen_current_user and _history_msg.get("role") == "user":
                _seen_current_user = True
                continue
            if _seen_current_user and _history_msg.get("role") == "assistant":
                _previous_assistant = str(_history_msg.get("content", "")).strip()
                break

        _confirmation_text = _last_user_content.strip().lower().replace("。", "")
        _positive_confirmation = any(
            _confirmation_text == phrase
            or _confirmation_text.startswith(phrase + "，")
            or _confirmation_text.startswith(phrase + ",")
            for phrase in (
                "可以", "行", "好的", "好", "同意", "确认", "就这样",
                "按这个来", "没问题", "ok", "yes",
            )
        ) and not any(word in _confirmation_text for word in ("不行", "不要", "不同意", "取消"))
        _is_plan_confirmation = (
            _previous_assistant.startswith("提醒方案：")
            and _positive_confirmation
        )
        _continuing_reminder_planning = _previous_assistant.startswith("提醒规划：")

        # 明确提醒时刻和普通会议直接入库；只有出行或信息不足时才先规划。
        _force_reminder = _is_plan_confirmation or (
            _reminder_intent
            and _reminder_has_time
            and _event_reminder_scene != "travel"
        )
        _reminder_needs_planning = (
            _event_reminder_scene == "travel"
            or (_reminder_intent and not _reminder_has_time)
            or _continuing_reminder_planning
        )

        if _reminder_needs_planning:
            if _event_reminder_scene == "travel":
                _planning_instruction = (
                    "[提醒规划模式] 用户给出的是需要出行的事件时间，不是提醒时间。"
                    "先确认出发地点、交通方式、预计路程和希望预留的机动时间；"
                    "缺少关键信息时继续追问。回复必须以「提醒规划：」开头，"
                    "不得调用任何工具，不得声称已经设置。"
                )
            elif _continuing_reminder_planning:
                _planning_instruction = (
                    "[提醒规划续答] 结合上一轮问题和用户本轮补充的信息继续规划。"
                    "若出发地、交通方式或路程仍不明确，以「提醒规划：」开头继续追问；"
                    "若信息已足够，计算路程、机动时间和准备时间，给出具体提醒时刻，"
                    "以「提醒方案：」开头并询问确认。不得调用工具，不得声称已经设置。"
                )
            else:
                _planning_instruction = (
                    "[提醒规划模式] 用户希望设置提醒，但没有说清事件时间或提醒时刻。"
                    "用最少的问题确认具体日期、事件时间，以及是否涉及出行。"
                    "回复必须以「提醒规划：」开头，不得调用工具，不得声称已经设置。"
                )
            _append_system_instruction(
                "## 本轮最高优先级提醒规则\n" + _planning_instruction
            )

        # 检测微信发送意图 → 第一轮强制指定 send_weixin_message
        _WEIXIN_SEND_KW = ["发微信", "微信通知", "微信告诉", "发个微信", "用微信", "微信发", "微信给"]
        _force_weixin = (
            not (_reminder_intent or _is_plan_confirmation or _continuing_reminder_planning)
            and any(kw in _last_user_content for kw in _WEIXIN_SEND_KW)
        )

        # 检测文档生成意图 → 第一轮强制指定 generate_document
        _DOC_GEN_KW = ["生成报告", "写报告", "生成纪要", "写纪要", "生成通知", "写通知",
                       "生成文档", "写文档", "生成word", "写word", "导出文档", "导出word",
                       "生成Word", "写Word", "导出Word", "生成WORD", "写总结", "生成总结"]
        _force_doc_gen = any(kw in _last_user_content for kw in _DOC_GEN_KW)

        # 检测传统表格新建意图 → WPS不支持API新建，禁用强制调用
        _SHEETS_CREATE_KW = ["新建表格", "新建excel", "新建Excel", "新建EXCEL",
                             "创建excel", "创建Excel", "创建EXCEL",
                             "建个表格", "建个excel", "建个Excel",
                             "新建电子表格", "创建电子表格"]
        _force_sheets_create = False

        # 检测变更查询意图 → 第一轮强制指定 get_change_log
        # ⚠️ 严格匹配：只在用户明确问"变化/更新/新增/修改/删除"时调，避免误触
        _CHANGE_LOG_KW = ["最近有什么变化", "有什么变化", "有什么更新", "最近更新",
                          "改了什么", "有什么进展", "发生了什么", "最近发生", "有哪些变化",
                          "有什么新增", "有什么修改", "有什么删除", "变更记录", "操作记录"]
        _force_change_log = any(kw in _last_user_content for kw in _CHANGE_LOG_KW)

        if not self.supports_tools and any((
            _force_sheets_create, _force_change_log, _force_doc_gen,
            _force_reminder, _force_weixin, _force_tool_first,
        )):
            return (
                "当前主模型配置为不支持工具调用，只能进行普通对话。"
                "请在设置的主模型高级配置中启用“支持工具调用”，"
                "或更换具备工具调用能力的模型后再执行此操作。"
            )

        _create_records_success = False  # 追踪 create_records 是否成功
        _reminder_created_success = False  # 追踪本轮提醒是否已真实写入数据库
        for _turn in range(50):

            # 处理思考模式：模型名带 -reasoning 后缀表示启用思考模式
            _actual_model = self.model
            _enable_reasoning = False
            if self.model.lower().endswith("-reasoning"):
                _enable_reasoning = True
                # 兼容小写(-reasoning)和大写(-Reasoning)两种后缀
                _actual_model = self.model[:-len("-reasoning")]
            if self.reasoning_mode == "on":
                _enable_reasoning = True
            elif self.reasoning_mode == "off":
                _enable_reasoning = False

            _kwargs = dict(
                model=_actual_model,
                # 最后一道结构防线：未来新增规则即使误追加 system，也会在
                # 发出请求前合并回首位。普通消息、tool_calls 与 tool 结果顺序不变。
                messages=_coalesce_system_messages(full_messages),
                max_tokens=self.max_output_tokens,
            )
            if self.supports_tools:
                _kwargs["tools"] = TOOLS
            # 规划阶段只允许自然语言澄清/提案，彻底禁止提前写入提醒。
            if _reminder_needs_planning:
                _kwargs.pop("tools", None)
            # DeepSeek 思考模式控制
            if self.provider == "deepseek" and _actual_model in {"deepseek-v4-flash", "deepseek-v4-pro"}:
                if _enable_reasoning:
                    _kwargs["reasoning_effort"] = (
                        "max" if self.reasoning_effort == "auto" else self.reasoning_effort
                    )
                    _kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                else:
                    _kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            # SCNet DeepSeek V4 思考模式控制（-Reasoning 后缀 → enable_thinking=true）
            elif self.provider == "scnet" and _actual_model in {"DeepSeek-V4-Flash", "DeepSeek-V4-Pro"}:
                if _enable_reasoning:
                    effort = "high" if self.reasoning_effort in {"auto", "max"} else self.reasoning_effort
                    _kwargs["extra_body"] = {"enable_thinking": True, "reasoning_effort": effort}
                else:
                    _kwargs["extra_body"] = {"enable_thinking": False}
            elif _enable_reasoning:
                # 自定义 OpenAI 兼容模型仅在用户明确开启推理时发送标准风格参数。
                # 不主动猜测各厂商私有的 thinking/enable_thinking 请求结构。
                effort = self.reasoning_effort
                if effort == "auto":
                    effort = "high"
                if effort == "max":
                    effort = "high"
                _kwargs["reasoning_effort"] = effort
            # 千问 3.6-plus 和 deepseek-reasoner/v4系列 不支持任何形式的 tool_choice
            _is_qwen_36plus = (self.provider == "qwen" and _actual_model == "qwen3.6-plus")
            _is_deepseek_no_tc = (self.provider == "deepseek" and _actual_model in {
                "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro"
            })
            _no_tool_choice = _is_qwen_36plus or _is_deepseek_no_tc

            # 规划阶段不允许 tool_choice；其余场景按意图强制工具。
            if _reminder_needs_planning:
                pass
            # 第一轮：传统表格新建意图 → 强制指定 sheets_create_file，优先级最高
            elif _turn == 0 and _force_sheets_create and self.supports_tools and not _no_tool_choice:
                _kwargs["tool_choice"] = {"type": "function", "function": {"name": "sheets_create_file"}}
            # 第一轮：变更查询意图 → 强制指定 get_change_log
            elif _turn == 0 and _force_change_log and self.supports_tools and not _no_tool_choice:
                _kwargs["tool_choice"] = {"type": "function", "function": {"name": "get_change_log"}}
            # 第一轮：文档生成意图 → 强制指定 generate_document
            elif _turn == 0 and _force_doc_gen and self.supports_tools and not _no_tool_choice:
                _kwargs["tool_choice"] = {"type": "function", "function": {"name": "generate_document"}}
            # 第一轮：当前用户的定时提醒 → 强制写入提醒表
            elif _turn == 0 and _force_reminder and self.supports_tools and not _no_tool_choice:
                _kwargs["tool_choice"] = {"type": "function", "function": {"name": "add_reminder"}}
            # 第一轮：微信发送意图 → 强制指定工具
            elif _turn == 0 and _force_weixin and self.supports_tools and not _no_tool_choice:
                _kwargs["tool_choice"] = {"type": "function", "function": {"name": "send_weixin_message"}}
            # 第一轮：其他操作指令 → 强制调用某个工具（防止复用历史结果）
            elif _turn == 0 and _force_tool_first and self.supports_tools and not _no_tool_choice:
                _kwargs["tool_choice"] = "required"
            try:
                resp = await self.client.chat.completions.create(**_kwargs)
            except (APITimeoutError, APIConnectionError) as _network_error:
                # 模型服务或本地代理偶发连接失败：轻量重试一次，仍失败则明确返回，
                # 绝不让异常冒到聊天界面，也绝不谎称提醒已经设置。
                print(
                    f"[LLM NETWORK] {_network_error.__class__.__name__}, "
                    "retrying once..."
                )
                try:
                    import asyncio as _asyncio
                    await _asyncio.sleep(1)
                    _retry_kwargs = dict(_kwargs)
                    _retry_kwargs["max_tokens"] = min(
                        int(_retry_kwargs.get("max_tokens", 8192)), 4096
                    )
                    resp = await self.client.chat.completions.create(**_retry_kwargs)
                except (APITimeoutError, APIConnectionError) as _retry_error:
                    print(
                        f"[LLM NETWORK] retry failed: "
                        f"{_retry_error.__class__.__name__}: {_retry_error}"
                    )
                    if _force_reminder or _reminder_needs_planning:
                        return "模型服务连接超时，本次提醒尚未设置。请稍后再试，我不会把它误报为设置成功。"
                    return "模型服务连接超时，请稍后再试。"
            except RateLimitError as _rate_error:
                print(f"[LLM RATE LIMIT] {_rate_error}")
                if _force_reminder or _reminder_needs_planning:
                    return "模型服务当前繁忙，本次提醒尚未设置。请稍后再试。"
                return "模型服务当前繁忙，请稍后再试。"
            except Exception as _api_error:
                # 部分千问 OpenAI 兼容网关不接受以 tool 结果结尾的
                # 二次请求，会返回 no user query found in messages。只在该
                # 特定错误与消息形态同时出现时补语义等价的续问；正常
                # DeepSeek/Agnes/其他模型的请求结构完全不变。
                _sent_messages = _kwargs.get("messages") or []
                if not _needs_tool_result_user_bridge(_api_error, _sent_messages):
                    raise
                print(
                    "[LLM COMPAT] strict tool-result gateway requires a trailing "
                    "user query; retrying once with a semantic continuation"
                )
                full_messages = _append_tool_result_user_bridge(full_messages)
                _compat_kwargs = dict(_kwargs)
                _compat_kwargs["messages"] = _coalesce_system_messages(full_messages)
                resp = await self.client.chat.completions.create(**_compat_kwargs)
            msg = resp.choices[0].message

            if not msg.tool_calls:
                if _force_reminder and not _reminder_created_success:
                    if _turn == 0:
                        # 某些模型不支持 tool_choice；明确纠正一次，但绝不把普通文字当作成功。
                        _append_system_instruction(
                            "## 本轮最高优先级提醒写入纠正\n"
                            "必须立即调用 add_reminder 写入数据库。"
                            "禁止仅用文字声称提醒已设置。"
                        )
                        continue
                    return "提醒未能写入系统，请稍后重试；本次没有虚假地标记为设置成功。"
                reply = msg.content or ""
                # 后台自动学习：不阻塞响应，fire-and-forget
                import asyncio as _asyncio
                _asyncio.create_task(
                    self._auto_learn(_last_user_content, reply, uid, _actual_model)
                )
                return reply

            _assistant_msg = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            }
            # 思考模式下 reasoning_content 必须回传，否则 API 返回 400
            if getattr(msg, "reasoning_content", None):
                _assistant_msg["reasoning_content"] = msg.reasoning_content
            full_messages.append(_assistant_msg)

            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    # 先尝试截取第一个完整 JSON 对象（处理 JSON 后附加多余文字的情况）
                    raw = tc.function.arguments.strip()
                    depth, end = 0, 0
                    for i, ch in enumerate(raw):
                        if ch == '{': depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0:
                                end = i + 1
                                break
                    try:
                        args = json.loads(raw[:end]) if end else None
                    except json.JSONDecodeError:
                        args = None
                    # 截取后仍然失败，把错误反馈给 AI 让它重试
                    if args is None:
                        full_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({
                                "error": f"工具参数 JSON 格式错误，请检查并重新调用。原始参数：{tc.function.arguments[:200]}"
                            }, ensure_ascii=False),
                        })
                        continue
                print(f"[TOOL CALL] {name}")
                print(f"  args={json.dumps(args, ensure_ascii=False, indent=2)}")

                if on_tool_call:
                    await on_tool_call(name, args)

                # 本地工具不依赖 WPS token，可在未连接时使用
                # send_wps_bot_message 用 App Token，不依赖用户 WPS token
                _LOCAL_TOOLS = {"search_knowledge", "get_change_log", "save_memory", "send_notification", "list_system_users", "send_wps_bot_message", "send_wecom_message", "send_weixin_message", "generate_document", "add_reminder", "list_reminders", "cancel_reminder"}
                try:
                    if name == "save_memory":
                        if uid:
                            old = _db_get_user_memory(uid)
                            new_content = args.get("content", "")
                            merged = (old + "\n- " + new_content) if old else "- " + new_content
                            _db_save_user_memory(uid, merged)
                            result = {"ok": True, "message": f"已永久记住：{new_content}"}
                        else:
                            result = {"error": "无法获取用户ID，记忆保存失败"}
                    elif name == "send_notification":
                        result = _send_notification(args, sender_name=username)
                    elif name == "get_change_log":
                        result = _get_change_log(args.get("file_id"), args.get("limit", 20), uid=uid)
                    elif name == "mention_in_record":
                        at_name   = args.get("at_name", "")
                        wps_uid   = _db_get_wps_uid(at_name)
                        comment   = f"@{at_name} {args.get('message', '')}"
                        result    = await _wps_comment(
                            access_token,
                            args["file_id"], args["sheet_id"], args["record_id"],
                            comment, wps_uid,
                        )
                    elif name == "create_dbsheet":
                        if not access_token:
                            result = {"error": "未连接WPS，请先点击右上角「连接WPS」完成授权"}
                        elif not _active_file_id:
                            result = {"error": "请先在设置中添加至少一个多维表格，以便确定存储位置"}
                        else:
                            result = await create_dbsheet(access_token, args["name"], _active_file_id)
                            if result.get("ok") and uid:
                                _db_add_wps_file(uid, result["file_id"], result["file_name"])
                    elif name == "send_wecom_message":
                        webhook_url = _db_get_system_config("wecom_webhook_url")
                        if not webhook_url:
                            result = {"error": "企业微信群机器人未配置，请管理员在系统设置「企业微信」中填写 Webhook URL"}
                        else:
                            to_uname = args.get("to_username", "")
                            wecom_uid = _db_get_wecom_userid(to_uname)
                            result = await _wecom_send(
                                webhook_url, args["text"], wecom_uid
                            )
                    elif name == "send_weixin_message":
                        import httpx as _httpx
                        from auth.db import get_user_by_username as _db_get_user_by_uname
                        from auth.db import get_user_by_display_name as _db_get_user_by_dname
                        from auth.db import get_personal_weixin_id as _db_get_pwid
                        from auth.db import get_user_by_id as _db_get_user_by_id
                        to_uname = args.get("to_username", "")
                        target_user = _db_get_user_by_uname(to_uname)
                        if not target_user:
                            target_user = _db_get_user_by_dname(to_uname)
                        if not target_user:
                            result = {"error": f"用户 {to_uname} 不存在"}
                        else:
                            pwid = _db_get_pwid(dict(target_user)["id"])
                            if not pwid:
                                pwid = dict(target_user).get("weixin_id", "")
                            if not pwid:
                                result = {"error": f"用户 {to_uname} 未设置个人微信ID，请让对方在设置页填写"}
                            else:
                                # 拼发件人真实姓名前缀
                                sender_user = _db_get_user_by_id(uid) if uid else None
                                if sender_user:
                                    sender_name = dict(sender_user).get("display_name") or dict(sender_user).get("username") or username
                                else:
                                    sender_name = username
                                text_with_sender = f"【来自 {sender_name}】{args['text']}"
                                local_token = _db_get_system_config("weixin_bot_token", "")
                                try:
                                    import app as _app
                                    # 不能在目标映射缺失时轮询其他用户的桥接；多账号环境下
                                    # 会把消息交给错误/已过期的 bot。先用内存映射，再通过
                                    # /health 核对桥接实际加载的个人微信 userId。
                                    candidate_ports = []
                                    mapped_port = _app._wechat_port_map.get(pwid)
                                    if mapped_port:
                                        candidate_ports.append(mapped_port)
                                    candidate_ports.extend(_app._wechat_port_map.values())
                                    candidate_ports.extend(range(3001, 3011))
                                    candidate_ports = list(dict.fromkeys(candidate_ports))

                                    import asyncio as _asyncio
                                    async with _httpx.AsyncClient(timeout=90, trust_env=False) as _client:
                                        exact_ports = []
                                        for _port in candidate_ports:
                                            try:
                                                _health = await _client.get(
                                                    f"http://127.0.0.1:{_port}/health",
                                                    timeout=1.0,
                                                )
                                                _health_data = _health.json() if _health.status_code == 200 else {}
                                                if (_health_data.get("ok") is True
                                                        and _health_data.get("userId") == pwid):
                                                    exact_ports.append(_port)
                                            except Exception:
                                                continue

                                        if not exact_ports:
                                            result = {"error": f"用户 {to_uname} 的微信桥接未运行或账号不匹配"}
                                        else:
                                            result = {"error": "微信桥接未返回发送结果"}
                                        for _port in exact_ports:
                                            for _retry in range(3):
                                                try:
                                                    _resp = await _client.post(
                                                        f"http://127.0.0.1:{_port}/local/send",
                                                        json={"to": pwid, "text": text_with_sender, "token": local_token},
                                                    )
                                                    try:
                                                        _resp_data = _resp.json()
                                                    except Exception:
                                                        _resp_data = {}
                                                    if _resp.status_code == 200 and _resp_data.get("ok") is True:
                                                        display = dict(target_user).get("display_name") or to_uname
                                                        result = {"ok": True, "message": f"已成功发送微信消息给 {display}"}
                                                    else:
                                                        _detail = (_resp_data.get("error") or _resp.text or "桥接返回空错误").strip()
                                                        result = {"error": f"发送失败（端口 {_port}，HTTP {_resp.status_code}）：{_detail}"}
                                                    print(
                                                        f"[WEIXIN TOOL] target={to_uname!r} weixin={pwid!r} "
                                                        f"port={_port} status={_resp.status_code} ok={result.get('ok', False)}"
                                                    )
                                                    break
                                                except Exception as _re:
                                                    if _retry < 2:
                                                        await _asyncio.sleep(1)
                                                    else:
                                                        result = {"error": f"连接微信桥接失败: {_re}"}
                                            if result.get("ok"):
                                                break
                                except Exception as _e:
                                    result = {"error": f"连接微信桥接失败: {_e}"}
                    elif name == "add_reminder":
                        if not uid:
                            result = {"error": "无法获取用户ID，提醒设置失败"}
                        else:
                            content = args.get("content", "").strip()
                            remind_at = args.get("remind_at", "").strip()
                            event_at = args.get("event_at", "").strip() or remind_at
                            if not content or not remind_at or not event_at:
                                result = {"error": "content、remind_at 和 event_at 均不能为空"}
                            else:
                                try:
                                    from core.reminder_text import normalize_reminder_schedule
                                    remind_at, event_at = normalize_reminder_schedule(
                                        _last_user_content, remind_at, event_at, now=_now
                                    )
                                    _reminder_dt = datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
                                    if _reminder_dt <= _now.replace(tzinfo=None):
                                        raise ValueError(
                                            f"提醒时间 {remind_at} 已经过去，请根据当前北京时间重新计算后再设置"
                                        )
                                    rid = _db_add_reminder(
                                        uid, content, remind_at, event_at=event_at
                                    )
                                    result = {"ok": True, "reminder_id": rid,
                                              "message": (
                                                  f"提醒已设置，将在 {remind_at} 推送微信消息："
                                                  f"{content}（事件时间：{event_at}）"
                                              )}
                                    _reminder_created_success = True
                                except Exception as _e:
                                    result = {"error": f"提醒设置失败: {_e}"}
                    elif name == "list_reminders":
                        if not uid:
                            result = {"error": "无法获取用户ID"}
                        else:
                            rows = _db_list_reminders(uid)
                            if not rows:
                                result = {"ok": True, "reminders": [], "message": "当前没有待触发的提醒"}
                            else:
                                result = {"ok": True, "reminders": rows,
                                          "message": f"共 {len(rows)} 条待触发提醒"}
                    elif name == "cancel_reminder":
                        if not uid:
                            result = {"error": "无法获取用户ID"}
                        else:
                            rid = args.get("reminder_id")
                            if rid is None:
                                result = {"error": "reminder_id 不能为空"}
                            else:
                                ok = _db_cancel_reminder(int(rid), uid)
                                if ok:
                                    result = {"ok": True, "message": f"提醒 {rid} 已取消"}
                                else:
                                    result = {"error": f"提醒 {rid} 不存在或无权限取消"}
                    elif not access_token and name not in _LOCAL_TOOLS:
                        result = {"error": "未连接WPS，请先点击右上角「连接WPS」完成授权"}
                    elif name in TOOL_MAP:
                        # 传统表格工具（sheets_*）不做 file_id 白名单校验，用户直接提供 file_id
                        _is_sheets_tool = name.startswith("sheets_")
                        if not _is_sheets_tool and _active_file_id and "file_id" in args:
                            ai_fid = args.get("file_id", "")
                            if ai_fid not in _valid_file_ids:
                                print(f"[FILE_ID FALLBACK] AI used unknown file_id={ai_fid}, falling back to {_active_file_id}")
                                args["file_id"] = _active_file_id

                        # 多维表格工具 sheet_id 兜底补齐：
                        # 优先从本次会话的 schema 缓存里找，其次从 default_file 配置里找
                        if name in {"list_records", "create_records", "update_records", "delete_records"}:
                            _need_fallback = ("sheet_id" not in args) or (not args.get("sheet_id")) or (args.get("sheet_id") == 0)
                            if _need_fallback:
                                _fid = args.get("file_id", "")
                                _fallback_sid = None
                                # 优先：schema 缓存（本次会话 get_schema 的结果）
                                if _fid in _schema_cache:
                                    _fallback_sid = _schema_cache[_fid].get("first_sheet_id")
                                # 其次：default_file 配置（数据库里存的）
                                if not _fallback_sid and default_file and _fid == default_file.get("file_id"):
                                    _fallback_sid = default_file.get("sheet_id")
                                if _fallback_sid and isinstance(_fallback_sid, int) and _fallback_sid > 0:
                                    print(f"[SHEET_ID FALLBACK] tool={name} sheet_id missing/0, using {_fallback_sid}")
                                    args["sheet_id"] = _fallback_sid

                        import asyncio as _asyncio
                        _r = TOOL_MAP[name](access_token, args)
                        result = await _r if _asyncio.iscoroutine(_r) else _r

                        # AI 操作成功后主动写变更日志，和 webhook 保持一致
                        if name in {"create_records", "update_records", "delete_records"} and not result.get("error"):
                            _action_map = {"create_records": "createRecord", "update_records": "updateSheet", "delete_records": "removeRecord"}
                            _log_data = json.dumps({
                                "action": _action_map[name],
                                "operator": username,
                                "records": args.get("records") or [{"id": rid} for rid in (args.get("record_ids") or [])],
                                "source": "ai_api",
                            }, ensure_ascii=False)
                            try:
                                from auth.db import add_change_log as _add_log
                                _add_log(args.get("file_id", ""), _action_map[name], _log_data)
                            except Exception:
                                pass

                        # get_schema 成功后写入缓存，供后续工具调用补齐 sheet_id
                        if name == "get_schema" and isinstance(result, dict):
                            _fid = args.get("file_id", "")
                            _sheets = result.get("sheets") or result.get("data", {}).get("sheets") or []
                            if _sheets and _fid:
                                _first = _sheets[0]
                                _sid = _first.get("sheet_id") or _first.get("id")
                                if _sid:
                                    _schema_cache[_fid] = {"first_sheet_id": int(_sid), "sheets": _sheets}
                                    print(f"[SCHEMA CACHE] file_id={_fid} cached, first sheet_id={_sid}")
                    else:
                        result = {"error": f"未知工具: {name}"}
                except Exception as e:
                    import sys, traceback
                    error_detail = traceback.format_exc()
                    # Windows 控制台常见为 gbk，遇到 ✅ 等字符会触发 UnicodeEncodeError
                    try:
                        print(f"[TOOL ERROR] {name} 执行失败:\n{error_detail}")
                    except UnicodeEncodeError:
                        sys.stdout.write(f"[TOOL ERROR] {name} 执行失败:\n{error_detail}\n")
                    result = {"error": str(e)}

                try:
                    print(f"[TOOL RESULT] {name} => {str(result)[:200]}")
                except UnicodeEncodeError:
                    sys.stdout.write(f"[TOOL RESULT] {name} => {str(result)[:200]}\n")
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _compress_tool_result(name, result),
                })

        return "已达到最大工具调用轮次，请重新描述需求。"
