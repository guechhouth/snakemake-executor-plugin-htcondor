"""
Tests for HTCondor submit attribute handling.

Raw attributes are passed via the htcondor_submit_<name> resource prefix:
    htcondor_submit_output_destination -> output_destination (string)
    htcondor_submit_MY__SendCredential -> My.SendCredential (double underscore converts to dot)
    htcondor_submit_nice_user          -> nice_user (boolean)
    htcondor_submit_priority           -> priority (integer)

String values are wrapped in double quotes (for ClassAd string literals).
Non-string values (bool, int) are passed through as-is.

"""

import pytest
from unittest.mock import Mock, patch

import snakemake_executor_plugin_htcondor as plugin
from snakemake_executor_plugin_htcondor import Executor

from conftest import create_mock_executor


class FakeSubmitResult:
    """Minimal fake HTCondor submit result that exposes a cluster id."""

    def cluster(self):
        return 12345


def make_fake_submit(captured_submit_dict):
    """Build a fake htcondor.Submit class that captures the submit dict."""

    class FakeSubmit:
        """Capture submit_dict passed to htcondor.Submit and satisfy run_job."""

        def __init__(self, submit_dict):
            captured_submit_dict.update(submit_dict)

        def issue_credentials(self):
            return None

    return FakeSubmit


class FakeSchedd:
    """Minimal fake HTCondor schedd that returns a submit result."""

    def submit(self, submit_description):
        return FakeSubmitResult()


class TestSubmittingAttributes:
    """Tests for HTCondor raw and ClassAd submit attribute handling."""

    @pytest.fixture
    def mock_executor(self, tmp_path):
        """Create a mock executor with minimal setup"""
        executor = create_mock_executor()
        executor.jobDir = str(tmp_path / "jobs")
        executor.workflow = Mock()
        executor.workflow.storage_settings.shared_fs_usage = False
        executor.workflow.executor_settings = Mock()
        executor.workflow.executor_settings.output_destination = None
        executor.envvars = Mock(return_value={})
        executor.report_job_submission = Mock()
        executor._unified_log_file = str(tmp_path / "snakemake-rules.log")
        # Bind real method under test + helper used by run_job
        executor.run_job = Executor.run_job.__get__(executor, Executor)
        executor._set_resources = Executor._set_resources.__get__(executor, Executor)

        # Stub unrelated heavy pieces
        executor._get_exec_args_and_transfer_files = Mock(
            return_value=("python", "-m snakemake --cores 1", [], [], [])
        )
        executor._handle_explicit_unit_resources = Mock()
        executor._log_resource_requests = Mock()
        return executor

    def test_run_job_applies_htcondor_submit_raw_attributes(self, mock_executor):
        """Test that htcondor_submit_* resources are copied into the submit dict."""
        captured_submit_dict = {}
        patcher_submit = patch.object(
            plugin.htcondor, "Submit", make_fake_submit(captured_submit_dict)
        )
        patcher_schedd = patch.object(plugin.htcondor, "Schedd", FakeSchedd)
        patcher_submit.start()
        patcher_schedd.start()

        try:
            job = Mock()
            job.name = "rule_a"
            job.jobid = 7
            job.threads = 1
            job.resources = {
                "htcondor_submit_output_destination": "/mnt/gdrive/outputs",
                "htcondor_submit_MY__SendCredential": True,
                "htcondor_submit_priority": 5,
                "htcondor_submit_nice_user": True,
            }

            mock_executor.run_job(job)

            assert captured_submit_dict["output_destination"] == '"/mnt/gdrive/outputs"'
            assert captured_submit_dict["MY.SendCredential"] is True
            assert captured_submit_dict["priority"] == 5
            assert captured_submit_dict["nice_user"] is True
            assert captured_submit_dict["batch_name"] == "rule_a-7"
        finally:
            patcher_submit.stop()
            patcher_schedd.stop()

    def test_run_job_applies_classad_attributes(self, mock_executor):
        """Test that classad_* resources are copied into the submit dict."""
        captured_submit_dict = {}

        patcher_submit = patch.object(
            plugin.htcondor, "Submit", make_fake_submit(captured_submit_dict)
        )
        patcher_schedd = patch.object(plugin.htcondor, "Schedd", FakeSchedd)
        patcher_submit.start()
        patcher_schedd.start()

        try:
            job = Mock()
            job.name = "rule_b"
            job.jobid = 8
            job.threads = 1
            job.resources = {
                "classad_MyDataSource": "https://example.com/data",
                "classad_nice_user": True,
                "classad_priority": 5,
            }

            mock_executor.run_job(job)

            assert captured_submit_dict["+MyDataSource"] == '"https://example.com/data"'
            assert captured_submit_dict["+nice_user"] is True
            assert captured_submit_dict["+priority"] == 5
            assert captured_submit_dict["batch_name"] == "rule_b-8"
        finally:
            patcher_submit.stop()
            patcher_schedd.stop()

    def test_run_job_applies_classad_and_htcondor_submit_together(self, mock_executor):
        """Test that classad_* and htcondor_submit_* resources can coexist."""
        captured_submit_dict = {}
        patcher_submit = patch.object(
            plugin.htcondor, "Submit", make_fake_submit(captured_submit_dict)
        )
        patcher_schedd = patch.object(plugin.htcondor, "Schedd", FakeSchedd)
        patcher_submit.start()
        patcher_schedd.start()

        try:
            job = Mock()
            job.name = "rule_c"
            job.jobid = 9
            job.threads = 1
            job.resources = {
                "classad_MyDataSource": "https://example.com/data",
                "classad_priority": 5,
                "htcondor_submit_nice_user": True,
                "htcondor_submit_MY__SendCredential": False,
            }

            mock_executor.run_job(job)

            assert captured_submit_dict["+MyDataSource"] == '"https://example.com/data"'
            assert captured_submit_dict["+priority"] == 5
            assert captured_submit_dict["nice_user"] is True
            assert captured_submit_dict["MY.SendCredential"] is False
            assert captured_submit_dict["batch_name"] == "rule_c-9"
        finally:
            patcher_submit.stop()
            patcher_schedd.stop()

    def test_run_job_passes_through_list_and_dict_values(self, mock_executor):
        """Lists and dicts (non-string types) are passed through unchanged."""
        # Note: the production code currently passes non-string values
        # (including lists and dicts) through unchanged into the submit
        # description.
        # We can implement stricter validation or serialization methods if required.
        captured_submit_dict = {}
        patcher_submit = patch.object(
            plugin.htcondor, "Submit", make_fake_submit(captured_submit_dict)
        )
        patcher_schedd = patch.object(plugin.htcondor, "Schedd", FakeSchedd)
        patcher_submit.start()
        patcher_schedd.start()

        try:
            job = Mock()
            job.name = "rule_d"
            job.jobid = 10
            job.threads = 1
            job.resources = {
                # htcondor_submit_ keys
                "htcondor_submit_tags": ["a", "b"],
                "htcondor_submit_metadata": {"k": "v"},
                # classad_ keys
                "classad_ListAttr": [1, 2, 3],
                "classad_MapAttr": {"x": True},
            }

            mock_executor.run_job(job)

            assert captured_submit_dict["tags"] == ["a", "b"]
            assert captured_submit_dict["metadata"] == {"k": "v"}
            assert captured_submit_dict["+ListAttr"] == [1, 2, 3]
            assert captured_submit_dict["+MapAttr"] == {"x": True}
            assert captured_submit_dict["batch_name"] == "rule_d-10"
        finally:
            patcher_submit.stop()
            patcher_schedd.stop()
