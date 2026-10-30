"""
Unit tests for the HTCondor executor's metadata file (used by `htcondor snake status`).

These tests aim to verify:
- CLI: --htcondor-metadata is off by default, and accepts "default" or a file path.
- Path resolution: "default" writes to jobdir (with the ClusterId in the name under a
  management job); a file path is used as given, made absolute and its parent created;
  directories are rejected.
- Rollover: an existing metadata file is kept as <name>.old.<N><ext> (next free N)
  instead of being overwritten, and a failed rename stops the run.
- Content: metadata is written with or without a management job, for grouped and
  non-grouped jobs, and local jobs are excluded from executable_nodes.
- Updates: status and exit information change when the job's state changes, and the
  file is only flushed when something changed. Cancelled jobs are marked removed.
- Publishing: the metadata path is set on the management job's ClassAd when there is one.

Current gap: Executor.__post_init__ is not run in these tests, so reading the
management id (ClusterId) from the job ad at $_CONDOR_JOB_AD is not covered, and
conftest.create_mock_metadata_executor mirrors __post_init__'s order
(resolve -> rollover -> initialize -> publish) by hand. These two must be kept in sync.
Metadata update from check_active_jobs are not covered yet.
"""

import json
import os
import time

import pytest
import configargparse
from unittest.mock import Mock, patch

from snakemake_interface_executor_plugins.registry import ExecutorPluginRegistry
from snakemake_interface_common.exceptions import WorkflowError
import snakemake_executor_plugin_htcondor as plugin
from snakemake_executor_plugin_htcondor import (
    Executor,
    METADATA_DEFAULT,
    JobState,
    JobStatus,
)

from conftest import (
    create_mock_metadata_executor,
    mock_htcondor_submission,
)


def parse_executor_settings(cli_args):
    """Parse cli_args with the htcondor plugin's registered CLI options and return its ExecutorSettings."""
    registry = ExecutorPluginRegistry()
    executor_plugin = registry.get_plugin("htcondor")
    parser = configargparse.ArgumentParser()
    executor_plugin.register_cli_args(parser, "executor")
    args = parser.parse_args(cli_args)
    return executor_plugin.get_settings(args)


def make_resolver(tmp_path, mgmt_id=None):
    """_resolve_metadata_file only reads self.jobDir and self._mgmt_id."""
    executor = Mock(spec=Executor)
    executor.jobDir = str(tmp_path / "jobs")
    executor._mgmt_id = mgmt_id
    return Executor._resolve_metadata_file.__get__(executor, Executor)


def make_rollover():
    """_rollover_metadata_file only reads self.logger."""
    executor = Mock(spec=Executor)
    executor.logger = Mock()
    return Executor._rollover_metadata_file.__get__(executor, Executor)


def make_job(name="rule_a", jobid=1, threads=1, resources=None):
    """Plain Mock job suitable for run_job (not the transfer-focused helpers)."""
    job = Mock()
    job.name = name
    job.jobid = jobid
    job.threads = threads
    job.resources = resources or {}
    job.is_group = Mock(return_value=False)
    return job


def make_group_job(individual_jobs, name="test_group_job", jobid=100):
    """Plain Mock group job; job.jobs holds the individual node jobs."""
    job = Mock()
    job.name = name
    job.jobid = jobid
    job.threads = 1
    job.resources = {}
    job.is_group = Mock(return_value=True)
    job.jobs = individual_jobs
    return job


