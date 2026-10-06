import os
import sys
import csv
import datetime

import handler.PixivArtistHandler as PixivArtistHandler
import common.PixivHelper as PixivHelper
import handler.PixivSketchHandler as PixivSketchHandler
import handler.PixivTagsHandler as PixivTagsHandler
from common.PixivException import PixivException
from model.PixivListItem import PixivListItem
from model.PixivTags import PixivTags

def _prompt_user_network_action(member_id, error_detail):
    """
    网络错误重试耗尽后的阻塞式用户交互。
    优先使用 Windows 原生 MessageBox 弹窗（模态、永不消失直到用户点击），
    降级到 CLI input() 阻塞。

    Returns:
        "retry" - 用户要求重试（从当前失败点继续）
        "skip"  - 用户要求跳过此作者
        "quit"  - 用户要求终止整个任务
    """
    title = "Pixiv 网络/代理错误"
    msg_lines = [
        f"member_id={member_id} 遇到网络/代理错误，自动重试已耗尽。",
        f"请检查网络状态或代理/VPN 设置后选择操作。",
        "",
        f"错误详情: {str(error_detail)[:400]}",
    ]
    msg_text = "\n".join(msg_lines)

    # 优先尝试 Windows 原生 MessageBox（模态弹窗，永不消失直到点击）
    try:
        import win32api
        import win32con

        # 先询问是否要重试（Yes=重试 / No=跳过 / Cancel=退出）
        # MessageBox 按钮文字不可自定义，用 MB_YESNOCANCEL 三按钮
        full_msg = msg_text + "\n\n" + "是 = 重试  |  否 = 跳过此作者  |  取消 = 退出整个任务"
        result = win32api.MessageBox(
            0,
            full_msg,
            title,
            win32con.MB_YESNOCANCEL | win32con.MB_ICONWARNING | win32con.MB_TOPMOST,
        )
        if result == win32con.IDYES:
            return "retry"
        elif result == win32con.IDNO:
            return "skip"
        else:
            return "quit"
    except ImportError:
        pass
    except Exception:
        pass

    # 降级到 CLI input() 阻塞
    while True:
        print()
        print("=" * 60)
        print(f"[网络错误] member_id={member_id} 自动重试已耗尽")
        print(f"  错误: {str(error_detail)[:300]}")
        print("  提示: 请检查代理/VPN 或网络连接后选择操作")
        print("-" * 60)
        choice = input(
            "请选择 [R]重试 / [S]跳过此作者 / [Q]退出整个任务: "
        ).strip().lower()
        if choice in ("r", "retry"):
            return "retry"
        elif choice in ("s", "skip"):
            return "skip"
        elif choice in ("q", "quit", "x"):
            return "quit"
        print("无效输入，请输入 R / S / Q")


_NOT_FOUND_KEYWORDS = [
    "不存在",
    "离开",
    "not found",
    "not exist",
    "no such user",
    "不存在或已被封禁",
]


def _is_not_found_error(ex):
    """Check if a PixivException indicates a not-found / non-existent resource."""
    msg = str(ex)
    msg_lower = msg.lower()
    for kw in _NOT_FOUND_KEYWORDS:
        if kw in msg or kw in msg_lower:
            return True
    return False


def _parse_date(date_str):
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()
    for fmt in (
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d",
    ):
        try:
            return datetime.datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _sort_records(records):
    def sort_key(r):
        enabled = r.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() == "true"
        elif not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() == "true"

        if not enabled:
            return (-1, datetime.datetime.min, datetime.datetime.min)

        ld = _parse_date(r.get("last_download_date", "") or "")
        if ld is None:
            ld = datetime.datetime.min

        ad = _parse_date(r.get("anchor_date", "") or "")
        if ad is None:
            ad = datetime.datetime.min

        return (0, ld, ad)

    records.sort(key=sort_key, reverse=True)
    return records


