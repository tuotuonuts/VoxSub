; VoxSub installer script (Inno Setup 6)
; 语幕 VoxSub - 大众实时翻译安装包
; Build: 用 InnoSetup (iscc) 编译本脚本 -> VoxSub-Setup-0.3.3-beta.exe
;   iscc scripts\installer.iss
; 说明: 模型(2.4GB)不打包进安装包, 首次运行经诊断页/设置引导下载(断点续传+SHA256)。
;       安装包本体只含 VoxSub.exe + 运行时; 模型放 %LOCALAPPDATA%\VoxSub\models。
;       自签版发布物附 SHA256 + 解除 SmartScreen 指引。正式 OV 证书后签名由
;       build.ps1 的 osslsigncode 统一处理(SignedSetup)。

#define MyAppName "语幕 VoxSub"
#define MyAppVersion "0.3.3-beta"
#define MyAppPublisher "VoxSub"
#define MyAppExeName "VoxSub.exe"
#define MyAppId "{{7B5F6A3C-2E8D-4B1A-9C7E-VOXSUB0000001}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\VoxSub
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
; 正式版发布物统一输出到 D:\OneDrive\app_dve\Release (用户约定 2026-08-17)
OutputDir=..\..\Release
OutputBaseFilename=VoxSub-Setup-0.3.3-beta
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; 图标(若有: SetupIconFile=..\assets\icon.ico)
; 签名(正式 OV 证书后启用):
; SignTool=signtool /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f
; SignedUninstaller=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
; VoxSub 主程序 + 全部运行时 (PyInstaller onedir 输出)
Source: "..\dist\VoxSub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Registry]
; 可选: 注册文件关联(.wav/.mp4 -> 语幕打开) -- 二期再做, 此略

[Code]
(*
  安装后检测模型缺失, 弹提示引导下载 (ModelScope 主源)。
  注意: 此脚本本身不下载模型(避免安装器巨大), 由 app 首次运行诊断页引导。
*)
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
