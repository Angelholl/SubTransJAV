"""
SubTransJAV PyWebView API

Backend API for the standalone SRT translation GUI.
Maintains the thin wrapper pattern - delegates work to the
``subtransjav.refine.cli`` subprocess and streams its output.
"""

import contextlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast

import webview
from webview import FileDialog

from subtransjav.utils.process_manager import (
    PSUTIL_AVAILABLE,
    terminate_process_tree,
)

from .event_stream import (  # noqa: E402  webview-free 可测模块
    HEARTBEAT_STALE_S_DEFAULT,
    EventStreamParser,
    format_event_line,
    resume_state_for_path,
)
from .strings import msg  # noqa: E402  用户可见文案唯一中文来源

# Project root (subtransjav/webview_gui/api.py -> project root)
REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# 会话内用户选择的路径登记（信任边界：scan_resume_states 只处理这些路径）
# 由受信入口登记：文件对话框 / 文件夹扫描 / 拖放（main.py on_drop_event）。
# ---------------------------------------------------------------------------
SESSION_SELECTED_PATHS: set[str] = set()


def register_session_paths(paths) -> None:
    """登记用户通过受信入口选择的路径（拖放入口由 main.py 调用）。"""
    for p in paths or []:
        if isinstance(p, str) and p:
            try:
                SESSION_SELECTED_PATHS.add(str(Path(p).resolve()))
            except (OSError, ValueError):
                continue


def _ensure_template_dir(templates_dir) -> str:
    """角色卡目录守卫（反路径穿越加固）。

    角色卡是仓库固定资源语义，仅放行两类目录：
      1. 服务端默认模板目录（config/templates）——不传目录时的正常主路径；
      2. 本会话经受信入口（原生文件夹对话框/拖放）登记的用户自选目录，
         且必须通过 _resolve_safe_path 锚点校验（home/仓库根白名单）。
    其余前端任意路径一律拒绝，阻断被攻陷前端借角色卡读写接口
    越锚访问用户主目录下的同名文件。
    """
    try:
        from subtransjav.refine.config import default_templates_dir
        default_dir = str(_resolve_safe_path(default_templates_dir()))
    except Exception:
        default_dir = ""
    if not templates_dir:
        return default_dir
    resolved = str(_resolve_safe_path(templates_dir))
    if default_dir and os.path.normcase(resolved) == os.path.normcase(default_dir):
        return resolved
    allowed = {os.path.normcase(p) for p in SESSION_SELECTED_PATHS}
    if os.path.normcase(resolved) not in allowed:
        raise ValueError(
            msg("template_dir_not_allowed", path=resolved))
    return resolved


# Security guards live in a webview-free module so they are testable on CI
from .security import (  # noqa: E402
    _resolve_safe_path,
    _validate_user_directory,
    is_safe_url_scheme,
)

# ---------------------------------------------------------------------------
# Observability: module-level logger writing to Logs/gui.log
# ---------------------------------------------------------------------------
_log = logging.getLogger("subtransjav.gui")
if not _log.handlers:
    _log_dir = REPO_ROOT / "Logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(
        _log_dir / "gui.log", encoding="utf-8",
    )
    _fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _fh.setLevel(logging.INFO)
    _log.addHandler(_fh)
    _log.setLevel(logging.INFO)


def _log_exc(context: str) -> None:
    """Log current exception with traceback at ERROR level."""
    _log.exception(context)


def _get_documents_dir() -> Path:
    """Platform-neutral Documents directory resolution."""
    home = Path.home()
    docs = home / "Documents"
    return docs if docs.exists() else home


def _compute_default_output_dir() -> Path:
    """Default output dir: <Documents>/SubTransJAV/output."""
    base = _get_documents_dir()
    if base.name.lower() != "documents" or not base.exists():
        base = Path.home()
    p = base / "SubTransJAV" / "output"
    p.mkdir(parents=True, exist_ok=True)
    return p


DEFAULT_OUTPUT = _compute_default_output_dir()


def _refine_error_tip(e: Exception) -> str:
    """refine 测试失败的中文诊断提示"""
    s = str(e)
    if "RegionError" in s or "not available in your country" in s:
        return msg("tip_region_blocked")
    if "429" in s or "RateLimit" in s or "FreeUsageLimit" in s:
        return msg("tip_rate_limited")
    if "unavailable" in s or "Upstream" in s:
        return msg("tip_upstream_down")
    if "api_key" in s.lower() or "401" in s:
        return msg("tip_invalid_key")
    return msg("tip_check_key_network")


