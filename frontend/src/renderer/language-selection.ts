export function splitLanguagePair(pair: string): [string, string] {
  // Registry codes use ISO language + optional script (e.g. zh-hant), not a
  // fixed list of languages. Keep both sides intact when restoring settings.
  const match = /^((?:auto|[a-z]{2,3})(?:-[a-z]{4})?)-([a-z]{2,3}(?:-[a-z]{4})?)$/.exec(pair.toLowerCase());
  return match ? [match[1]!, match[2]!] : ["", ""];
}

type Call = (command: string, args: Record<string, unknown>) => Promise<unknown>;
let languageUpdateQueue: Promise<void> = Promise.resolve();

function commandSucceeded(result: unknown): boolean {
  if (result === null || result === undefined) return false;
  if (typeof result === "object" && result !== null && "outcome" in result) {
    return (result as { outcome?: unknown }).outcome === "ok";
  }
  return true;
}

export function persistLanguagePair(
  source: string,
  target: string,
  call: Call,
  report: (message: string) => void,
  isCurrent: () => boolean = () => true,
): Promise<void> {
  const update = languageUpdateQueue.then(async () => {
    if (!isCurrent()) return;
    const langsResult = await call("set_langs", { source, target });
    if (!commandSucceeded(langsResult)) {
      throw new Error("set_langs 未确认成功");
    }
    if (!isCurrent()) return;
    const configResult = await call("set_config", { updates: { lang_pair: `${source}-${target}` } });
    if (!commandSucceeded(configResult)) {
      throw new Error("set_config 未确认成功");
    }
  });
  languageUpdateQueue = update.catch((error: unknown) => {
    report(`语言对 ${source}-${target} 更新失败: ${String(error)}`);
  });
  return update;
}