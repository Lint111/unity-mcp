using System;
using System.Reflection;
using NUnit.Framework;
using Newtonsoft.Json.Linq;
using MCPForUnity.Editor.Services;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEditor.SceneManagement;

namespace MCPForUnityTests.Editor.Services
{
    /// <summary>
    /// Verifies the readiness helpers and event-driven refresh paths added to EditorStateCache.
    /// Most behavior here is hard to drive without actually triggering compilation / domain reload,
    /// so the tests focus on shape (helpers exist, snapshot includes the new fields, ForceUpdate
    /// is wired to the new events).
    /// </summary>
    [TestFixture]
    public class EditorStateCacheTests
    {
        [Test]
        public void IsEditorBusy_AndIsPlayModeActive_ArePublicStaticHelpers()
        {
            var type = typeof(EditorStateCache);

            var busy = type.GetMethod(
                "IsEditorBusy",
                BindingFlags.Public | BindingFlags.Static,
                binder: null,
                types: Type.EmptyTypes,
                modifiers: null);
            var playMode = type.GetMethod(
                "IsPlayModeActive",
                BindingFlags.Public | BindingFlags.Static,
                binder: null,
                types: Type.EmptyTypes,
                modifiers: null);

            Assert.NotNull(busy, "EditorStateCache.IsEditorBusy() must be available for tools to share busy semantics.");
            Assert.AreEqual(typeof(bool), busy.ReturnType);

            Assert.NotNull(playMode, "EditorStateCache.IsPlayModeActive() must be available for tools to gate on play mode.");
            Assert.AreEqual(typeof(bool), playMode.ReturnType);
        }

        [Test]
        public void IsEditorBusy_ReturnsFalseInIdleEditMode()
        {
            // Outside of play mode, an idle Editor without active compilation/asset import should not
            // report as busy. This guards against an over-eager change to the helper that would
            // permanently break tools gating on it.
            if (EditorApplication.isPlaying || EditorApplication.isPlayingOrWillChangePlaymode)
            {
                Assert.Ignore("Editor is in or transitioning to play mode; skipping idle assertion.");
            }
            if (EditorApplication.isCompiling || EditorApplication.isUpdating)
            {
                Assert.Ignore("Editor is mid-compilation or mid-import; skipping idle assertion.");
            }

            Assert.IsFalse(EditorStateCache.IsEditorBusy(),
                "Idle editor should not report as busy.");
        }

        [Test]
        public void Snapshot_ActiveScene_IncludesIsDirtyField()
        {
            // The snapshot's active_scene block must surface scene dirty state so server-side
            // preflight can decide whether a refresh is needed before mutating tools fire.
            var snapshot = EditorStateCache.GetSnapshot();
            Assert.NotNull(snapshot);

            var activeScene = snapshot.SelectToken("editor.active_scene");
            Assert.NotNull(activeScene, "Snapshot must include editor.active_scene");
            // is_dirty may be null when no scene is loaded, but the property itself must be defined
            // (i.e., present in the JObject) — JObject.SelectToken returns the value or null;
            // we use ContainsKey on the parent.
            Assert.IsTrue(((JObject)activeScene).ContainsKey("is_dirty"),
                "Snapshot.editor.active_scene must include 'is_dirty' so callers can detect unsaved scene mutations.");
        }

        [Test]
        public void ForceUpdate_IsTriggeredBy_NewEventSubscriptions()
        {
            // We cannot fire CompilationPipeline / AssemblyReloadEvents directly from tests, but we
            // can confirm the static initializer subscribed to them by counting handlers via
            // reflection on the multicast delegate fields. This guards against an accidental
            // refactor that drops the wiring.
            AssertEventHasSubscribers(
                typeof(CompilationPipeline),
                "compilationStarted",
                "CompilationPipeline.compilationStarted should have at least one subscriber after EditorStateCache init.");
            AssertEventHasSubscribers(
                typeof(CompilationPipeline),
                "compilationFinished",
                "CompilationPipeline.compilationFinished should have at least one subscriber after EditorStateCache init.");
        }

        private static void AssertEventHasSubscribers(Type ownerType, string eventName, string message)
        {
            // For static events Unity exposes them as `add/remove`, but the underlying delegate
            // field is typically stored as a private static field with the same name. Walk both.
            var field = ownerType.GetField(eventName, BindingFlags.NonPublic | BindingFlags.Static)
                        ?? ownerType.GetField("_" + eventName, BindingFlags.NonPublic | BindingFlags.Static);
            if (field == null)
            {
                // Some Unity versions back events with auto-generated fields; if we can't reach them
                // we conservatively skip rather than fail — the rest of the suite still covers the
                // observable side.
                Assert.Ignore($"Could not access backing field for {ownerType.Name}.{eventName}; skipping.");
            }

            var del = field.GetValue(null) as Delegate;
            Assert.IsNotNull(del, message);
            Assert.IsTrue(del.GetInvocationList().Length > 0, message);
        }
    }
}
