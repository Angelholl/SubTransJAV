"""
SubTransJAV PyWebView API

Backend API for the standalone SRT translation GUI.
Maintains the thin wrapper pattern - delegates work to the
``subtransjav.refine.cli`` subprocess and streams its output.
"""

import json
import logging
import os
import sys
import queue
import threading
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any

import webview
from webview import FileDialog

from subtransjav.utils.process_manager import (
    terminate_process_tree,
    PSUTIL_AVAILABLE,
)

# Project root (subtransjav/webview_gui/api.py -> project root)
REPO_ROOT = Path(__file__).resolve().parents[2]


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
        return "该模型对中国大陆区域封锁(403)，请换其他模型"
    if "429" in s or "RateLimit" in s or "FreeUsageLimit" in s:
        return "免费额度限速(429)，稍等几分钟再试或换模型"
    if "unavailable" in s or "Upstream" in s:
        return "上游服务临时宕机，稍后重试或换模型"
    if "api_key" in s.lower() or "401" in s:
        return "密钥无效或未配置"
    return "请检查密钥/网络"


def _build_refine_args(options: Dict[str, Any]) -> List[str]:
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
      lmstudio_endpoint / zen_endpoint / custom_endpoint: str
      deepseek_key / zen_key / custom_key: str
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

    # 批间并发数（1-5，缺省2；CLI 端 __post_init__ 会再钳制一次）
    try:
        n_conc = int(options.get("v2_concurrency") or 2)
    except (TypeError, ValueError):
        n_conc = 2
    args.extend(["--v2-concurrency", str(max(1, min(5, n_conc)))])

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

    return args