class TestIndividualComponents:
    """Tests for the CLI layer and path resolution"""

    ########## Testing the CLI layer ################
    def test_cli_metadata_absent(self):
        """Test when no --htcondor-metadata is specified in the arguments"""
        settings = parse_executor_settings([])
        assert settings.metadata is None

    def test_cli_metadata_no_arg(self):
        """Test when --htcondor-metadata is specified but no default or path option is given"""
        with pytest.raises(SystemExit) as excinfo:
            parse_executor_settings(["--htcondor-metadata"])

        assert excinfo.value.code == 2

    def test_cli_metadata_default(self):
        """Test when --htcondor-metadata is specified with default option"""
        settings = parse_executor_settings(["--htcondor-metadata", METADATA_DEFAULT])

        assert settings.metadata == METADATA_DEFAULT

    def test_cli_metadata_path(self):
        """Testing when --htcondor-metadata is specified with a path"""
        new_path = "/newDir/metadata.json"
        settings = parse_executor_settings(["--htcondor-metadata", new_path])

        assert settings.metadata == new_path

    ########## Testing the Path Resolution ##########
    def test_path_resolution_metadata_disable(self, tmp_path):
        """Test when --htcondor-metadata is not specified"""

        resolve = make_resolver(tmp_path, mgmt_id=None)
        assert resolve(None) is None

    def test_path_resolution_metadata_enable_no_path_and_mgmt_id(self, tmp_path):
        """Test when --htcondor-metadata is specified with default option
        under a management job: file name carries the ClusterId"""
        resolve = make_resolver(tmp_path, mgmt_id=123)
        assert resolve(METADATA_DEFAULT) == str(
            tmp_path / "jobs" / "snakemake-metadata-123.json"
        )

    def test_path_resolution_metadata_enable_no_path(self, tmp_path):
        """Test when --htcondor-metadata is specified with default option
        outside a management job: plain file name"""
        resolve = make_resolver(tmp_path, mgmt_id=None)
        assert resolve(METADATA_DEFAULT) == str(
            tmp_path / "jobs" / "snakemake-metadata.json"
        )

    def test_path_resolution_metadata_enable_path_and_mgmt_id(self, tmp_path):
        """Test when --htcondor-metadata is specified with a path under a
        management job: a user-supplied file path is used as-is"""
        resolve = make_resolver(tmp_path, mgmt_id=123)
        path = str(tmp_path / "m.json")
        assert resolve(path) == path

    def test_path_resolution_metadata_enable_path_with_parent(self, tmp_path):
        """Test when --htcondor-metadata is specified with a path whose parent
        directory does not exist yet: the parent is created"""
        resolve = make_resolver(tmp_path, mgmt_id=None)
        parent = tmp_path / "new" / "dir"
        path = str(parent / "m.json")

        assert not parent.exists()
        assert resolve(path) == path
        assert parent.is_dir()

    def test_path_resolution_metadata_rejects_dir_trailing_sep(self, tmp_path):
        """A path ending in a separator is a directory and this not accepted."""
        resolve = make_resolver(tmp_path, mgmt_id=123)
        out_dir = tmp_path / "out"

        with pytest.raises(WorkflowError):
            resolve(str(out_dir) + os.sep)
        assert not out_dir.exists()

    def test_path_resolution_metadata_rejects_existing_dir(self, tmp_path):
        """An existing directory without a trailing separator is still rejected"""
        resolve = make_resolver(tmp_path, mgmt_id=None)
        with pytest.raises(WorkflowError):
            resolve(str(tmp_path))

    def test_path_resolution_metadata_enable_relative_path(self, tmp_path, monkeypatch):
        """Test when --htcondor-metadata is a relative path: it is resolved
        against the current working directory"""
        monkeypatch.chdir(tmp_path)
        resolve = make_resolver(tmp_path, mgmt_id=None)
        assert resolve("rel.json") == str(tmp_path / "rel.json")

    ########## Test CLI to resolver ##########
    def test_cli_to_resolver(self, tmp_path):
        """Test end-to-end behavior from cli parsing to path resolution"""
        settings = parse_executor_settings(["--htcondor-metadata", "default"])

        resolve = make_resolver(tmp_path, mgmt_id=123)

        assert resolve(settings.metadata) == str(
            tmp_path / "jobs" / "snakemake-metadata-123.json"
        )

    ######## Test file rollover #############
    def test_no_existing_file(self, tmp_path):
        """Metadata with the name is created for the first time, so no rollover is required"""
        rollover = make_rollover()
        path = tmp_path / "snakemake-metadata.json"

        rollover(str(path))

        # No entry
        assert list(tmp_path.iterdir()) == []

    def test_existing_file_rolls_to_old_1(self, tmp_path):
        """The new metadata has the same path as the old one (for e.g. in default mode).
        File rollover is required"""
        rollover = make_rollover()
        path = tmp_path / "snakemake-metadata.json"
        path.write_text('{"run": 1}')  # create actual file

        rollover(str(path))

        assert not path.exists()  # Path is now free
        assert (tmp_path / "snakemake-metadata.old.1.json").read_text() == '{"run": 1}'

    def test_pick_next_free_number(self, tmp_path):
        """Earlier rollovers are left alone; the next free number is used"""
        rollover = make_rollover()
        path = tmp_path / "snakemake-metadata.json"
        old_1 = tmp_path / "snakemake-metadata.old.1.json"
        old_1.write_text('{"run": 1}')
        path.write_text('{"run": 2}')

        rollover(str(path))

        assert old_1.read_text() == '{"run": 1}'
        assert (tmp_path / "snakemake-metadata.old.2.json").read_text() == '{"run": 2}'
        assert not path.exists()

    def test_file_without_extension(self, tmp_path):
        """A file with no extension gets .old.<N> appended"""
        rollover = make_rollover()
        path = tmp_path / "meta"
        path.write_text("x")

        rollover(str(path))

        assert not path.exists()
        assert (tmp_path / "meta.old.1").read_text() == "x"

    def test_rename_failure_raises(self, tmp_path):
        """If the old file can't be renamed, stop rather than overwrite it"""
        rollover = make_rollover()
        path = tmp_path / "snakemake-metadata.json"
        path.write_text('{"run": 1}')

        with patch("os.rename", side_effect=OSError("read-only")):
            with pytest.raises(WorkflowError):
                rollover(str(path))

        assert path.read_text() == '{"run": 1}'

    def test_new_run_keeps_previous_record(self, tmp_path):
        """Two runs with the default path: the first run's file survives as .old.1"""
        first = create_mock_metadata_executor(tmp_path, mgmt_id=None)
        first_started = first._metadata["started_at"]

        second = create_mock_metadata_executor(tmp_path, mgmt_id=None)

        old_1 = tmp_path / "jobs" / "snakemake-metadata.old.1.json"
        assert json.loads(old_1.read_text())["started_at"] == first_started
        with open(second._metadata_file) as f:
            assert json.load(f)["started_at"] == second._metadata["started_at"]


