; VoxSub installer script (Inno Setup 6+)
; 语幕 VoxSub - 大众实时翻译安装包
; Build: 用 InnoSetup (iscc) 编译本脚本 -> VoxSub-Setup-0.4.1-beta.exe
;   iscc scripts\installer.iss
; 说明: 大型模型不打包进安装包, 首次运行经模型广场下载(断点续传+SHA256)。
;       2.3MB 的基础 VAD 随程序分发，首次启动会自动修复到
;       用户模型默认保存到安装目录的 Models\vad，确保新设备下载 ASR 后能直接运行。
;       自签版发布物附 SHA256 + 解除 SmartScreen 指引。正式 OV 证书后签名由
;       build.ps1 的 osslsigncode 统一处理(SignedSetup)。

#define MyAppName "VoxSub"
#define MyAppVersion "0.4.1-beta"
#define MyAppPublisher "VoxSub"
#define MyAppExeName "VoxSub.exe"
#define MyAppId "{{7B5F6A3C-2E8D-4B1A-9C7E-VOXSUB0000001}"

[Setup]
AppId={#MyAppId}
AppName={cm:MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoDescription=VoxSub Setup
VersionInfoProductName=VoxSub
DefaultDirName={autopf}\VoxSub
DefaultGroupName={cm:MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
; 正式版发布物统一输出到 D:\OneDrive\app_dve\Release (用户约定 2026-08-17)
OutputDir=..\..\Release
OutputBaseFilename=VoxSub-Setup-0.4.1-beta
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 按 Windows UI 语言自动选择；无对应翻译时回退到英文，不显示语言选择弹窗。
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=no
ArchitecturesInstallIn64BitMode=x64compatible
; 图标(若有: SetupIconFile=..\assets\icon.ico)
; 签名(正式 OV 证书后启用):
; SignTool=signtool /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f
; SignedUninstaller=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "chinesetraditional"; MessagesFile: "compiler:Languages\ChineseTraditional.isl"

[CustomMessages]
english.MyAppName=VoxSub
chinesesimplified.MyAppName=语幕 VoxSub
chinesetraditional.MyAppName=語幕 VoxSub
english.CreateDesktopShortcut=Create a desktop shortcut
chinesesimplified.CreateDesktopShortcut=创建桌面快捷方式
chinesetraditional.CreateDesktopShortcut=建立桌面捷徑
english.AdditionalIcons=Additional icons:
chinesesimplified.AdditionalIcons=附加图标：
chinesetraditional.AdditionalIcons=附加圖示：
english.LaunchApp=Launch %1
chinesesimplified.LaunchApp=立即启动 %1
chinesetraditional.LaunchApp=立即啟動 %1

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopShortcut}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; VoxSub 主程序 + 全部运行时 (PyInstaller onedir 输出)
Source: "..\dist\VoxSub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; 模型是用户数据，不随升级/卸载删除。只让普通用户写 Models，程序文件仍受保护。
Name: "{app}\Models"; Permissions: users-modify

[Icons]
Name: "{group}\{cm:MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{cm:MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchApp,{cm:MyAppName}}"; Flags: nowait postinstall skipifsilent

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
