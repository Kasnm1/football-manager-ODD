using System;
using System.Collections.Generic;
using System.Drawing;
using System.Diagnostics;
using System.IO;
using System.Globalization;
using System.Net;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace FMODDDesktop
{
    internal sealed class MainWindow : Form
    {
        private const int DwmUseImmersiveDarkMode = 20;
        private const int DwmBorderColor = 34;
        private const int DwmCaptionColor = 35;
        private const int DwmTextColor = 36;

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int valueSize);

        [DllImport("user32.dll")]
        private static extern bool DestroyIcon(IntPtr iconHandle);

        private readonly WebView2 browser;
        private readonly Panel startupOverlay;
        private readonly string address;
        private readonly string requestedProfilePath;
        private readonly string locale;
        private Icon windowIcon;
        private bool serviceRecoveryInProgress;
        private Rectangle lastNormalBounds;
        private bool restoreMaximized;

        // BEGIN LOCALIZED USER TEXT
        private static readonly Dictionary<string, string> EnglishUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "Initialising..." },
            { "startup.webview_failed", "The embedded browser could not start, so FMODD has safely stopped interface initialisation.\n\nPlease make sure Microsoft Edge WebView2 Runtime is installed, then remove administrator-run settings from Steam, Football Manager, and FMODD before trying again.\nDiagnostic log: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "Select player portrait UID mapping XML" },
            { "dialog.portrait_xml_filter", "XML files (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Select the Football Manager graphics root folder (XML files are found recursively)" },
            { "dialog.data_root", "Select the FMODD data storage folder (applies after restart)" },
        };

        private static readonly Dictionary<string, string> ChineseUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "初始化中..." },
            { "startup.webview_failed", "内嵌浏览器启动失败，FMODD 已安全停止界面初始化。\n\n请确认 Microsoft Edge WebView2 Runtime 已安装，并取消 Steam、Football Manager 与 FMODD 的管理员运行设置后重试。\n详细记录：%LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "选择球员头像 UID 映射 XML" },
            { "dialog.portrait_xml_filter", "XML 文件 (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "选择 Football Manager graphics 根目录（自动递归查找 XML）" },
            { "dialog.data_root", "选择 FMODD 数据保存目录（重启后生效）" },
        };

        private static readonly Dictionary<string, string> TraditionalChineseUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "初始設定中..." },
            { "startup.webview_failed", "內嵌瀏覽器啟動失敗，FMODD 已安全停止介面初始化。\n\n請確認 Microsoft Edge WebView2 Runtime 已安裝，並取消 Steam、Football Manager 與 FMODD 的以系統管理員身分執行設定後再試。\n詳細記錄：%LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "選擇球員頭像 UID 對應 XML" },
            { "dialog.portrait_xml_filter", "XML 檔案 (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "選擇 Football Manager graphics 根目錄（自動遞迴尋找 XML）" },
            { "dialog.data_root", "選擇 FMODD 資料儲存資料夾（重新啟動後生效）" },
        };

        private static readonly Dictionary<string, string> KoreanUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "초기화하는 중..." },
            { "startup.webview_failed", "내장 브라우저를 시작할 수 없어 FMODD가 인터페이스 초기화를 안전하게 중지했습니다.\n\nMicrosoft Edge WebView2 Runtime이 설치되어 있는지 확인한 다음 Steam, Football Manager 및 FMODD의 관리자 실행 설정을 해제하고 다시 시도하세요.\n진단 로그: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "선수 초상화 UID 매핑 XML 선택" },
            { "dialog.portrait_xml_filter", "XML 파일 (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Football Manager graphics 루트 폴더 선택(XML 파일을 재귀적으로 찾음)" },
            { "dialog.data_root", "FMODD 데이터 저장 폴더 선택(재시작 후 적용)" },
        };

        private static readonly Dictionary<string, string> GermanUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "Wird initialisiert..." },
            { "startup.webview_failed", "Der eingebettete Browser konnte nicht gestartet werden. FMODD hat die Initialisierung der Oberfläche deshalb sicher beendet.\n\nStellen Sie sicher, dass Microsoft Edge WebView2 Runtime installiert ist. Deaktivieren Sie anschließend die Ausführung als Administrator für Steam, Football Manager und FMODD und versuchen Sie es erneut.\nDiagnoseprotokoll: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "XML-Datei für die Spielerporträt-UID-Zuordnung auswählen" },
            { "dialog.portrait_xml_filter", "XML-Dateien (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Football-Manager-Grafikstammordner auswählen (XML-Dateien werden rekursiv gesucht)" },
            { "dialog.data_root", "FMODD-Datenspeicherordner auswählen (wird nach dem Neustart angewendet)" },
        };

        private static readonly Dictionary<string, string> SpanishUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "Inicializando..." },
            { "startup.webview_failed", "No se ha podido iniciar el navegador integrado, por lo que FMODD ha detenido de forma segura la inicialización de la interfaz.\n\nComprueba que Microsoft Edge WebView2 Runtime esté instalado. Después, desactiva la ejecución como administrador de Steam, Football Manager y FMODD antes de volver a intentarlo.\nRegistro de diagnóstico: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "Seleccionar XML de asignación de UID de retratos de jugadores" },
            { "dialog.portrait_xml_filter", "Archivos XML (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Seleccionar la carpeta raíz de gráficos de Football Manager (los archivos XML se buscan de forma recursiva)" },
            { "dialog.data_root", "Seleccionar la carpeta de almacenamiento de datos de FMODD (se aplicará tras reiniciar)" },
        };

        private static readonly Dictionary<string, string> FrenchUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "Initialisation..." },
            { "startup.webview_failed", "Le navigateur intégré n’a pas pu démarrer. FMODD a donc interrompu l’initialisation de l’interface en toute sécurité.\n\nVérifiez que Microsoft Edge WebView2 Runtime est installé, puis désactivez l’exécution en tant qu’administrateur pour Steam, Football Manager et FMODD avant de réessayer.\nJournal de diagnostic : %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "Sélectionner le fichier XML de correspondance des UID de portraits de joueurs" },
            { "dialog.portrait_xml_filter", "Fichiers XML (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Sélectionner le dossier racine des graphismes de Football Manager (les fichiers XML sont recherchés récursivement)" },
            { "dialog.data_root", "Sélectionner le dossier de stockage des données FMODD (appliqué après le redémarrage)" },
        };

        private static readonly Dictionary<string, string> RussianUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "Инициализация..." },
            { "startup.webview_failed", "Не удалось запустить встроенный браузер, поэтому FMODD безопасно остановил инициализацию интерфейса.\n\nУбедитесь, что среда выполнения Microsoft Edge WebView2 установлена. Затем отключите запуск от имени администратора для Steam, Football Manager и FMODD и повторите попытку.\nЖурнал диагностики: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "Выберите XML-файл сопоставления UID портретов игроков" },
            { "dialog.portrait_xml_filter", "Файлы XML (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Выберите корневую папку графики Football Manager (поиск XML-файлов выполняется рекурсивно)" },
            { "dialog.data_root", "Выберите папку хранения данных FMODD (применяется после перезапуска)" },
        };

        private static readonly Dictionary<string, string> JapaneseUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "初期化しています..." },
            { "startup.webview_failed", "内蔵ブラウザーを起動できなかったため、FMODDはインターフェイスの初期化を安全に停止しました。\n\nMicrosoft Edge WebView2 Runtimeがインストールされていることを確認してください。その後、Steam、Football Manager、FMODDの管理者として実行する設定を解除して、もう一度お試しください。\n診断ログ：%LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "選手ポートレートUID割り当てXMLを選択" },
            { "dialog.portrait_xml_filter", "XMLファイル (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Football Managerのgraphicsルートフォルダーを選択（XMLファイルは再帰的に検索されます）" },
            { "dialog.data_root", "FMODDデータ保存フォルダーを選択（再起動後に適用）" },
        };

        private static readonly Dictionary<string, string> BrazilianPortugueseUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "Inicializando..." },
            { "startup.webview_failed", "Não foi possível iniciar o navegador integrado; por isso, o FMODD interrompeu com segurança a inicialização da interface.\n\nVerifique se o Microsoft Edge WebView2 Runtime está instalado. Em seguida, desative a opção de executar como administrador no Steam, no Football Manager e no FMODD antes de tentar novamente.\nLog de diagnóstico: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "Selecionar o XML de mapeamento de UID dos retratos dos jogadores" },
            { "dialog.portrait_xml_filter", "Arquivos XML (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Selecionar a pasta raiz de gráficos do Football Manager (os arquivos XML são procurados em todas as subpastas)" },
            { "dialog.data_root", "Selecionar a pasta de armazenamento de dados do FMODD (aplicada após reiniciar)" },
        };

        private static readonly Dictionary<string, string> EuropeanPortugueseUserText = new Dictionary<string, string>
        {
            { "startup.initialising", "A inicializar..." },
            { "startup.webview_failed", "Não foi possível iniciar o navegador incorporado; por isso, o FMODD interrompeu em segurança a inicialização da interface.\n\nConfirme que o Microsoft Edge WebView2 Runtime está instalado. Em seguida, desative a execução como administrador no Steam, Football Manager e FMODD antes de tentar novamente.\nRegisto de diagnóstico: %LOCALAPPDATA%\\FMODD\\webview-startup-error.txt" },
            { "dialog.portrait_xml_title", "Selecionar o XML de correspondência dos UID dos retratos dos jogadores" },
            { "dialog.portrait_xml_filter", "Ficheiros XML (*.xml)|*.xml" },
            { "dialog.portrait_graphics_root", "Selecionar a pasta raiz de gráficos do Football Manager (os ficheiros XML são procurados em todas as subpastas)" },
            { "dialog.data_root", "Selecionar a pasta de armazenamento de dados do FMODD (aplicada após reiniciar)" },
        };
        // END LOCALIZED USER TEXT

        internal MainWindow(string address, string profilePath, string iconPath, string locale)
        {
            this.address = address;
            this.requestedProfilePath = profilePath;
            this.locale = NormalizeLocale(locale);
            Text = "FMODD V2.7.0beta";
            ShowIcon = true;
            Width = 1500;
            Height = 920;
            MinimumSize = new Size(1040, 700);
            StartPosition = FormStartPosition.CenterScreen;
            LoadWindowPlacement();
            BackColor = Color.FromArgb(244, 247, 245);
            if (File.Exists(iconPath))
            {
                try
                {
                    using (Image source = Image.FromFile(iconPath))
                    using (Bitmap bitmap = new Bitmap(source, new Size(64, 64)))
                    {
                        IntPtr handle = bitmap.GetHicon();
                        try
                        {
                            windowIcon = (Icon)Icon.FromHandle(handle).Clone();
                            Icon = windowIcon;
                        }
                        finally { DestroyIcon(handle); }
                    }
                }
                catch { }
            }
            browser = new WebView2();
            browser.Dock = DockStyle.Fill;
            browser.Visible = false;
            startupOverlay = BuildStartupOverlay();
            Controls.Add(browser);
            Controls.Add(startupOverlay);
            startupOverlay.BringToFront();
            Move += OnWindowBoundsChanged;
            Resize += OnWindowBoundsChanged;
            FormClosing += OnFormClosing;
            Shown += OnShown;
        }

        private static string NormalizeLocale(string requestedLocale)
        {
            if (String.Equals(requestedLocale, "zh-CN", StringComparison.OrdinalIgnoreCase)) return "zh-CN";
            if (String.Equals(requestedLocale, "zh-TW", StringComparison.OrdinalIgnoreCase)) return "zh-TW";
            if (String.Equals(requestedLocale, "ko-KR", StringComparison.OrdinalIgnoreCase)) return "ko-KR";
            if (String.Equals(requestedLocale, "de-DE", StringComparison.OrdinalIgnoreCase)) return "de-DE";
            if (String.Equals(requestedLocale, "es-ES", StringComparison.OrdinalIgnoreCase)) return "es-ES";
            if (String.Equals(requestedLocale, "fr-FR", StringComparison.OrdinalIgnoreCase)) return "fr-FR";
            if (String.Equals(requestedLocale, "ru-RU", StringComparison.OrdinalIgnoreCase)) return "ru-RU";
            if (String.Equals(requestedLocale, "ja-JP", StringComparison.OrdinalIgnoreCase)) return "ja-JP";
            if (String.Equals(requestedLocale, "pt-BR", StringComparison.OrdinalIgnoreCase)) return "pt-BR";
            if (String.Equals(requestedLocale, "pt-PT", StringComparison.OrdinalIgnoreCase)) return "pt-PT";
            return "en-GB";
        }

        private string UserText(string key)
        {
            Dictionary<string, string> catalog = EnglishUserText;
            if (locale == "zh-CN") catalog = ChineseUserText;
            else if (locale == "zh-TW") catalog = TraditionalChineseUserText;
            else if (locale == "ko-KR") catalog = KoreanUserText;
            else if (locale == "de-DE") catalog = GermanUserText;
            else if (locale == "es-ES") catalog = SpanishUserText;
            else if (locale == "fr-FR") catalog = FrenchUserText;
            else if (locale == "ru-RU") catalog = RussianUserText;
            else if (locale == "ja-JP") catalog = JapaneseUserText;
            else if (locale == "pt-BR") catalog = BrazilianPortugueseUserText;
            else if (locale == "pt-PT") catalog = EuropeanPortugueseUserText;
            string text;
            if (catalog.TryGetValue(key, out text)) return text;
            return EnglishUserText.TryGetValue(key, out text) ? text : key;
        }

        private static string WindowPlacementPath()
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (String.IsNullOrWhiteSpace(localAppData)) return "";
            return Path.Combine(localAppData, "FMODD", "window-state.txt");
        }

        private void LoadWindowPlacement()
        {
            lastNormalBounds = new Rectangle(0, 0, Width, Height);
            restoreMaximized = false;
            try
            {
                string statePath = WindowPlacementPath();
                if (String.IsNullOrWhiteSpace(statePath) || !File.Exists(statePath)) return;
                string[] values = File.ReadAllLines(statePath);
                int x;
                int y;
                int width;
                int height;
                if (values.Length != 6 || values[0] != "1" ||
                    !Int32.TryParse(values[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out x) ||
                    !Int32.TryParse(values[2], NumberStyles.Integer, CultureInfo.InvariantCulture, out y) ||
                    !Int32.TryParse(values[3], NumberStyles.Integer, CultureInfo.InvariantCulture, out width) ||
                    !Int32.TryParse(values[4], NumberStyles.Integer, CultureInfo.InvariantCulture, out height) ||
                    (values[5] != "0" && values[5] != "1") ||
                    width <= 0 || height <= 0 || width > 100000 || height > 100000 ||
                    Math.Abs((long)x) > 1000000 || Math.Abs((long)y) > 1000000)
                    return;

                Rectangle visibleBounds = FitToVisibleWorkingArea(new Rectangle(x, y, width, height));
                if (visibleBounds.IsEmpty) return;
                StartPosition = FormStartPosition.Manual;
                Bounds = visibleBounds;
                lastNormalBounds = visibleBounds;
                restoreMaximized = values[5] == "1";
                if (restoreMaximized) WindowState = FormWindowState.Maximized;
            }
            catch
            {
                // A missing or damaged optional preference must not block startup.
            }
        }

        private static Rectangle FitToVisibleWorkingArea(Rectangle savedBounds)
        {
            Rectangle bestWorkingArea = Rectangle.Empty;
            long bestIntersectionArea = 0;
            foreach (Screen screen in Screen.AllScreens)
            {
                Rectangle intersection = Rectangle.Intersect(savedBounds, screen.WorkingArea);
                long intersectionArea = (long)Math.Max(0, intersection.Width) * Math.Max(0, intersection.Height);
                if (intersectionArea > bestIntersectionArea)
                {
                    bestIntersectionArea = intersectionArea;
                    bestWorkingArea = screen.WorkingArea;
                }
            }
            if (bestIntersectionArea == 0 || bestWorkingArea.IsEmpty) return Rectangle.Empty;

            int width = Math.Min(Math.Max(savedBounds.Width, 1040), bestWorkingArea.Width);
            int height = Math.Min(Math.Max(savedBounds.Height, 700), bestWorkingArea.Height);
            int x = Math.Max(bestWorkingArea.Left, Math.Min(savedBounds.Left, bestWorkingArea.Right - width));
            int y = Math.Max(bestWorkingArea.Top, Math.Min(savedBounds.Top, bestWorkingArea.Bottom - height));
            return new Rectangle(x, y, width, height);
        }

        private void OnWindowBoundsChanged(object sender, EventArgs eventArgs)
        {
            if (WindowState == FormWindowState.Normal)
            {
                lastNormalBounds = Bounds;
                restoreMaximized = false;
            }
            else if (WindowState == FormWindowState.Maximized)
            {
                restoreMaximized = true;
            }
        }

        private void OnFormClosing(object sender, FormClosingEventArgs eventArgs)
        {
            Rectangle bounds = lastNormalBounds;
            if (WindowState == FormWindowState.Normal) bounds = Bounds;
            if (bounds.Width <= 0 || bounds.Height <= 0) bounds = RestoreBounds;
            try
            {
                string statePath = WindowPlacementPath();
                if (String.IsNullOrWhiteSpace(statePath)) return;
                string directory = Path.GetDirectoryName(statePath);
                Directory.CreateDirectory(directory);
                string temporaryPath = statePath + ".tmp";
                File.WriteAllLines(temporaryPath, new[]
                {
                    "1",
                    bounds.X.ToString(CultureInfo.InvariantCulture),
                    bounds.Y.ToString(CultureInfo.InvariantCulture),
                    bounds.Width.ToString(CultureInfo.InvariantCulture),
                    bounds.Height.ToString(CultureInfo.InvariantCulture),
                    restoreMaximized ? "1" : "0",
                });
                if (File.Exists(statePath))
                    File.Replace(temporaryPath, statePath, null, true);
                else
                    File.Move(temporaryPath, statePath);
            }
            catch
            {
                // Window placement is best-effort and never blocks a normal exit.
            }
        }

        private Panel BuildStartupOverlay()
        {
            Panel overlay = new Panel();
            overlay.Dock = DockStyle.Fill;
            overlay.BackColor = Color.FromArgb(244, 247, 245);

            TableLayoutPanel centering = new TableLayoutPanel();
            centering.Dock = DockStyle.Fill;
            centering.ColumnCount = 1;
            centering.RowCount = 1;
            centering.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            centering.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

            FlowLayoutPanel content = new FlowLayoutPanel();
            content.AutoSize = true;
            content.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            content.FlowDirection = FlowDirection.TopDown;
            content.WrapContents = false;
            content.Anchor = AnchorStyles.None;

            Label title = new Label();
            title.Text = UserText("startup.initialising");
            title.TextAlign = ContentAlignment.MiddleCenter;
            title.Font = new Font("Microsoft YaHei UI", 14, FontStyle.Bold);
            title.ForeColor = Color.FromArgb(32, 53, 45);
            title.AutoSize = false;
            title.Size = new Size(220, 32);
            title.Margin = new Padding(0, 0, 0, 12);

            ProgressBar progress = new ProgressBar();
            progress.Style = ProgressBarStyle.Marquee;
            progress.MarqueeAnimationSpeed = 28;
            progress.Size = new Size(220, 4);
            progress.Margin = new Padding(0);

            content.Controls.Add(title);
            content.Controls.Add(progress);
            centering.Controls.Add(content, 0, 0);
            overlay.Controls.Add(centering);
            return overlay;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && windowIcon != null)
            {
                windowIcon.Dispose();
                windowIcon = null;
            }
            base.Dispose(disposing);
        }

        protected override void OnHandleCreated(EventArgs eventArgs)
        {
            base.OnHandleCreated(eventArgs);
            ApplyTitleBarColors();
        }

        private void ApplyTitleBarColors()
        {
            try
            {
                int enabled = 1;
                int green = ColorTranslator.ToWin32(Color.FromArgb(18, 61, 50));
                int white = ColorTranslator.ToWin32(Color.White);
                DwmSetWindowAttribute(Handle, DwmUseImmersiveDarkMode, ref enabled, sizeof(int));
                DwmSetWindowAttribute(Handle, DwmBorderColor, ref green, sizeof(int));
                DwmSetWindowAttribute(Handle, DwmCaptionColor, ref green, sizeof(int));
                DwmSetWindowAttribute(Handle, DwmTextColor, ref white, sizeof(int));
            }
            catch
            {
                // Older Windows builds keep their native title-bar colors.
            }
        }

        private async void OnShown(object sender, EventArgs eventArgs)
        {
            try
            {
                string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                if (String.IsNullOrWhiteSpace(localAppData))
                    throw new InvalidOperationException("Unable to determine the desktop user's LocalAppData directory");
                // Resolve the UDF inside the WebView host so it always belongs
                // to the same user token that launches the Edge subprocesses.
                string profilePath = Path.Combine(localAppData, "FMODD", "WebView2", "Default");
                Directory.CreateDirectory(profilePath);
                string writeProbe = Path.Combine(profilePath, ".fmodd-write-test");
                File.WriteAllText(writeProbe, "ok");
                File.Delete(writeProbe);
                CoreWebView2EnvironmentOptions options = new CoreWebView2EnvironmentOptions("--disable-background-networking --disable-component-update --no-proxy-server");
                CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(null, profilePath, options);
                await browser.EnsureCoreWebView2Async(environment);
                browser.CoreWebView2.Settings.AreDevToolsEnabled = false;
                browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                browser.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;
                browser.CoreWebView2.Settings.IsStatusBarEnabled = false;
                browser.CoreWebView2.WebMessageReceived += OnWebMessageReceived;
                browser.CoreWebView2.NavigationCompleted += OnNavigationCompleted;
                browser.CoreWebView2.NewWindowRequested += delegate(object source, CoreWebView2NewWindowRequestedEventArgs args)
                {
                    Uri externalTarget;
                    if (Uri.TryCreate(args.Uri, UriKind.Absolute, out externalTarget)
                        && externalTarget.Scheme == Uri.UriSchemeHttps
                        && (String.Equals(externalTarget.Host, "fmodd.com", StringComparison.OrdinalIgnoreCase)
                            || String.Equals(externalTarget.Host, "ko-fi.com", StringComparison.OrdinalIgnoreCase)))
                    {
                        try { Process.Start(args.Uri); } catch { }
                    }
                    args.Handled = true;
                };
                browser.CoreWebView2.NavigationStarting += delegate(object source, CoreWebView2NavigationStartingEventArgs args)
                {
                    Uri target;
                    if (args.Uri == "about:blank") return;
                    if (!Uri.TryCreate(args.Uri, UriKind.Absolute, out target) || target.Host != "127.0.0.1") args.Cancel = true;
                };
                await RecoverServiceNavigationAsync();
            }
            catch (Exception error)
            {
                try
                {
                    string logDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FMODD");
                    Directory.CreateDirectory(logDirectory);
                    string diagnostics =
                        DateTime.Now.ToString("O") + Environment.NewLine +
                        "RequestedProfile=" + requestedProfilePath + Environment.NewLine +
                        "HostLocalAppData=" + Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData) + Environment.NewLine +
                        error.ToString();
                    File.WriteAllText(Path.Combine(logDirectory, "webview-startup-error.txt"), diagnostics);
                }
                catch { }
                MessageBox.Show(
                    UserText("startup.webview_failed"),
                    "FMODD", MessageBoxButtons.OK, MessageBoxIcon.Error
                );
                Close();
            }
        }

        private bool LocalServiceIsReady()
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(address.TrimEnd('/') + "/api/health");
            request.Method = "GET";
            request.Proxy = null;
            request.Timeout = 1000;
            request.ReadWriteTimeout = 1000;
            request.KeepAlive = false;
            try
            {
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                    return response.StatusCode == HttpStatusCode.OK;
            }
            catch (WebException)
            {
                return false;
            }
        }

        private async Task RecoverServiceNavigationAsync()
        {
            if (serviceRecoveryInProgress || IsDisposed) return;
            serviceRecoveryInProgress = true;
            browser.Visible = false;
            startupOverlay.Visible = true;
            startupOverlay.BringToFront();
            try
            {
                while (!IsDisposed)
                {
                    bool ready = await Task.Run((Func<bool>)LocalServiceIsReady);
                    if (ready)
                    {
                        browser.CoreWebView2.Navigate(address);
                        return;
                    }
                    await Task.Delay(500);
                }
            }
            finally
            {
                serviceRecoveryInProgress = false;
            }
        }

        private async void OnNavigationCompleted(object sender, CoreWebView2NavigationCompletedEventArgs args)
        {
            if (IsDisposed) return;
            Uri target = browser.Source;
            if (target == null || target.Host != "127.0.0.1") return;
            if (args.IsSuccess)
            {
                browser.Visible = true;
                startupOverlay.Visible = false;
                return;
            }
            if (serviceRecoveryInProgress) return;
            await RecoverServiceNavigationAsync();
        }

        private void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs args)
        {
            string message;
            try { message = args.TryGetWebMessageAsString(); }
            catch { return; }
            if (message == "choose-portrait-xml")
            {
                string selectedXml = "";
                using (OpenFileDialog dialog = new OpenFileDialog())
                {
                    dialog.Title = UserText("dialog.portrait_xml_title");
                    dialog.Filter = UserText("dialog.portrait_xml_filter");
                    dialog.CheckFileExists = true;
                    dialog.Multiselect = false;
                    if (dialog.ShowDialog(this) == DialogResult.OK)
                        selectedXml = dialog.FileName ?? "";
                }
                try { browser.CoreWebView2.PostWebMessageAsString("portrait-xml-path::" + selectedXml); }
                catch { }
                return;
            }
            if (message == "choose-portrait-graphics-root")
            {
                string selectedGraphics = "";
                using (FolderBrowserDialog dialog = new FolderBrowserDialog())
                {
                    dialog.Description = UserText("dialog.portrait_graphics_root");
                    dialog.ShowNewFolderButton = false;
                    if (dialog.ShowDialog(this) == DialogResult.OK)
                        selectedGraphics = dialog.SelectedPath ?? "";
                }
                try { browser.CoreWebView2.PostWebMessageAsString("portrait-graphics-root::" + selectedGraphics); }
                catch { }
                return;
            }
            if (message != "choose-data-root") return;
            string selectedPath = "";
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
                dialog.Description = UserText("dialog.data_root");
                dialog.ShowNewFolderButton = true;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    selectedPath = dialog.SelectedPath ?? "";
            }
            try { browser.CoreWebView2.PostWebMessageAsString(selectedPath); }
            catch { }
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length < 3) return 2;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainWindow(args[0], args[1], args[2], args.Length > 3 ? args[3] : "en-GB"));
            return 0;
        }
    }
}
