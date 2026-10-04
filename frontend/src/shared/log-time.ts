/** Absolute event time is distinct from observation time. Never sort the receive stream. */
import type { LogEntry } from "../renderer/protocol";
import { guessStderrLevel } from "./log-levels";

type Translate = (source: string) => string;

/** Preserve file order and exact original lines; never retrofit today's date/zone. */
export function parseFileLogs(text: string, receivedAtMs = Date.now()): LogEntry[] {
  return text.split(/\r?\n/).filter(line => line.trim() !== "").map((raw, index) => {
    const prefix = /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)/.exec(raw)?.[0] ?? "";
    const body = raw.slice(prefix.length).replace(/^\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+/, "");
    return normalizeLog({ ts: prefix, level: guessStderrLevel(raw), message: body, raw, source: "file", run_id: /\[run=([^\]]+)\]/.exec(raw)?.[1], session_id: /\[session=([^\]]+)\]/.exec(raw)?.[1] }, receivedAtMs, index + 1);
  });
}

/** Display semantics first, explicitly labeled raw evidence and reception metadata second. */
export function exportLogEntries(entries: readonly LogEntry[], tr: Translate = s => s): string {
  return entries.map(e => `${formatLogTime(e, tr)} ${e.level.toUpperCase()} ${e.message}\n  ${JSON.stringify({ source: e.source, receivedAt: new Date(e.receivedAtMs ?? 0).toISOString(), receiveSequence: e.receiveSequence, raw: e.raw })}`).join("\n");
}
const pad = (value: number, width = 2): string => String(value).padStart(width, "0");

/** Local zone is resolved at display time; offset is per instant (including DST). */
export function formatLogTime(entry: LogEntry, tr: Translate = s => s): string {
  if (entry.eventTimeMs == null) return `${entry.ts || "—"} [${tr("时间不完整或无效：无法确定绝对时刻")}]`;
  const d = new Date(entry.eventTimeMs);
  const offset = -d.getTimezoneOffset();
  const zone = `UTC${offset < 0 ? "-" : "+"}${pad(Math.floor(Math.abs(offset) / 60))}:${pad(Math.abs(offset) % 60)}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)} ${zone}`;
}

export function normalizeLog(entry: LogEntry, receivedAtMs: number, receiveSequence: number): LogEntry {
  const ts = String(entry.ts ?? "");
  const absolute = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(ts);
  // Date.parse normalizes Feb 30 and 24:00; reject those rather than invent an instant.
  const calendar = new Date(`${ts.slice(0, 19)}Z`);
  const validCalendar = Number.isFinite(calendar.getTime()) && calendar.toISOString().slice(0, 19) === ts.slice(0, 19);
  const parsed = absolute && validCalendar ? Date.parse(ts) : NaN;
  return { ...entry, ts, eventTimeMs: Number.isFinite(parsed) ? parsed : null,
    receivedAtMs, receiveSequence, source: entry.source ?? "renderer",
    raw: entry.raw ?? JSON.stringify(entry) };
}
