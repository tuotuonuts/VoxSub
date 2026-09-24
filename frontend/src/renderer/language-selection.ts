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
): Promise<void> {
  const update = languageUpdateQueue.then(async () => {
    const langsResult = await call("set_langs", { source, target });
    if (!commandSucceeded(langsResult)) {
      throw new Error("set_langs 未确认成功");
    }
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