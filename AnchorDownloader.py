"""
AnchorDownloader.py - Enhanced anchor list downloader (Option 20)
==================================================================
功能:
  1. 通过 PixivUtil2.py 直接调用，在同一进程内运行
  2. 完整保留终端颜色、进度条等样式
  3. 集成 Windows 11 原生通知 (windows_toasts)
  4. 支持 --proxy / --noproxy 代理控制
  5. 支持日志记录和下载统计

用法:
  python PixivUtil2.py -s 20 --enhanced [options]
  python PixivUtil2.py -s 20 --enhanced --proxy socks5h://127.0.0.1:7890
  python PixivUtil2.py -s 20 --enhanced --noproxy
  python PixivUtil2.py -s 20 --enhanced --log [--log-file custom.log] [--download-log downloaded.log]
"""

import sys
import os
import re
import csv
import configparser
import threading
import html
import unicodedata
import typing
from threading import Event
from datetime import datetime
from pathlib import Path

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class _Tee:
    def __init__(self, terminal, log_file, strip_ansi=True):
        self._terminal = terminal
        self._log_file = log_file
        self._strip_ansi = strip_ansi

    def write(self, data):
        self._terminal.write(data)
        clean = _ANSI_RE.sub("", data) if self._strip_ansi else data
        try:
            self._log_file.write(clean)
            self._log_file.flush()
        except Exception:
            pass

    def flush(self):
        self._terminal.flush()
        try:
            self._log_file.flush()
        except Exception:
            pass


def ensure_windows_toasts():
    try:
        import windows_toasts

        return True
    except ImportError:
        print("[通知] windows_toasts 未安装，正在尝试安装...")
        try:
            import subprocess

            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "windows_toasts"],
            )
            import windows_toasts

            print("[通知] windows_toasts 安装成功！")
            return True
        except Exception as e:
            print(f"[通知] windows_toasts 安装失败: {e}")
            print("[通知] 请手动运行: pip install windows_toasts")
            return False


class ToastNotifier:
    def __init__(self):
        self.enabled = False
        self.toaster = None
        self.Toast = None
        self.ToastDuration = None
        try:
            from windows_toasts import Toast, ToastDuration, WindowsToaster

            self.toaster = WindowsToaster("Pixiv 下载器")
            self.Toast = Toast
            self.ToastDuration = ToastDuration
            self.enabled = True
        except ImportError:
            print("[通知] 警告: windows_toasts 未安装，通知功能已禁用")
            print("[通知] 运行以下命令安装: pip install windows_toasts")

    def notify(self, title, message, duration="short"):
        if not self.enabled:
            print(f"[通知] {title}: {message}")
            return False
        try:
            toast = self.Toast()
            toast.text_fields = [title, message]
            if duration == "long":
                toast.duration = self.ToastDuration.Long
            self.toaster.show_toast(toast)
            return True
        except Exception as e:
            print(f"[通知] 发送通知失败: {e}")
            return False


