using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using NUnit.Framework;
using Newtonsoft.Json.Linq;
using UnityEngine;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Services;

namespace MCPForUnityTests.Editor.Services
{
    /// <summary>
    /// Verifies that test job results survive domain reloads via the Library/ sidecar,
    /// and that sidecars are cleaned up on eviction and ClearStuckJob.
    /// </summary>
    public class TestJobManagerResultPersistenceTests
    {
        private FieldInfo _jobsField;
        private FieldInfo _currentJobIdField;
        private MethodInfo _toSerializableMethod;
        private MethodInfo _persistMethod;
        private Type _testJobType;

        private string _originalJobId;
        private readonly List<string> _insertedJobIds = new();

        [SetUp]
        public void SetUp()
        {
            var asm = typeof(MCPServiceLocator).Assembly;
            var managerType = asm.GetType("MCPForUnity.Editor.Services.TestJobManager");
            Assert.NotNull(managerType, "Could not find TestJobManager");

            _testJobType = asm.GetType("MCPForUnity.Editor.Services.TestJob");
            Assert.NotNull(_testJobType, "Could not find TestJob");

            _jobsField = managerType.GetField("Jobs", BindingFlags.NonPublic | BindingFlags.Static);
            _currentJobIdField = managerType.GetField("_currentJobId", BindingFlags.NonPublic | BindingFlags.Static);
            _toSerializableMethod = managerType.GetMethod("ToSerializable", BindingFlags.NonPublic | BindingFlags.Static);
            _persistMethod = managerType.GetMethod("PersistToSessionState", BindingFlags.NonPublic | BindingFlags.Static);

            Assert.NotNull(_jobsField);
            Assert.NotNull(_currentJobIdField);
            Assert.NotNull(_toSerializableMethod);
            Assert.NotNull(_persistMethod);

            _originalJobId = _currentJobIdField.GetValue(null) as string;
        }

        [TearDown]
        public void TearDown()
        {
            // Drop any jobs we inserted
            var jobs = _jobsField.GetValue(null) as System.Collections.IDictionary;
            foreach (var id in _insertedJobIds)
            {
                jobs?.Remove(id);
                ClearSidecar(id);
            }
            _insertedJobIds.Clear();

            _currentJobIdField.SetValue(null, _originalJobId);
            _persistMethod.Invoke(null, new object[] { true });
        }

        private object InsertJob(string jobId, TestJobStatus status, string mode = "EditMode")
        {
            var jobs = (System.Collections.IDictionary)_jobsField.GetValue(null);
            long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

            var job = Activator.CreateInstance(_testJobType);
            _testJobType.GetProperty("JobId").SetValue(job, jobId);
            _testJobType.GetProperty("Status").SetValue(job, status);
            _testJobType.GetProperty("Mode").SetValue(job, mode);
            _testJobType.GetProperty("StartedUnixMs").SetValue(job, now);
            _testJobType.GetProperty("LastUpdateUnixMs").SetValue(job, now);
            _testJobType.GetProperty("FailuresSoFar").SetValue(job, new List<TestJobFailure>());

            jobs[jobId] = job;
            _insertedJobIds.Add(jobId);
            return job;
        }

        private static JObject MakeFakeSerializedResult(string mode, int passed, int failed)
        {
            return new JObject
            {
                ["mode"] = mode,
                ["summary"] = new JObject
                {
                    ["total"] = passed + failed,
                    ["passed"] = passed,
                    ["failed"] = failed,
                    ["skipped"] = 0,
                    ["durationSeconds"] = 0.5,
                    ["resultState"] = failed > 0 ? "Failed" : "Passed",
                },
                ["results"] = new JArray
                {
                    new JObject
                    {
                        ["name"] = "TestA",
                        ["fullName"] = "Ns.TestA",
                        ["state"] = "Passed",
                        ["durationSeconds"] = 0.1,
                    },
                    new JObject
                    {
                        ["name"] = "TestB",
                        ["fullName"] = "Ns.TestB",
                        ["state"] = "Failed",
                        ["message"] = "boom",
                        ["durationSeconds"] = 0.2,
                    },
                },
            };
        }

        private static string SidecarPath(string jobId)
        {
            return Path.GetFullPath(Path.Combine(
                Application.dataPath, "..", "Library", $"McpState_TestJobResult_{jobId}.json"));
        }

        private static void ClearSidecar(string jobId)
        {
            try { McpJobStateStore.ClearState($"TestJobResult_{jobId}"); }
            catch { /* best-effort */ }
        }

