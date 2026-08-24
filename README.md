# OpenNexus 多维表格智能助手

<p align="center">
  <img src="static/icon-512.png" width="112" alt="OpenNexus logo">
</p>

<p align="center"><strong>让 WPS 多维表格、知识库、智能提醒、消息触达与 MCP 真正协同起来</strong></p>

<p align="center">
  简体中文 · <a href="README_EN.md">English</a>
</p>

<p align="center"><a href="https://github.com/bjmengxf98/OpenNexus">github.com/bjmengxf98/OpenNexus</a></p>

> OpenNexus 的定位不是普通表格聊天工具，而是可私有部署的部门级 AI 工作中枢。

OpenNexus 是一个面向部门与小型团队的开源业务智能助手。它把 WPS 多维表格、自然语言智能体、任务与项目分析、知识库、消息提醒和 MCP 接口整合到一个可在电脑与手机 PWA 中使用的系统里。

## 主要能力

- 用自然语言查询和维护 WPS 多维表格
- 每日进展、任务、项目和部门整体驾驶舱
- 定时提醒，以及企业微信、WPS 私信和实验性的个人微信通知
- 文档上传、图片识别、知识库检索（RAG）和文档生成
- 多话题对话、用户与角色管理、审计与反馈
- 对外提供 MCP 服务，让 WorkBuddy 等兼容客户端调用 OpenNexus 的业务能力；支持最小权限令牌、代码级高风险审批和完整审计
- 深色/浅色主题与手机 PWA 适配

## 技术架构

- 后端：Python 3.12、FastAPI、SQLite
- 前端：原生 HTML、CSS、JavaScript
- 数据与集成：WPS OpenAPI、OpenAI 兼容模型接口
- 可选个人微信桥接：Node.js（第三方 MIT 项目的适配版本）

## 快速开始

### 1. 准备环境

需要 Python 3.12 或更高版本。仅在启用个人微信桥接时需要 Node.js 18 或更高版本。

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS：

```bash
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

### 2. 配置

编辑 `.env`，至少设置：

- `SESSION_SECRET`：长随机字符串
- `INITIAL_ADMIN_EMAIL` 与 `INITIAL_ADMIN_PASSWORD`：仅在首次创建管理员时使用
- 如需 WPS：填写 `WPS_APP_ID`、`WPS_APP_SECRET` 和正确的回调地址
- 如需全局合规通知白名单：在私有 `.env` 中填写 `COMPLIANCE_MEMBER_NAMES` 和 `COMPLIANCE_MEMBER_IDS`

生产环境使用 HTTPS 时，把 `SESSION_COOKIE_SECURE` 设为 `1`。

### 3. 启动

Windows：

```powershell
.venv\Scripts\python app.py
```

Linux/macOS：

```bash
.venv/bin/python app.py
```

打开 `http://127.0.0.1:8000`。首次启动且数据库中没有管理员时，系统会根据 `.env` 创建初始管理员。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest test_reminders.py test_dashboard.py test_app_new.py test_pwa.py test_admin_settings_new.py -q
```

## 数据与安全

- `data/app.db`、`.env`、日志、上传文件和本地配置均不会纳入版本库。
- 不要提交真实用户资料、WPS/模型令牌、微信二维码、聊天记录或部门业务截图。
- 如果这些内容曾经进入 Git 历史，仅删除文件并不够；请立即轮换密钥并清理历史。
- 生产数据库当前由部署者自行保护，建议限制文件权限并进行加密备份。
- 安全问题请参阅 [SECURITY.md](SECURITY.md)。
- 完整操作说明参阅 [中文用户帮助](docs/用户帮助.md)。

## 个人微信桥接说明

`wechat-claude-code-main/` 来源于第三方 MIT 许可项目，并经过 OpenNexus 的多账号、消息推送和服务衔接适配。原许可证保留在该目录中，详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

如需启用该可选组件，请先构建其 Node.js 服务：

```bash
cd wechat-claude-code-main
npm ci
npm run build
```

此能力属于实验性集成，OpenNexus 与微信/腾讯不存在隶属或背书关系。启用前请自行确认平台规则、账号与数据安全风险；核心 WPS、MCP、知识库和其他通知功能不依赖它。

## 参与贡献

欢迎提交问题、功能建议和合并请求。开始前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。提交内容不得包含单位内部数据、个人信息或任何凭据。

## 开源许可

OpenNexus 自有代码采用 [Apache License 2.0](LICENSE)。第三方组件仍适用其各自许可证；详见 [NOTICE](NOTICE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

Copyright 2026 孟宪锋。项目自有代码依据 Apache License 2.0 发布。
