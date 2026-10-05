/** Readable inventory formatting; never infer a chipset, RAM brand or GPU memory. */
import type { HardwareCategory, HardwareProfile } from "../renderer/protocol";
type Item = Record<string, string | number | null>;
type Translate = (text: string) => string;
export interface HardwareRow { label: string; values: string[] }
const text = (item: Item, key: string): string => String(item[key] ?? "").trim();
const positive = (item: Item, key: string): number => typeof item[key] === "number" && Number.isFinite(item[key]) && item[key] > 0 ? item[key] : 0;
const amount = (value: number): string => Number(value.toFixed(1)).toString();
const join = (...parts: string[]): string => parts.filter(Boolean).join(" · ");
const name = (item: Item, key: string, tr: Translate): string => text(item, key) || tr("型号未提供");
function memoryItem(item: Item, tr: Translate): string {
  const raw = text(item, "Manufacturer");
  const brand = !raw || /^(?:0x)?[0-9a-f]{4,}$/i.test(raw) || /^(unknown|undefined)$/i.test(raw) ? tr("品牌未提供") : raw;
  const ddr: Record<number, string> = { 20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5" };
  const bytes = positive(item, "Capacity");
  const configured = positive(item, "ConfiguredClockSpeed");
  const speed = configured || positive(item, "Speed");
  return join([brand, text(item, "PartNumber")].filter(Boolean).join(" "), bytes ? amount(bytes / 2 ** 30) + " GB" : tr("容量未提供"),
    ddr[positive(item, "SMBIOSMemoryType")] || tr("内存类型未提供"), speed ? `${speed} MT/s${configured ? "" : " (" + tr("标称") + ")"}` : tr("速率未提供"));
}
function formatItem(category: HardwareCategory, item: Item, tr: Translate): string {
  switch (category) {
    case "motherboard": return [text(item, "Manufacturer"), name(item, "Product", tr)].filter(Boolean).join(" ");
    case "memory": return memoryItem(item, tr);
    case "gpus": return join(name(item, "Name", tr), positive(item, "vramGb") ? amount(positive(item, "vramGb")) + " GB" : tr("显存未确认"), text(item, "DriverVersion") ? tr("驱动") + " " + text(item, "DriverVersion") : "");
    case "monitors": return join(name(item, "Name", tr), [text(item, "Manufacturer"), text(item, "ProductCode")].filter(Boolean).join(" / "), positive(item, "SizeInches") ? tr("约 {size} 英寸（EDID）").replace("{size}", amount(positive(item, "SizeInches"))) : tr("尺寸未提供"));
    case "disks": return join(name(item, "Model", tr), positive(item, "Size") ? String(Math.round(positive(item, "Size") / 1e9)) + " GB" : tr("容量未提供"));
    case "os": return join(name(item, "Caption", tr), text(item, "Version"), text(item, "BuildNumber") ? "Build " + text(item, "BuildNumber") : "");
    case "bios": return join(text(item, "Manufacturer"), name(item, "SMBIOSBIOSVersion", tr));
    default: return name(item, "Name", tr);
  }
}
export function hardwareRows(profile: HardwareProfile, tr: Translate): HardwareRow[] {
  const cpu = /Family\s+\d+\s+Model\s+\d+/i.test(profile.cpu) ? tr("型号未提供") : profile.cpu || tr("型号未提供");
  const rows: HardwareRow[] = [{label: tr("处理器"), values: [tr("{model}（{cores} 核 / {threads} 线程）").replace("{model}", cpu).replace("{cores}", String(profile.physicalCores)).replace("{threads}", String(profile.logicalCores))]}];
  const labels: Array<[HardwareCategory, string]> = [["motherboard", "主板"], ["memory", "内存"], ["gpus", "显卡"], ["monitors", "显示器"], ["disks", "磁盘"], ["sound", "声卡"], ["network", "网卡"], ["os", "操作系统"], ["bios", "BIOS"]];
  for (const [category, label] of labels) {
    const group = profile.inventory?.categories[category];
    let values = [tr(group?.status === "not_detected" ? "未检测到" : "未能读取")];
    if (group?.status === "ok") values = group.items.length ? group.items.map(item => formatItem(category, item, tr)) : [tr("未能读取")];
    if (!group && category === "memory") values = [amount(profile.ramGb) + " GB · " + tr("系统可用总量，内存条信息未检查")];
    if (!group && category === "gpus") values = [profile.gpu ? join(profile.gpu, tr("显存未确认")) : tr("未检查")];
    if (category === "memory" && group?.status === "ok") {
      const capacities = group.items.map(item => positive(item, "Capacity"));
      if (capacities.length && capacities.every(Boolean)) values.unshift(amount(capacities.reduce((a,b) => a+b, 0) / 2**30) + " GB（" + capacities.map(c => amount(c / 2**30) + " GB").join(" + ") + "）");
    }
    rows.push({label: tr(label), values});
  }
  rows.push({label: "NPU", values: [profile.npu || tr("未检测到")]}, {label: tr("可用推理后端（非实际运行设备）"), values: [profile.gpuProvider || "CPU"]});
  return rows;
}
