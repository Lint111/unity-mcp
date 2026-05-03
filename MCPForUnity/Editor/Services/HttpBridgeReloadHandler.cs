using System;
using System.Threading.Tasks;
using MCPForUnity.Editor.Constants;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services.Transport;
using MCPForUnity.Editor.Windows;
using UnityEditor;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Ensures HTTP transports resume after domain reloads similar to the legacy stdio bridge.
    /// </summary>
    [InitializeOnLoad]
    internal static class HttpBridgeReloadHandler
    {
        private static readonly TimeSpan[] ResumeRetrySchedule =
        {
            TimeSpan.Zero,
            TimeSpan.FromSeconds(1),
            TimeSpan.FromSeconds(3),
            TimeSpan.FromSeconds(5),
            TimeSpan.FromSeconds(10),
            TimeSpan.FromSeconds(30)
        };

        static HttpBridgeReloadHandler()
        {
            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;
            AssemblyReloadEvents.afterAssemblyReload += OnAfterAssemblyReload;
        }

        private static void OnBeforeAssemblyReload()
        {
            try
            {
                var transport = MCPServiceLocator.TransportManager;
                bool shouldResume = transport.IsRunning(TransportMode.Http);

                if (shouldResume)
                {
                    EditorPrefs.SetBool(EditorPrefKeys.ResumeHttpAfterReload, true);
                }
                else
                {
                    EditorPrefs.DeleteKey(EditorPrefKeys.ResumeHttpAfterReload);
                }

                if (shouldResume)
                {
                    // beforeAssemblyReload is synchronous; force a synchronous teardown so we do not
                    // leave an orphaned socket due to an unfinished async close handshake.
                    transport.ForceStop(TransportMode.Http);
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to evaluate HTTP bridge reload state: {ex.Message}");
            }
        }

        private static void OnAfterAssemblyReload()
        {
            bool resume = false;
            try
            {
                // Only resume HTTP if it is still the selected transport.
                bool useHttp = EditorConfigurationCache.Instance.UseHttpTransport;
                resume = useHttp && EditorPrefs.GetBool(EditorPrefKeys.ResumeHttpAfterReload, false);
                if (resume)
                {
                    EditorPrefs.DeleteKey(EditorPrefKeys.ResumeHttpAfterReload);
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"Failed to read HTTP bridge reload flag: {ex.Message}");
                resume = false;
            }

            if (!resume)
            {
                return;
            }

            // Fire the resume immediately on a Task.Delay-driven retry schedule.
            // Do NOT gate this on EditorApplication.delayCall — delayCall waits for
            // a main-thread tick, which Unity throttles hard while unfocused. The
            // retry loop (Task.Delay) runs on a timer thread and keeps making
            // progress regardless of focus state.
            _ = ResumeHttpWithRetriesAsync();
        }

        private static async Task ResumeHttpWithRetriesAsync()
        {
            Exception lastException = null;

            // Keep Unity ticking at full speed for the duration of the retry schedule
            // so the StartAsync work (which does hop to the main thread) completes
            // even if the user has not focused the Editor since the reload.
            EditorReadyPump.Acquire("http-bridge-reload");
            try
            {
                for (int i = 0; i < ResumeRetrySchedule.Length; i++)
                {
                    int attempt = i + 1;
                    McpLog.Debug($"[HTTP Reload] Resume attempt {attempt}/{ResumeRetrySchedule.Length}");

                    TimeSpan delay = ResumeRetrySchedule[i];
                    if (delay > TimeSpan.Zero)
                    {
                        McpLog.Debug($"[HTTP Reload] Waiting {delay.TotalSeconds:0.#}s before resume attempt {attempt}");
                        try { await Task.Delay(delay); }
                        catch { return; }
                    }

                    // Abort retries if the user switched transports while we were waiting.
                    if (!EditorConfigurationCache.Instance.UseHttpTransport)
                    {
                        return;
                    }

                    try
                    {
                        bool started = await MCPServiceLocator.TransportManager.StartAsync(TransportMode.Http);
                        if (started)
                        {
                            McpLog.Debug($"[HTTP Reload] Resume succeeded on attempt {attempt}");
                            MCPForUnityEditorWindow.RequestHealthVerification();
                            return;
                        }

                        var state = MCPServiceLocator.TransportManager.GetState(TransportMode.Http);
                        string reason = string.IsNullOrWhiteSpace(state?.Error) ? "no error detail" : state.Error;
                        McpLog.Debug($"[HTTP Reload] Resume attempt {attempt} failed: {reason}");
                    }
                    catch (Exception ex)
                    {
                        lastException = ex;
                        McpLog.Debug($"[HTTP Reload] Resume attempt {attempt} threw: {ex.Message}");
                    }
                }

                if (lastException != null)
                {
                    McpLog.Warn($"Failed to resume HTTP MCP bridge after domain reload: {lastException.Message}");
                }
                else
                {
                    McpLog.Warn("Failed to resume HTTP MCP bridge after domain reload");
                }
            }
            finally
            {
                EditorReadyPump.Release("http-bridge-reload");
            }
        }
    }
}
