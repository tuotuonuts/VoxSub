type Call = (command: string, args: Record<string, unknown>) => Promise<unknown>;
let languageUpdateQueue: Promise<void> = Promise.resolve();

export function persistLanguagePair(
  source: string,
  target: string,
  call: Call,
  report: (message: string) => void,
): Promise<void> {
  const update = languageUpdateQueue.then(async () => {
    await call("set_langs", { source, target });
    await call("set_config", { updates: { lang_pair: `${source}-${target}` } });
  });
  languageUpdateQueue = update.catch((error: unknown) => {
    report(`语言对 ${source}-${target} 更新失败: ${String(error)}`);
  });
  return update;
}
