/** User-facing task messages. Technical command names stay in diagnostic logs. */
import type { CommandName } from "../renderer/protocol";

// Exhaustive: adding an IPC command requires choosing a human-readable name.
const LABELS: Record<CommandName, string> = {
  record_overlay_diagnostic: "更新悬浮窗诊断",
  ping: "检查应用连接",
  shutdown: "关闭应用",
  start: "启动会话",
  stop: "结束会话",
  pause: "暂停会话",
  resume: "继续会话",
  state: "查询会话状态",
  set_mode: "切换模式",
  set_langs: "更新翻译语言",
  language_capabilities: "检查支持的语言",
  set_input_file: "选择音视频文件",
  list_audio_devices: "读取音源列表",
  list_capture_targets: "读取应用列表",
  set_audio_devices: "更新音源",
  set_capture_process: "选择应用音源",
  set_stt: "更新识别方式",
  set_translator: "选择翻译模型",
  set_asr_model: "选择识别模型",
  set_asr_tuning: "保存识别调优",
  asr_tuning_meta: "读取识别调优选项",
  translate_tiers: "读取翻译选项",
  set_tts: "更新朗读设置",
  set_tts_models: "选择朗读模型",
  set_recording: "更新录音设置",
  last_recording: "查询录音文件",
  list_models: "读取模型列表",
  clear_logs: "清理日志",
  log_path: "查询日志位置",
  import_models: "导入模型",
  release_notes: "读取更新说明",
  recent_logs: "读取近期日志",
  install_model: "安装模型",
  prepare_model_download: "准备模型下载",
  pause_model_download: "暂停模型下载",
  delete_model_download: "删除未完成下载",
  uninstall_model: "卸载模型",
  model_dir: "查询模型位置",
  get_config: "读取设置",
  set_config: "保存设置",
  run_self_check: "运行自检",
  developer_mode: "更新开发者模式",
  diagnostic_snapshot: "收集诊断信息",
  diagnostic_session: "更新详细日志设置",
  export_diagnostics: "导出诊断报告",
  list_devices: "读取设备列表",
  hardware_profile: "检查设备信息",
  export_subtitles: "导出字幕",
  ocr_recognize: "识别图片文字",
  render_ocr_image: "生成翻译图片",
  copy_file: "保存文件",
  ocr_cache_dir: "查询图片缓存位置",
  ocr_release: "释放图片翻译资源",
  detect_legacy: "查找旧版数据",
  plan_migration: "准备数据迁移",
  start_migration: "迁移数据",
  verify_copy: "校验迁移数据",
  write_model_snapshot: "保存模型清单",
  cleanup_migrated_source: "清理已迁移的原目录",
  migration_decision: "保存迁移选项",
  ocr_translate: "翻译图片文字",
  job_list: "查看后台任务",
  job_status: "查询任务进度",
  cancel_job: "取消后台任务",
};

// A completed request is not proof that a model loaded or a session stopped.
const SUCCESS: Partial<Record<CommandName, string>> = {
  set_config: "设置已保存",
  set_langs: "翻译语言已更新",
  set_mode: "模式选择已更新",
  set_stt: "识别方式已更新",
  set_asr_model: "识别模型选择已更新",
  set_translator: "翻译模型选择已更新",
  set_asr_tuning: "识别调优已保存",
  set_audio_devices: "音源选择已更新",
  set_capture_process: "应用音源选择已更新",
  set_recording: "录音设置已更新",
  set_tts: "朗读设置已更新",
  set_tts_models: "朗读模型选择已更新",
  set_input_file: "音视频文件已选择",
  start: "启动请求已处理",
  stop: "结束请求已处理",
  pause: "暂停请求已处理",
  resume: "继续请求已处理",
  cancel_job: "取消请求已提交",
  run_self_check: "自检已完成，请查看检查结果",
};

export function describeJobFeedback(
  command: string | undefined,
  status: string,
  translate: (source: string) => string = source => source,
): string {
  const known = typeof command === "string" && Object.hasOwn(LABELS, command);
  const key = command as CommandName;
  const label = translate(known ? LABELS[key] : "后台任务");
  if (status === "succeeded") {
    const specific = known && Object.hasOwn(SUCCESS, key) ? SUCCESS[key] : undefined;
    return specific ? translate(specific) : `${label}${translate("：")}${translate("已完成")}`;
  }
  if (status === "failed") {
    return `${label}${translate("：")}${translate("失败，请查看诊断日志")}`;
  }
  if (status === "cancelled") {
    return `${label}${translate("：")}${translate("已取消")}`;
  }
  return translate("任务状态尚未确认");
}
