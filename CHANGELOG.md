# Changelog

All notable public changes to OpenNexus are recorded here.

## [Unreleased]

### Added

- User-, conversation-, and WPS-data-source-scoped context memory
- Relevant history retrieval based only on the current user's own statements

### Changed

- Conversation summaries are isolated per topic and exclude assistant-generated claims
- Live WPS data remains authoritative; runtime file and sheet selection is not persisted as memory
- Legacy memory behavior remains available as a compatibility fallback

## [1.0.0] - 2026-08-09

### Added

- Native HTML/CSS/JavaScript desktop and mobile PWA interface
- Natural-language WPS multidimensional spreadsheet operations
- Daily progress, task, project, and department dashboards
- Topic-based conversation history and role-aware administration
- Knowledge base with RAG embedding and document ingestion
- Smart reminders and retryable notification delivery
- WeCom, WPS messaging, and experimental personal-WeChat integration
- MCP server and token management for compatible external AI clients
- Chinese and English open-source documentation and community templates

### Security

- Removed hard-coded WPS, SMTP, session, and initial-administrator credentials
- Excluded runtime databases, backups, uploads, logs, local settings, and internal media from source control
- Added security policy, example environment configuration, dependency pins, and automated tests

This is the first public release baseline. Earlier internal development history is intentionally not reproduced because it contains deployment-specific and business-specific information.
