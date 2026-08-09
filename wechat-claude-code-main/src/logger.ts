import { mkdirSync, appendFileSync, readdirSync, unlinkSync, statSync, truncateSync } from "node:fs";
import { join, dirname } from "node:path";
import { homedir } from "node:os";

const LOG_DIR = join(homedir(), ".wechat-claude-code", "logs");
const MAX_LOG_FILES = 3;                  // 只保留最近3天
const MAX_LOG_BYTES = 50 * 1024 * 1024;  // 单文件最大50MB

/** Clean up old log files and truncate oversized current log. */
function cleanupOldLogs(): void {
  try {
    const files = readdirSync(LOG_DIR)
      .filter((f) => f.startsWith("bridge-") && f.endsWith(".log"))
      .sort();
    while (files.length > MAX_LOG_FILES) {
      unlinkSync(join(LOG_DIR, files.shift()!));
    }
    // 当天日志超过50MB则截断（只保留最后1MB）
    const today = new Date().toISOString().slice(0, 10);
    const todayLog = join(LOG_DIR, `bridge-${today}.log`);
    try {
      const size = statSync(todayLog).size;
      if (size > MAX_LOG_BYTES) {
        truncateSync(todayLog, 1024 * 1024);
      }
    } catch { }
  } catch {
    // Ignore errors during cleanup
  }
}

/**
 * Redact sensitive values from a string:
 * - Bearer tokens (Authorization headers)
 * - aes_key values
 * - generic token/secret values in JSON payloads
 */
export function redact(obj: unknown): string {
  const raw = typeof obj === "string" ? obj : JSON.stringify(obj);
  if (!raw) return raw;

  let safe = raw;
  // Mask Bearer tokens: "Bearer <anything>"
  safe = safe.replace(/Bearer\s+[^\s"\\]+/gi, "Bearer ***");
  // Mask generic token/secret/password/api_key values in JSON
  safe = safe.replace(
    /"(?:(?:[\w]+_)?token|secret|password|api_key)"\s*:\s*"[^"]*"/gi,
    (match) => {
      const key = match.match(/"[^"]*"/)?.[0] ?? '""';
      return `${key}: "***"`;
    },
  );
  return safe;
}

function ensureLogDir(): void {
  mkdirSync(LOG_DIR, { recursive: true });
  cleanupOldLogs();
}

function getLogFilePath(): string {
  const now = new Date();
  const date = now.toISOString().slice(0, 10); // YYYY-MM-DD
  return join(LOG_DIR, `bridge-${date}.log`);
}

function writeLogLine(level: string, message: string, data?: unknown): void {
  ensureLogDir();
  const timestamp = new Date().toISOString();
  const parts = [timestamp, level, message];
  if (data !== undefined) {
    parts.push(redact(data));
  }
  const line = parts.join(" ") + "\n";
  appendFileSync(getLogFilePath(), line, "utf-8");
}

export const logger = {
  info(message: string, data?: unknown): void {
    writeLogLine("INFO", message, data);
  },
  warn(message: string, data?: unknown): void {
    writeLogLine("WARN", message, data);
  },
  error(message: string, data?: unknown): void {
    writeLogLine("ERROR", message, data);
  },
  debug(message: string, data?: unknown): void {
    writeLogLine("DEBUG", message, data);
  },
} as const;