def process_list(caller, config, list_file_name=None, tags=None, include_sketch=False):
    db = caller.__dbManager__
    br = caller.__br__

    result = None
    try:
        # Getting the list
        if config.processFromDb:
            PixivHelper.print_and_log("info", "Processing from database.")
            if config.dayLastUpdated == 0:
                result = db.selectAllMember()
            else:
                print(f"Select only last {config.dayLastUpdated} days.")
                result = db.selectMembersByLastDownloadDate(config.dayLastUpdated)
        else:
            PixivHelper.print_and_log(
                "info", f"Processing from list file: {list_file_name}"
            )
            result = PixivListItem.parseList(list_file_name, config.rootDirectory)

        ignore_file_list = "ignore_list.txt"
        if os.path.exists(ignore_file_list):
            PixivHelper.print_and_log(
                "info", f"Processing ignore list for member: {ignore_file_list}"
            )
            ignore_list = PixivListItem.parseList(
                ignore_file_list, config.rootDirectory
            )
            for ignore in ignore_list:
                for item in result:
                    if item.memberId == ignore.memberId:
                        result.remove(item)
                        break

        PixivHelper.print_and_log("info", f"Found {len(result)} items.")
        current_member = 1
        for item in result:
            retry_count = 0
            while True:
                try:
                    prefix = f"[{current_member} of {len(result)}] "
                    PixivArtistHandler.process_member(
                        caller,
                        config,
                        item.memberId,
                        user_dir=item.path,
                        tags=tags,
                        title_prefix=prefix,
                    )
                    break
                except KeyboardInterrupt:
                    raise
                except BaseException as ex:
                    if retry_count > config.retry:
                        PixivHelper.print_and_log(
                            "error", f"Giving up member_id: {item.memberId} ==> {ex}"
                        )
                        break
                    retry_count = retry_count + 1
                    print(
                        f"Something wrong, retrying after 2 second ({retry_count}) ==> {ex}"
                    )
                    PixivHelper.print_delay(2)

            retry_count = 0
            while include_sketch:
                try:
                    # Issue 1007
                    # fetching artist token...
                    artist_model, _ = br.getMemberPage(item.memberId)
                    prefix = f"[{current_member} ({item.memberId} - {artist_model.artistToken}) of {len(result)}] "
                    PixivSketchHandler.process_sketch_artists(
                        caller, config, artist_model.artistToken, title_prefix=prefix
                    )
                    break
                except KeyboardInterrupt:
                    raise
                except BaseException as ex:
                    if retry_count > config.retry:
                        PixivHelper.print_and_log(
                            "error",
                            f"Giving up member_id: {item.memberId} when processing PixivSketch ==> {ex}",
                        )
                        break
                    retry_count = retry_count + 1
                    print(
                        f"Something wrong, retrying after 2 second ({retry_count}) ==> {ex}"
                    )
                    PixivHelper.print_delay(2)

            current_member = current_member + 1
            br.clear_history()
            print(f"done for member id = {item.memberId}.")
            print("")
    except Exception as ex:
        if isinstance(ex, KeyboardInterrupt):
            raise
        caller.ERROR_CODE = getattr(ex, "errorCode", -1)
        PixivHelper.print_and_log("error", f"Error at process_list(): {sys.exc_info()}")
        print("Failed")
        raise


def process_tags_list(
    caller,
    config,
    filename,
    page=1,
    end_page=0,
    wild_card=True,
    sort_order="date_d",
    bookmark_count=None,
    start_date=None,
    end_date=None,
):

    try:
        print("Reading:", filename)
        tags = PixivTags.parseTagsList(filename)
        for tag in tags:
            PixivTagsHandler.process_tags(
                caller,
                config,
                tag,
                page=page,
                end_page=end_page,
                wild_card=wild_card,
                start_date=start_date,
                end_date=end_date,
                use_tags_as_dir=config.useTagsAsDir,
                bookmark_count=bookmark_count,
                sort_order=sort_order,
            )
            PixivHelper.wait(config=config)
    except Exception as ex:
        if isinstance(ex, KeyboardInterrupt):
            raise
        caller.ERROR_CODE = getattr(ex, "errorCode", -1)
        PixivHelper.print_and_log(
            "error", f"Error at process_tags_list(): {sys.exc_info()}"
        )
        raise


def import_list(caller, config, list_name="list.txt"):
    list_path = config.downloadListDirectory + os.sep + list_name
    if os.path.exists(list_path):
        list_txt = PixivListItem.parseList(list_path, config.rootDirectory)
        caller.__dbManager__.importList(list_txt)
        print(f"Updated {len(list_txt)} items.")
    else:
        msg = f"List file not found: {list_path}"
        PixivHelper.print_and_log("warn", msg)


def _empty_record():
    return {
        "member_id": None,
        "artist": "",
        "anchor_id": None,
        "anchor_date": "",
        "enabled": True,
        "has_r18": None,
        "last_download_date": "",
        "last_download_images": 0,
        "last_skipped_images": 0,
        "last_update_status": "",
        "mark": "",
        "ai_mark": "",
        "information": "",
    }


def _set_defaults(record):
    if not record["anchor_date"]:
        record["anchor_date"] = ""
    if record.get("enabled") is None:
        record["enabled"] = True
    if "has_r18" not in record:
        record["has_r18"] = None
    if not record["last_download_date"]:
        record["last_download_date"] = ""
    if not isinstance(record.get("last_download_images"), int):
        record["last_download_images"] = 0
    if not isinstance(record.get("last_skipped_images"), int):
        record["last_skipped_images"] = 0
    if not record["last_update_status"]:
        record["last_update_status"] = ""
    if not record.get("mark"):
        record["mark"] = ""
    if not record.get("ai_mark"):
        record["ai_mark"] = ""
    if not record["information"]:
        record["information"] = ""


CSV_FIELDS = [
    "member_id",
    "artist",
    "anchor_id",
    "anchor_date",
    "enabled",
    "has_r18",
    "last_download_date",
    "last_download_images",
    "last_skipped_images",
    "last_update_status",
    "mark",
    "ai_mark",
    "information",
]


def _parse_count(value):
    if value is None:
        return 0
    value = str(value).strip()
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        import re

        m = re.match(r"(\d+)", value)
        if m:
            return int(m.group(1))
        return 0


