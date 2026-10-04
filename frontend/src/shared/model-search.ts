/** Local catalog search. No UI, IPC, regex built from input, or query persistence. */
export type ModelSearchScope = "all" | "name" | "tags";
interface SearchField { text: string; compact: string; words: string[]; scope: "name" | "tags" | "description"; weight: number }
export interface SearchDocument<T> { value: T; fields: SearchField[] }

export function normalizeSearch(text: string): string {
  return text.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

export function createSearchDocument<T>(value: T, fields: {
  names: readonly string[]; tags: readonly string[]; descriptions?: readonly string[];
}): SearchDocument<T> {
  const groups = [["name", fields.names, 100], ["tags", fields.tags, 75],
    ["description", fields.descriptions ?? [], 35]] as const;
  const indexed: SearchField[] = [];
  for (const [scope, strings, weight] of groups) {
    for (const text of new Set(strings.map(normalizeSearch).filter(Boolean))) {
      indexed.push({ text, compact: text.replace(/ /g, ""), words: text.split(/ +/), scope, weight });
    }
  }
  return { value, fields: indexed };
}

/** Bounded optimal-string-alignment distance, including adjacent letter transpositions. */
function typoDistance(a: string, b: string, limit: number): number {
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  let older = previous;
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    for (let j = 1; j <= b.length; j++) {
      row[j] = Math.min(row[j - 1]! + 1, previous[j]! + 1,
        previous[j - 1]! + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        row[j] = Math.min(row[j]!, older[j - 2]! + 1);
      }
    }
    older = previous; previous = row;
  }
  return previous[b.length]!;
}

function fieldScore(field: SearchField, term: string): number {
  if (field.text.includes(term)) return field.weight + (field.text === term ? 15 : 0);
  const compact = term.replace(/ /g, "");
  if (compact.length >= 2 && field.compact.includes(compact)) return field.weight - 5;
  // Typos on short words/CJK are too ambiguous. Keep them literal; cap edit-work at 32 chars.
  if (!/^[a-z0-9]{4,32}$/.test(term)) return 0;
  const limit = term.length >= 8 ? 2 : 1;
  let best = 0;
  for (const word of field.words) {
    if (!/^[a-z0-9]{4,32}$/.test(word)) continue;
    const edits = typoDistance(term, word, limit);
    if (edits <= limit) best = Math.max(best, field.weight - 25 - edits * 4);
  }
  return best;
}

export function searchModels<T>(documents: readonly SearchDocument<T>[], query: string,
  scope: ModelSearchScope = "all"): T[] {
  // Whitespace-delimited terms are ANDed; quotes keep a multi-word phrase together.
  const raw = query.slice(0, 160).match(/"[^"]+"|'[^']+'|\S+/g) ?? [];
  const terms = [...new Set(raw.map(normalizeSearch).filter(Boolean))];
  if (!terms.length) return documents.map(doc => doc.value);
  const hits: Array<{ value: T; score: number; index: number }> = [];
  documents.forEach((doc, index) => {
    const fields = doc.fields.filter(field => scope === "all" || field.scope === scope);
    let score = 0;
    for (const term of terms) {
      const best = fields.reduce((max, field) => Math.max(max, fieldScore(field, term)), 0);
      if (!best) return;
      score += best;
    }
    hits.push({ value: doc.value, score, index });
  });
  return hits.sort((a, b) => b.score - a.score || a.index - b.index).map(hit => hit.value);
}
