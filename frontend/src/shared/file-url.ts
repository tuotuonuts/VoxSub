/** Encode local image paths without turning #, ?, %, spaces into URL syntax. */
export function localFileUrl(file: string): string {
  const path = file.replace(/\\/g, "/");
  if (path.startsWith("//")) {
    const [host, ...parts] = path.slice(2).split("/");
    return `file://${host}/${parts.map(encodeURIComponent).join("/")}`;
  }
  const parts = path.split("/").map((part, index) => index === 0 && /^[A-Za-z]:$/.test(part) ? part : encodeURIComponent(part));
  return `file://${path.startsWith("/") ? "" : "/"}${parts.join("/")}`;
}
