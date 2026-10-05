/** Keep readable preview text only while its source is progressing compatibly.
 * Authoritative backend draft events always replace this provisional merge.
 */
export interface SubtitleDraft { source: string; translation: string }

export function mergePartialDraft(current: SubtitleDraft | null, source: string): SubtitleDraft {
  const older = current?.source.trim().replace(/\s+/g, " ").toLocaleLowerCase() ?? "";
  const newer = source.trim().replace(/\s+/g, " ").toLocaleLowerCase();
  let progressing = Boolean(older && newer.startsWith(older));
  if (!progressing && older && newer) {
    const left = older.match(/[\p{L}\p{N}']+/gu) ?? [];
    const right = newer.match(/[\p{L}\p{N}']+/gu) ?? [];
    let common = 0;
    while (common < left.length && left[common] === right[common]) common++;
    progressing = right.length >= left.length && common >= Math.max(1, left.length - 1);
  }
  return { source, translation: progressing ? current?.translation ?? "" : "" };
}
