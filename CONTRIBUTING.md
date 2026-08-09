# 贡献指南

[简体中文](CONTRIBUTING.md) · [English](CONTRIBUTING_EN.md)

感谢参与 OpenNexus。

## 开发流程

1. 从主分支创建功能分支。
2. 复制 `.env.example` 为 `.env`，只使用测试账号和测试数据。
3. 安装 `requirements.txt` 和 `requirements-dev.txt`。
4. 保持改动聚焦，并为行为变化补充测试和文档。
5. 提交合并请求前运行项目测试。

```bash
python -m pytest test_reminders.py test_dashboard.py test_app_new.py test_pwa.py test_admin_settings_new.py -q
```

## 数据与凭据红线

不得提交以下内容：

- `.env`、数据库及备份、生产日志
- WPS、模型、邮件、MCP、微信或其他访问凭据
- 真实姓名、联系方式、聊天记录、部门任务和项目数据
- 含真实业务内容的截图、录屏、上传文件与二维码

示例和测试夹具必须匿名化、最小化，并能公开传播。

## 第三方代码

引入或修改第三方代码时，请在合并请求中注明来源、版本、许可证和本地修改，保留其版权及许可证文本。不得引入与 Apache-2.0 发布方式不兼容、来源不明或无权再分发的内容。
