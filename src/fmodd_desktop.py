from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from time import sleep

from fm_odds_web import Handler, LocalOddsState, start_local_runtime
from tools.app_settings import load_settings
from tools.app_paths import ASSET_ROOT, FROZEN, ensure_data_directories
from tools.storage_retention import enforce_storage_budget


PORT = 7856
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
_SINGLE_INSTANCE_MUTEX = "Local\\FMODD-Desktop"
DEFAULT_DESKTOP_LOCALE = "en-GB"
SUPPORTED_DESKTOP_LOCALES = (
    "en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP",
    "pt-BR", "pt-PT",
)


DESKTOP_MESSAGE_CATALOGS = {
    "en-GB": {
        "startup.already_running": "FMODD is already running. Do not start it again.",
        "startup.integrity_failed": "Program file integrity verification failed. Please download FMODD again.\n\n{details}",
        "startup.host_missing": "The desktop window component is missing. Please download FMODD again.",
        "startup.service_start_failed": "Unable to start the local service: {error}",
        "startup.host_start_failed": "Unable to start the desktop window: {error}",
    },
    "zh-CN": {
        "startup.already_running": "FMODD 已在运行，请勿重复启动。",
        "startup.integrity_failed": "程序文件完整性校验失败，请重新下载 FMODD。\n\n{details}",
        "startup.host_missing": "桌面窗口组件缺失，请重新下载 FMODD。",
        "startup.service_start_failed": "无法启动本地服务：{error}",
        "startup.host_start_failed": "桌面窗口启动失败：{error}",
    },
    "zh-TW": {
        "startup.already_running": "FMODD 已在執行，請勿重複啟動。",
        "startup.integrity_failed": "程式檔案完整性驗證失敗，請重新下載 FMODD。\n\n{details}",
        "startup.host_missing": "桌面視窗元件缺失，請重新下載 FMODD。",
        "startup.service_start_failed": "無法啟動本機服務：{error}",
        "startup.host_start_failed": "桌面視窗啟動失敗：{error}",
    },
    "ko-KR": {
        "startup.already_running": "FMODD가 이미 실행 중입니다. 다시 시작하지 마세요.",
        "startup.integrity_failed": "프로그램 파일 무결성 검증에 실패했습니다. FMODD를 다시 다운로드하세요.\n\n{details}",
        "startup.host_missing": "데스크톱 창 구성 요소가 없습니다. FMODD를 다시 다운로드하세요.",
        "startup.service_start_failed": "로컬 서비스를 시작할 수 없습니다: {error}",
        "startup.host_start_failed": "데스크톱 창을 시작할 수 없습니다: {error}",
    },
    "de-DE": {
        "startup.already_running": "FMODD wird bereits ausgeführt. Starten Sie es nicht erneut.",
        "startup.integrity_failed": "Die Integritätsprüfung der Programmdateien ist fehlgeschlagen. Laden Sie FMODD erneut herunter.\n\n{details}",
        "startup.host_missing": "Die Desktopfenster-Komponente fehlt. Laden Sie FMODD erneut herunter.",
        "startup.service_start_failed": "Der lokale Dienst konnte nicht gestartet werden: {error}",
        "startup.host_start_failed": "Das Desktopfenster konnte nicht gestartet werden: {error}",
    },
    "es-ES": {
        "startup.already_running": "FMODD ya está en ejecución. No lo inicies de nuevo.",
        "startup.integrity_failed": "La comprobación de integridad de los archivos del programa ha fallado. Vuelve a descargar FMODD.\n\n{details}",
        "startup.host_missing": "Falta el componente de la ventana de escritorio. Vuelve a descargar FMODD.",
        "startup.service_start_failed": "No se ha podido iniciar el servicio local: {error}",
        "startup.host_start_failed": "No se ha podido iniciar la ventana de escritorio: {error}",
    },
    "fr-FR": {
        "startup.already_running": "FMODD est déjà en cours d’exécution. Ne le lancez pas à nouveau.",
        "startup.integrity_failed": "La vérification de l’intégrité des fichiers du programme a échoué. Téléchargez de nouveau FMODD.\n\n{details}",
        "startup.host_missing": "Le composant de la fenêtre de bureau est manquant. Téléchargez de nouveau FMODD.",
        "startup.service_start_failed": "Impossible de démarrer le service local : {error}",
        "startup.host_start_failed": "Impossible d’ouvrir la fenêtre de l’application : {error}",
    },
    "ru-RU": {
        "startup.already_running": "FMODD уже запущен. Не запускайте его повторно.",
        "startup.integrity_failed": "Проверка целостности файлов программы завершилась ошибкой. Скачайте FMODD повторно.\n\n{details}",
        "startup.host_missing": "Компонент окна рабочего стола отсутствует. Скачайте FMODD повторно.",
        "startup.service_start_failed": "Не удалось запустить локальную службу: {error}",
        "startup.host_start_failed": "Не удалось открыть окно приложения: {error}",
    },
    "ja-JP": {
        "startup.already_running": "FMODDはすでに実行中です。重複して起動しないでください。",
        "startup.integrity_failed": "プログラムファイルの整合性を確認できませんでした。FMODDを再ダウンロードしてください。\n\n{details}",
        "startup.host_missing": "デスクトップウィンドウのコンポーネントがありません。FMODDを再ダウンロードしてください。",
        "startup.service_start_failed": "ローカルサービスを起動できませんでした：{error}",
        "startup.host_start_failed": "デスクトップウィンドウを起動できませんでした：{error}",
    },
    "pt-BR": {
        "startup.already_running": "O FMODD já está em execução. Não o inicie novamente.",
        "startup.integrity_failed": "A verificação de integridade dos arquivos do programa falhou. Baixe o FMODD novamente.\n\n{details}",
        "startup.host_missing": "O componente da janela do aplicativo está ausente. Baixe o FMODD novamente.",
        "startup.service_start_failed": "Não foi possível iniciar o serviço local: {error}",
        "startup.host_start_failed": "Não foi possível abrir a janela do aplicativo: {error}",
    },
    "pt-PT": {
        "startup.already_running": "O FMODD já está em execução. Não o inicie novamente.",
        "startup.integrity_failed": "A verificação da integridade dos ficheiros do programa falhou. Transfira novamente o FMODD.\n\n{details}",
        "startup.host_missing": "O componente da janela da aplicação está em falta. Transfira novamente o FMODD.",
        "startup.service_start_failed": "Não foi possível iniciar o serviço local: {error}",
        "startup.host_start_failed": "Não foi possível abrir a janela da aplicação: {error}",
    },
}


