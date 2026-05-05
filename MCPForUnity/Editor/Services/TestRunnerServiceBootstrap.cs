using System;
using MCPForUnity.Editor.Helpers;
using UnityEditor;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// After a domain reload, if a test job was in flight, eagerly construct the
    /// <see cref="TestRunnerService"/> via <see cref="MCPServiceLocator.Tests"/> so its
    /// <c>TestRunnerApi</c> callbacks are registered before Unity has a chance to deliver
    /// <c>RunFinished</c>. Without this, the lazily-recreated service may miss the
    /// callback and the job sticks in "running" until the 60-second stale cutoff in
    /// <see cref="TestJobManager.TryRestoreFromSessionState"/> reaps it as orphaned.
    /// </summary>
    [InitializeOnLoad]
    internal static class TestRunnerServiceBootstrap
    {
        static TestRunnerServiceBootstrap()
        {
            try
            {
                // Touching HasRunningJob runs TestJobManager's static ctor, which restores
                // _currentJobId from SessionState. If a job is still pending, we force the
                // service into existence so its callbacks are wired up.
                if (TestJobManager.HasRunningJob)
                {
                    var _ = MCPServiceLocator.Tests;
                }
            }
            catch (Exception ex)
            {
                McpLog.Warn($"[TestRunnerServiceBootstrap] Eager init failed: {ex.Message}");
            }
        }
    }
}
