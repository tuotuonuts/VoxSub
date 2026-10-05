/** Bounded UTF-8 line framing for already-decoded stdio chunks. No partial log/JSON lines. */
export class BoundedLineStream {
  private parts: string[] = [];
  private bytes = 0;
  private dropping = false;
  constructor(private readonly maxBytes: number, private readonly line: (text: string) => void,
    private readonly overflow: () => void) {
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new Error("Invalid line limit");
  }
  push(chunk: string): void {
    let start = 0;
    for (;;) {
      const newline = chunk.indexOf("\n", start);
      this.append(chunk.slice(start, newline < 0 ? undefined : newline));
      if (newline < 0) return;
      this.flush();
      start = newline + 1;
    }
  }
  private append(part: string): void {
    if (this.dropping || !part) return;
    this.bytes += Buffer.byteLength(part, "utf8");
    if (this.bytes > this.maxBytes) {
      this.parts = []; this.bytes = 0; this.dropping = true;
      this.overflow();
      return;
    }
    this.parts.push(part);
  }
  flush(): void {
    const text = this.parts.join("");
    const dropping = this.dropping;
    this.parts = []; this.bytes = 0; this.dropping = false;
    if (!dropping && text) this.line(text.endsWith("\r") ? text.slice(0, -1) : text);
  }
}