class TestMetadata:
    """Verify different cases as stated in the intro docstrings"""

    @pytest.fixture
    def mock_executor(self, tmp_path):
        return create_mock_metadata_executor(tmp_path, mgmt_id=999)

    def test_metadata_with_no_mgmt_id(self, tmp_path):
        """When there is no management id, metadata should not contain management information"""
        executor = create_mock_metadata_executor(tmp_path, mgmt_id=None)
        job = make_job(name="rule_c", jobid=7)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            executor.run_job(job)

        job_key = "12345:7"
        meta_level = executor._metadata
        assert os.path.basename(executor._metadata_file) == "snakemake-metadata.json"
        assert "mgmt_job_id" not in meta_level
        assert "mgmt_job_name" not in meta_level

        job_info = meta_level["jobs"][job_key]
        assert job_info["cluster_id"] == 12345
        assert job_info["rule_name"] == "rule_c"
        assert job_info["jobid"] == 7
        assert job_info["display_name"] == "rule_c-7"
        assert job_info["grouped"] is False
        assert job_info["status"] == JobStatus.IDLE.value
        assert job_info["exit_code"] is None
        assert job_info["exit_signal"] is None
        assert "submitted_at" in job_info
        assert "last_updated" in job_info

        # The on-disk file must match what's in memory, proving _write_metadata
        # actually ran (and left no dangling .tmp file behind).
        with open(executor._metadata_file) as f:
            on_disk = json.load(f)
        assert on_disk == executor._metadata
        assert not os.path.exists(f"{executor._metadata_file}.tmp")

    def test_metadata_with_mgmt_id(self, mock_executor):
        """When there is a management id, metadata should contain management information"""
        job = make_job(name="rule_a", jobid=7)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)

        job_key = "12345:7"
        meta_level = mock_executor._metadata
        assert (
            os.path.basename(mock_executor._metadata_file)
            == "snakemake-metadata-999.json"
        )
        assert meta_level["mgmt_job_id"] == 999
        assert meta_level["mgmt_job_name"] == "snakemake-mgmt-999"

        job_info = meta_level["jobs"][job_key]
        assert job_info["cluster_id"] == 12345
        assert job_info["rule_name"] == "rule_a"
        assert job_info["jobid"] == 7
        assert job_info["display_name"] == "rule_a-7"
        assert job_info["grouped"] is False
        assert job_info["status"] == JobStatus.IDLE.value
        assert job_info["exit_code"] is None
        assert job_info["exit_signal"] is None
        assert "submitted_at" in job_info
        assert "last_updated" in job_info

        # The on-disk file must match what's in memory, proving _write_metadata
        # actually ran (and left no dangling .tmp file behind).
        with open(mock_executor._metadata_file) as f:
            on_disk = json.load(f)
        assert on_disk == mock_executor._metadata
        assert not os.path.exists(f"{mock_executor._metadata_file}.tmp")

    def test_all_keys_present_grouped(self, mock_executor):
        """Under normal working condition, all keys and associated values should be correct for grouped jobs"""
        individual_jobs = [
            make_job(name="rule_a", jobid=1),
            make_job(name="rule_b", jobid=2),
        ]
        group_job = make_group_job(individual_jobs, jobid=99)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(group_job)

        # One metadata job_info per node in the group, not one for the whole group.
        assert len(mock_executor._metadata["jobs"]) == len(individual_jobs)
        for j in individual_jobs:
            job_info = mock_executor._metadata["jobs"][f"12345:{j.jobid}"]
            assert job_info["cluster_id"] == 12345
            assert job_info["rule_name"] == j.name
            assert job_info["jobid"] == j.jobid
            assert job_info["display_name"] == f"{j.name}-{j.jobid}"
            assert job_info["grouped"] is True
            assert job_info["status"] == JobStatus.IDLE.value
            assert job_info["exit_code"] is None
            assert job_info["exit_signal"] is None
            assert "submitted_at" in job_info
            assert "last_updated" in job_info

    def test_local_jobs_excluded_from_executable_nodes(self, tmp_path):
        """Local jobs will not be submitted to the EP and thus, should not be included in the `executable_nodes` key"""
        executor = create_mock_metadata_executor(tmp_path, mgmt_id=None)

        local_job = Mock(is_local=True)
        remote_job = Mock(is_local=False)
        executor.workflow.dag = Mock()
        executor.workflow.dag.jobs = [local_job, remote_job]
        executor.workflow.dag.needrun_jobs = Mock(return_value=[local_job, remote_job])

        executor._metadata_file = os.path.join(
            executor.jobDir, "snakemake-metadata-555.json"
        )
        executor._initialize_metadata(555)

        assert executor._metadata["total_nodes"] == 2
        assert executor._metadata["executable_nodes"] == 1

    def test_update_job_status(self, mock_executor):
        """
        When new status differs from the current recorded status, metadata's status
        and timestamp should be updated.

        This should only affect a particular job that is going through the update
        """
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)

        job_key = "12345:1"
        initial_last_updated = mock_executor._metadata["jobs"][job_key]["last_updated"]

        # Same status: no-op, no write flagged, timestamp untouched.
        mock_executor._update_job_status_in_metadata(
            12345, JobState(status=JobStatus.IDLE)
        )
        assert mock_executor._metadata_dirty is False
        assert (
            mock_executor._metadata["jobs"][job_key]["last_updated"]
            == initial_last_updated
        )

        # Status and timestamp update, dirty flag set.
        before = time.time()
        mock_executor._update_job_status_in_metadata(
            12345, JobState(status=JobStatus.RUNNING)
        )
        assert mock_executor._metadata_dirty is True
        assert (
            mock_executor._metadata["jobs"][job_key]["status"]
            == JobStatus.RUNNING.value
        )
        assert mock_executor._metadata["jobs"][job_key]["last_updated"] >= before

        # A status update for an unrelated cluster_id must not touch this job_info.
        mock_executor._metadata_dirty = False
        mock_executor._update_job_status_in_metadata(
            99999, JobState(status=JobStatus.COMPLETED)
        )
        assert mock_executor._metadata_dirty is False
        assert (
            mock_executor._metadata["jobs"][job_key]["status"]
            == JobStatus.RUNNING.value
        )

    def test_update_job_exit_info(self, mock_executor):
        """
        A finished job stays "completed" (as in HTCondor) and records its exit
        code, or the signal that killed it, so readers can tell failure from success.
        """
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)

        job_info = mock_executor._metadata["jobs"]["12345:1"]

        # Non-zero exit
        mock_executor._update_job_status_in_metadata(
            12345, JobState(status=JobStatus.COMPLETED, exit_code=1)
        )
        assert job_info["status"] == JobStatus.COMPLETED.value
        assert job_info["exit_code"] == 1
        assert job_info["exit_signal"] is None

        # Killed by signal: the exit code changes even though the status does not
        mock_executor._metadata_dirty = False
        mock_executor._update_job_status_in_metadata(
            12345,
            JobState(status=JobStatus.COMPLETED, exit_by_signal=True, exit_signal=9),
        )
        assert mock_executor._metadata_dirty is True
        assert job_info["status"] == JobStatus.COMPLETED.value
        assert job_info["exit_code"] is None
        assert job_info["exit_signal"] == 9

    def test_cancel_marks_jobs_removed(self, mock_executor):
        """
        Cancelling marks the cancelled jobs as removed and writes the metadata
        right away, since no polling cycle follows to flush it.
        """
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)

        mock_executor._unified_event_log_reader = None
        mock_executor.cancel_jobs = Executor.cancel_jobs.__get__(
            mock_executor, Executor
        )
        with patch.object(plugin.htcondor, "Schedd", return_value=Mock()):
            mock_executor.cancel_jobs([Mock(external_jobid=12345)])

        with open(mock_executor._metadata_file) as f:
            on_disk = json.load(f)
        assert on_disk["jobs"]["12345:1"]["status"] == JobStatus.REMOVED.value

    def test_cancel_failure_leaves_metadata(self, mock_executor):
        """If the schedd removal fails, the jobs stay in the queue, so metadata is unchanged."""
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)

        schedd = Mock()
        schedd.act.side_effect = RuntimeError("schedd unreachable")
        mock_executor._unified_event_log_reader = None
        mock_executor.cancel_jobs = Executor.cancel_jobs.__get__(
            mock_executor, Executor
        )
        with patch.object(plugin.htcondor, "Schedd", return_value=schedd):
            mock_executor.cancel_jobs([Mock(external_jobid=12345)])

        assert (
            mock_executor._metadata["jobs"]["12345:1"]["status"] == JobStatus.IDLE.value
        )

    def test_flush_only_writes_when_dirty(self, mock_executor):
        """
        _flush_metadata_if_dirty should only persist to disk (and clear the
        dirty flag) when something actually changed since the last flush.
        """
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)

        mock_executor._metadata_dirty = False
        mock_executor._write_metadata = Mock(wraps=mock_executor._write_metadata)

        # Not dirty
        mock_executor._flush_metadata_if_dirty()
        assert mock_executor._write_metadata.call_count == 0

        # Dirty
        mock_executor._update_job_status_in_metadata(
            12345, JobState(status=JobStatus.RUNNING)
        )
        mock_executor._flush_metadata_if_dirty()
        assert mock_executor._write_metadata.call_count == 1
        assert mock_executor._metadata_dirty is False

    def test_publish_metadata_path_with_mgmt_id(self, tmp_path):
        """With a management id, the metadata path is set as HTCondorSnakeMetadata
        on the management job's ClassAd so `htcondor snake status` can find it"""
        schedd = Mock()
        executor = create_mock_metadata_executor(tmp_path, mgmt_id=999, schedd=schedd)

        schedd.edit.assert_called_once_with(
            999, "HTCondorSnakeMetadata", plugin.classad.quote(executor._metadata_file)
        )

    def test_publish_metadata_path_without_mgmt_id(self, tmp_path):
        """Without a management id there is no job to attach the path to, so the schedd is not touched"""
        schedd = Mock()
        create_mock_metadata_executor(tmp_path, mgmt_id=None, schedd=schedd)

        schedd.edit.assert_not_called()

    def test_publish_metadata_path_failure_warns(self, tmp_path):
        """If the schedd edit fails, the executor logs a warning and keeps running"""
        schedd = Mock()
        schedd.edit.side_effect = RuntimeError("schedd unreachable")
        executor = create_mock_metadata_executor(tmp_path, mgmt_id=999, schedd=schedd)

        executor.logger.warning.assert_called_once()

    def test_run_job_survives_metadata_write_failure(self, mock_executor):
        """
        A metadata write failure after submission must not fail run_job: the job
        is already running on HTCondor. The record stays in memory and is marked
        dirty so the next polling cycle retries the write.
        """
        mock_executor._write_metadata = Mock(side_effect=OSError("disk full"))
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(job)  # must not raise

        mock_executor.report_job_submission.assert_called_once()
        warnings = [str(c) for c in mock_executor.logger.warning.call_args_list]
        assert any("Failed to record HTCondor Cluster ID 12345" in w for w in warnings)
        assert mock_executor._metadata_dirty is True
        assert "12345:1" in mock_executor._metadata["jobs"]

    def test_metadata_disabled(self, tmp_path):
        """Without --htcondor-metadata, run_job records nothing and no file is written"""
        executor = create_mock_metadata_executor(tmp_path, mgmt_id=None, metadata=None)
        job = make_job(name="rule_a", jobid=1)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            executor.run_job(job)

        assert executor._metadata_file is None
        assert executor._metadata == {}
        assert [
            f for f in os.listdir(executor.jobDir) if f.startswith("snakemake-metadata")
        ] == []

    def test_update_applies_to_all_grouped_records(self, mock_executor):
        """
        A grouped submission shares one HTCondor cluster, so one status update for
        that cluster must change every node record under it.
        """
        individual_jobs = [
            make_job(name="rule_a", jobid=1),
            make_job(name="rule_b", jobid=2),
        ]
        group_job = make_group_job(individual_jobs, jobid=99)

        captured_submit_dict = {}
        with mock_htcondor_submission(captured_submit_dict):
            mock_executor.run_job(group_job)

        mock_executor._metadata_dirty = False
        mock_executor._update_job_status_in_metadata(
            12345, JobState(status=JobStatus.RUNNING)
        )

        assert mock_executor._metadata_dirty is True
        for j in individual_jobs:
            assert (
                mock_executor._metadata["jobs"][f"12345:{j.jobid}"]["status"]
                == JobStatus.RUNNING.value
            )

    def test_retry_replaces_previous_attempt(self, mock_executor):
        """
        A retried node is resubmitted under a new cluster id; only the latest
        attempt's record should remain so status doesn't count the failed one.
        """
        job = make_job(name="rule_a", jobid=7)

        mock_executor._add_job_to_metadata(100, job)
        mock_executor._update_job_status_in_metadata(
            100, JobState(status=JobStatus.COMPLETED, exit_code=1)
        )
        mock_executor._add_job_to_metadata(205, job)

        assert list(mock_executor._metadata["jobs"]) == ["205:7"]
        assert (
            mock_executor._metadata["jobs"]["205:7"]["status"] == JobStatus.IDLE.value
        )
        assert "100:7" not in mock_executor._metadata["jobs"]

        with open(mock_executor._metadata_file) as f:
            assert list(json.load(f)["jobs"]) == ["205:7"]