def resolve_desktop_locale(settings: dict[str, object] | None = None) -> str:
    """Use the established settings contract, including its legacy locale default."""
    if settings is None:
        try:
            settings = load_settings()
        except Exception:
            return DEFAULT_DESKTOP_LOCALE
    locale = str(settings.get("ui_locale") or "").strip()
    return locale if locale in SUPPORTED_DESKTOP_LOCALES else DEFAULT_DESKTOP_LOCALE


def desktop_message(locale: str, key: str, **params: object) -> str:
    """Render only catalogued startup text; diagnostic values stay verbatim."""
    catalog = DESKTOP_MESSAGE_CATALOGS.get(locale, DESKTOP_MESSAGE_CATALOGS[DEFAULT_DESKTOP_LOCALE])
    template = catalog.get(key) or DESKTOP_MESSAGE_CATALOGS[DEFAULT_DESKTOP_LOCALE][key]
    return template.format(**params)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD), ("lpReserved", ctypes.wintypes.LPWSTR),
        ("lpDesktop", ctypes.wintypes.LPWSTR), ("lpTitle", ctypes.wintypes.LPWSTR),
        ("dwX", ctypes.wintypes.DWORD), ("dwY", ctypes.wintypes.DWORD),
        ("dwXSize", ctypes.wintypes.DWORD), ("dwYSize", ctypes.wintypes.DWORD),
        ("dwXCountChars", ctypes.wintypes.DWORD), ("dwYCountChars", ctypes.wintypes.DWORD),
        ("dwFillAttribute", ctypes.wintypes.DWORD), ("dwFlags", ctypes.wintypes.DWORD),
        ("wShowWindow", ctypes.wintypes.WORD), ("cbReserved2", ctypes.wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", ctypes.wintypes.HANDLE), ("hStdOutput", ctypes.wintypes.HANDLE),
        ("hStdError", ctypes.wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.wintypes.HANDLE), ("hThread", ctypes.wintypes.HANDLE),
        ("dwProcessId", ctypes.wintypes.DWORD), ("dwThreadId", ctypes.wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_userenv = ctypes.WinDLL("userenv", use_last_error=True)
_kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
_kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
_kernel32.TerminateProcess.restype = ctypes.wintypes.BOOL
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
_user32.GetShellWindow.restype = ctypes.wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
_advapi32.OpenProcessToken.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
_advapi32.DuplicateTokenEx.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.wintypes.HANDLE),
]
_advapi32.DuplicateTokenEx.restype = ctypes.wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
    ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
