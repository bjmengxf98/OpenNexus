# Security Policy

[简体中文](SECURITY.md) · [English](SECURITY_EN.md)

## Reporting a Vulnerability

Do not disclose exploitable details, credentials, or user data in a public issue. Prefer the repository's GitHub Private Vulnerability Reporting / Security Advisory feature. Include affected versions, reproduction steps, and likely impact.

## Supported Versions

Security fixes target the latest version on the default branch. Older deployments should upgrade before validating a report.

## Deployment Requirements

- Use a unique, strong `SESSION_SECRET`; enable HTTPS and secure cookies in production.
- Never publish `.env`, SQLite databases, backups, logs, uploads, or QR codes.
- Apply least privilege and regular rotation to WPS, model, email, MCP, and WeChat credentials.
- MCP tokens are displayed only once and must not be shared in screenshots or public chats.
- Restrict access to the administration UI and database backups to trusted networks and users.
- Treat the personal-WeChat bridge as experimental and evaluate platform, privacy, and account risks before enabling it.

If a credential ever enters Git history or a public attachment, revoke or rotate it immediately and remove it from the entire history. Deleting it only in a later commit does not eliminate the exposure.