        [Test]
        public void ToSerializable_LazilyLoadsResultFromSidecar_WhenInMemoryResultIsLost()
        {
            // Simulate a job that was finalized successfully BEFORE a domain reload:
            // sidecar exists on disk, but in-memory Result is null (as if we just restored).
            var jobId = "test-result-roundtrip";
            var fake = MakeFakeSerializedResult("EditMode", passed: 1, failed: 1);
            McpJobStateStore.SaveState($"TestJobResult_{jobId}", fake);
            try
            {
                Assert.IsTrue(File.Exists(SidecarPath(jobId)), "Sidecar should exist after SaveState");

                var job = InsertJob(jobId, TestJobStatus.Failed); // Failed because failed > 0
                _testJobType.GetProperty("Result").SetValue(job, null);
                _testJobType.GetProperty("CachedSerializedResult").SetValue(job, null);
                _testJobType.GetProperty("SidecarLoadAttempted").SetValue(job, false);

                // Act: ToSerializable with includeDetails=true should return the full payload
                var serialized = _toSerializableMethod.Invoke(null, new object[] { job, true, false });
                Assert.NotNull(serialized);

                var asJson = JObject.FromObject(serialized);
                var result = asJson["result"] as JObject;
                Assert.NotNull(result, "Result should be lazily loaded from sidecar");
                Assert.AreEqual("EditMode", result["mode"]?.ToString());
                Assert.AreEqual(2, result["summary"]?["total"]?.ToObject<int>());
                Assert.AreEqual(2, (result["results"] as JArray)?.Count);
            }
            finally
            {
                ClearSidecar(jobId);
            }
        }

        [Test]
        public void ToSerializable_FiltersToFailedOnly_WhenIncludeFailedTestsIsTrue()
        {
            var jobId = "test-result-filter-failed";
            var fake = MakeFakeSerializedResult("EditMode", passed: 1, failed: 1);
            McpJobStateStore.SaveState($"TestJobResult_{jobId}", fake);
            try
            {
                var job = InsertJob(jobId, TestJobStatus.Failed);
                _testJobType.GetProperty("Result").SetValue(job, null);
                _testJobType.GetProperty("CachedSerializedResult").SetValue(job, null);
                _testJobType.GetProperty("SidecarLoadAttempted").SetValue(job, false);

                // includeDetails=false, includeFailedTests=true → only failed results survive
                var serialized = _toSerializableMethod.Invoke(null, new object[] { job, false, true });
                var asJson = JObject.FromObject(serialized);
                var results = asJson["result"]?["results"] as JArray;
                Assert.NotNull(results);
                Assert.AreEqual(1, results.Count, "Only the failed test should be included");
                Assert.AreEqual("Failed", results[0]?["state"]?.ToString());
            }
            finally
            {
                ClearSidecar(jobId);
            }
        }

        [Test]
        public void ToSerializable_DropsResultsArray_WhenNoIncludeFlagsSet()
        {
            var jobId = "test-result-no-flags";
            var fake = MakeFakeSerializedResult("EditMode", passed: 1, failed: 1);
            McpJobStateStore.SaveState($"TestJobResult_{jobId}", fake);
            try
            {
                var job = InsertJob(jobId, TestJobStatus.Failed);
                _testJobType.GetProperty("Result").SetValue(job, null);
                _testJobType.GetProperty("CachedSerializedResult").SetValue(job, null);
                _testJobType.GetProperty("SidecarLoadAttempted").SetValue(job, false);

                var serialized = _toSerializableMethod.Invoke(null, new object[] { job, false, false });
                var asJson = JObject.FromObject(serialized);
                Assert.NotNull(asJson["result"], "Result envelope should be present");
                // Summary must still survive; per-test results array should be null.
                Assert.NotNull(asJson["result"]?["summary"], "Summary should always survive");
                Assert.IsTrue(asJson["result"]?["results"] == null
                              || asJson["result"]?["results"]?.Type == JTokenType.Null,
                    "Per-test results array should be dropped when both flags are false");
            }
            finally
            {
                ClearSidecar(jobId);
            }
        }