_advapi32.CreateProcessWithTokenW.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.LPWSTR, ctypes.wintypes.DWORD, ctypes.c_void_p,
    ctypes.wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
]
_advapi32.CreateProcessWithTokenW.restype = ctypes.wintypes.BOOL
_userenv.CreateEnvironmentBlock.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), ctypes.wintypes.HANDLE, ctypes.wintypes.BOOL,
]
_userenv.CreateEnvironmentBlock.restype = ctypes.wintypes.BOOL
_userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
_userenv.DestroyEnvironmentBlock.restype = ctypes.wintypes.BOOL


class NativeProcess:
    """Small Popen-compatible wrapper for a process launched with a user token."""

    def __init__(self, handle: int, pid: int) -> None:
        self.handle = handle
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if _kernel32.WaitForSingleObject(self.handle, 0) == 0x102:
            return None
        code = ctypes.wintypes.DWORD()
        if _kernel32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            self.returncode = int(code.value)
        else:
            self.returncode = 1
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if _kernel32.WaitForSingleObject(self.handle, milliseconds) == 0x102:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return int(self.poll() or 0)

    def terminate(self) -> None:
        if self.poll() is None:
            _kernel32.TerminateProcess(self.handle, 1)

    def kill(self) -> None:
        self.terminate()


def message(message_text: str, error: bool = False) -> None:
    ctypes.windll.user32.MessageBoxW(None, message_text, "FMODD V2.7.0beta", 0x10 if error else 0x40)


def close_controllers(state: LocalOddsState) -> None:
    for controller in (
        state.redbull_hook,
        state.referee_hook,
        state.ca_growth_hook,
        state.attribute_growth_hook,
        state.team_nuclear_hooks,
        state.club_policy_hooks,
        state.simple_effects,
    ):
        try:
            controller.close()
        except Exception:
            pass


