using System;
using System.Reflection;
using System.Threading;
using MCPForUnity.Editor.Helpers;
using UnityEditor;

namespace MCPForUnity.Editor.Services
{
    /// <summary>
    /// Drives Unity to complete compile + domain-reload work triggered by MCP tools,
    /// even when the Editor window is backgrounded.
    ///
    /// Without this, <c>CompilationPipeline.RequestScriptCompilation</c> and
    /// <c>AssetDatabase.Refresh</c> only queue work; the actual compile runs on the
    /// Editor's main-thread tick. Unity throttles that tick hard while unfocused
    /// (Preferences -> General -> Interaction Mode / Application Idle Time), so the
    /// queued work sits there until the user alt-tabs back to the Editor.
    ///
    /// Flips Interaction Mode to "No Throttling" + <c>ApplicationIdleTime=0</c> for
    /// the duration of a request, then restores the user's original settings once
    /// the editor reports idle (compile done + import done) or a safety timeout
    /// elapses. Survives domain reloads via <see cref="SessionState"/>.
    ///
    /// Note: <c>TestRunnerNoThrottle</c> manages the same EditorPrefs keys for the
    /// test-run case. <c>RefreshUnity</c> already rejects calls while tests are
    /// running (<c>TestRunStatus.IsRunning</c>), so the two scopes do not overlap
    /// in normal use.
    /// </summary>
    [InitializeOnLoad]
    internal static class EditorReadyPump
    {
        private const string ApplicationIdleTimeKey = "ApplicationIdleTime";
        private const string InteractionModeKey = "InteractionMode";

        private const string SessionKey_RefCount = "EditorReadyPump_RefCount";
        private const string SessionKey_PrevIdleTime = "EditorReadyPump_PrevIdleTime";
        private const string SessionKey_PrevInteractionMode = "EditorReadyPump_PrevInteractionMode";
        private const string SessionKey_SettingsCaptured = "EditorReadyPump_SettingsCaptured";

        private static readonly TimeSpan DefaultMaxWait = TimeSpan.FromSeconds(120);
        private static readonly TimeSpan IdleGrace = TimeSpan.FromSeconds(3);

        static EditorReadyPump()
        {
            // Post-reload recovery. Any active pump's EditorApplication.update
            // callback was destroyed along with the domain; drain the surviving
            // refcount so we don't permanently leak No-Throttling.
            int pending = SessionState.GetInt(SessionKey_RefCount, 0);
            if (pending <= 0) return;

            SessionState.SetInt(SessionKey_RefCount, 0);
            Restore();
        }

        /// <summary>
        /// Ensure Unity ticks at full speed until pending compilation / asset import
        /// finishes, then restore the user's throttling setting. Returns immediately;
        /// the pump runs asynchronously on <see cref="EditorApplication.update"/>.
        /// </summary>
        public static void RequestPump(string reason, TimeSpan? maxWait = null)
        {
            TimeSpan wait = maxWait ?? DefaultMaxWait;
            DateTime deadline = DateTime.UtcNow + wait;
            DateTime graceEnd = DateTime.UtcNow + IdleGrace;

            Acquire(reason);

            int released = 0;
            EditorApplication.CallbackFunction tick = null;
            tick = () =>
            {
                if (Volatile.Read(ref released) != 0) return;

                bool shouldRelease;
                try
                {
                    bool timedOut = DateTime.UtcNow >= deadline;
                    bool idle = !EditorApplication.isCompiling
                                && !EditorApplication.isUpdating
                                && !EditorApplication.isPlayingOrWillChangePlaymode;
                    bool pastGrace = DateTime.UtcNow >= graceEnd;
                    shouldRelease = timedOut || (idle && pastGrace);
                }
                catch
                {
                    shouldRelease = true;
                }

                if (!shouldRelease) return;
                if (Interlocked.Exchange(ref released, 1) != 0) return;

                try { EditorApplication.update -= tick; } catch { }
                try { Release(reason); }
                catch (Exception ex)
                {
                    McpLog.Warn($"[EditorReadyPump] release failed: {ex.Message}");
                }
            };

            EditorApplication.update += tick;
            try { EditorApplication.QueuePlayerLoopUpdate(); } catch { }
        }

        private static void Acquire(string reason)
        {
            int refCount = SessionState.GetInt(SessionKey_RefCount, 0);
            if (refCount == 0)
            {
                CaptureAndApply();
            }
            SessionState.SetInt(SessionKey_RefCount, refCount + 1);
            McpLog.Debug($"[EditorReadyPump] Acquired ({reason}). RefCount={refCount + 1}");
        }

        private static void Release(string reason)
        {
            int refCount = SessionState.GetInt(SessionKey_RefCount, 0);
            if (refCount <= 0)
            {
                McpLog.Debug($"[EditorReadyPump] Release with refCount=0 ({reason}), ignoring.");
                return;
            }

            int newCount = refCount - 1;
            SessionState.SetInt(SessionKey_RefCount, newCount);
            McpLog.Debug($"[EditorReadyPump] Released ({reason}). RefCount={newCount}");

            if (newCount == 0)
            {
                Restore();
            }
        }

        private static void CaptureAndApply()
        {
            if (!SessionState.GetBool(SessionKey_SettingsCaptured, false))
            {
                SessionState.SetInt(SessionKey_PrevIdleTime, EditorPrefs.GetInt(ApplicationIdleTimeKey, 4));
                SessionState.SetInt(SessionKey_PrevInteractionMode, EditorPrefs.GetInt(InteractionModeKey, 0));
                SessionState.SetBool(SessionKey_SettingsCaptured, true);
            }
            EditorPrefs.SetInt(ApplicationIdleTimeKey, 0);
            EditorPrefs.SetInt(InteractionModeKey, 1); // No Throttling
            ForceApplyInteractionSettings();
        }

        private static void Restore()
        {
            if (!SessionState.GetBool(SessionKey_SettingsCaptured, false))
                return;

            EditorPrefs.SetInt(ApplicationIdleTimeKey, SessionState.GetInt(SessionKey_PrevIdleTime, 4));
            EditorPrefs.SetInt(InteractionModeKey, SessionState.GetInt(SessionKey_PrevInteractionMode, 0));
            ForceApplyInteractionSettings();

            SessionState.SetBool(SessionKey_SettingsCaptured, false);
        }

        private static void ForceApplyInteractionSettings()
        {
            try
            {
                typeof(EditorApplication)
                    .GetMethod("UpdateInteractionModeSettings", BindingFlags.Static | BindingFlags.NonPublic)
                    ?.Invoke(null, null);
            }
            catch
            {
                // Reflection may fail on some Unity versions — non-fatal.
            }
        }
    }
}
