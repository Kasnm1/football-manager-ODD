using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("FMODD V1.2")]
[assembly: AssemblyProduct("FMODD")]
[assembly: AssemblyCompany("FMODD")]
[assembly: AssemblyDescription("FM26 本地盘口工具兼容版启动程序")]
[assembly: AssemblyVersion("1.2.0.0")]
[assembly: AssemblyFileVersion("1.2.0.0")]

namespace FMODDCompatibleLauncher
{
    internal static class Program
    {
        [STAThread]
        private static int Main()
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string runtimeDirectory = Path.Combine(root, "data", "abc");
            string runtimePath = Path.Combine(runtimeDirectory, "FMODD.Runtime.exe");
            if (!File.Exists(runtimePath))
            {
                MessageBox.Show(
                    "程序运行文件不完整，请完整解压后重新运行。",
                    "FMODD V1.2",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 2;
            }
            try
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName = runtimePath,
                    WorkingDirectory = runtimeDirectory,
                    UseShellExecute = true,
                });
                return 0;
            }
            catch (Exception error)
            {
                MessageBox.Show(
                    "无法启动 FMODD：" + error.Message,
                    "FMODD V1.2",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }
        }
    }
}