def cleanup_stale_profiles() -> None:
    temporary_root = Path(tempfile.gettempdir())
    for path in temporary_root.glob("FMODD-WebView2-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def create_desktop_session_directories() -> tuple[Path, Path]:
    """Create a disposable host directory and a persistent per-user WebView UDF."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("Unable to determine the current user's LocalAppData directory.")

    desktop_root = Path(local_app_data) / "FMODD"
    runtime_root = desktop_root / "Runtime"
    webview_root = desktop_root / "WebView2"
    runtime_root.mkdir(parents=True, exist_ok=True)
    webview_root.mkdir(parents=True, exist_ok=True)

    # Runtime copies are ours and can be replaced. WebView2's UDF must not be
    # deleted while an orphaned browser process may still hold it open.
    for path in runtime_root.glob("Session-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    runtime_path = Path(tempfile.mkdtemp(prefix="Session-", dir=runtime_root))
    webview_path = webview_root / "Default"
    return runtime_path, webview_path


def remove_profile(path: Path) -> None:
    for _attempt in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        sleep(1)


def acquire_single_instance() -> int | None:
    """Keep only one formal desktop instance bound to the local service port."""
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
    if not handle or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        if handle:
            _kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_single_instance(handle: int | None) -> None:
    if handle:
        _kernel32.CloseHandle(handle)


def create_local_server() -> tuple[ThreadingHTTPServer, int]:
    """Prefer the documented port, then let Windows choose an allowed port.

    Some Windows installations reserve otherwise ordinary ports through
    Hyper-V, VPN, security, or endpoint-management policies. Binding port 0
    asks Winsock for an available loopback port and avoids requiring users to
    change those system policies.
    """
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as preferred_error:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as fallback_error:
            raise OSError(
                f"Neither preferred port {PORT} nor a Windows dynamic port could be bound; "
                f"preferred error: {preferred_error}; dynamic port error: {fallback_error}"
            ) from fallback_error
    return server, int(server.server_address[1])


def copy_desktop_host(source: Path, runtime_path: Path) -> Path:
    """Run WebViewHost outside PyInstaller's one-file extraction directory."""
    destination = runtime_path / "host"
    shutil.copytree(source, destination)
    return destination / "FMODD.WebViewHost.exe"


def _protected_release_runtime() -> bool:
    """Detect compiled modules that only exist in the protected distribution."""
    tools_root = ASSET_ROOT / "tools"
    sentinels = (
        "game_layout",
        "native_library_loader",
        "rust_native_core",
    )
    return any(
        any(tools_root.glob(f"{module_name}.*.pyd"))
        for module_name in sentinels
    )


def verify_release_integrity() -> list[str]:
    """Validate protected release files before copying or executing them."""
    if not FROZEN:
        return []
    if getattr(sys, "_fmodd_release_integrity_verified", False):
        return []
    try:
        from tools._release_integrity import verify_packaged_files
    except ImportError:
        if _protected_release_runtime():
            return ["tools/_release_integrity is missing from the protected runtime"]
        return []
    return list(verify_packaged_files(str(ASSET_ROOT)))


def _token_is_elevated(token: int) -> bool:
    elevated = ctypes.wintypes.DWORD()
    size = ctypes.wintypes.DWORD()
    if not _advapi32.GetTokenInformation(
        token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(elevated.value)


def launch_unelevated(executable: Path, arguments: list[str], cwd: Path) -> NativeProcess:
    """Launch the WebView host with the desktop shell's non-elevated token."""
    shell_window = _user32.GetShellWindow()
    shell_pid = ctypes.wintypes.DWORD()
    if not shell_window or not _user32.GetWindowThreadProcessId(shell_window, ctypes.byref(shell_pid)):
        raise RuntimeError("Unable to obtain the Windows desktop user token")

    shell_process = _kernel32.OpenProcess(0x1000, False, shell_pid.value)
    if not shell_process:
        raise ctypes.WinError(ctypes.get_last_error())
    shell_token = ctypes.wintypes.HANDLE()
    primary_token = ctypes.wintypes.HANDLE()
    environment = ctypes.c_void_p()
    try:
        if not _advapi32.OpenProcessToken(
            shell_process, 0x0001 | 0x0002 | 0x0008, ctypes.byref(shell_token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _advapi32.DuplicateTokenEx(
            shell_token, 0x02000000, None, 2, 1, ctypes.byref(primary_token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if _token_is_elevated(primary_token):
            raise RuntimeError("The Windows desktop process is also elevated; the embedded UI cannot be started safely")
        if not _userenv.CreateEnvironmentBlock(
            ctypes.byref(environment), primary_token, False,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        info = PROCESS_INFORMATION()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *arguments]))
        if not _advapi32.CreateProcessWithTokenW(
            primary_token, 1, str(executable), command,
            CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            environment, None, ctypes.byref(startup), ctypes.byref(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        _kernel32.CloseHandle(info.hThread)
        return NativeProcess(int(info.hProcess), int(info.dwProcessId))
    finally:
        if environment:
            _userenv.DestroyEnvironmentBlock(environment)
        if primary_token:
            _kernel32.CloseHandle(primary_token)
        if shell_token:
            _kernel32.CloseHandle(shell_token)
        _kernel32.CloseHandle(shell_process)


def launch_desktop_host(
    executable: Path, arguments: list[str],
) -> subprocess.Popen[bytes] | NativeProcess:
    if ctypes.windll.shell32.IsUserAnAdmin():
        try:
            return launch_unelevated(executable, arguments, executable.parent)
        except Exception as error:
            # Some systems run Explorer itself elevated (for example when UAC
            # is disabled), while others deny CreateProcessWithTokenW. The
            # isolated LocalAppData profile and host-side write probe make a
            # same-token launch safe enough to use as the compatibility path.
            try:
                log_root = Path(os.environ.get("LOCALAPPDATA", "")) / "FMODD"
                log_root.mkdir(parents=True, exist_ok=True)
                (log_root / "desktop-token-fallback.txt").write_text(
                    f"{type(error).__name__}: {error}", encoding="utf-8",
                )
            except OSError:
                pass
            return subprocess.Popen(
                [str(executable), *arguments],
                cwd=str(executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
    return subprocess.Popen(
        [str(executable), *arguments],
        cwd=str(executable.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


def main() -> int:
    locale = resolve_desktop_locale()
    mutex = acquire_single_instance()
    if mutex is None:
        message(desktop_message(locale, "startup.already_running"))
        return 0
    host_dir = ASSET_ROOT / "desktop_host"
    host_path = host_dir / "FMODD.WebViewHost.exe"
    icon_path = ASSET_ROOT / "web" / "assets" / "app-icon.png"
    integrity_errors = verify_release_integrity()
    if integrity_errors:
        message(desktop_message(
            locale, "startup.integrity_failed", details="\n".join(integrity_errors),
        ), True)
        return 1
    if not host_path.is_file():
        message(desktop_message(locale, "startup.host_missing"), True)
        return 1

    ensure_data_directories()
    if FROZEN:
        enforce_storage_budget()
    state = LocalOddsState()
    Handler.state = state
    try:
        server, service_port = create_local_server()
    except OSError as error:
        message(desktop_message(locale, "startup.service_start_failed", error=error), True)
        return 1

    start_local_runtime(state)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    runtime_path: Path | None = None
    profile_path: Path | None = None
    host: subprocess.Popen[bytes] | NativeProcess | None = None
    try:
        cleanup_stale_profiles()
        runtime_path, profile_path = create_desktop_session_directories()
        runtime_host_path = copy_desktop_host(host_dir, runtime_path)
        address = f"http://127.0.0.1:{service_port}"
        host = launch_desktop_host(
            runtime_host_path, [address, str(profile_path), str(icon_path), locale],
        )
        while host.poll() is None:
            sleep(2)
        return int(host.wait(timeout=8) if host.poll() is None else host.returncode or 0)
    except Exception as error:
        message(desktop_message(locale, "startup.host_start_failed", error=error), True)
        return 1
    finally:
        if host is not None and host.poll() is None:
            host.terminate()
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait(timeout=5)
        server.shutdown()
        close_controllers(state)
        server.server_close()
        # Keep the WebView2 UDF. The Runtime can retain browser processes for a
        # short time after the host exits, and deleting a live UDF corrupts the
        # next desktop startup.
        if runtime_path is not None:
            remove_profile(runtime_path)
        release_single_instance(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