class TranslateAPI:
    """
    API class exposed to JavaScript via PyWebView.

    All public methods are callable from JavaScript via:
        pywebview.api.method_name(args)
    """

    def __init__(self):
        """Initialize API state."""
        self.process: Optional[subprocess.Popen] = None

        # 退出时清理 refine 临时目录并终止残留子进程
        import atexit
        atexit.register(self._on_exit_cleanup)
        self._refine_tmp_dirs: List[str] = []

        # Lock for _translate_process access (GUI thread vs reader thread)
        self._translate_lock = threading.Lock()

        # Default output directory (ensure it exists and is normalized)
        self.default_output = str(_compute_default_output_dir())

    # ========================================================================
    # Version / misc
    # ========================================================================

    def get_version(self) -> Dict[str, Any]:
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
                "message": "Could not load version information"
            }

    def get_system_status(self) -> Dict[str, Any]:
        """Get system status including optional features like grammar hints."""
        status = {
            "success": True,
            "features": {}
        }
        
        # Check SudachiPy availability for grammar hints
        try:
            from subtransjav.refine.grammar_hint import is_grammar_hint_available
            status["features"]["grammar_hints"] = {
                "available": is_grammar_hint_available(),
                "description": "日语形态素分析提示（阶段A 自动启用）"
            }
        except ImportError:
            status["features"]["grammar_hints"] = {
                "available": False,
                "description": "日语形态素分析提示（未安装 sudachipy）"
            }
        
        return status

    def open_url(self, url: str) -> Dict[str, Any]:
        """Open a URL in the system browser."""
        try:
            if not is_safe_url_scheme(url):
                return {"success": False, "error": "仅支持 http/https 链接"}
            import webbrowser
            webbrowser.open(url)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ========================================================================
    # File dialogs
    # ========================================================================

    def select_folder(self) -> Dict[str, Any]:
        """Open native folder dialog to select a folder."""
        windows = webview.windows
        if not windows:
            return {"success": False, "message": "No active window"}

        result = windows[0].create_file_dialog(FileDialog.FOLDER)
        if result and len(result) > 0:
            return {"success": True, "path": result[0]}
        return {"success": False, "message": "No folder selected"}

    def select_output_directory(self) -> Dict[str, Any]:
        """Open native folder dialog to select output directory."""
        return self.select_folder()

    def select_srt_files(self) -> Dict[str, Any]:
        """Open file dialog to select SRT files for translation."""
        windows = webview.windows
        if not windows:
            return {"success": False, "message": "No active window"}

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
            return {"success": True, "paths": list(result)}
        return {"success": False, "message": "No files selected"}

    def select_srt_folder(self) -> Dict[str, Any]:
        """Open folder dialog and find .srt files in the selected folder."""
        windows = webview.windows
        if not windows:
            return {"success": False, "message": "No active window"}

        result = windows[0].create_file_dialog(FileDialog.FOLDER)
        if result and len(result) > 0:
            folder = Path(result[0])
            srt_files = sorted(str(f) for f in folder.glob("*.srt"))
            if srt_files:
                return {"success": True, "paths": srt_files, "folder": result[0]}
            return {"success": False, "message": "No .srt files found in selected folder"}
        return {"success": False, "message": "No folder selected"}

    def scan_srt_folder(self, folder: str, recursive: bool = True,
                        pattern: str = "*.srt", min_size: int = 0,
                        max_size: int = 0, min_date: str = "",
                        max_date: str = "",
                        exclude: Optional[List[str]] = None) -> Dict[str, Any]:
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
            return {
                "success": True,
                "paths": files,
                "summary": summary,
            }
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_output_folder(self, path: str, create: bool = True) -> Dict[str, Any]:
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
                        "message": f"目录不存在: {folder}"}

            if sys.platform.startswith("win"):
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)])
            else:
                subprocess.run(["xdg-open", str(folder)])

            return {"success": True, "message": "Folder opened"}
        except Exception as e:
            return {"success": False, "message": f"Cannot open folder: {e}"}

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
            self._translate_process: Optional[subprocess.Popen] = None
            self._translate_status = "idle"
            self._translate_error: Optional[str] = None
            self._translate_log_queue: queue.Queue = queue.Queue()
            self._translate_thread: Optional[threading.Thread] = None
            self._translate_files_total = 0
            self._translate_files_completed = 0
            self._translate_current_file = None
            self._translate_lines_total = 0
            self._translate_lines_done = 0
            self._translate_current_stage = ""

    def start_translation(self, options: Dict[str, Any]) -> Dict[str, Any]:
        """
        Start the refine translation subprocess.

        Args:
            options: Options collected by buildRefineOptionsV2() in app.js.
        """
        self._init_translation_state()

        with self._translate_lock:
            if self._translate_process is not None:
                return {"success": False, "error": "Translation already in progress"}
            # Sentinel: mark "starting" to block double-start while Popen runs
            self._translate_process = True

        self._translate_files_total = 0
        self._translate_files_completed = 0
        self._translate_current_file = None
        self._translate_lines_total = 0
        self._translate_lines_done = 0
        self._translate_current_stage = ""

        try:
            args = _build_refine_args(options)

            # 记录 refine 临时目录，供程序退出时清理
            try:
                from subtransjav.refine.orchestrator import (
                    refine_tmp_dir, strip_lang_suffix)
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

            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
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

            self._translate_thread = threading.Thread(
                target=self._stream_translation_output,
                daemon=True
            )
            self._translate_thread.start()

            return {
                "success": True,
                "message": f"Translation started with {len(options.get('inputs', []))} file(s)",
                "pid": proc.pid
            }

        except Exception as e:
            _log_exc("start_translation")
            with self._translate_lock:
                proc = self._translate_process
                self._translate_process = None
            if proc is not None and proc is not True and hasattr(proc, 'kill'):
                try:
                    proc.kill()
                except Exception:
                    pass
            self._translate_status = "error"
            return {"success": False, "error": str(e)}

    def _stream_translation_output(self):
        """Background thread to stream translation output and parse progress."""
        import re
        with self._translate_lock:
            proc = self._translate_process
        # Guard against sentinel (True) — shouldn't happen but be safe
        if proc is True or not hasattr(proc, 'stdout'):
            proc = None
        try:
            if proc and proc.stdout:
                for line in proc.stdout:
                    self._translate_log_queue.put(line)
                    # refine 引擎总行数："Translating 1875 lines in 2 scenes"
                    mt = re.search(r'Translating (\d+) lines', line)
                    if mt:
                        self._translate_lines_total = int(mt.group(1))
                        self._translate_lines_done = 0
                        self._translate_files_total = self._translate_lines_total
                    # v2 管线总数："[llm] 共 1875 条，分 63 批（并发=1）"
                    mv = re.search(r'共 (\d+) 条，分 (\d+) 批', line)
                    if mv:
                        self._translate_lines_total = int(mv.group(1))
                        self._translate_lines_done = 0
                        self._translate_files_total = self._translate_lines_total
                    # v2 批次进度："⏳ 批次 3/63"
                    mb2 = re.search(r'批次 (\d+)/(\d+)', line)
                    if mb2 and self._translate_lines_total:
                        batch_no, batch_total = int(mb2.group(1)), int(mb2.group(2))
                        per_batch = self._translate_lines_total / max(1, batch_total)
                        self._translate_lines_done = min(
                            int(per_batch * batch_no), self._translate_lines_total)
                        _stage_label = self._translate_current_stage or "处理中"
                        self._translate_current_file = (
                            f"已翻译约 {self._translate_lines_done}"
                            f"/{self._translate_lines_total} 行（{_stage_label}）")
                    # refine 批次进度："Scene 1 batch 3: 17 lines and 0 untranslated."
                    mb = re.search(
                        r'Scene (\d+) batch (\d+): (\d+) lines and (\d+) untranslated',
                        line)
                    if mb:
                        done = int(mb.group(3))
                        untrans = int(mb.group(4))
                        if untrans == 0:
                            total = self._translate_lines_total
                            self._translate_lines_done = min(self._translate_lines_done + done, total)
                            _stage_label = self._translate_current_stage or f"场景 {mb.group(1)}"
                            self._translate_current_file = (
                                f"已翻译约 {self._translate_lines_done}"
                                f"/{total or '?'} 行（{_stage_label} 批次 {mb.group(2)}）")
                    # 检测 orchestrator 阶段标记："[STAGE] 阶段1 日译中翻译"
                    ms = re.search(r'\[STAGE\]\s*(.+)', line)
                    if ms:
                        self._translate_current_stage = ms.group(1).strip()
                        self._translate_lines_done = 0
                    # Capture error messages for GUI display
                    if 'TRANSLATION FAILED' in line:
                        self._translate_error = 'Translation failed — no subtitles were translated'
                    elif line.startswith('Failed:') and not self._translate_error:
                        self._translate_error = line.strip()
                    elif 'Batch processing finished with' in line and 'error' in line:
                        self._translate_error = line.strip()
                    elif '[refine] 执行失败' in line and not self._translate_error:
                        self._translate_error = line.strip()
        except Exception as e:
            _log_exc("_stream_translation_output")
            self._translate_log_queue.put(f"\n[ERROR] {e}\n")
        finally:
            if proc:
                proc.wait()

    def cancel_translation(self) -> Dict[str, Any]:
        """Cancel running translation process."""
        self._init_translation_state()

        with self._translate_lock:
            proc = self._translate_process
            if proc is None:
                return {"success": False, "error": "No translation in progress"}
            # Sentinel (True) means start_translation is still launching — cannot cancel yet
            if proc is True:
                return {"success": False, "error": "Translation is still starting, try again"}

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

            self._translate_log_queue.put("\n[CANCELLED] Translation cancelled.\n")
            with self._translate_lock:
                self._translate_process = None

            return {"success": True, "message": "Translation cancelled"}
        except Exception as e:
            _log_exc("cancel_translation")
            return {"success": False, "error": str(e)}

    def get_translation_status(self) -> Dict[str, Any]:
        """Get current translation status."""
        self._init_translation_state()

        with self._translate_lock:
            proc = self._translate_process

        # Guard against sentinel (True) from start_translation
        if proc is True:
            proc = None

        if proc is not None:
            poll = proc.poll()
            if poll is not None:
                exit_code = poll
                with self._translate_lock:
                    if self._translate_process is proc:
                        self._translate_process = None

                if self._translate_status != "cancelled":
                    if exit_code == 0:
                        self._translate_status = "completed"
                        self._translate_log_queue.put("\n[SUCCESS] Translation completed.\n")
                    else:
                        self._translate_status = "error"
                        if not self._translate_error:
                            self._translate_error = f"Translation process exited with code {exit_code}"
                        self._translate_log_queue.put(f"\n[ERROR] Exit code: {exit_code}\n")

        files_total = getattr(self, '_translate_files_total', 0)
        lines_done = getattr(self, '_translate_lines_done', 0)
        progress = int(100 * lines_done / max(files_total, 1)) if files_total > 0 else 0

        return {
            "status": self._translate_status,
            "progress": progress,
            "current_file": getattr(self, '_translate_current_file', None),
            "files_completed": getattr(self, '_translate_files_completed', 0),
            "files_total": files_total,
            "has_logs": not self._translate_log_queue.empty(),
            "error": self._translate_error,
        }

    def get_translation_logs(self) -> List[str]:
        """Get new translation log lines."""
        self._init_translation_state()

        logs = []
        while not self._translate_log_queue.empty():
            try:
                logs.append(self._translate_log_queue.get_nowait())
            except queue.Empty:
                break
        return logs

    # ================================================================
    # Refine UI 辅助 API（净语翻译两阶段界面）
    # ================================================================
    def refine_default_paths(self) -> Dict[str, Any]:
        """返回词库/角色卡目录的默认路径"""
        try:
            from subtransjav.refine.config import (
                default_glossary_path, default_templates_dir)
            return {
                "success": True,
                "templates_dir": default_templates_dir(),
                "glossary_path": default_glossary_path(),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def refine_list_models(self, provider: str, endpoint: str = None,
                           api_key: str = None) -> Dict[str, Any]:
        """在线拉取服务商可用模型列表（Zen 免费模型置顶）"""
        try:
            from openai import OpenAI
            from subtransjav.refine.config import PROVIDER_ENDPOINT_DEFAULTS
            from subtransjav.refine.secrets import read_secret

            provider = (provider or "").lower()
            if provider == "deepseek":
                base = "https://api.deepseek.com/v1"
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
                return {"success": False, "error": "缺少接口地址(endpoint)"}
            if provider not in ("lmstudio", "ollama") and not key:
                return {"success": False, "error": "缺少 API Key（请先在密钥区保存）"}

            client = OpenAI(base_url=base, api_key=key or "none", timeout=30)
            models = sorted(m.id for m in client.models.list())
            if provider == "zen":
                models.sort(key=lambda x: (not x.endswith("-free"), x))
            return {"success": True, "models": models}
        except Exception as e:
            _log_exc("refine_list_models")
            return {"success": False, "error": f"{type(e).__name__}: {e}",
                    "tip": _refine_error_tip(e)}

    def list_local_models(self, endpoint: str = None) -> Dict[str, Any]:
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
                          endpoint: str = None, api_key: str = None) -> Dict[str, Any]:
        """单阶段连通性测试：极小请求验证服务商+模型可用性"""
        try:
            from openai import OpenAI
            from subtransjav.refine.secrets import read_secret

            provider = (provider or "").lower()
            model = (model or "").strip()
            if not model:
                return {"success": False, "error": "未填写模型名"}
            if provider == "deepseek":
                base = "https://api.deepseek.com/v1"
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
                return {"success": False, "error": "缺少接口地址(endpoint)"}

            client = OpenAI(base_url=base, api_key=key or "none", timeout=60)
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "回复：OK"}],
                max_tokens=512, temperature=0, stream=False)
            msg = r.choices[0].message
            txt = (msg.content or "").strip()[:40]
            if not txt:
                rc = (getattr(msg, "reasoning_content", None) or "").strip()
                txt = ("(推理模型)..." + rc[-28:]) if rc else "(空响应)"
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
            return os.path.join(CONFIG_DIR, "refine_stage_settings.json")
        except Exception:
            return os.path.join(os.getcwd(), "refine_stage_settings.json")

    def refine_save_stage_settings(self, stages: List[Dict[str, Any]] = None,
                                   keys: List[Dict[str, Any]] = None,
                                   settings: Dict[str, Any] = None) -> Dict[str, Any]:
        """保存每阶段设置。
        stages: [{"stage":1, "provider":"zen", "endpoint":"https://..."}, ...]
        keys:   [{"stage":1, "provider":"zen", "key":"sk-..."}, ...]  -> DPAPI 密钥库
        settings: {"v2_concurrency": 2, ...}  -> 写入 JSON 顶层 settings 字典
        """
        saved_eps = 0
        saved_keys = 0
        saved_settings = 0
        try:
            if stages or settings:
                path = self._refine_stage_settings_path()
                data = {"stages": [], "settings": {}}
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

    def refine_get_stage_settings(self) -> Dict[str, Any]:
        """读取已保存的每阶段设置；密钥不回传明文，只返回 has_key 标记"""
        try:
            path = self._refine_stage_settings_path()
            stages = []
            settings = {}
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

    def refine_get_glossary(self, path: str = None) -> Dict[str, Any]:
        """读取词库词条列表"""
        try:
            from subtransjav.refine.config import default_glossary_path
            from subtransjav.refine.glossary import load_glossary
            p = path or default_glossary_path()
            p = str(_resolve_safe_path(p))
            rows = load_glossary(p)
            return {"success": True, "path": p, "rows": [[s, d] for s, d in rows]}
        except Exception as e:
            _log_exc("refine_get_glossary")
            return {"success": False, "error": str(e)}

    def refine_save_glossary(self, rows: List[List[str]], path: str = None) -> Dict[str, Any]:
        """保存词库词条"""
        try:
            from subtransjav.refine.config import default_glossary_path
            from subtransjav.refine.glossary import save_glossary
            p = path or default_glossary_path()
            p = str(_resolve_safe_path(p))
            clean = []
            for r in rows or []:
                if len(r) >= 2 and str(r[0]).strip() and str(r[1]).strip():
                    pair = (str(r[0]).strip(), str(r[1]).strip())
                    if pair not in clean:
                        clean.append(pair)
            save_glossary(p, clean)
            return {"success": True, "count": len(clean), "path": p}
        except Exception as e:
            _log_exc("refine_save_glossary")
            return {"success": False, "error": str(e)}

    def refine_get_template(self, stage_index, templates_dir: str = None) -> Dict[str, Any]:
        """读取角色卡原文。stage_index: 'A'|'B'（v2 两阶段）。"""
        try:
            from subtransjav.refine.pipeline_v2 import V2_TEMPLATE_FILES
            from subtransjav.refine.config import default_templates_dir
            tag = str(stage_index).upper()
            if tag not in V2_TEMPLATE_FILES:
                return {"success": False,
                        "error": f"无效阶段标识：{stage_index}（应为 A 或 B）"}
            d = templates_dir or default_templates_dir()
            d = str(_resolve_safe_path(d))
            p = os.path.join(d, V2_TEMPLATE_FILES[tag])
            if not os.path.isfile(p):
                return {"success": False,
                        "error": f"模板文件不存在：{p}", "path": p}
            with open(p, encoding="utf-8") as _f:
                text = _f.read()
            return {"success": True, "path": p, "text": text,
                    "note": "阶段B(审校抛光)的硬性豁免段由引擎运行时自动追加，无需写在本卡内"
                            if tag == "B" else ""}
        except Exception as e:
            _log_exc("refine_get_template")
            return {"success": False, "error": str(e)}

    def refine_save_template(self, stage_index, text: str,
                             templates_dir: str = None) -> Dict[str, Any]:
        """保存角色卡文本（stage_index: 'A'|'B'）"""
        try:
            from subtransjav.refine.pipeline_v2 import V2_TEMPLATE_FILES
            from subtransjav.refine.config import default_templates_dir
            tag = str(stage_index).upper()
            if tag not in V2_TEMPLATE_FILES:
                return {"success": False,
                        "error": f"无效阶段标识：{stage_index}（应为 A 或 B）"}
            d = templates_dir or default_templates_dir()
            d = str(_resolve_safe_path(d))
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, V2_TEMPLATE_FILES[tag])
            with open(p, "w", encoding="utf-8") as f:
                f.write(text or "")
            return {"success": True, "path": p}
        except Exception as e:
            _log_exc("refine_save_template")
            return {"success": False, "error": str(e)}

    def refine_pick_folder(self) -> Dict[str, Any]:
        return self.select_folder()

    def refine_pick_csv_open(self) -> Dict[str, Any]:
        """打开词库 CSV/TXT 文件选择对话框"""
        try:
            windows = webview.windows
            if not windows:
                return {"success": False, "error": "无活动窗口"}
            result = windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("词库文件 (*.csv;*.txt)", "所有文件 (*.*)"))
            if result:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": "已取消"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def refine_pick_csv_save(self) -> Dict[str, Any]:
        """词库导出保存对话框"""
        try:
            windows = webview.windows
            if not windows:
                return {"success": False, "error": "已取消"}
            result = windows[0].create_file_dialog(
                webview.SAVE_DIALOG,
                file_types=("CSV 文件 (*.csv)",),
                save_filename="glossary_export.csv")
            if result:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": "已取消"}
        except Exception as e:
            _log_exc("tm_pick_db")
            return {"success": False, "error": str(e)}

    # ================================================================
    # 翻译记忆库 (Translation Memory) API
    # ================================================================

    def tm_get_stats(self, db_path: str = None) -> Dict[str, Any]:
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

    def tm_clear(self, stage: int = None, db_path: str = None) -> Dict[str, Any]:
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
                return {"success": True, "message": "翻译记忆库已清空"}
            finally:
                tm.close()
        except Exception as e:
            _log_exc("tm_clear")
            return {"success": False, "error": str(e)}

    def tm_export_csv(self, path: str = None,
                      stage: int = None, db_path: str = None) -> Dict[str, Any]:
        """导出翻译记忆库为 CSV"""
        try:
            from subtransjav.refine.tm import TranslationMemory
            if not path:
                return {"success": False, "error": "未指定导出路径"}
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
                      db_path: str = None) -> Dict[str, Any]:
        """从 CSV 导入翻译记忆库"""
        try:
            from subtransjav.refine.tm import TranslationMemory
            if not path:
                return {"success": False, "error": "未指定导入文件"}
            path = str(_resolve_safe_path(path))
            tm = TranslationMemory(db_path) if db_path else TranslationMemory()
            try:
                added = tm.import_csv(path)
                return {"success": True, "added": added,
                        "message": f"已导入 {added} 条新记录"}
            finally:
                tm.close()
        except Exception as e:
            _log_exc("tm_import_csv")
            return {"success": False, "error": str(e)}

    def tm_pick_db(self) -> Dict[str, Any]:
        """打开翻译记忆库数据库文件选择对话框"""
        try:
            windows = webview.windows
            if not windows:
                return {"success": False, "error": "无活动窗口"}
            result = windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("SQLite 数据库 (*.db)", "所有文件 (*.*)"))
            if result:
                return {"success": True, "path": result[0]}
            return {"success": False, "error": "已取消"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ================================================================
    # Cleanup
    # ================================================================

    def cleanup_refine_tmp_dirs(self) -> Dict[str, Any]:
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