def parse_anchor_list(filename):
    records = []
    if not os.path.exists(filename):
        PixivHelper.print_and_log("warn", f"Anchor file not found: {filename}")
        return records

    with open(filename, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # 获取所有列名，识别额外列（不在 CSV_FIELDS 中的列）
        extra_columns = (
            [col for col in reader.fieldnames if col not in CSV_FIELDS]
            if reader.fieldnames
            else []
        )

        for row in reader:
            record = _empty_record()
            record["member_id"] = row.get("member_id", "").strip() or None
            record["artist"] = row.get("artist", "").strip()
            record["anchor_id"] = row.get("anchor_id", "").strip() or None
            record["anchor_date"] = row.get("anchor_date", "").strip()

            enabled_val = row.get("enabled", "").strip().lower()
            record["enabled"] = enabled_val in (
                "true",
                "yes",
                "1",
                "on",
                "enabled",
            )

            r18_val = row.get("has_r18", "").strip().lower()
            if r18_val in ("true", "yes", "1", "on"):
                record["has_r18"] = True
            elif r18_val in ("false", "no", "0", "off"):
                record["has_r18"] = False
            else:
                record["has_r18"] = None

            record["last_download_date"] = row.get("last_download_date", "").strip()

            download_val = row.get("last_download_images", "")
            if not download_val:
                download_val = row.get("last_download", "")
            record["last_download_images"] = _parse_count(download_val)

            skipped_val = row.get("last_skipped_images", "")
            if not skipped_val:
                skipped_val = row.get("last_skipped", "")
            record["last_skipped_images"] = _parse_count(skipped_val)

            record["last_update_status"] = row.get("last_update_status", "").strip()
            record["mark"] = row.get("mark", "").strip()
            record["ai_mark"] = row.get("ai_mark", "").strip()
            record["information"] = row.get("information", "").strip()

            # 保存额外列的内容（如备注列），程序不读取也不修改
            if extra_columns:
                record["_extra_columns"] = extra_columns
                record["_extra_values"] = {
                    col: row.get(col, "") for col in extra_columns
                }

            if record["member_id"] is None:
                PixivHelper.print_and_log(
                    "warn",
                    f"Skipping row with empty member_id: anchor_id={record['anchor_id']}",
                )
                continue

            _set_defaults(record)
            records.append(record)

    return records


def _get_member_info(caller, member_id):
    try:
        import common.PixivBrowserFactory as PixivBrowserFactory

        br = PixivBrowserFactory.getBrowser()
        artist, _ = br.getMemberPage(member_id, silent=True)
        if artist and artist.artistName:
            return artist.artistName
    except Exception:
        pass
    return None


def _check_member_exists(caller, member_id):
    """Check if member_id exists on Pixiv.

    Returns:
        True if member exists (valid)
        False if member does not exist or is suspended
        None if unable to determine (network error, etc.)
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            import common.PixivBrowserFactory as PixivBrowserFactory
            from common.PixivException import PixivException

            br = PixivBrowserFactory.getBrowser()
            artist, _ = br.getMemberPage(member_id, page=1, silent=True)
            return True
        except PixivException as ex:
            # 只有明确的业务错误码才能判定"不存在"——这些是 Pixiv API
            # 返回的结构化信号，100% 可靠。
            if ex.errorCode in (
                PixivException.USER_ID_NOT_EXISTS,
                PixivException.USER_ID_SUSPENDED,
            ):
                return False
            # OTHER_MEMBER_ERROR: Pixiv API 返回的 {error:true, body:null}
            # 这类响应（HTTP 200），通过 _is_not_found_error 的关键词
            # 检查（针对 API message 字段）来判定——较可靠。
            if ex.errorCode == PixivException.OTHER_MEMBER_ERROR:
                if _is_not_found_error(ex):
                    return False
            # SERVER_ERROR 永远不能判定为"不存在"！它代表网络/代理/SSL
            # 问题，错误消息里可能包含代理返回的 HTML 片段（里面恰好
            # 有 "404"、"Not Found" 等字样），过去用字符串匹配会误判。
            # 正确做法：继续重试，耗尽后返回 None 告知"无法确定"。
            if attempt < max_retries - 1:
                PixivHelper.print_delay(3)
            else:
                return None
        except Exception:
            if attempt < max_retries - 1:
                PixivHelper.print_delay(3)
            else:
                return None
    return None


def _check_anchor_exists(caller, image_id):
    """Check if anchor image exists on Pixiv.

    Returns:
        True if image exists
        False if image does not exist or was deleted
        None if unable to determine (network error, etc.)
    """
    try:
        import common.PixivBrowserFactory as PixivBrowserFactory
        from common.PixivException import PixivException

        br = PixivBrowserFactory.getBrowser()
        image, _ = br.getImagePage(image_id=image_id)
        if image and getattr(image, "imageId", None):
            return True
        return False
    except PixivException as ex:
        # IMAGE_DELETED: Pixiv API 明确返回图片已删除——可靠信号
        if ex.errorCode == PixivException.IMAGE_DELETED:
            return False
        # OTHER_MEMBER_ERROR: 来自 Pixiv API 的错误响应（如图片页面
        # 的 {error:true, body:null} 结构），通过 _is_not_found_error
        # 关键词（针对 API message 字段）判定
        if ex.errorCode == PixivException.OTHER_MEMBER_ERROR:
            if _is_not_found_error(ex):
                return False
        # SERVER_ERROR: 网络/代理/SSL 问题，不能判定为"图片不存在"
        #（代理 HTML 错误页里可能恰好有 "404" 等字样被拼进消息）。
        # 正确做法：返回 None 告知"无法确定"。
        return None
    except Exception:
        return None


def _get_latest_work_id(caller, member_id, r18mode=False):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            import common.PixivBrowserFactory as PixivBrowserFactory
            from common.PixivException import PixivException

            br = PixivBrowserFactory.getBrowser()
            artist, _ = br.getMemberPage(
                member_id, page=1, r18mode=r18mode, silent=True
            )
            if artist and artist.imageList and len(artist.imageList) > 0:
                return str(artist.imageList[0])
        except PixivException as e:
            if e.errorCode in (
                PixivException.USER_ID_NOT_EXISTS,
                PixivException.USER_ID_SUSPENDED,
            ):
                PixivHelper.print_and_log(
                    "info",
                    f"Member {member_id} does not exist or is suspended. Cannot get latest work ID.",
                )
                return None
            if e.errorCode == PixivException.OTHER_MEMBER_ERROR:
                if _is_not_found_error(e):
                    PixivHelper.print_and_log(
                        "info",
                        f"Member {member_id} not found. Cannot get latest work ID.",
                    )
                    return None
            if attempt < max_retries - 1:
                PixivHelper.print_delay(3)
        except Exception:
            if attempt < max_retries - 1:
                PixivHelper.print_delay(3)
    return None


def _check_has_r18(caller, member_id):
    try:
        import common.PixivBrowserFactory as PixivBrowserFactory

        br = PixivBrowserFactory.getBrowser()
        artist, _ = br.getMemberPage(member_id, page=1, r18mode=True, silent=True)
        if artist and artist.imageList and len(artist.imageList) > 0:
            return True
        return False
    except Exception:
        return None


def _fetch_missing_record_info(caller, record, force=False):
    changed = False
    if not record["artist"] or record["artist"] == "Unknown":
        name = _get_member_info(caller, record["member_id"])
        if name:
            record["artist"] = name
            changed = True
    if not record["anchor_date"] or force:
        date = _get_image_date(caller, record["anchor_id"])
        if date:
            record["anchor_date"] = date
            changed = True
    return changed


def _write_anchor_file(filename, records):
    # 原子写入流程：
    #   1. 先清理上次崩溃残留的旧 tmp 文件（同目录 *.tmp.*）
    #   2. 同目录下写带随机后缀的临时文件
    #   3. 写完 → 刷盘 → close
    #   4. os.replace(tmp, filename) 原子替换（NTFS 保证原子性：要么成功替换，要么原文件完好无损）
    #
    # CTRL+C 在任何一步的结果：
    #   * 步骤 2 中途中断 → tmp 半截，原文件 untouched
    #   * 步骤 4 之前中断 → tmp 完整但未替换，原文件 untouched
    #   * 步骤 4 期间中断 → os.replace 是操作系统级原子，要么成功要么失败，不会有中间态

    import tempfile
    import glob as _glob

    abs_path = os.path.abspath(filename)
    base_dir = os.path.dirname(abs_path)
    base_name = os.path.basename(abs_path)

    # 步骤 1: 清理同目录下的历史残留 tmp（例如上次进程崩溃在写一半留下的）
    # 这些 tmp 永远不会被读取，留在磁盘只占空间。匹配模式：basename + ".tmp.*"
    try:
        leftover_glob = os.path.join(
            base_dir, base_name + ".tmp." + "*"
        )
        for leftover in _glob.glob(leftover_glob):
            try:
                if leftover != abs_path:
                    os.remove(leftover)
            except OSError:
                pass
    except OSError:
        pass

    # 检查是否有额外列需要保留
    has_extra = any("_extra_columns" in r for r in records)

    # 构建完整的字段列表（标准字段 + 所有记录中的额外列）
    write_fields = list(CSV_FIELDS)
    if has_extra:
        seen = set(write_fields)
        for r in records:
            if "_extra_columns" in r:
                for col in r["_extra_columns"]:
                    if col not in seen:
                        write_fields.append(col)
                        seen.add(col)

    # 步骤 2: 写临时文件到同目录（保证 tmp 和目标在同一卷，os.replace 才支持跨文件系统）
    tmp_created_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=base_name + ".tmp.",
            dir=base_dir,
            delete=False,
        ) as tmp_f:
            tmp_created_path = tmp_f.name
            writer = csv.DictWriter(tmp_f, fieldnames=write_fields)
            writer.writeheader()
            for i, r in enumerate(records):
                row = {}
                row["member_id"] = r["member_id"] or ""
                row["artist"] = r["artist"] or ""
                row["anchor_id"] = r["anchor_id"] or ""
                row["anchor_date"] = r["anchor_date"] or ""
                row["enabled"] = "true" if r["enabled"] else "false"
                if r["has_r18"] is True:
                    row["has_r18"] = "true"
                elif r["has_r18"] is False:
                    row["has_r18"] = "false"
                else:
                    row["has_r18"] = ""
                row["last_download_date"] = r["last_download_date"] or ""
                row["last_download_images"] = r.get("last_download_images", 0)
                row["last_skipped_images"] = r.get("last_skipped_images", 0)
                row["last_update_status"] = r["last_update_status"] or ""
                row["mark"] = r.get("mark", "") or ""
                row["ai_mark"] = r.get("ai_mark", "") or ""
                row["information"] = r["information"] or ""

                # 写入额外列的内容（如备注列），保持原样不修改
                if "_extra_values" in r:
                    for col, value in r["_extra_values"].items():
                        row[col] = value

                writer.writerow(row)

            # 强制 flush 后 fsync 到磁盘（防止 writeback 缓存与 rename 在文件系统层的竞态）
            tmp_f.flush()
            try:
                os.fsync(tmp_f.fileno())
            except OSError:
                pass

        # 步骤 3 & 4: 原子替换。
        # Windows NTFS + Python 3.x: os.replace 等价于 MoveFileExW(MOVEFILE_REPLACE_EXISTING),
        # 是原子系统调用，结果只有两种：
        #   * 成功：tmp 已经被改名为 filename，磁盘上不再有 tmp
        #   * 失败（例如目标被防病毒软件独占锁住）：两者都 untouched，tmp 保留稍后可手动恢复
        os.replace(tmp_created_path, abs_path)

    except BaseException:
        # 写入或替换过程中任何异常，尽量清理 tmp（避免残留）。
        # 替换阶段 os.replace 抛出异常的场景：
        #   1) 权限不足 / 文件被其它进程独占锁住 → tmp 还在，但原文件 untouched
        #   2) 磁盘空间不足 → tmp 可能半截
        # 无论哪种情况，删除 tmp 是安全的（因为正式文件没被修改，下次还能重写）。
        if tmp_created_path is not None:
            try:
                if os.path.exists(tmp_created_path):
                    os.remove(tmp_created_path)
            except OSError:
                pass
        raise


def _apply_result_to_record(
    caller,
    record,
    result_dict,
    member_id,
    anchor_image_id,
    callbacks=None,
):
    """把 result_dict 中的下载结果统一应用到 record。

    被以下两个路径共同调用，避免逻辑重复与中断场景漏更新:
      1) PixivListHandler.process_anchor_list 正常完成后的 if result_dict: 块
      2) KeyboardInterrupt 在 process_member 内部 raise 之前的应急更新
    返回 True 表示 record 被修改过(用于上层 any_record_changed 判定)。
    """
    if not result_dict:
        record["last_update_status"] = "skipped"
        record["information"] = (
            "Download processing returned no result "
            "(member may be invalid or inaccessible)."
        )
        PixivHelper.print_and_log(
            "warn",
            f"No result for member_id={member_id}. Marking as skipped.",
        )
        if callbacks and callbacks.get("on_member_skip"):
            callbacks["on_member_skip"](
                member_id, "下载返回空结果", is_error=True
            )
        return False

    last_downloaded_id = result_dict.get("last_downloaded_id")
    last_downloaded_date = result_dict.get("last_download_date")
    anchor_updated = result_dict.get("anchor_updated", False)
    anchor_found = result_dict.get("anchor_found", False)
    error_occurred = result_dict.get("error_occurred", False)
    status = result_dict.get("status", "unknown")
    downloaded_count = result_dict.get("downloaded_count", 0)
    skipped_count = result_dict.get("skipped_count", 0)
    artist_name = result_dict.get("artist_name")

    if artist_name:
        record["artist"] = artist_name

    changed = False

    if not anchor_found and not error_occurred:
        record["last_update_status"] = "skipped"
        record["information"] = (
            f"Anchor image {anchor_image_id} not found "
            f"(may have been deleted by artist)."
        )
        PixivHelper.print_and_log(
            "warn",
            f"Anchor {anchor_image_id} not found for member_id={member_id}.",
        )
        # 属于 SKIP 类（锚点图片不存在/被删除），写入下载日志
        if (
            callbacks
            and callbacks.get("on_member_skip")
            and not result_dict.get("_fatal_reported")
        ):
            callbacks["on_member_skip"](
                member_id,
                f"锚点图片不存在或已删除（anchor_id={anchor_image_id}）",
                is_error=True,
            )
    else:
        if anchor_updated and last_downloaded_id:
            new_anchor_id = str(last_downloaded_id)
            if record.get("anchor_id") != new_anchor_id:
                record["anchor_id"] = new_anchor_id
                changed = True
            if last_downloaded_date:
                record["anchor_date"] = last_downloaded_date
            else:
                new_date = _get_image_date(caller, last_downloaded_id)
                if new_date:
                    record["anchor_date"] = new_date

            if record.get("has_r18") is False:
                r18_result = _check_has_r18(caller, member_id)
                if r18_result is not None and r18_result is not False:
                    record["has_r18"] = r18_result
                    PixivHelper.print_and_log(
                        "info",
                        f"Member {member_id}: Has R18 works updated = {r18_result}",
                    )

        if anchor_updated or downloaded_count > 0 or skipped_count > 0:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            record["last_download_date"] = now_str
            record["last_download_images"] = downloaded_count
            record["last_skipped_images"] = skipped_count
            changed = True

        record["last_update_status"] = status

        skip_reason = ""
        if status in ("skipped", "error") and error_occurred:
            skip_reason = result_dict.get("skip_reason", "")
            if "not found" in skip_reason.lower():
                # 作者不存在/已被封禁（来自 PixivArtistHandler 中
                # USER_ID_NOT_EXISTS/USER_ID_SUSPENDED/OTHER_MEMBER_ERROR 分支）
                PixivHelper.print_and_log(
                    "warn",
                    f"Member ID {member_id} not found or suspended on Pixiv for member_id={member_id}.",
                )
                # 属于 SKIP 类（作者不存在），写入下载日志
                if (
                    callbacks
                    and callbacks.get("on_member_skip")
                    and not result_dict.get("_fatal_reported")
                ):
                    callbacks["on_member_skip"](
                        member_id,
                        "画师不存在或已被封禁",
                        is_error=True,
                    )
            elif "Network error" in skip_reason:
                PixivHelper.print_and_log(
                    "warn",
                    f"Network error prevented anchor verification for member_id={member_id}. Skipping without modifying anchor.",
                )
                if (
                    callbacks
                    and callbacks.get("on_member_skip")
                    and not result_dict.get("_fatal_reported")
                ):
                    callbacks["on_member_skip"](
                        member_id,
                        "网络错误导致无法验证锚点，跳过处理",
                        is_error=True,
                    )

        info_parts = []
        if error_occurred:
            info_parts.append("Error occurred during download")
        if "Network error" in skip_reason:
            info_parts.append("Network error prevented anchor verification")
        if status == "interrupted":
            info_parts.append("Download interrupted by user")
        record["information"] = "; ".join(info_parts)

    return changed


def _snapshot_record(record):
    return (
        record["member_id"],
        record["artist"],
        record["anchor_id"],
        record["anchor_date"],
        record["enabled"],
        record["has_r18"],
        record["last_download_date"],
        record["last_download_images"],
        record["last_skipped_images"],
        record["last_update_status"],
        record.get("mark", ""),
        record.get("ai_mark", ""),
        record["information"],
    )


def process_anchor_list(caller, config, anchor_file=None, callbacks=None):
    try:
        if anchor_file is None:
            anchor_file = "AnchorList.csv"

        if not os.path.exists(anchor_file):
            PixivHelper.print_and_log(
                "error", f"Anchor list file not found: {anchor_file}"
            )
            print("Please create AnchorList.csv with the required format.")
            print("See the header row in AnchorList.csv for the format.")
            return

        records = parse_anchor_list(anchor_file)
        if not records:
            PixivHelper.print_and_log(
                "warn", "No valid records found in AnchorList.csv"
            )
            return

        PixivHelper.print_and_log(
            "info", f"Processing from anchor list file: {anchor_file}"
        )
        PixivHelper.print_and_log("info", f"Found {len(records)} anchor records.")

        current_member = 1
        total_members = len(records)
        any_record_changed = False
        for record in records:
            member_id = record["member_id"]
            anchor_image_id = record["anchor_id"]

            before_snapshot = _snapshot_record(record)

            if not record.get("enabled", True):
                current_member = current_member + 1
                PixivHelper.print_and_log(
                    "info",
                    f"Skipping member_id={member_id}: Disabled (Enabled=false).",
                )
                print(f"skipped for member id = {member_id} (disabled).")
                print("")
                if callbacks and callbacks.get("on_member_skip"):
                    callbacks["on_member_skip"](member_id, "画师已禁用", is_error=False)
                continue

            if not anchor_image_id:
                record["last_update_status"] = "skipped"
                record["information"] = (
                    "Anchor ID is empty; cannot determine incremental download point."
                )
                if _snapshot_record(record) != before_snapshot:
                    any_record_changed = True
                current_member = current_member + 1
                PixivHelper.print_and_log(
                    "warn",
                    f"Skipping member_id={member_id}: Anchor ID is empty.",
                )
                print(f"skipped for member id = {member_id} (empty anchor).")
                print("")
                if callbacks and callbacks.get("on_member_skip"):
                    callbacks["on_member_skip"](member_id, "锚点ID为空", is_error=True)
                continue

            member_exists = _check_member_exists(caller, member_id)
            if member_exists is False:
                record["last_update_status"] = "skipped"
                record["information"] = (
                    f"Member ID {member_id} does not exist or is suspended on Pixiv."
                )
                if _snapshot_record(record) != before_snapshot:
                    any_record_changed = True
                current_member = current_member + 1
                PixivHelper.print_and_log(
                    "warn",
                    f"Member ID {member_id} not found or suspended on Pixiv for member_id={member_id}.",
                )
                print(
                    f"skipped for member id = {member_id} (member not found on Pixiv)."
                )
                print("")
                if callbacks and callbacks.get("on_member_skip"):
                    callbacks["on_member_skip"](
                        member_id, "画师不存在或已被封禁", is_error=True
                    )
                continue
            elif member_exists is None:
                PixivHelper.print_and_log(
                    "warn",
                    f"Cannot verify member_id={member_id} (network error?). Proceeding anyway.",
                )

            # PixivHelper.print_and_log(
            #     "info",
            #     f"Anchor verification will be performed during download scan for member_id={member_id}.",
            # )

            _fetch_missing_record_info(caller, record)

            if record.get("has_r18") is None:
                r18_result = _check_has_r18(caller, member_id)
                if r18_result is not None:
                    record["has_r18"] = r18_result
                    PixivHelper.print_and_log(
                        "info",
                        f"Member {member_id}: Has R18 works = {r18_result}",
                    )

            if _snapshot_record(record) != before_snapshot:
                any_record_changed = True
                before_snapshot = _snapshot_record(record)

            if (
                record["artist"]
                and record["artist"] != "Unknown"
                and record["anchor_date"]
            ):
                latest_id = _get_latest_work_id(
                    caller, member_id, r18mode=config.r18mode
                )
                if latest_id and latest_id == anchor_image_id:
                    PixivHelper.print_and_log(
                        "info",
                        f"Member {member_id}: Anchor {anchor_image_id} is already "
                        f"the latest work. Skipping (no changes).",
                    )
                    current_member = current_member + 1
                    print(
                        f"skipped for member id = {member_id} (anchor already latest)."
                    )
                    print("")
                    if callbacks and callbacks.get("on_member_skip"):
                        callbacks["on_member_skip"](
                            member_id, "锚点已是最新", is_error=False
                        )
                    continue

            mark_value = record.get("mark", "").strip().upper()
            ai_mark_value = record.get("ai_mark", "").strip().upper()
            base_download_dir = (
                config.downloadListDirectory or config.rootDirectory or "."
            )
            dir_parts = []
            if mark_value == "X":
                dir_parts.append("X")
            if ai_mark_value == "AI":
                dir_parts.append("AI")
            if dir_parts:
                user_dir = os.path.join(base_download_dir, *dir_parts)
            else:
                user_dir = base_download_dir

            result_dict = {}
            retry_count = 0
            had_retry = False
            while True:
                try:
                    prefix = f"[{current_member} of {total_members}] "
                    PixivArtistHandler.process_member(
                        caller,
                        config,
                        member_id,
                        user_dir=user_dir,
                        title_prefix=prefix,
                        anchor_image_id=anchor_image_id,
                        anchor_file=None,
                        result_dict=result_dict,
                    )
                    # 成功完成:若之前发生过重试,说明网络已恢复,此时才上报"已恢复"
                    if had_retry and callbacks and callbacks.get("on_recovered_error"):
                        callbacks["on_recovered_error"]()
                    break
                except KeyboardInterrupt:
                    # 场景 B: CTRL+C 发生在 process_member 内部、但不是在
                    # process_image 的 except KeyboardInterrupt 中（例如在
                    # PixivHelper.wait 等待窗口按下），会直接走这里 raise。
                    # 此时 result_dict 已经被 process_member 的 except
                    # KeyboardInterrupt 填充（last_downloaded_id/anchor_updated
                    # 都对），但如果不在这里先应用，record 就不会被更新,
                    # 锚点不会前移到已下载的图，下次会重复下载该图。
                    # 因此在 raise 之前先应急调用 _apply_result_to_record。
                    if result_dict:
                        try:
                            _apply_result_to_record(
                                caller,
                                record,
                                result_dict,
                                member_id,
                                anchor_image_id,
                                callbacks=callbacks,
                            )
                            if _snapshot_record(record) != before_snapshot:
                                any_record_changed = True
                        except BaseException as apply_ex:
                            PixivHelper.print_and_log(
                                "warn",
                                f"Failed to apply result to record on KeyboardInterrupt "
                                f"for member_id={member_id}: {apply_ex}",
                            )
                    # 不吞 Ctrl+C,让其向上传播到 process_anchor_list 的
                    # except BaseException,在那里统一落盘并重抛,确保中断也能保存已处理记录
                    raise
                except BaseException as ex:
                    if retry_count > config.retry:
                        # 判断是否为网络/代理错误——只有网络错误才弹窗让用户介入
                        is_network_error = (
                            isinstance(ex, PixivException)
                            and ex.errorCode == PixivException.SERVER_ERROR
                        ) or (
                            result_dict
                            and isinstance(result_dict.get("skip_reason"), str)
                            and "Network error" in result_dict.get("skip_reason", "")
                        )

                        if is_network_error:
                            PixivHelper.print_and_log(
                                "warn",
                                f"Network error for member_id={member_id}, "
                                f"auto-retry exhausted. Prompting user...",
                            )
                            print()
                            print("=" * 60)
                            print(
                                f"[网络错误] member_id={member_id} "
                                f"自动重试已耗尽，等待用户决定"
                            )
                            print("=" * 60)

                            action = _prompt_user_network_action(member_id, ex)

                            if action == "retry":
                                # 用户要求重试：清空 stale 数据，重置计数器，重新走完整流程
                                PixivHelper.print_and_log(
                                    "info",
                                    f"User opted to retry member_id={member_id}. Resetting retry counter.",
                                )
                                result_dict.clear()
                                retry_count = 0
                                had_retry = False
                                print_delay_val = getattr(config, "retryWait", 2)
                                PixivHelper.print_delay(print_delay_val)
                                continue
                            elif action == "quit":
                                # 用户要求退出：清理并抛出 KeyboardInterrupt 走正常中断流程
                                PixivHelper.print_and_log(
                                    "warn",
                                    f"User opted to quit on network error at member_id={member_id}.",
                                )
                                result_dict.clear()
                                raise KeyboardInterrupt()
                            # else: skip —— 落到下面的 break，标记此作者失败，继续下一个
                            PixivHelper.print_and_log(
                                "info",
                                f"User opted to skip member_id={member_id} due to network error.",
                            )
                        else:
                            # 非网络错误（作者不存在、无图片等业务错误）：原有自动跳过逻辑不变
                            PixivHelper.print_and_log(
                                "error", f"Giving up member_id: {member_id} ==> {ex}"
                            )

                        if result_dict:
                            result_dict.setdefault("status", "error")
                            result_dict.setdefault("error_occurred", True)
                            # 标记已通过 on_fatal_error 上报，避免后续 if result_dict 分支
                            # 再走 on_member_skip 造成同一失败被双重上报
                            result_dict["_fatal_reported"] = True
                        if callbacks and callbacks.get("on_fatal_error"):
                            callbacks["on_fatal_error"](member_id, str(ex))
                        break
                    retry_count = retry_count + 1
                    had_retry = True
                    # 修复: 清空 result_dict,避免上次失败时残留的 stale 字段
                    # (如 skip_reason="Network error during member page retrieval")
                    # 污染本次重试的判断,导致"图片下载失败"被错误归类为
                    # "网络验证失败"而触发 on_member_skip 双重上报。
                    # process_member 内部所有分支(成功/失败/中断)都会重新填充
                    # result_dict 的完整字段,清空不会影响正常流程。
                    result_dict.clear()
                    print(
                        f"Something wrong, retrying after 2 second ({retry_count}) ==> {ex}"
                    )
                    PixivHelper.print_delay(2)

            _apply_result_to_record(
                caller,
                record,
                result_dict,
                member_id,
                anchor_image_id,
                callbacks=callbacks,
            )

            if _snapshot_record(record) != before_snapshot:
                any_record_changed = True

            current_member = current_member + 1
            print(f"done for member id = {member_id}.")
            dl_count = 0
            anchor_upd = False
            artist_n = record.get("artist", "")
            anchor_dt = record.get("anchor_date", "")
            if result_dict:
                dl_count = result_dict.get("downloaded_count", 0)
                anchor_upd = result_dict.get("anchor_updated", False)
                artist_n = result_dict.get("artist_name", "") or artist_n
            print(
                f"__DOWNLOAD_INFO__ member_id={member_id} "
                f"downloaded={dl_count} anchor_updated={anchor_upd} "
                f"anchor_date={anchor_dt} "
                f"artist={artist_n}"
            )
            if callbacks and callbacks.get("on_download_complete"):
                file_names = result_dict.get("file_names", [])
                callbacks["on_download_complete"](
                    member_id, dl_count, anchor_upd, artist_n, anchor_dt, file_names
                )
            print("")

        if any_record_changed:
            _sort_records(records)
            _write_anchor_file(anchor_file, records)
            PixivHelper.print_and_log("info", "AnchorList.csv updated and sorted.")
        else:
            PixivHelper.print_and_log("info", "No changes to AnchorList.csv.")
    except BaseException as ex:
        # Python 3 中 KeyboardInterrupt 继承自 BaseException 而非 Exception,
        # 原 `except Exception` 是死代码,中断时不会进入此分支,导致无法落盘
        # 中断/异常也要落盘已处理的记录,避免下次全量重扫
        # 注意:异常可能发生在 any_record_changed/records 定义之前,需用 locals().get 防御
        try:
            if locals().get("any_record_changed") and locals().get("records"):
                _sort_records(records)
                _write_anchor_file(anchor_file, records)
                PixivHelper.print_and_log(
                    "info",
                    f"AnchorList.csv updated on {type(ex).__name__}.",
                )
        except Exception as write_ex:
            PixivHelper.print_and_log(
                "error",
                f"Failed to write AnchorList.csv on {type(ex).__name__}: {write_ex}",
            )
        if isinstance(ex, KeyboardInterrupt):
            raise
        PixivHelper.print_and_log(
            "error", f"Error at process_anchor_list(): {sys.exc_info()}"
        )
        raise


def _get_image_date(caller, image_id):
    try:
        import common.PixivBrowserFactory as PixivBrowserFactory

        image, _ = PixivBrowserFactory.getBrowser().getImagePage(image_id=image_id)
        if image and hasattr(image, "worksDateDateTime") and image.worksDateDateTime:
            dt_val = image.worksDateDateTime
            if dt_val.tzinfo is None:
                from datetime import timezone

                dt_val = dt_val.replace(tzinfo=timezone.utc)
            local_tz = datetime.datetime.now().astimezone().tzinfo
            dt_val = dt_val.astimezone(local_tz)
            return dt_val.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None