def _build_refine_args(options: dict[str, Any]) -> list[str]:
    """构建净语翻译 CLI 参数（v2 两阶段管线）。

    options 键：
      inputs: List[str]                输入 SRT 列表
      output_dir: str                  输出目录（'source' 哨兵=随输入）
      profile: str                     兜底档位 local|cloud
      s1_provider / s1_model           阶段A 服务商与模型（槽位0）
      s3_provider / s3_model           阶段B 服务商与模型（槽位2）
      templates_dir: str               角色卡目录
      glossary: str                    词库 CSV 路径
      apply_glossary_stage1/2: bool
      batch_local / batch_cloud: int
      v2_concurrency: int              批间并发数（1-5，缺省1）
      v2_ctx: int                      本地模型上下文窗口（显式对齐引擎与管线两侧；
                                       缺省不传=CLI 默认 22272，缺省为作者 16GB 单卡实测档案值，请按显存调整）
      lmstudio_endpoint / zen_endpoint / custom_endpoint: str
      deepseek_key / zen_key / custom_key: str
      source_filter: str                闸门0 源侧幻觉检测档位 strict|default|off（缺省 default 不传参）
      auto_synopsis: bool               剧情自摘要（默认 True；显式 False 才传 --no-auto-synopsis）
      dry_run: bool                     试运行（仅生成执行计划，不调用模型）
      verbose: bool
    """
    args = [sys.executable, "-u", "-m", "subtransjav.refine.cli"]

    inputs = options.get("inputs") or []
    for p in inputs:
        # Guard against filenames starting with '-' being parsed as CLI flags
        if p.startswith("-"):
            args.append(f"--input={p}")
        else:
            args.extend(["-i", p])

    out_dir = options.get("output_dir", "")
    if out_dir and out_dir.lower().strip() != "source":
        args.extend(["-o", out_dir])

    # v2 两阶段管线（阶段A→s1 槽位、阶段B→s3 槽位）
    if options.get("profile"):
        args.extend(["--profile", str(options["profile"])])

    for n in (1, 3):
        pv = options.get(f"s{n}_provider")
        if pv:
            args.extend([f"--s{n}-provider", str(pv)])
        mv = options.get(f"s{n}_model")
        if mv:
            args.extend([f"--s{n}-model", str(mv)])

    if options.get("templates_dir"):
        args.extend(["--templates-dir", options["templates_dir"]])
    if options.get("glossary"):
        args.extend(["--glossary", options["glossary"]])
    if options.get("apply_glossary_stage1") is False:
        args.append("--no-gl1")
    if options.get("apply_glossary_stage2") is False:
        args.append("--no-gl2")

    bl = options.get("batch_local")
    if bl:
        args.extend(["--batch-local", str(bl)])
    bc = options.get("batch_cloud")
    if bc:
        args.extend(["--batch-cloud", str(bc)])

    # 批间并发数（缺省2；钳制上限经 config 单一来源，CLI 端 __post_init__ 会再钳制一次）
    try:
        n_conc = int(options.get("v2_concurrency") or 2)
    except (TypeError, ValueError):
        n_conc = 2
    from subtransjav.refine.config import resolve_tunable
    n_max = int(resolve_tunable("v2_concurrency_max"))
    args.extend(["--v2-concurrency", str(max(1, min(n_max, n_conc)))])

    # 本地模型上下文窗口：GUI 显式传值即对齐引擎与管线两侧（引擎加载 -c
    # 由管线内 ensure_lmstudio_model 派生自同一配置，见 utils/lmstudio.py）
    try:
        n_ctx = int(options.get("v2_ctx") or 0)
    except (TypeError, ValueError):
        n_ctx = 0
    if n_ctx > 0:
        args.extend(["--v2-ctx", str(n_ctx)])

    for key, flag in (("lmstudio_endpoint", "--lmstudio-endpoint"),
                      ("ollama_endpoint", "--ollama-endpoint"),
                      ("zen_endpoint", "--zen-endpoint"),
                      ("siliconflow_endpoint", "--siliconflow-endpoint"),
                      ("custom_endpoint", "--custom-endpoint")):
        v = options.get(key)
        if v:
            args.extend([flag, str(v)])

    # API keys are NOT passed via CLI args (visible in process list);
    # they are injected into the subprocess env by start_translation().

    # API 密钥不再通过命令行参数传递（进程列表可见），
    # 由 start_translation() 注入子进程环境变量。

    if options.get("fallback_local"):
        args.append("--fallback-local")
    if options.get("fallback_model"):
        args.extend(["--fallback-model", str(options["fallback_model"])])

    # 自定义净语规则配置目录
    if options.get("cleaner_config_dir"):
        args.extend(["--cleaner-config", str(options["cleaner_config_dir"])])

    if options.get("verbose"):
        args.append("--verbose")

    # 闸门0 源侧幻觉检测档位（仅非默认值时传递，choices 与 cli.py 保持一致）
    sf = options.get("source_filter")
    if sf and sf != "default":
        args.extend(["--source-filter", str(sf)])

    # 剧情自摘要（默认开启；仅显式关闭时传反转开关）
    if options.get("auto_synopsis") is False:
        args.append("--no-auto-synopsis")

    # 试运行：仅生成执行计划，不调用模型、不产出字幕
    if options.get("dry_run"):
        args.append("--dry-run")

    # 批量处理参数
    if options.get("input_dir"):
        args.extend(["--input-dir", str(options["input_dir"])])
    if options.get("recursive"):
        args.append("-r")
    if options.get("filter_pattern"):
        args.extend(["--filter-pattern", str(options["filter_pattern"])])
    if options.get("min_size"):
        args.extend(["--min-size", str(options["min_size"])])
    if options.get("max_size"):
        args.extend(["--max-size", str(options["max_size"])])

    # 翻译记忆库参数
    if options.get("no_tm"):
        args.append("--no-tm")
    if options.get("tm_db"):
        args.extend(["--tm-db", str(options["tm_db"])])
    if options.get("tm_threshold"):
        args.extend(["--tm-threshold", str(options["tm_threshold"])])

    # 断点恢复（复用已完成阶段；仅当用户勾选时传递）
    if options.get("resume"):
        args.append("--resume")

    # 覆盖已完成产物：仅 GUI 确认框确认后由前端显式传入 force=True 时追加
    # （D2026-0925-01 D6 终选：force 仅作为确认路径可达）
    if options.get("force"):
        args.append("--force")

    # 强制断点恢复：指纹校验不匹配仍复用旧产物；force_resume 隐含 resume
    # 由 RefineConfig.__post_init__ 不变式保证，无需在此重复拼 --resume
    if options.get("force_resume"):
        args.append("--force-resume")

    # 学习闸开关（manifest 钉定的三个影响学习行为的开关之二，入产物指纹；
    # tm_learn_gate 默认 True 与 TM 勾选语义重叠，GUI 不设开关，
    # 保持 CLI --no-tm-learn-gate 通道）
    if options.get("glossary_learn"):
        args.append("--glossary-learn")
    if options.get("glossary_conflict_block"):
        args.append("--glossary-conflict-block")

    # NDJSON 结构化事件流（GUI 侧解析进度/风险/心跳；同仓 CLI 固定支持）
    args.extend(["--event-format", "ndjson"])

    return args


