"""Safe actionable download errors; never echo URLs, proxy credentials or raw bodies."""
from __future__ import annotations

import http.client
import ssl
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit


class DownloadError(RuntimeError):
    def __init__(self, code: str, detail: str, suggestion: str) -> None:
        self.code = code
        self.detail = detail
        self.suggestion = suggestion
        super().__init__(f"{detail}。{suggestion}")


def describe_download_error(error: BaseException) -> DownloadError:
    if isinstance(error, DownloadError):
        return error
    if isinstance(error, HTTPError):
        details = {401: "下载源要求身份验证", 403: "下载源拒绝访问",
                   404: "下载地址不存在或已失效", 410: "文件已从下载源移除",
                   416: "断点超出服务器文件范围"}
        detail = details.get(error.code, "下载服务器请求失败")
        return DownloadError("http", f"HTTP {error.code}：{detail}",
                             "请更换下载源；若仍失败，请检查网络或更新模型清单")
    reason = getattr(error, "reason", error)
    if isinstance(reason, TimeoutError):
        return DownloadError("timeout", "连接下载源超时", "断点已保留，请检查网络或代理后继续下载")
    if isinstance(reason, ssl.SSLError):
        return DownloadError("tls", "下载源安全连接或证书验证失败", "请检查系统时间及代理证书；不要关闭安全验证")
    if isinstance(reason, http.client.IncompleteRead):
        return DownloadError("interrupted", "下载连接提前中断", "断点已保留，请继续下载或更换下载源")
    return DownloadError("connection", "无法完成下载连接或文件写入", "请检查网络、代理及磁盘空间后继续下载")


def safe_source_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    except ValueError:
        return "[invalid download source]"
