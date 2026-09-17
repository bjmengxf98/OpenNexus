# Changelog

All notable public changes to OpenNexus are recorded here.

## [Unreleased]

### Added

- User-, conversation-, and WPS-data-source-scoped context memory
- Relevant history retrieval based only on the current user's own statements

### Changed

- Normal business replies now hide WPS internal record IDs and technical letter codes while retaining them for tool execution and verification; explicit technical ID requests remain supported.
- The administration user filter now resists browser credential autofill, searches email addresses explicitly, and provides clear-filter and unambiguous refresh-data actions so returning to the Users tab cannot hide all accounts behind an accidental login-email filter.
- Browser uploads now remain bound to their conversation across clarification turns instead of being deleted after the first assistant reply; confirmed WPS attachment uploads and explicit conversation clearing/deletion clean the managed file, while internal server paths stay hidden from the chat UI.
- Personal-WeChat outbound delivery now persists the latest per-account conversation context without an inactivity timeout, distinguishes connected from activated, refuses false success without a confirmed message ID, and can hold only explicitly interactive messages for up to 30 minutes until the user sends the first WeChat message; scheduled reminders are never placed in this bridge queue, and ambiguous upstream throttling no longer erases a valid saved context.
- Personal-WeChat bridges are now isolated per OpenNexus deployment by credential directory, port range, and instance identity; a process never borrows another checkout's bridge, takeover or token expiry stops automatic restart and requires a new QR scan, while interactive sends fail fast and are attempted at most once per assistant turn.
- Reminder delivery now expires messages more than 30 minutes late, past their separate event time, or after five failed attempts; terminal expiry is audited and historical backlog is not replayed after WeChat reconnects.
- Assistant prompts now include an authoritative Beijing-time calendar for the day before yesterday, yesterday, today, and tomorrow, preventing models from calculating relative weekdays with the wrong year.
- Dashboard summaries now reject identifier-like and nonnumeric fields as numeric indicators, translate WPS link/cascade internals into business labels, deduplicate indicators, and keep the overview to a small set of decision-useful metrics.
- Model-designed dashboard charts now share an auto-fitting responsive grid with metrics and hints, preventing a variable number of charts from stacking in one narrow column while leaving the other column empty.
- Dashboard overviews can now use the configured model to create a validated declarative design from real worksheet schemas; calculations remain local, plans are cached per user/file/schema, and model access requires the existing explicit confirmation.
- Dashboard overview synchronization now reads only WPS database-sheet types, skips and reports dashboards, instruction pages, and other non-record pages, and no longer reveals the previous file's dashboard after a failed file switch.
- The dashboard now rechecks stale empty daily snapshots, selects the latest cached date for a switched file, and avoids refetching unrelated worksheets when opening or refreshing a specialized view.
- The dashboard overview now reads every worksheet in the selected WPS multidimensional file and derives record counts, numeric metrics, categorical distributions, and previews from actual fields; task/project/daily tabs appear only for matching sheets, and partial reads are explicitly labeled.
- The dashboard is presented as `业务智能驾驶舱` across its page, reports, PWA shortcut, help and MCP skill; historical rule-generated snapshots display updated generic labels without rewriting WPS records or saved AI prose.
- Product-facing name is now `业务智能助手` across login, workspace, PWA, email, MCP metadata, and current help pages; WPS multidimensional spreadsheets remain a supported capability.
- DeepSeek direct-API presets now use `deepseek-flash`, advertise its 1M context and multimodal capability, preserve legacy model aliases, apply the official default reasoning effort, and retry once without thinking instead of saving an empty response
- Word generation now validates and normalizes structured sections before rendering; malformed model output is rejected with a retryable receipt and is never uploaded as visible JSON source text
- Conversation summaries are isolated per topic and exclude assistant-generated claims
- Live WPS data remains authoritative; runtime file and sheet selection is not persisted as memory
- Legacy memory behavior remains available as a compatibility fallback
- Task-creation replies now require a successful `create_records` tool receipt and post-write readback before claiming completion; fabricated WPS errors are blocked
- Optional personal-WeChat bridges now use bounded exponential restart backoff, persistent child-process logs, and a failure circuit breaker without affecting the main service
- Conversation sidebars now support owner-scoped batch selection and atomic deletion with explicit irreversible-action confirmation
- Mobile PWA sidebars now place the new-conversation action directly above the conversation list and below the mobile function menu
- Reminder-list questions now route to `list_reminders` and render the current user's database-backed result instead of allowing models to deny an existing capability
- Reminder confirmations now distinguish persisted schedules from best-effort delivery through currently available notification channels
- Repeated unfiltered WPS record reads now receive a non-terminating strategy hint to use precise filters or aggregation tools without imposing a fixed tool-round limit
- Conversation Token metadata now uses a higher-contrast separator in both light and dark themes

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
