/**
 * Electron 实例归属判定 —— 启动器清理逻辑的单一来源。
 *
 * 为什么单独成模块：这段判定被两处用到 ——
 *   · 启动器的清理步骤（真实进程列表）
 *   · 它的测试（合成路径字符串）
 * 如果两处各写一份 PowerShell，改了一处忘了另一处，测试就会"通过"而生产
 * 逻辑早已不同 —— 这正是本项目反复踩过的坑（浮窗菜单测试读 hidden 属性、
 * 真实问题是 CSS 覆盖，测试绿着、功能坏着）。
 *
 * ---------------------------------------------------------------------------
 * 要解决的真实问题
 * ---------------------------------------------------------------------------
 * 用户报"双击 启动Electron版.bat 没反应"报了两次，根因都在这里：
 *
 *   第一次：写死 '*voxsub-electron*'（当时的目录名）。迁入 <repo>/frontend
 *           后该条件永远匹配不到 —— 旧实例杀不掉，新实例被单实例锁挡住。
 *
 *   第二次：改成"只匹配本项目 node_modules"仍不够。工作区里还留着迁移前的
 *           旧检出（app_dve\voxsub-electron），从那里启动的实例与本项目
 *           **共用同一份 userData**（%APPDATA%\voxsub-electron，名字取自
 *           package.json 的 name），照样抢单实例锁，新实例启动后立刻退出。
 *
 * 教训：要清的不是"自己这一个进程"，而是"会争抢同一 userData 的那些进程"。
 *
 * ---------------------------------------------------------------------------
 * 判定规则（用真实进程数据定出来的，不是猜的）
 * ---------------------------------------------------------------------------
 * 实测两类进程的命令行：
 *   主进程   electron.exe D:\...\VoxSub\frontend
 *   子进程   electron.exe --type=gpu-process --user-data-dir="C:\...\AppData\Roaming\voxsub-electron"
 *
 * 因此满足以下任一即属于本应用的实例：
 *
 *   A. ExecutablePath 在本项目 node_modules 下
 *      —— 当前检出，最精确的一条；即使检出被改名也能清掉自己
 *
 *   B. ExecutablePath 含 node_modules\electron，且 CommandLine 含 "voxsub"
 *      —— 其它检出（主进程命令行带检出路径、子进程带 user-data-dir，
 *         两者都含 voxsub）。不写死具体目录名，所以旧检出、副本、改名后的
 *         检出都能覆盖。
 *
 * 为什么不会误杀正式安装的应用：安装版是 <安装目录>\VoxSub.exe，
 * 路径里没有 node_modules\electron。
 * 为什么不会误杀同工作区里别的 Electron 项目：它们的命令行不含 voxsub。
 */

/** 判定用的关键字。取 package.json 的 name（voxsub-electron）的词干。 */
const APP_KEYWORD = "voxsub";

/**
 * 生成归属判定的 PowerShell 表达式。
 *
 * @param {string} ownVar    PowerShell 变量名，值为本项目 node_modules 绝对路径
 * @param {string} pathVar   PowerShell 变量名，值为被测的 ExecutablePath
 * @param {string} cmdVar    PowerShell 变量名，值为被测的 CommandLine
 * @param {string} keyword   CommandLine 里要匹配的关键字
 * @returns {string[]} 可直接拼进脚本的行
 */
export function predicateLines(ownVar, pathVar, cmdVar, keyword = APP_KEYWORD) {
  return [
    `(${pathVar}.StartsWith(${ownVar}, [StringComparison]::OrdinalIgnoreCase) -or`,
    ` (${pathVar}.IndexOf('node_modules\\electron', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and`,
    `  ${cmdVar} -and`,
    `  ${cmdVar}.IndexOf('${keyword}', [StringComparison]::OrdinalIgnoreCase) -ge 0))`,
  ];
}

/**
 * 生成"清理已在运行的实例"脚本。
 *
 * 用 -EncodedCommand 传参是必须的：shell: true 会把多行脚本按空格拆开、
 * 丢掉换行，PowerShell 于是把 `Where-Object` 当成独立命令（exit 255）。
 *
 * @param {{ownMarker: string}} markers
 */
export function buildCleanupScript({ ownMarker }) {
  return [
    `$own = "${ownMarker}";`,
    // 用 Win32_Process 而不是 Get-Process：需要 CommandLine，Get-Process 不给。
    "$all = @(Get-CimInstance Win32_Process -Filter \"Name='electron.exe'\" -ErrorAction SilentlyContinue);",
    "$mine = @($all | Where-Object {",
    ...predicateLines("$own", "$_.ExecutablePath", "$_.CommandLine"),
    "});",
    "$killed = @();",
    "foreach ($p in $mine) {",
    "  $killed += $p.ExecutablePath;",
    "  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue",
    "};",
    // sidecar 残留（异常退出时可能留下）
    "$side = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue |",
    "  Where-Object { $_.CommandLine -and $_.CommandLine.Contains('ipc_server') });",
    "foreach ($p in $side) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }",
    "Write-Output (\"stopped=\" + $mine.Count + \",\" + $side.Count)",
    // 被杀实例的来源目录打出来：清理了意料之外的进程时能立刻发现
    "$dirs = @($killed | ForEach-Object { Split-Path (Split-Path (Split-Path $_ -Parent) -Parent) -Parent } | Sort-Object -Unique);",
    "foreach ($d in $dirs) { Write-Output (\"  from: \" + $d) }",
  ].join("\n");
}

/**
 * 生成"对给定 (路径, 命令行) 做归属判定"的脚本 —— 供测试用。
 *
 * 与 buildCleanupScript 共用 predicateLines，因此测的就是生产逻辑本身，
 * 而不是它的副本。输入是合成数据而非真实进程，所以测试不必真的去启动一个
 * 旧检出的 Electron（那会在用户桌面上弹窗）。
 *
 * @param {{ownMarker: string, cases: Array<{path: string, cmd: string}>}} options
 */
export function buildPredicateProbeScript({ ownMarker, cases }) {
  const quote = (s) => `"${String(s).replace(/"/g, '""')}"`;
  const entries = cases
    .map((c) => `@{ p = ${quote(c.path)}; c = ${quote(c.cmd)} }`)
    .join(", ");

  return [
    `$own = "${ownMarker}";`,
    `$cases = @(${entries});`,
    "foreach ($x in $cases) {",
    "  $hit =",
    ...predicateLines("$own", "$x.p", "$x.c"),
    "  ;",
    '  Write-Output ("MATCH=" + $hit + " ||| " + $x.p + " ||| " + $x.c)',
    "}",
  ].join("\n");
}
