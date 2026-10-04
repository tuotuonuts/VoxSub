/** Only upstream repository roots, never mirrors, release assets or executable URLs. */
export function repositoryTarget(value: string): { url: string; provider: "github" | "huggingface" } | null {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port || url.search || url.hash) return null;
    if (!/^\/[^/]+\/[^/]+\/?$/.test(url.pathname)) return null;
    if (url.hostname !== "github.com" && url.hostname !== "huggingface.co") return null;
    return { url: url.href, provider: url.hostname === "github.com" ? "github" : "huggingface" };
  } catch { return null; }
}