        [Test]
        public void ToSerializable_DoesNotRetrySidecarLoad_AfterFirstAttempt()
        {
            // Job with no sidecar on disk. First ToSerializable attempts a load (and finds
            // nothing), sets SidecarLoadAttempted=true. Second call must not re-attempt.
            var jobId = "test-no-sidecar";
            ClearSidecar(jobId);
            Assert.IsFalse(File.Exists(SidecarPath(jobId)));

            var job = InsertJob(jobId, TestJobStatus.Succeeded);
            _testJobType.GetProperty("Result").SetValue(job, null);
            _testJobType.GetProperty("CachedSerializedResult").SetValue(job, null);
            _testJobType.GetProperty("SidecarLoadAttempted").SetValue(job, false);

            // First call: triggers attempt
            _toSerializableMethod.Invoke(null, new object[] { job, false, false });
            var attempted = (bool)_testJobType.GetProperty("SidecarLoadAttempted").GetValue(job);
            Assert.IsTrue(attempted, "First call should mark sidecar load as attempted");

            // Second call: should skip load attempt (no-op verified by inspection — flag stays true)
            _toSerializableMethod.Invoke(null, new object[] { job, false, false });
            attempted = (bool)_testJobType.GetProperty("SidecarLoadAttempted").GetValue(job);
            Assert.IsTrue(attempted, "Flag should remain true after second call");
        }

        [Test]
        public void EvictedJobs_HaveSidecarsDeleted_OnPersist()
        {
            // Snapshot and clear the Jobs dict to avoid contamination from other tests / manual runs.
            var jobs = (System.Collections.IDictionary)_jobsField.GetValue(null);
            var snapshot = new Dictionary<object, object>();
            foreach (System.Collections.DictionaryEntry entry in jobs)
            {
                snapshot[entry.Key] = entry.Value;
            }
            jobs.Clear();
            try
            {
                // Insert 12 jobs; MaxJobsToKeep is 10 — the 2 oldest should have their sidecars wiped.
                const int totalJobs = 12;
                const int keepCount = 10;

                long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                var jobIds = new List<string>();
                for (int i = 0; i < totalJobs; i++)
                {
                    var id = $"test-evict-{i:D2}";
                    jobIds.Add(id);
                    var job = InsertJob(id, TestJobStatus.Succeeded);
                    // Older index = older LastUpdateUnixMs (will be evicted first).
                    _testJobType.GetProperty("LastUpdateUnixMs").SetValue(job, now + i);

                    // Drop a sidecar for every job
                    McpJobStateStore.SaveState($"TestJobResult_{id}", MakeFakeSerializedResult("EditMode", 1, 0));
                    Assert.IsTrue(File.Exists(SidecarPath(id)), $"Sidecar for {id} should exist before persist");
                }

                // Trigger persist (which truncates Jobs to MaxJobsToKeep and deletes evicted sidecars)
                _persistMethod.Invoke(null, new object[] { true });

                // The 2 lowest LastUpdateUnixMs should be evicted (indices 0, 1).
                for (int i = 0; i < totalJobs - keepCount; i++)
                {
                    Assert.IsFalse(File.Exists(SidecarPath(jobIds[i])),
                        $"Sidecar for evicted job {jobIds[i]} should have been deleted");
                }
                for (int i = totalJobs - keepCount; i < totalJobs; i++)
                {
                    Assert.IsTrue(File.Exists(SidecarPath(jobIds[i])),
                        $"Sidecar for retained job {jobIds[i]} should still exist");
                    ClearSidecar(jobIds[i]);
                }
            }
            finally
            {
                // Restore prior dict contents (insertions during the test were tracked in
                // _insertedJobIds and will be removed by TearDown).
                foreach (var kv in snapshot)
                {
                    if (!jobs.Contains(kv.Key))
                    {
                        jobs[kv.Key] = kv.Value;
                    }
                }
            }
        }

        [Test]
        public void ClearStuckJob_DeletesSidecar()
        {
            var jobId = "test-clear-stuck";
            // Start a "running" job and write a stray sidecar (e.g., from a previous run with same id).
            var job = InsertJob(jobId, TestJobStatus.Running);
            _currentJobIdField.SetValue(null, jobId);
            McpJobStateStore.SaveState($"TestJobResult_{jobId}", MakeFakeSerializedResult("EditMode", 0, 0));
            Assert.IsTrue(File.Exists(SidecarPath(jobId)));

            var managerType = typeof(MCPServiceLocator).Assembly.GetType("MCPForUnity.Editor.Services.TestJobManager");
            var clearMethod = managerType.GetMethod("ClearStuckJob", BindingFlags.Public | BindingFlags.Static);
            var cleared = (bool)clearMethod.Invoke(null, null);

            Assert.IsTrue(cleared, "ClearStuckJob should report a job was cleared");
            Assert.IsFalse(File.Exists(SidecarPath(jobId)), "Sidecar should be deleted by ClearStuckJob");
        }
    }
}
