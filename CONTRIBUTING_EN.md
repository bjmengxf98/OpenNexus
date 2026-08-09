# Contributing to OpenNexus

[简体中文](CONTRIBUTING.md) · [English](CONTRIBUTING_EN.md)

Thank you for contributing to OpenNexus.

## Development Workflow

1. Create a focused branch from the default branch.
2. Copy `.env.example` to `.env` and use test-only accounts and data.
3. Install `requirements.txt` and `requirements-dev.txt`.
4. Keep changes focused and add tests and documentation for behavioral changes.
5. Run the core test suite before opening a pull request.

```bash
python -m pytest test_reminders.py test_dashboard.py test_app_new.py test_pwa.py test_admin_settings_new.py -q
```

## Data and Credential Rules

Never commit:

- `.env`, databases, backups, or production logs
- WPS, model, email, MCP, WeChat, or other credentials
- Real names, contact details, chat history, tasks, or project data
- Screenshots, recordings, uploads, or QR codes containing real business information

Fixtures and examples must be anonymized, minimal, and safe for public distribution.

## Third-Party Code

When adding or modifying third-party code, document its source, version, license, and local changes in the pull request. Preserve all required copyright and license notices. Do not add code of unknown origin or code that cannot be redistributed with this project.