class TranslateAPI:
    """
    API class exposed to JavaScript via PyWebView.

    All public methods are callable from JavaScript via:
        pywebview.api.method_name(args)
    """

    def __init__(self):
        """Initialize API state."""
        self.process: subprocess.Popen | None = None

        # 退出时清理 refine 临时目录并终止残留子进程
        import atexit
        atexit.register(self._on_exit_cleanup)
        self._refine_tmp_dirs: list[str] = []

        # Lock for _translate_process access (GUI thread vs reader thread)
        self._translate_lock = threading.Lock()

        # Default output directory (ensure it exists and is normalized)
        self.default_output = str(_compute_default_output_dir())

    # ========================================================================
    # Version / misc
    # ========================================================================

    def get_version(self) -> dict[str, Any]:
        """Get application version information."""
        try:
            from subtransjav.__version__ import (
                __version__,
                __version_display__,
                __version_info__,
            )
            return {
                "success": True,
                "version": __version_display__,
                "version_pep440": __version__,
                "version_info": __version_info__,
            }
        except ImportError:
            return {
                "success": False,
                "version": "unknown",
                "message": msg("version_load_failed")
            }

    def get_system_status(self) -> dict[str, Any]:
        """Get system status including optional features like grammar hints."""
        features: dict[str, dict[str, Any]] = {}
        status = {
            "success": True,
            "features": features
        }

        # Check SudachiPy availability for grammar hints
        try:
            from subtransjav.refine.grammar_hint import is_grammar_hint_available
            features["grammar_hints"] = {
                "available": is_grammar_hint_available(),
                "description": msg("grammar_hints_available")
            }
        except ImportError:
            features["grammar_hints"] = {
                "available": False,
                "description": msg("grammar_hints_unavailable")
            }

        return status

    def open_url(self, url: str) -> dict[str, Any]:
        """Open a URL in the system browser."""
        try:
            if not is_safe_url_scheme(url):
                return {"success": False, "error": msg("url_scheme_unsupported")}
            import webbrowser
            webbrowser.open(url)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ========================================================================
    # File dialogs
    # ========================================================================

    def select_folder(self) -> dict[str, Any]:
        """Open native folder dialog to select a folder."""
        windows = webview.windows
        if not windows:
            return {"success": False, "message": msg("no_active_window")}

        result = windows[0].create_file_dialog(FileDialog.FOLDER)
        if result and len(result) > 0:
            register_session_paths(result)  # 受信入口：文件夹对话框选取即登记
            return {"success": True, "path": result[0]}
        return {"success": False, "message": msg("no_folder_selected")}

    def select_output_directory(self) -> dict[str, Any]:
        """Open native folder dialog to select output directory."""
        return self.select_folder()

    def select_srt_files(self) -> dict[str, Any]:
        """Open file dialog to select SRT files for translation."""
        windows = webview.windows
        if not windows:
            return {"success": False, "message": msg("no_active_window")}

        file_types = [
            'Subtitle Files (*.srt)',
            'All Files (*.*)'
        ]

        result = windows[0].create_file_dialog(
            FileDialog.OPEN,
            allow_multiple=True,
            file_types=file_types
        )

        if result and len(result) > 0:
            register_session_paths(result)
            return {"success": True, "paths": list(result)}
        return {"success": False, "message": msg("no_files_selected")}

    def select_srt_folder(self) -> dict[str, Any]:
        """Open folder dialog and find .srt files in the selected folder."""
        windows = webview.windows
        if not windows:
            return {"success": False, "message": msg("no_active_window")}

        result = windows[0].create_file_dialog(FileDialog.FOLDER)
        if result and len(result) > 0:
            folder = Path(result[0])
            srt_files = sorted(str(f) for f in folder.glob("*.srt"))
            if srt_files:
                register_session_paths(srt_files)
                return {"success": True, "paths": srt_files, "folder": result[0]}
            return {"success": False, "message": msg("no_srt_in_folder")}
        return {"success": False, "message": msg("no_folder_selected")}

    def scan_srt_folder(self, folder: str, recursive: bool = True,
                        pattern: str = "*.srt", min_size: int = 0,
                        max_size: int = 0, min_date: str = "",
                        max_date: str = "",
                        exclude: list[str] | None = None) -> dict[str, Any]:
        """扫描目录下的 SRT 文件（支持递归/过滤）。

        Args:
            folder:   根目录路径
            recursive: 是否递归子目录
            pattern:   文件名 glob 模式
            min_size:  最小文件大小（字节）
            max_size:  最大文件大小（字节，0=不限）
            min_date:  最早修改日期（YYYY-MM-DD）
            max_date:  最晚修改日期（YYYY-MM-DD）
            exclude:   排除的路径模式列表
        """
        try:
            from subtransjav.refine.batch import find_srt_files, scan_summary
            folder = _validate_user_directory(folder)
            files = find_srt_files(
                directory=folder,
                recursive=recursive,
                pattern=pattern,
                min_size=min_size,
                max_size=max_size,
                min_date=min_date,
                max_date=max_date,
                exclude_patterns=exclude,
            )
            summary = scan_summary(files)
            register_session_paths(files)
            return {
                "success": True,
                "paths": files,
                "summary": summary,
            }
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_output_folder(self, path: str, create: bool = True) -> dict[str, Any]:
        """Open a folder in file explorer.

        Args:
            path:   Directory path to open.
            create: If True (default), create the directory when it doesn't exist
                    (useful for output dirs). If False, return an error when the
                    directory doesn't exist (useful for browsing existing dirs).
        """
        try:
            path = _validate_user_directory(path)
            folder = Path(path)
            if create:
                folder.mkdir(parents=True, exist_ok=True)
            elif not folder.is_dir():
                return {"success": False,
                        "message": msg("dir_not_exist", path=folder)}

            if sys.platform.startswith("win"):
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)])
            else:
                subprocess.run(["xdg-open", str(folder)])

            return {"success": True, "message": msg("folder_opened")}
        except Exception as e:
            return {"success": False, "message": msg("cannot_open_folder", e=e)}

    def get_default_output_dir(self) -> str:
        """Get the default output directory path."""
        self.default_output = str(_compute_default_output_dir())
        return self.default_output

    # ========================================================================
    # Translation process management
    # ========================================================================

    def _init_translation_state(self):
        """Initialize translation-specific state if not already done."""
        if not hasattr(self, '_translate_process'):
            self._translate_process: subprocess.Popen | None = None
            self._translate_status = "idle"
            self._translate_error: str | None = None
            self._translate_log_queue: queue.Queue = queue.Queue()
            self._translate_thread: threading.Thread | None = None
            self._translate_files_total = 0
            self._translate_files_completed = 0
            self._translate_current_file = None
            self._translate_lines_total = 0
            self._translate_lines_done = 0
            self._translate_current_stage = ""
            # NDJSON 事件流解析器（start_translation 时重建）
            self._translate_parser: EventStreamParser | None = None

    def start_translation(self, options: dict[str, Any]) -> dict[str, Any]:
        """
        Start the refine translation subprocess.

        Args:
            options: Options collected by buildRefineOptionsV2() in app.js.
        """
        # D2026-0925-01 D6：终稿覆盖确认。与 resume_state_for_path 同源判定
        # （completed = {stem}_final_cn.srt 存在）；任一输入已完成且未带
        # force=True 时不启动进程，返回结构化 needs_confirm 由前端弹确认框。
        if not options.get("force"):
            existing = []
            for _p in (options.get("inputs") or []):
                try:
                    _st = resume_state_for_path(str(_p))
                except Exception:
                    continue
                if _st.get("state") == "completed":
                    existing.append(f"{_st['stem']}_final_cn.srt")
            if existing:
                return {
                    "success": False,
                    "needs_confirm": True,
                    "existing": existing,
                }

        self._init_translation_state()

        with self._translate_lock:
            if self._translate_process is not None:
                return {"success": False, "error": msg("translation_in_progress")}
            # Sentinel: mark "starting" to block double-start while Popen runs
            #（True 哨兵仅作占位，消费侧均先判 `is True`；cast 仅为类型清零）
            self._translate_process = cast(subprocess.Popen, True)

        self._translate_files_total = 0
        self._translate_files_completed = 0
        self._translate_current_file = None
        self._translate_lines_total = 0
        self._translate_lines_done = 0
        self._translate_current_stage = ""
        # 每次启动重建事件流解析器（进度/风险/心跳/摘要从零聚合）；
        # 心跳超时阈值走配置分层（默认 < 用户文件 < 环境变量）
        try:
            from subtransjav.refine.config import resolve_tunable
            _stale_s = float(resolve_tunable("heartbeat_stale_s"))
        except Exception:
            _stale_s = HEARTBEAT_STALE_S_DEFAULT
        self._translate_parser = EventStreamParser(heartbeat_stale_s=_stale_s)

        try:
            args = _build_refine_args(options)

            # 记录 refine 临时目录，供程序退出时清理
            try:
                from subtransjav.refine.pipeline_support import refine_tmp_dir, strip_lang_suffix
                for _p in (options.get("inputs") or []):
                    _ip = Path(_p).resolve()
                    _stem = strip_lang_suffix(_ip.stem)
                    _td = refine_tmp_dir(str(_ip), _stem)
                    if _td not in self._refine_tmp_dirs:
                        self._refine_tmp_dirs.append(_td)
            except Exception:
                pass

            # Unbuffered + UTF-8 so streaming works with non-ASCII output (#190)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8:replace"

            # Inject API keys into subprocess env (not CLI args) for security.
            # Variable names match those checked by RefineConfig.resolve_api_key().
            _key_env_map = {
                "deepseek_key": "DEEPSEEK_API_KEY",
                "zen_key": "OPENCODE_API_KEY",
                "siliconflow_key": "SILICONFLOW_API_KEY",
                "custom_key": "CUSTOM_API_KEY",
            }
            for opt_key, env_var in _key_env_map.items():
                v = options.get(opt_key)
                if v:
                    env[env_var] = str(v)

            # stdout/stderr 分离：stdout 逐行喂 NDJSON 事件解析器，
            # stderr 原样入日志队列；各自独立线程排空管道防死锁（#190）
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(REPO_ROOT),
                env=env
            )

            with self._translate_lock:
                self._translate_process = proc

            self._translate_status = "running"
            self._translate_error = None

            # 两个守护 reader 线程：stdout→解析器 feed + 人类可读行入日志队列；
            # stderr→原样入日志队列
            self._translate_thread = threading.Thread(
                target=self._pump_stdout, args=(proc,),
                name="gui-stdout-reader", daemon=True
            )
            self._translate_thread.start()
            threading.Thread(
                target=self._pump_stderr, args=(proc,),
                name="gui-stderr-reader", daemon=True
            ).start()

            return {
                "success": True,
                "message": msg("translation_started",
                               n=len(options.get('inputs', []))),
                "pid": proc.pid
            }

        except Exception as e:
            _log_exc("start_translation")
            with self._translate_lock:
                proc = self._translate_process
                self._translate_process = None
            if proc is not None and proc is not True and hasattr(proc, 'kill'):
                with contextlib.suppress(Exception):
                    proc.kill()
            self._translate_status = "error"
            return {"success": False, "error": str(e)}

    def _pump_stdout(self, proc: subprocess.Popen):
        """stdout 守护线程：NDJSON 事件解析 + 人类可读行入日志队列。

        - 事件行格式化成 "[事件] 阶段B 批次 3/10" 风格（心跳不落日志防刷屏）；
        - 非事件行原样入日志队列，并由解析器的遗留兼容层提取进度/错误。
        """
        parser = self._translate_parser or EventStreamParser()
        try:
            for line in proc.stdout:
                try:
                    event = parser.feed(line)
                except Exception:
                    _log_exc("_pump_stdout.feed")
                    event = None
                text = format_event_line(event) if event else None
                self._translate_log_queue.put(text if text is not None else line)
        except Exception as e:
            _log_exc("_pump_stdout")
            self._translate_log_queue.put(f"\n[ERROR] {e}\n")
        finally:
            with contextlib.suppress(Exception):
                proc.wait()

    def _pump_stderr(self, proc: subprocess.Popen):
        """stderr 守护线程：原样入日志队列。"""
        try:
            for line in proc.stderr:
                self._translate_log_queue.put(line)
        except Exception as e:
            _log_exc("_pump_stderr")
            self._translate_log_queue.put(f"\n[ERROR] {e}\n")

    def cancel_translation(self) -> dict[str, Any]:
        """Cancel running translation process."""
        self._init_translation_state()

        with self._translate_lock:
            proc = self._translate_process
            if proc is None:
                return {"success": False, "error": msg("no_translation_in_progress")}
            # Sentinel (True) means start_translation is still launching — cannot cancel yet
            if proc is True:
                return {"success": False, "error": msg("translation_still_starting")}

        try:
            self._translate_status = "cancelled"

            if PSUTIL_AVAILABLE:
                result = terminate_process_tree(proc.pid)
                if not result["success"]:
                    proc.terminate()
            else:
                proc.terminate()

            # Always wait for process to exit to avoid zombie processes
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

            self._translate_log_queue.put(
                f"\n[CANCELLED] {msg('log_cancelled')}\n")
            with self._translate_lock:
                self._translate_process = None

            return {"success": True, "message": msg("translation_cancelled")}
        except Exception as e:
            _log_exc("cancel_translation")
            return {"success": False, "error": str(e)}

    def get_translation_status(self) -> dict[str, Any]:
        """Get current translation status."""
        self._init_translation_state()

        with self._translate_lock:
            proc = self._translate_process

        # Guard against sentinel (True) from start_translation
        if proc is True:
            proc = None

        parser = getattr(self, '_translate_parser', None)
        snap = parser.snapshot() if parser is not None else {}
        warning_level = None

        if proc is not None:
            poll = proc.poll()
            if poll is not None:
                exit_code = poll
                with self._translate_lock:
                    if self._translate_process is proc:
                        self._translate_process = None

                risk_count = int(snap.get('risk_count') or 0)
                majority = bool(snap.get('untranslated_majority'))

                if self._translate_status != "cancelled":
                    if exit_code == 3:
                        # CLI 约定：exit 3 = 完成但存在严重质量风险
                        self._translate_status = "completed"
                        warning_level = "critical"
                        self._translate_log_queue.put(
                            f"\n[WARN] {msg('warn_exit3')}\n")
                    elif exit_code == 0:
                        self._translate_status = "completed"
                        if majority:
                            warning_level = "critical"
                            self._translate_log_queue.put(
                                f"\n[WARN] {msg('warn_majority')}\n")
                        elif risk_count > 0:
                            warning_level = "warning"
                            self._translate_log_queue.put(
                                f"\n[WARN] {msg('warn_risks', n=risk_count)}\n")
                        else:
                            self._translate_log_queue.put(
                                f"\n[SUCCESS] {msg('log_success')}\n")
                    else:
                        self._translate_status = "error"
                        if not self._translate_error:
                            self._translate_error = (
                                snap.get('error')
                                or msg("process_exit_code", code=exit_code))
                        self._translate_log_queue.put(
                            f"\n[ERROR] {msg('log_exit_code', code=exit_code)}\n")

        risk_count = int(snap.get('risk_count') or 0)
        majority = bool(snap.get('untranslated_majority'))

        return {
            "status": self._translate_status,
            "progress": int(snap.get('progress') or 0),
            "current_file": snap.get('current_file'),
            "files_completed": getattr(self, '_translate_files_completed', 0),
            "files_total": int(snap.get('total') or 0),
            "has_logs": not self._translate_log_queue.empty(),
            "error": self._translate_error or snap.get('error'),
            # --- NDJSON 事件流扩展键（保留全部旧键） ---
            "current_stage": snap.get('stage'),
            "risks": snap.get('risks', []),
            "risk_count": risk_count,
            "untranslated_majority": majority,
            "heartbeat_age": snap.get('heartbeat_age'),
            "heartbeat_stale_s": float(
                snap.get('heartbeat_stale_s') or HEARTBEAT_STALE_S_DEFAULT),
            "degraded": risk_count > 0 or majority,
            "warning_level": warning_level,
            "ndjson_mode": bool(snap.get('ndjson_mode')),
        }

    def get_translation_logs(self) -> list[str]:
        """Get new translation log lines."""
        self._init_translation_state()

        logs = []
        while not self._translate_log_queue.empty():
            try:
                logs.append(self._translate_log_queue.get_nowait())
            except queue.Empty:
                break
        return logs

    def scan_resume_states(self, paths: list[str]) -> list[dict[str, Any]]:
        """对用户本次会话选择的输入 srt 计算断点恢复状态。

        - 有 ``{stem}_final_cn.srt`` → completed（整文件已完成）
        - 有 ``{stem}_manifest.json`` 无终稿 → resumable（可复用已完成阶段）
        - 否则 → none

        信任边界：仅处理通过受信入口（文件对话框/文件夹扫描/拖放）登记过的
        路径，未登记的路径直接跳过，不做任意路径解析。
        """
        results: list[dict[str, Any]] = []
        for p in paths or []:
            if not isinstance(p, str) or not p:
                continue
            try:
                resolved = str(Path(p).resolve())
            except (OSError, ValueError):
                continue
            if resolved not in SESSION_SELECTED_PATHS:
                continue
            try:
                results.append(resume_state_for_path(resolved))
            except Exception:
                continue
        return results

    # ================================================================
    # Refine UI 辅助 API（净语翻译两阶段界面）
    # ================================================================
    def refine_default_paths(self) -> dict[str, Any]:
        """返回词库/角色卡目录的默认路径"""
        try:
            from subtransjav.refine.config import default_glossary_path, default_templates_dir
            return {
                "success": True,
                "templates_dir": default_templates_dir(),
                "glossary_path": default_glossary_path(),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def refine_list_models(self, provider: str, endpoint: str = None,
                           api_key: str = None) -> dict[str, Any]:
        """在线拉取服务商可用模型列表（Zen 免费模型置顶）"""
        try:
            from openai import OpenAI

            from subtransjav.refine.config import (
                DEEPSEEK_BASE_DEFAULT,
                DEFAULT_TIMEOUT_HTTP,
                PROVIDER_ENDPOINT_DEFAULTS,
            )
            from subtransjav.refine.secrets import read_secret

            provider = (provider or "").lower()
            if provider == "deepseek":
                base = DEEPSEEK_BASE_DEFAULT
                key = api_key or os.environ.get("DEEPSEEK_API_KEY", "") \
                    or read_secret("deepseek")
            else:
                base = endpoint or PROVIDER_ENDPOINT_DEFAULTS.get(provider, "")
                if provider == "zen":
                    key = api_key or os.environ.get("OPENCODE_API_KEY", "") \
                        or read_secret("zen")
                elif provider == "siliconflow":
                    key = api_key or os.environ.get("SILICONFLOW_API_KEY", "") \
                        or read_secret("siliconflow")
                elif provider == "lmstudio":
                    key = "lm-studio"
                elif provider == "ollama":
                    key = "ollama"
                else:
                    key = api_key or read_secret("custom")
            if not base:
                return {"success": False, "error": msg("endpoint_missing")}
            # endpoint 与外部 URL 同源信任级别：仅放行 http/https
            # （本地 LM Studio/Ollama 走 http://localhost 属核心功能，不放行私有地址拦截）
            if not is_safe_url_scheme(base):
                return {"success": False, "error": msg("endpoint_scheme_unsupported")}
            if provider not in ("lmstudio", "ollama") and not key:
                return {"success": False, "error": msg("api_key_missing")}

            client = OpenAI(base_url=base, api_key=key or "none",
                            timeout=DEFAULT_TIMEOUT_HTTP)
            models = sorted(m.id for m in client.models.list())
            if provider == "zen":
                models.sort(key=lambda x: (not x.endswith("-free"), x))
            return {"success": True, "models": models}
        except Exception as e:
            _log_exc("refine_list_models")
            return {"success": False, "error": f"{type(e).__name__}: {e}",
                    "tip": _refine_error_tip(e)}

    def list_local_models(self, endpoint: str = None) -> dict[str, Any]:
        """获取本地 LM Studio 可用模型列表（已加载 + 已下载）"""
        try:
            import requests as _req
            base = (endpoint or "http://localhost:1234/v1").rstrip("/")
            # 去掉 /v1 后缀得到 root
            root = base[:-3] if base.endswith("/v1") else base

            # 获取已加载模型
            loaded = []
            try:
                r = _req.get(f"{root}/v1/models", timeout=5)
                loaded = [m.get("id", "") for m in r.json().get("data", [])]
            except Exception:
                pass

            # 获取已下载模型
            downloaded = []
            try:
                r0 = _req.get(f"{root}/api/v0/models", timeout=5)
                downloaded = [m.get("id", "") for m in r0.json().get("data", [])]
            except Exception:
                pass

            # 合并去重，已加载的排前面
            all_models = list(dict.fromkeys(loaded + downloaded))
            return {"success": True, "models": all_models, "loaded": loaded}
        except Exception as e:
            _log_exc("list_local_models")
            return {"success": False, "error": str(e), "models": []}

    def refine_test_stage(self, provider: str, model: str,
                          endpoint: str = None, api_key: str = None) -> dict[str, Any]:
        """单阶段连通性测试：极小请求验证服务商+模型可用性"""
        try:
            from openai import OpenAI

            from subtransjav.refine.config import DEEPSEEK_BASE_DEFAULT, DEFAULT_TIMEOUT_HTTP
            from subtransjav.refine.secrets import read_secret

            provider = (provider or "").lower()
            model = (model or "").strip()
            if not model:
                return {"success": False, "error": msg("model_name_missing")}
            if provider == "deepseek":
                base = DEEPSEEK_BASE_DEFAULT
                key = api_key or os.environ.get("DEEPSEEK_API_KEY", "") \
                    or read_secret("deepseek")
            else:
                from subtransjav.refine.config import PROVIDER_ENDPOINT_DEFAULTS as _PED
                base = endpoint or _PED.get(provider, "")
                if provider == "zen":
                    key = api_key or os.environ.get("OPENCODE_API_KEY", "") \
                        or read_secret("zen")
                elif provider == "siliconflow":
                    key = api_key or os.environ.get("SILICONFLOW_API_KEY", "") \
                        or read_secret("siliconflow")
                elif provider == "lmstudio":
                    key = "lm-studio"
                elif provider == "ollama":
                    key = "ollama"
                else:
                    key = api_key or read_secret("custom")
            if not base:
                return {"success": False, "error": msg("endpoint_missing")}
            # endpoint 与外部 URL 同源信任级别：仅放行 http/https
            # （本地 LM Studio/Ollama 走 http://localhost 属核心功能，不放行私有地址拦截）
            if not is_safe_url_scheme(base):
                return {"success": False, "error": msg("endpoint_scheme_unsupported")}

            client = OpenAI(base_url=base, api_key=key or "none",
                            timeout=DEFAULT_TIMEOUT_HTTP)
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": msg("stage_test_ping")}],
                max_tokens=512, temperature=0, stream=False)
            resp = r.choices[0].message
            txt = (resp.content or "").strip()[:40]
            if not txt:
                rc = (getattr(resp, "reasoning_content", None) or "").strip()
                txt = (msg("stage_test_reasoning", tail=rc[-28:])
                       if rc else msg("stage_test_empty"))
            return {"success": True, "message": txt}
        except Exception as e:
            _log_exc("refine_test_stage")
            return {"success": False,
                    "error": f"{type(e).__name__}: {e}",
                    "tip": _refine_error_tip(e)}

    def _refine_stage_settings_path(self) -> str:
        """每阶段 服务商/接口地址 持久化文件（config/refine_stage_settings.json）"""
        try:
            from subtransjav.refine.config import CONFIG_DIR
        except Exception:
            return os.path.join(os.getcwd(), "refine_stage_settings.json")
        # 服务端固定资源：目录由 CONFIG_DIR 决定、文件名硬编码；
        # 仍经安全锚点校验后返回，阻断环境/配置注入的越界路径直达 open() 汇点。
        return str(_resolve_safe_path(
            os.path.join(CONFIG_DIR, "refine_stage_settings.json")))

    def refine_save_stage_settings(self, stages: list[dict[str, Any]] = None,
                                   keys: list[dict[str, Any]] = None,
                                   settings: dict[str, Any] = None) -> dict[str, Any]:
        """保存每阶段设置。
        stages: [{"stage":1, "provider":"zen", "endpoint":"https://...",
                  "model":"..."}, ...]
        keys:   [{"stage":1, "provider":"zen", "key":"sk-..."}, ...]  -> DPAPI 密钥库
        settings: {"v2_concurrency": 2, ...}  -> 写入 JSON 顶层 settings 字典
        """
        saved_eps = 0
        saved_keys = 0
        saved_settings = 0
        try:
            if stages or settings:
                path = self._refine_stage_settings_path()
                data: dict[str, Any] = {"stages": [], "settings": {}}
                try:
                    with open(path, encoding="utf-8") as f:
                        old = json.load(f)
                    if isinstance(old, dict):
                        if isinstance(old.get("stages"), list):
                            data["stages"] = old["stages"]
                        if isinstance(old.get("settings"), dict):
                            data["settings"] = old["settings"]
                except Exception:
                    pass

                if stages:
                    by_stage = {s.get("stage"): s for s in data["stages"]
                                if isinstance(s, dict)}
                    for item in stages or []:
                        try:
                            n = int(item.get("stage"))
                        except Exception:
                            continue
                        entry = by_stage.get(n, {"stage": n})
                        if item.get("provider") is not None:
                            entry["provider"] = str(item["provider"])
                        if item.get("endpoint") is not None:
                            entry["endpoint"] = str(item["endpoint"])
                        # C1（D2026-0925-01）：模型缺省值档位随 stages 直存直读，
                        # 不进 config.py 分层。
                        if item.get("model") is not None:
                            entry["model"] = str(item["model"])
                        by_stage[n] = entry
                        saved_eps += 1
                    data["stages"] = [by_stage[k] for k in sorted(by_stage)]

                if settings:
                    for k, v in settings.items():
                        if k is None or v is None:
                            continue
                        data["settings"][str(k)] = v
                        saved_settings += 1

                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            if keys:
                from subtransjav.refine.secrets import store_secret
                for item in keys or []:
                    prov = (item.get("provider") or "").strip()
                    key = (item.get("key") or "").strip()
                    if not prov:
                        continue
                    store_secret(prov, key)   # 空串 = 删除该密钥
                    saved_keys += 1
            return {"success": True, "endpoints_saved": saved_eps,
                    "keys_saved": saved_keys,
                    "settings_saved": saved_settings}
        except Exception as e:
            _log_exc("refine_save_stage_settings")
            return {"success": False, "error": str(e)}

    def refine_get_stage_settings(self) -> dict[str, Any]:
        """读取已保存的每阶段设置；密钥不回传明文，只返回 has_key 标记"""
        try:
            path = self._refine_stage_settings_path()
            stages: list[Any] = []
            settings: dict[str, Any] = {}
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        stages = data.get("stages", []) \
                            if isinstance(data.get("stages"), list) else []
                        settings = data.get("settings", {}) \
                            if isinstance(data.get("settings"), dict) else {}
                except Exception:
                    stages = []
            from subtransjav.refine.secrets import read_secret
            key_status = {}
            for prov in ("deepseek", "zen", "siliconflow", "custom"):
                try:
                    key_status[prov] = bool(read_secret(prov))
                except Exception:
                    key_status[prov] = False
            return {"success": True, "stages": stages,
                    "settings": settings, "key_status": key_status}
        except Exception as e:
            _log_exc("refine_get_stage_settings")
            return {"success": False, "error": str(e)}

    def refine_get_glossary(self, path: str = None) -> dict[str, Any]:
        """读取词库词条列表（含可选别名第三列）。

        行格式 [src, dst, aliases]：aliases 为 `|` 分隔的别名文本，
        无别名时为空字符串 ""（行长度恒为 3，便于前端渲染）。
        """
        try:
            from subtransjav.refine.config import default_glossary_path
            from subtransjav.refine.glossary import load_glossary_ex
            p = path or default_glossary_path()
            p = str(_resolve_safe_path(p))
            rows = load_glossary_ex(p)
            return {"success": True, "path": p,
                    "rows": [[s, d, "|".join(a) if a else ""]
                             for s, d, a in rows]}
        except Exception as e:
            _log_exc("refine_get_glossary")
            return {"success": False, "error": str(e)}

    def refine_save_glossary(self, rows: list[list[str]], path: str = None) -> dict[str, Any]:
        """保存词库词条（保留别名：GUI 前端无别名编辑列，保存前先读
        旧库回填别名第三列，避免两列清洗静默抹掉 target_aliases）。

        返回值含 alias_kept = 实际写出时带别名的词条数。
        """
        try:
            from subtransjav.refine.config import default_glossary_path
            from subtransjav.refine.glossary import load_glossary_ex, save_glossary
            p = path or default_glossary_path()
            p = str(_resolve_safe_path(p))
            old_aliases: dict[str, tuple] = {}
            for src, _dst, aliases in load_glossary_ex(p):
                old_aliases.setdefault(src, aliases)
            clean = []
            alias_kept = 0
            for r in rows or []:
                if len(r) >= 2 and str(r[0]).strip() and str(r[1]).strip():
                    src, dst = str(r[0]).strip(), str(r[1]).strip()
                    aliases = old_aliases.get(src, ())
                    if len(r) >= 3 and str(r[2]).strip():
                        aliases = tuple(a.strip() for a in str(r[2]).split("|")
                                        if a.strip())
                    pair = (src, dst)
                    if pair not in clean:
                        clean.append((src, dst, aliases) if aliases else pair)
                        if aliases:
                            alias_kept += 1
            save_glossary(p, clean)
            return {"success": True, "count": len(clean),
                    "alias_kept": alias_kept, "path": p}
        except Exception as e:
            _log_exc("refine_save_glossary")
            return {"success": False, "error": str(e)}

    def refine_get_template(self, stage_index, templates_dir: str = None) -> dict[str, Any]:
        """读取角色卡原文。stage_index: 'A'|'B'（v2 两阶段）。"""
        try:
            from subtransjav.refine.pipeline_v2 import V2_TEMPLATE_FILES
            tag = str(stage_index).upper()
            if tag not in V2_TEMPLATE_FILES:
                return {"success": False,
                        "error": msg("invalid_stage_tag", tag=stage_index)}
            d = _ensure_template_dir(templates_dir)
            p = os.path.join(d, V2_TEMPLATE_FILES[tag])
            if not os.path.isfile(p):
                return {"success": False,
                        "error": msg("template_file_missing", path=p), "path": p}
            with open(p, encoding="utf-8") as _f:
                text = _f.read()
            return {"success": True, "path": p, "text": text,
                    "note": msg("template_b_note") if tag == "B" else ""}
        except Exception as e:
            _log_exc("refine_get_template")
            return {"success": False, "error": str(e)}

    def read_output_artifact(self, path: str) -> dict[str, Any]:
        """读取输出目录中的质量报告导读 JSON（仅 *_质量报告导读.json 白名单后缀）。"""
        try:
            p = str(path or "").strip()
            if not p:
                return {"success": False, "error": msg("guide_path_empty")}
            try:
                _validate_user_directory(p)
            except ValueError as ve:
                return {"success": False, "error": msg("guide_path_denied", e=ve)}
            if not os.path.isfile(p):
                return {"success": False, "error": msg("guide_file_missing", path=p)}
            suffix = "_质量报告导读.json"
            if not os.path.basename(p).endswith(suffix):
                return {"success": False,
                        "error": msg("guide_suffix_only", suffix=suffix,
                                     name=os.path.basename(p))}
            with open(p, encoding="utf-8") as _f:
                data = json.load(_f)
            if not isinstance(data, dict):
                return {"success": False,
                        "error": msg("guide_bad_format")}
            return {"success": True, "path": p, "data": data}
        except json.JSONDecodeError:
            _log_exc("read_output_artifact")
            return {"success": False,
                    "error": msg("guide_corrupted")}
        except Exception as e:
            _log_exc("read_output_artifact")
            return {"success": False, "error": str(e)}

    def refine_save_template(self, stage_index, text: str,
                             templates_dir: str = None) -> dict[str, Any]:
        """保存角色卡文本（stage_index: 'A'|'B'）"""
        try:
            from subtransjav.refine.pipeline_v2 import V2_TEMPLATE_FILES
            tag = str(stage_index).upper()
            if tag not in V2_TEMPLATE_FILES:
                return {"success": False,
                        "error": msg("invalid_stage_tag", tag=stage_index)}
            d = _ensure_template_dir(templates_dir)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, V2_TEMPLATE_FILES[tag])
            with open(p, "w", encoding="utf-8") as f:
                f.write(text or "")
            return {"success": True, "path": p}
        except Exception as e:
            _log_exc("refine_save_template")
            return {"success": False, "error": str(e)}

    def refine_pick_folder(self) -> dict[str, Any]:
        return self.select_folder()

    def refine_pick_csv_open(self) -> dict[str, Any]:
        """打开词库 CSV/TXT 文件选择对话框"""
        try:
            windows = webview.windows
            if not windows:
                return {"success": False, "error": msg("no_active_window")}
            result = windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=(msg("file_type_glossary"), msg("file_type_all")))
            if result:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": msg("dialog_cancelled")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def refine_pick_csv_save(self) -> dict[str, Any]:
        """词库导出保存对话框"""
        try:
            windows = webview.windows
            if not windows:
                return {"success": False, "error": msg("dialog_cancelled")}
            result = windows[0].create_file_dialog(
                webview.SAVE_DIALOG,
                file_types=(msg("file_type_csv"),),
                save_filename="glossary_export.csv")
            if result:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": msg("dialog_cancelled")}
        except Exception as e:
            _log_exc("tm_pick_db")
            return {"success": False, "error": str(e)}

    # ================================================================
    # 翻译记忆库 (Translation Memory) API
    # ================================================================

    def tm_get_stats(self, db_path: str = None) -> dict[str, Any]:
        """获取翻译记忆库统计信息"""
        try:
            from subtransjav.refine.tm import TranslationMemory
            tm = TranslationMemory(db_path) if db_path else TranslationMemory()
            try:
                stats = tm.stats()
                return {"success": True, **stats}
            finally:
                tm.close()
        except Exception as e:
            _log_exc("tm_get_stats")
            return {"success": False, "error": str(e)}

    def tm_clear(self, stage: int = None, db_path: str = None) -> dict[str, Any]:
        """清空翻译记忆库。

        Args:
            stage: 指定阶段 (0/1/2)，None=清空全部
            db_path: 自定义数据库路径
        """
        try:
            from subtransjav.refine.tm import TranslationMemory
            tm = TranslationMemory(db_path) if db_path else TranslationMemory()
            try:
                tm.clear(stage)
                return {"success": True, "message": msg("tm_cleared")}
            finally:
                tm.close()
        except Exception as e:
            _log_exc("tm_clear")
            return {"success": False, "error": str(e)}

    def tm_export_csv(self, path: str = None,
                      stage: int = None, db_path: str = None) -> dict[str, Any]:
        """导出翻译记忆库为 CSV"""
        try:
            from subtransjav.refine.tm import TranslationMemory
            if not path:
                return {"success": False, "error": msg("tm_export_path_missing")}
            path = str(_resolve_safe_path(path))
            tm = TranslationMemory(db_path) if db_path else TranslationMemory()
            try:
                tm.export_csv(path, stage)
                return {"success": True, "path": path}
            finally:
                tm.close()
        except Exception as e:
            _log_exc("tm_export_csv")
            return {"success": False, "error": str(e)}

    def tm_import_csv(self, path: str = None,
                      db_path: str = None) -> dict[str, Any]:
        """从 CSV 导入翻译记忆库"""
        try:
            from subtransjav.refine.tm import TranslationMemory
            if not path:
                return {"success": False, "error": msg("tm_import_path_missing")}
            path = str(_resolve_safe_path(path))
            tm = TranslationMemory(db_path) if db_path else TranslationMemory()
            try:
                added = tm.import_csv(path)
                return {"success": True, "added": added,
                        "message": msg("tm_imported", n=added)}
            finally:
                tm.close()
        except Exception as e:
            _log_exc("tm_import_csv")
            return {"success": False, "error": str(e)}

    def tm_pick_db(self) -> dict[str, Any]:
        """打开翻译记忆库数据库文件选择对话框"""
        try:
            windows = webview.windows
            if not windows:
                return {"success": False, "error": msg("no_active_window")}
            result = windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=(msg("file_type_sqlite"), msg("file_type_all")))
            if result:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": msg("dialog_cancelled")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # Cleanup
    # ================================================================

    def cleanup_refine_tmp_dirs(self) -> dict[str, Any]:
        """手动清理 refine 临时目录（退出钩子亦调用此逻辑）"""
        import shutil
        cleaned = []
        for d in dict.fromkeys(getattr(self, "_refine_tmp_dirs", [])):
            try:
                if d and os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned.append(d)
            except Exception:
                pass
        if getattr(self, "_refine_tmp_dirs", None):
            self._refine_tmp_dirs.clear()
        return {"success": True, "cleaned": cleaned}

    def _on_exit_cleanup(self):
        """GUI 进程退出：终止残留子进程 + 清理 refine 临时目录"""
        proc = getattr(self, "_translate_process", None)
        # Ignore sentinel (True) — means start_translation never completed
        if proc is True:
            proc = None
        try:
            if proc and proc.poll() is None:
                if PSUTIL_AVAILABLE:
                    terminate_process_tree(proc.pid)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass
        with self._translate_lock:
            self._translate_process = None
        import shutil
        for d in dict.fromkeys(getattr(self, "_refine_tmp_dirs", [])):
            try:
                if d and os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        if getattr(self, "_refine_tmp_dirs", None):
            self._refine_tmp_dirs.clear()