class DownloadMonitor:
    def __init__(self, notifier, anchor_file="AnchorList.csv", config=None):
        self.notifier = notifier
        self.anchor_file = anchor_file
        self.config = config
        self.start_time = None
        self.end_time = None
        self.total_members = 0
        self.all_members = 0

        self.fatal_errors = []
        self.recovered_error_count = 0

        self.artists_with_downloads = []
        self.current_download_batch = []

        self.artists_download_files = {}
        self.download_log_path = None
        self._log_lock = threading.Lock()
        self._interactive_event = Event()
        self.anchor_missing = {}
        self.skipped_authors = {}
        self._anchor_dates = {}
        self._anchor_dates_loaded = False

        self._log_header_written = False
        self._log_separator_written = False
        self._run_start_time = None
        self._processed_authors_total = 0
        self._updated_authors = 0
        self._total_downloaded_images = 0
        self._interrupted = False
        # 已计数的画师集合,避免 on_download_info 与 on_member_skip 对同一画师重复计数
        self._processed_mids = set()
        # 网络类跳过去抖:连续累计后只在 flush/完成时统一汇报,避免逐条弹通知轰炸
        self._network_skip_count = 0
        self._network_skip_samples = []

        self._load_initial_data()

    def _load_initial_data(self):
        self.initial_data = {}
        try:
            with open(self.anchor_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    mid = row["member_id"].strip()
                    self.initial_data[mid] = {
                        "artist": sanitize_text(row.get("artist", "Unknown").strip()),
                        "anchor_id": row.get("anchor_id", "").strip(),
                        "anchor_date": row.get("anchor_date", "").strip(),
                        "last_download_images": int(
                            row.get("last_download_images", "0") or 0
                        ),
                        "mark": row.get("mark", "").strip(),
                        "ai_mark": row.get("ai_mark", "").strip(),
                    }
        except Exception as e:
            print(f"[警告] 无法加载锚点文件: {e}")

    def get_anchor_count(self):
        try:
            count = 0
            count_all = 0
            with open(self.anchor_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    count_all += 1
                    enabled = row.get("enabled", "true").strip().lower()
                    if enabled == "true":
                        count += 1
            return count, count_all
        except Exception:
            return 0, 0

    def on_download_info(
        self,
        member_id,
        downloaded,
        anchor_updated,
        artist,
        anchor_date="",
        file_names=None,
    ):
        # 无条件计入"已处理"(通过集合去重,避免与 on_member_skip 重复)
        # 修复: 此前仅在 downloaded>0 or anchor_updated 时才计数,
        # 导致"处理成功但无新图"的画师漏计,网络越差偏差越大
        mid_key = str(member_id)
        if mid_key not in self._processed_mids:
            self._processed_mids.add(mid_key)
            self._processed_authors_total += 1
        if downloaded > 0 or anchor_updated:
            artist_clean = sanitize_text(
                artist or self.initial_data.get(member_id, {}).get("artist", "Unknown")
            )
            file_count = len(file_names) if file_names else downloaded
            entry = {
                "member_id": member_id,
                "artist": artist_clean,
                "downloaded_count": file_count,
                "anchor_updated": anchor_updated,
                "anchor_date": anchor_date,
                "mark": self.initial_data.get(member_id, {}).get("mark", ""),
                "ai_mark": self.initial_data.get(member_id, {}).get("ai_mark", ""),
            }
            self.artists_with_downloads.append(entry)
            self.current_download_batch.append(entry)
            if downloaded > 0:
                self._updated_authors += 1
                self._total_downloaded_images += file_count
                if file_names:
                    self.artists_download_files[member_id] = file_names[:]
                if self.download_log_path:
                    try:
                        self.write_download_log(member_id, entry, file_names)
                    except Exception as e:
                        print(f"[警告] 写入下载日志失败: {e}")
            elif anchor_updated:
                if self.download_log_path:
                    try:
                        self.write_download_log(member_id, entry, file_names)
                    except Exception as e:
                        print(f"[警告] 写入下载日志失败: {e}")

    def on_member_skip(self, member_id, reason, is_error=True):
        mid = str(member_id)
        if reason == "画师已禁用":
            return
        # 通过集合去重计数,避免与 on_download_info 对同一画师重复计数
        if mid not in self._processed_mids:
            self._processed_mids.add(mid)
            self._processed_authors_total += 1
        if not is_error:
            return
        self.anchor_missing[mid] = reason
        self.skipped_authors[mid] = reason
        if self.download_log_path:
            try:
                artist_info = self.initial_data.get(mid, {})
                self.write_download_log(
                    mid,
                    {
                        "artist": artist_info.get("artist", mid),
                        "downloaded_count": 0,
                    },
                    file_names=[reason],
                )
            except Exception:
                pass

    def on_recovered_error(self):
        self.recovered_error_count += 1

    def on_fatal_error(self, error_msg):
        self.fatal_errors.append(error_msg)

    def send_start_notification(self):
        self.start_time = datetime.now()
        self.total_members, self.all_members = self.get_anchor_count()
        title = "Pixiv 下载任务已开始"
        msg = f"锚点列表: {self.anchor_file}"
        if self.total_members > 0:
            msg += f"\n待处理成员: {self.total_members}"
        msg += f"\n开始时间: {self.start_time.strftime('%H:%M:%S')}"
        self._run_start_time = self.start_time
        self._log_header_written = False
        self._log_separator_written = False
        self._processed_authors_total = 0
        self._updated_authors = 0
        self._total_downloaded_images = 0
        self._interrupted = False
        self.skipped_authors = {}
        self._processed_mids.clear()
        self._network_skip_count = 0
        self._network_skip_samples = []
        self.notifier.notify(title, msg, duration="long")

    def flush_network_skips(self):
        """把累计的网络类跳过一次性汇报,避免逐条通知轰炸"""
        if self._network_skip_count == 0:
            return
        title = f"网络错误,已跳过 {self._network_skip_count} 位画师"
        lines = list(self._network_skip_samples[:5])
        if self._network_skip_count > 5:
            lines.append(f"...等共 {self._network_skip_count} 位")
        msg = "\n".join(lines) if lines else "无详情"
        self.notifier.notify(title, msg, duration="long")
        self._network_skip_count = 0
        self._network_skip_samples = []

    def flush_download_batch(self):
        # 先把累计的网络跳过一次性弹出
        self.flush_network_skips()
        if not self.current_download_batch:
            return
        has_download = any(
            item.get("downloaded_count", 0) > 0 for item in self.current_download_batch
        )
        if not has_download:
            self.current_download_batch.clear()
            return
        title = f"下载完成 {len(self.current_download_batch)} 位画师"
        lines = []
        for item in self.current_download_batch[:5]:
            name = item["artist"]
            count = item["downloaded_count"]
            if count > 0:
                lines.append(f"{name}: {count} 张")
        if len(self.current_download_batch) > 5:
            lines.append(f"...等共 {len(self.current_download_batch)} 位")
        msg = "\n".join(lines)
        self.notifier.notify(title, msg)
        self.current_download_batch.clear()

    def send_completion_notification(self):
        self.end_time = datetime.now()
        duration = self.end_time - self.start_time
        total_seconds = int(duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if self.fatal_errors:
            title = "Pixiv 下载任务异常"
        elif self._interrupted:
            title = "Pixiv 下载任务已中断"
        elif self.skipped_authors:
            title = "Pixiv 下载任务部分完成"
        else:
            title = "Pixiv 下载任务全部完成"
        parts = [f"耗时: {hours}时{minutes}分{seconds}秒"]
        parts.append(f"完成时间: {self.end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if self.total_members > 0:
            parts.append(f"处理成员: {self.total_members}/{self.all_members}")

        artist_lines = []
        if self.artists_with_downloads:
            total_images = sum(
                a["downloaded_count"] for a in self.artists_with_downloads
            )
            parts.append(
                f"下载: {len(self.artists_with_downloads)} 位画师, {total_images} 张作品"
            )
            anchor_map = self._load_anchor_dates()
            for a in self.artists_with_downloads:
                mid = a["member_id"]
                name = a["artist"]
                cnt = a["downloaded_count"]
                ad = anchor_map.get(mid, "")
                artist_lines.append(f"[{name}]: {cnt}张新图 - {ad}")

        if self.fatal_errors:
            parts.append(f"致命错误: {len(self.fatal_errors)} 个")

        if self.recovered_error_count > 0:
            parts.append(f"网络重试: {self.recovered_error_count} 次 (已恢复)")

        msg = "\n".join(parts)
        if artist_lines:
            msg += "\n\n" + "\n".join(artist_lines)

        if self.fatal_errors:
            msg += f"\n最后错误: {self.fatal_errors[-1][:200]}"

        if self.anchor_missing:
            missing_lines = []
            for mid, msg_line in self.anchor_missing.items():
                name = self.initial_data.get(mid, {}).get("artist", mid)
                missing_lines.append(f"[{name}]: {msg_line}")
            msg += "\n\n锚点未找到的成员:\n" + "\n".join(missing_lines)

        if (
            self.download_log_path
            and os.path.exists(self.download_log_path)
            and self.notifier.enabled
        ):
            try:
                from windows_toasts import (
                    InteractableWindowsToaster,
                    Toast,
                    ToastButton,
                )

                it = InteractableWindowsToaster("Pixiv 下载器")
                toast = Toast()
                toast.text_fields = [title, msg]
                try:
                    from windows_toasts import ToastScenario

                    toast.scenario = ToastScenario.Reminder
                except Exception:
                    pass
                try:
                    btn = ToastButton("打开下载目录及日志", "open_log")
                    for method_name in (
                        "add_action",
                        "AddAction",
                        "add_button",
                        "AddButton",
                    ):
                        method = getattr(toast, method_name, None)
                        if callable(method):
                            method(btn)
                            break
                except Exception:
                    pass

                def on_activated(args):
                    try:
                        root_dir = self.config.rootDirectory if self.config else "."
                        os.startfile(root_dir)
                        os.startfile(self.download_log_path)
                    except Exception as e:
                        print(f"[错误] 打开失败: {e}")
                    try:
                        self._interactive_event.set()
                    except Exception:
                        pass

                toast.on_activated = on_activated
                self._interactive_event.clear()
                it.show_toast(toast)
                try:
                    self._interactive_event.wait(timeout=3)
                except KeyboardInterrupt:
                    pass
            except Exception:
                self.notifier.notify(title, msg, duration="long")
        else:
            self.notifier.notify(title, msg, duration="long")

    def _load_anchor_dates(self):
        if self._anchor_dates_loaded:
            return self._anchor_dates
        self._anchor_dates = {}
        try:
            with open(self.anchor_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    mid = row.get("member_id", "").strip()
                    ad = row.get("anchor_date", "").strip()
                    if mid:
                        self._anchor_dates[mid] = ad
        except Exception:
            pass
        self._anchor_dates_loaded = True
        return self._anchor_dates

    @staticmethod
    def _strip_ext(name):
        name = os.path.basename(name)
        base, _ = os.path.splitext(name)
        return base if base else name

    def write_download_log(self, member_id, entry, file_names=None):
        if not self.download_log_path:
            return
        cnt = entry.get("downloaded_count", 0)
        is_anchor_missing = (
            file_names
            and len(file_names) == 1
            and re.match(r"Anchor \d+", str(file_names[0]))
        )
        # 跳过场景: file_names 携带跳过原因字符串(非"Anchor \d+"),cnt==0
        # 此前被 `if cnt == 0` 短路,导致 on_member_skip 的日志写入是死代码
        is_skip_reason = (
            file_names
            and len(file_names) == 1
            and not re.match(r"Anchor \d+", str(file_names[0]))
            and cnt == 0
        )
        if is_anchor_missing:
            return
        if cnt == 0 and not is_skip_reason:
            return
        with self._log_lock:
            try:
                p = Path(self.download_log_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    if not self._log_header_written:
                        ts = (self._run_start_time or datetime.now()).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        f.write(f"Download Date: {ts}\n")
                        self._log_header_written = True

                    if not self._log_separator_written:
                        f.write("-" * 20 + "\n")
                        self._log_separator_written = True

                    artist = sanitize_text(entry.get("artist", "Unknown"))
                    ad = entry.get("anchor_date", "") or self._load_anchor_dates().get(
                        member_id, ""
                    )

                    # 跳过记录单独成行,便于在下载日志中追踪跳过原因
                    if is_skip_reason:
                        reason_str = sanitize_text(str(file_names[0]))
                        f.write(f"[SKIP] {artist}({member_id}) -> {reason_str}\n")
                        return

                    mark_tag = ""
                    mark_val = entry.get("mark", "").strip().upper()
                    ai_mark_val = entry.get("ai_mark", "").strip().upper()
                    tags = []
                    if mark_val == "X":
                        tags.append("X")
                    if ai_mark_val == "AI":
                        tags.append("AI")
                    if tags:
                        mark_tag = f"[{'|'.join(tags)}]"

                    line = f"{artist}({member_id}){mark_tag} -> {ad}"

                    if file_names:
                        safe_names = [
                            self._strip_ext(sanitize_text(x)) for x in file_names
                        ]
                        if len(safe_names) > 50:
                            safe_names = safe_names[:50] + ["..."]
                        files_str = ",".join(safe_names)
                        line += f" : {files_str} | {cnt} new images"
                    else:
                        line += f" | {cnt} new images"

                    f.write(line + "\n")
            except Exception as e:
                print(f"[警告] 写下载日志失败: {e}")

    def write_run_summary(self):
        if not self.download_log_path:
            return
        with self._log_lock:
            try:
                p = Path(self.download_log_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a", encoding="utf-8") as f:
                    if not self._log_header_written:
                        ts = (self._run_start_time or datetime.now()).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        f.write(f"Download Date: {ts}\n")
                        self._log_header_written = True

                    if self.fatal_errors:
                        status = "error"
                    elif self._interrupted:
                        status = "interrupted"
                    elif self.skipped_authors:
                        status = "incomplete"
                    else:
                        status = "completed"

                    total = self._total_downloaded_images
                    proc = self._processed_authors_total
                    upd = self._updated_authors
                    elapsed = ""
                    if self._run_start_time:
                        delta = datetime.now() - self._run_start_time
                        elapsed = str(delta).split(".")[0]

                    f.write("-" * 20 + "\n")
                    f.write(
                        f"Summary: {status}[{elapsed}] | {proc} artists processed | "
                        f"{upd} updated | {total} images"
                    )
                    f.write("\n")

                    if status == "error" and self.fatal_errors:
                        f.write("Errors:\n")
                        for e in self.fatal_errors[-5:]:
                            f.write(f"  - {e}\n")

                    if status == "incomplete" and self.skipped_authors:
                        f.write("-" * 20 + "\n")
                        f.write(f"Skipped {len(self.skipped_authors)} authors:\n")
                        for mid, reason in self.skipped_authors.items():
                            artist_name = self.initial_data.get(mid, {}).get(
                                "artist", mid
                            )
                            f.write(f"  - {artist_name}({mid}): {reason}\n")

                    if status == "interrupted":
                        f.write("Note: Download was interrupted before completion.\n")

                    f.write("//" + "=" * 80 + "//\n")
            except Exception as e:
                print(f"[警告] 写入运行摘要失败: {e}")

    def send_fatal_error_notification(self, error_msg):
        self.notifier.notify(
            "Pixiv 下载出错",
            f"错误:\n{error_msg[:200]}",
            duration="long",
        )


def sanitize_text(s: typing.Optional[str]) -> str:
    if s is None:
        return ""
    try:
        t = html.unescape(str(s))
        t = unicodedata.normalize("NFKC", t)
        t = re.sub(r"\x1B\[[0-9;]*[A-Za-z]", "", t)
        t = "".join(ch for ch in t if ord(ch) >= 32 or ch in "\n\t")
        return t
    except Exception:
        return str(s)


def read_config(config_path):
    config = configparser.RawConfigParser()
    config.optionxform = str
    with open(config_path, "r", encoding="utf-8-sig") as f:
        config.read_file(f)
    return config


def _resolve_proxy_override(config, proxy_arg, noproxy_arg):
    original_use_proxy = config.useProxy
    original_proxy_address = config.proxyAddress

    if noproxy_arg:
        new_use_proxy = False
        new_proxy_address = original_proxy_address
        action = "强制禁用代理"
    elif proxy_arg is not None:
        new_use_proxy = True
        proxy_value = proxy_arg.strip() if proxy_arg else ""
        if proxy_value == "__USE_CONFIG__":
            new_proxy_address = original_proxy_address
        elif proxy_value:
            new_proxy_address = proxy_value
        else:
            new_proxy_address = original_proxy_address
        if not new_proxy_address:
            raise ValueError(
                "指定了 --proxy 但 config.ini 中未配置 proxyAddress，"
                "也未通过 --proxy-address 提供代理地址。\n"
                "用法:\n"
                "  python PixivUtil2.py -s 20 --enhanced --proxy\n"
                "  python PixivUtil2.py -s 20 --enhanced --proxy --proxy-address socks5h://127.0.0.1:7890\n"
                "  或在 config.ini 中设置 proxyAddress"
            )
        action = "强制使用代理"
    else:
        new_use_proxy = original_use_proxy
        new_proxy_address = original_proxy_address
        action = "使用config设置"

    changed = (new_use_proxy != original_use_proxy) or (
        new_use_proxy and new_proxy_address != original_proxy_address
    )

    if new_use_proxy:
        status = f"{action}: {new_proxy_address}"
    else:
        status = f"{action}: 不使用代理"

    return {
        "changed": changed,
        "original_use_proxy": original_use_proxy,
        "original_proxy_address": original_proxy_address,
        "new_use_proxy": new_use_proxy,
        "new_proxy_address": new_proxy_address,
        "status": status,
    }


def _apply_proxy_override(config, override):
    if not override["changed"]:
        return
    # 先备份到 override,以便应用失败时能回滚到原始值
    override["_applied"] = False
    original_use_proxy = config.useProxy
    original_proxy_address = config.proxyAddress
    try:
        config.useProxy = override["new_use_proxy"]
        config.proxyAddress = override["new_proxy_address"]

        import common.PixivBrowserFactory as PixivBrowserFactory

        if PixivBrowserFactory._browser is not None:
            PixivBrowserFactory._browser._configureBrowser(config)
        else:
            PixivBrowserFactory.defaultConfig = config
        override["_applied"] = True
    except Exception as e:
        # 应用失败(如 SOCKS 地址非法触发 assert),还原到原始 config 状态
        # 避免留下"已切换但浏览器未生效"的中间态
        print(f"[警告] 应用代理设置失败,还原 config: {e}")
        try:
            config.useProxy = original_use_proxy
            config.proxyAddress = original_proxy_address
            import common.PixivBrowserFactory as PixivBrowserFactory

            if PixivBrowserFactory._browser is not None:
                PixivBrowserFactory._browser._configureBrowser(config)
        except Exception as restore_e:
            print(f"[警告] 还原代理设置也失败: {restore_e}")
        raise


def _restore_proxy_config(config, override):
    # 仅在确实应用过时才需要还原;且还原过程绝不应抛异常覆盖原始错误
    if not override.get("changed") or not override.get("_applied", False):
        return
    try:
        config.useProxy = override["original_use_proxy"]
        config.proxyAddress = override["original_proxy_address"]

        import common.PixivBrowserFactory as PixivBrowserFactory

        if PixivBrowserFactory._browser is not None:
            PixivBrowserFactory._browser._configureBrowser(config)
    except Exception as e:
        print(f"[警告] 还原代理设置失败: {e}")


def run_anchor_download(
    caller,
    config,
    anchor_file="AnchorList.csv",
    send_notifications=True,
    progress_interval=1,
    log_file=None,
    download_log_path=None,
    proxy_arg=None,
    noproxy_arg=False,
):
    notifier = ToastNotifier()

    anchor_file = os.path.abspath(anchor_file)
    monitor = DownloadMonitor(notifier, anchor_file, config)
    if download_log_path:
        monitor.download_log_path = download_log_path
    else:
        monitor.download_log_path = os.path.abspath("AutoDownload.log")

    if not os.path.exists(anchor_file):
        print(f"[错误] 锚点列表文件不存在: {anchor_file}")
        return False

    pixiv_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "PixivUtil2.py")
    )
    config_path = os.path.join(os.path.dirname(pixiv_path), "config.ini")
    if not os.path.exists(config_path):
        print(f"[错误] 配置文件不存在: {config_path}")
        return False

    proxy_override = _resolve_proxy_override(config, proxy_arg, noproxy_arg)
    proxy_status = proxy_override["status"]

    log_fh = None
    _orig_stdout = sys.stdout
    _tee = None
    if log_file:
        try:
            log_fh = open(log_file, "a", encoding="utf-8", buffering=1)
            log_fh.write(f"\n{'='*60}\n")
            log_fh.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_fh.write(f"锚点列表: {anchor_file}\n")
            log_fh.write(f"代理设置: {proxy_status}\n")
            log_fh.write(f"{'='*60}\n")
            log_fh.flush()

            _tee = _Tee(_orig_stdout, log_fh)
            sys.stdout = _tee

            try:
                from common.PixivHelper import get_logger

                logger = get_logger()
                logger.disabled = True
                logger.handlers.clear()
                config.disableLog = True
            except Exception:
                pass
        except Exception as e:
            print(f"[警告] 无法写入日志文件: {e}")
            log_fh = None

    try:
        # 应用代理覆盖需在 try 块内,失败时 _apply_proxy_override 内部已自还原 config,
        # 此处仅做日志与返回
        try:
            _apply_proxy_override(config, proxy_override)
        except Exception as e:
            print(f"[错误] 应用代理设置失败: {e}")
            return False

        if send_notifications:
            monitor.send_start_notification()

        print(f"\n{'='*60}")
        print(f"  Pixiv 自动下载启动 (增强模式)")
        print(f"  锚点列表: {anchor_file}")
        print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  通知间隔: 每 {progress_interval} 位有下载的画师")
        print(f"  代理设置: {proxy_status}")
        if log_file:
            print(f"  日志文件: {log_file}")
        if download_log_path:
            print(f"  下载日志: {download_log_path}")
        print(f"{'='*60}\n")

        callbacks = _build_callbacks(
            monitor, progress_interval, send_notifications, None
        )

        exit_code = 0
        try:
            import handler.PixivListHandler as PixivListHandler

            PixivListHandler.process_anchor_list(
                caller, config, anchor_file=anchor_file, callbacks=callbacks
            )
        except KeyboardInterrupt:
            monitor._interrupted = True
            print("\n[中断] 用户取消了下载任务")
            exit_code = 1
            if send_notifications:
                monitor.flush_download_batch()
                monitor.send_fatal_error_notification("下载任务被用户中断")
        except Exception as e:
            print(f"\n[错误] 下载过程发生异常: {e}")
            monitor.fatal_errors.append(str(e))
            exit_code = 2
            if send_notifications:
                monitor.flush_download_batch()
                monitor.send_fatal_error_notification(str(e))

        if send_notifications:
            monitor.flush_download_batch()

        print(f"\n{'='*60}")
        print(f"  完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        if exit_code == 0:
            print(f"  状态: 成功完成")
        elif exit_code == 1:
            print(f"  状态: 已中断")
        else:
            print(f"  状态: 异常退出")

        if monitor.artists_with_downloads:
            total_images = sum(
                a["downloaded_count"] for a in monitor.artists_with_downloads
            )
            print(f"  下载画师: {len(monitor.artists_with_downloads)} 位")
            print(f"  下载文件: {total_images} 张")

        if monitor.recovered_error_count > 0:
            print(f"  网络重试(已恢复): {monitor.recovered_error_count} 次")

        if monitor.fatal_errors:
            print(f"  致命错误: {len(monitor.fatal_errors)} 个")
            for err in monitor.fatal_errors[-3:]:
                print(f"    - {err[:100]}")

        print(f"{'='*60}\n")

        try:
            monitor.write_run_summary()
        except Exception:
            pass

        if send_notifications:
            monitor.send_completion_notification()

        return exit_code == 0 and not monitor.fatal_errors
    finally:
        # 逐步 try/except,确保任一步失败不会阻塞后续清理
        # 避免代理还原失败导致 stdout 仍指向 _Tee、日志句柄泄漏
        try:
            _restore_proxy_config(config, proxy_override)
        except Exception as e:
            print(f"[警告] finally: 还原代理设置异常: {e}")
        if _tee:
            try:
                sys.stdout = _orig_stdout
            except Exception as e:
                print(f"[警告] finally: 还原 stdout 异常: {e}")
        if log_fh:
            try:
                log_fh.close()
            except Exception:
                pass


def _build_callbacks(monitor, progress_interval, send_notifications, log_fh):
    # 网络类跳过的关键词,这类跳过累计后统一汇报,避免逐条通知轰炸
    _NETWORK_SKIP_KEYWORDS = ("网络错误", "Network error", "下载返回空结果")

    def on_member_skip(member_id, reason, is_error=True):
        monitor.on_member_skip(member_id, reason, is_error=is_error)
        if not send_notifications or not is_error:
            return
        try:
            artist_name = monitor.initial_data.get(str(member_id), {}).get(
                "artist", str(member_id)
            )
            # 网络类跳过去抖,累计到 flush 时一次性汇报
            if any(kw in reason for kw in _NETWORK_SKIP_KEYWORDS):
                monitor._network_skip_count += 1
                if len(monitor._network_skip_samples) < 5:
                    monitor._network_skip_samples.append(
                        f"{artist_name}({member_id}): {reason[:60]}"
                    )
                return
            # 画师失踪/锚点为空等立即弹,标题用更准确的"画师不存在"
            monitor.notifier.notify(
                "Pixiv 画师/作品 不存在",
                f"{artist_name}({member_id}): {reason[:80]}",
                duration="short",
            )
        except Exception:
            pass

    def on_download_complete(
        member_id, downloaded, anchor_updated, artist, anchor_date, file_names=None
    ):
        monitor.on_download_info(
            member_id, downloaded, anchor_updated, artist, anchor_date, file_names
        )
        # __DOWNLOAD_INFO__ 由 process_anchor_list 末尾 print 输出,
        # _Tee 已截获 stdout 写入 log_fh,此处无需再直接写,避免重复
        if len(monitor.current_download_batch) >= progress_interval:
            if send_notifications:
                monitor.flush_download_batch()

    def on_recovered_error():
        monitor.on_recovered_error()

    def on_fatal_error(member_id, reason):
        monitor.on_fatal_error(f"成员 {member_id}: {reason[:100]}")
        if send_notifications:
            try:
                artist_name = monitor.initial_data.get(str(member_id), {}).get(
                    "artist", str(member_id)
                )
                monitor.notifier.notify(
                    "Pixiv 下载异常",
                    f"{artist_name}({member_id}) 下载失败\n{reason[:80]}",
                    duration="long",
                )
            except Exception:
                pass

    def on_all_complete():
        pass

    return {
        "on_member_skip": on_member_skip,
        "on_download_complete": on_download_complete,
        "on_recovered_error": on_recovered_error,
        "on_fatal_error": on_fatal_error,
        "on_all_complete": on_all_complete,
    }
