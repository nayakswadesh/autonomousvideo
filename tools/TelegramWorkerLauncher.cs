using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Reflection;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

internal static class TelegramWorkerLauncher
{
    private const string MutexName = "Local\\AutonomousStudioTelegramWorkerLauncher";

    private sealed class WorkerProcess
    {
        public int ProcessId;
        public int ParentProcessId;
    }

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        bool ownsMutex = false;
        using (var mutex = new Mutex(true, MutexName, out ownsMutex))
        {
            if (!ownsMutex)
            {
                ShowInfo("The Telegram worker launcher is already active. No duplicate was started.");
                return;
            }

            try
            {
                Run();
            }
            finally
            {
                mutex.ReleaseMutex();
            }
        }
    }

    private static void Run()
    {
        string repository = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string studioExecutable = Path.Combine(repository, ".venv", "Scripts", "studio.exe");
        string pythonExecutable = Path.Combine(repository, ".venv", "Scripts", "python.exe");

        if (!File.Exists(studioExecutable))
        {
            ShowError(
                "The studio executable was not found. Keep this launcher in the Autonomous Video " +
                "project folder and ensure .venv\\Scripts\\studio.exe exists."
            );
            return;
        }

        List<WorkerProcess> workers;
        try
        {
            workers = FindWorkerProcesses(studioExecutable, pythonExecutable);
        }
        catch (Exception exception)
        {
            ShowError(
                "Windows would not allow the launcher to inspect running processes, so it did not " +
                "start another worker.\n\n" + exception.Message
            );
            return;
        }

        List<WorkerProcess> roots = FindWorkerRoots(workers);
        if (roots.Count == 1)
        {
            ShowInfo(
                "The Telegram worker is already running (PID " + roots[0].ProcessId + "). " +
                "No duplicate was started."
            );
            return;
        }

        if (roots.Count > 1)
        {
            var processIds = new List<string>();
            foreach (WorkerProcess root in roots)
            {
                processIds.Add(root.ProcessId.ToString());
            }

            ShowError(
                "Multiple Telegram worker chains are already running (PIDs " +
                string.Join(", ", processIds.ToArray()) + "). No additional worker was started."
            );
            return;
        }

        string logDirectory = Path.Combine(repository, "projects", "runtime");
        Directory.CreateDirectory(logDirectory);
        string stdoutPath = Path.Combine(logDirectory, "telegram-narration-review-worker.stdout.log");
        string stderrPath = Path.Combine(logDirectory, "telegram-narration-review-worker.stderr.log");

        var startInfo = new ProcessStartInfo
        {
            FileName = studioExecutable,
            Arguments = "--root projects/live worker",
            WorkingDirectory = repository,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };

        try
        {
            using (var stdout = CreateLogWriter(stdoutPath))
            using (var stderr = CreateLogWriter(stderrPath))
            using (var process = new Process())
            {
                object stdoutLock = new object();
                object stderrLock = new object();
                process.StartInfo = startInfo;
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
                {
                    if (eventArgs.Data == null)
                    {
                        return;
                    }

                    lock (stdoutLock)
                    {
                        stdout.WriteLine(eventArgs.Data);
                    }
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
                {
                    if (eventArgs.Data == null)
                    {
                        return;
                    }

                    lock (stderrLock)
                    {
                        stderr.WriteLine(eventArgs.Data);
                    }
                };

                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();

                ShowInfo(
                    "Telegram worker started successfully (PID " + process.Id + ").\n\n" +
                    "You can close this message; the worker will continue in the background. " +
                    "Double-clicking this launcher again will not create a duplicate."
                );

                process.WaitForExit();
                int exitCode = process.ExitCode;
                process.WaitForExit();

                if (exitCode != 0)
                {
                    ShowError(
                        "The Telegram worker stopped with exit code " + exitCode + ".\n\n" +
                        "Review the logs in projects\\runtime, then open this launcher to retry."
                    );
                }
            }
        }
        catch (Exception exception)
        {
            ShowError("The Telegram worker could not be started.\n\n" + exception.Message);
        }
    }

    private static StreamWriter CreateLogWriter(string path)
    {
        var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
        var writer = new StreamWriter(stream);
        writer.AutoFlush = true;
        writer.WriteLine(
            "[" + DateTimeOffset.Now.ToString("O") + "] Worker launched by Telegram Worker Launcher."
        );
        return writer;
    }

    private static List<WorkerProcess> FindWorkerProcesses(
        string studioExecutable,
        string pythonExecutable
    )
    {
        string normalizedStudio = Path.GetFullPath(studioExecutable);
        string normalizedPython = Path.GetFullPath(pythonExecutable);
        var commandPattern = new Regex(
            "--root\\s+[\\\"']?projects[\\\\/]live[\\\"']?\\s+worker",
            RegexOptions.IgnoreCase
        );
        var results = new List<WorkerProcess>();

        const string query =
            "SELECT ProcessId, ParentProcessId, ExecutablePath, CommandLine " +
            "FROM Win32_Process " +
            "WHERE Name = 'studio.exe' OR Name = 'python.exe' OR Name = 'pythonw.exe'";

        using (var searcher = new ManagementObjectSearcher(query))
        using (ManagementObjectCollection processes = searcher.Get())
        {
            foreach (ManagementObject candidate in processes)
            {
                string executablePath = Convert.ToString(candidate["ExecutablePath"]);
                string commandLine = Convert.ToString(candidate["CommandLine"]);
                if (string.IsNullOrWhiteSpace(executablePath) ||
                    string.IsNullOrWhiteSpace(commandLine))
                {
                    continue;
                }

                string normalizedCandidate;
                try
                {
                    normalizedCandidate = Path.GetFullPath(executablePath);
                }
                catch
                {
                    continue;
                }

                bool isProjectExecutable =
                    string.Equals(normalizedCandidate, normalizedStudio, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(normalizedCandidate, normalizedPython, StringComparison.OrdinalIgnoreCase);

                if (!isProjectExecutable || !commandPattern.IsMatch(commandLine))
                {
                    continue;
                }

                results.Add(new WorkerProcess
                {
                    ProcessId = Convert.ToInt32((uint)candidate["ProcessId"]),
                    ParentProcessId = Convert.ToInt32((uint)candidate["ParentProcessId"])
                });
            }
        }

        return results;
    }

    private static List<WorkerProcess> FindWorkerRoots(List<WorkerProcess> workers)
    {
        var workerIds = new HashSet<int>();
        foreach (WorkerProcess worker in workers)
        {
            workerIds.Add(worker.ProcessId);
        }

        var roots = new List<WorkerProcess>();
        foreach (WorkerProcess worker in workers)
        {
            if (!workerIds.Contains(worker.ParentProcessId))
            {
                roots.Add(worker);
            }
        }

        return roots;
    }

    private static void ShowInfo(string message)
    {
        MessageBox.Show(
            message,
            "Autonomous Studio — Telegram Worker",
            MessageBoxButtons.OK,
            MessageBoxIcon.Information
        );
    }

    private static void ShowError(string message)
    {
        MessageBox.Show(
            message,
            "Autonomous Studio — Telegram Worker",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error
        );
    }
}
