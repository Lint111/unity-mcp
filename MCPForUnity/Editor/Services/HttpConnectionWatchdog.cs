using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services.Transport;
using MCPForUnity.Editor.Windows;
using UnityEditor;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Background watchdog that detects when the MCP server is running but Unity's
    /// WebSocket connection has been lost (e.g. after a domain reload while Unity is
    /// unfocused, or a transient network blip). Probes the server <c>/health</c>
    /// endpoint on a <see cref="Task.Delay"/> loop and triggers reconnection.
    ///
    /// Uses <see cref="Task.Delay"/> rather than <see cref="EditorApplication.update"/>
    /// so it keeps running when the Editor is backgrounded — Unity throttles its main
    /// thread while unfocused, but .NET timer threads keep ticking.
    ///
    /// Reconnect attempts call <see cref="EditorReadyPump.Acquire"/> so the actual
    /// <c>StartAsync</c> work (which does run on the main thread) is not stalled by
    /// the same throttling.
    /// </summary>
    [InitializeOnLoad]
    internal static class HttpConnectionWatchdog
    {
        private static readonly TimeSpan CheckInterval = TimeSpan.FromSeconds(15);
        private static readonly TimeSpan InitialDelay = TimeSpan.FromSeconds(45);
        private static readonly TimeSpan HealthTimeout = TimeSpan.FromSeconds(3);

        private static CancellationTokenSource _cts;
        private static readonly HttpClient _httpClient = new HttpClient { Timeout = HealthTimeout };

        static HttpConnectionWatchdog()
        {
            _cts?.Cancel();
            _cts = new CancellationTokenSource();

            if (EditorConfigurationCache.Instance.UseHttpTransport)
            {
                _ = WatchdogLoopAsync(_cts.Token);
            }

            AssemblyReloadEvents.beforeAssemblyReload += OnBeforeAssemblyReload;
        }

        private static void OnBeforeAssemblyReload()
        {
            _cts?.Cancel();
        }

        private static async Task WatchdogLoopAsync(CancellationToken token)
        {
            // Give HttpBridgeReloadHandler's retry schedule (up to ~50s) a chance to
            // reconnect first — only step in if it gave up.
            try { await Task.Delay(InitialDelay, token); }
            catch (OperationCanceledException) { return; }

            while (!token.IsCancellationRequested)
            {
                try
                {
                    await CheckAndReconnectAsync(token);
                }
                catch (OperationCanceledException) { return; }
                catch (Exception ex)
                {
                    McpLog.Debug($"[Watchdog] Check failed: {ex.Message}");
                }

                try { await Task.Delay(CheckInterval, token); }
                catch (OperationCanceledException) { return; }
            }
        }

        private static async Task CheckAndReconnectAsync(CancellationToken token)
        {
            if (!EditorConfigurationCache.Instance.UseHttpTransport)
                return;

            var transport = MCPServiceLocator.TransportManager;
            if (transport.IsRunning(TransportMode.Http))
                return;

            string healthUrl = HttpEndpointUtility.GetBaseUrl().TrimEnd('/') + "/health";
            try
            {
                using var response = await _httpClient.GetAsync(healthUrl, token).ConfigureAwait(false);
                if (!response.IsSuccessStatusCode) return;
            }
            catch
            {
                return;
            }

            McpLog.Debug("[Watchdog] Server alive but disconnected. Attempting reconnection...");
            EditorReadyPump.Acquire("watchdog-reconnect");

            try
            {
                bool started = await transport.StartAsync(TransportMode.Http);
                if (started)
                {
                    McpLog.Debug("[Watchdog] Reconnection succeeded.");
                    MCPForUnityEditorWindow.RequestHealthVerification();
                }
                else
                {
                    var state = transport.GetState(TransportMode.Http);
                    McpLog.Debug($"[Watchdog] Reconnection failed: {state?.Error ?? "unknown"}");
                }
            }
            catch (Exception ex)
            {
                McpLog.Debug($"[Watchdog] Reconnection threw: {ex.Message}");
            }
            finally
            {
                EditorReadyPump.Release("watchdog-reconnect");
            }
        }
    }
}
