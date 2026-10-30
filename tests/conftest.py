"""
Shared test fixtures and utilities for HTCondor executor tests.
"""

from contextlib import contextmanager
from typing import Optional
from unittest.mock import Mock, patch
import os

import snakemake_executor_plugin_htcondor as plugin
from snakemake_executor_plugin_htcondor import Executor, METADATA_DEFAULT


def create_mock_executor(shared_fs_prefixes=None):
    """
    Create a mock executor with common setup.

    Args:
        shared_fs_prefixes: List of shared filesystem prefixes. Defaults to empty list.

    Returns:
        Mock executor with bound methods.
    """
    executor = Mock(spec=Executor)
    executor.logger = Mock()
    executor.shared_fs_prefixes = shared_fs_prefixes or []
    executor.workflow = Mock()
    executor.workflow.configfiles = []
    executor.workflow.workdir_init = "/test/workdir"
    executor.get_snakefile = Mock(return_value="Snakefile")

    # Bind the actual methods to the mock
    executor._get_files_for_transfer = Executor._get_files_for_transfer.__get__(
        executor, Executor
    )
    executor._add_file_if_transferable = Executor._add_file_if_transferable.__get__(
        executor, Executor
    )
    executor._parse_file_list = Executor._parse_file_list.__get__(executor, Executor)

    return executor


def create_mock_submit_executor(tmp_path):
    """Create a mock executor configured for run_job submit tests."""
    executor = create_mock_executor()
    executor.jobDir = str(tmp_path / "jobs")
    executor.workflow = Mock()
    executor.workflow.storage_settings.shared_fs_usage = False
    executor.workflow.executor_settings = Mock()
    executor.workflow.executor_settings.output_destination = None
    executor.envvars = Mock(return_value={})
    executor.report_job_submission = Mock()
    executor._unified_log_file = str(tmp_path / "snakemake-rules.log")
    executor._metadata_file = None

    executor.run_job = Executor.run_job.__get__(executor, Executor)
    executor._set_resources = Executor._set_resources.__get__(executor, Executor)

    executor._get_exec_args_and_transfer_files = Mock(
        return_value=("python", "-m snakemake --cores 1", [], [], [])
    )
    executor._handle_explicit_unit_resources = Mock()
    executor._log_resource_requests = Mock()
    return executor


@contextmanager
def mock_htcondor_submission(captured_submit_dict):
    """Patch HTCondor submission objects and capture the submit dict."""

    class FakeSubmitResult:
        def cluster(self):
            return 12345

    class FakeSubmit:
        def __init__(self, submit_dict):
            captured_submit_dict.update(submit_dict)

        def issue_credentials(self):
            return None

    class FakeSchedd:
        def submit(self, submit_description):
            return FakeSubmitResult()

    with patch.object(plugin.htcondor, "Submit", FakeSubmit), patch.object(
        plugin.htcondor, "Schedd", FakeSchedd
    ):
        yield


def create_mock_individual_job(
    script=None,
    notebook=None,
    input_files=None,
    output_files=None,
    htcondor_transfer_input_files=None,
    htcondor_transfer_output_files=None,
    job_wrapper=None,
    wildcards=None,
    params=None,
):
    """
    Create a mock individual (non-grouped) job with common setup.

    Args:
        script: Path to script file or None
        notebook: Path to notebook file or None
        input_files: List of input files
        output_files: List of output files
        htcondor_transfer_input_files: Additional input files from resource
        htcondor_transfer_output_files: Additional output files from resource
        job_wrapper: Path to job wrapper script or None
        wildcards: Dict of wildcards for the job
        params: Dict of params for the job

    Returns:
        Mock job configured as individual job.
    """
    job = Mock()
    job.is_group = Mock(return_value=False)
    job.name = "test_individual_job"
    job.input = input_files or []
    job.output = output_files or []
    job.wildcards = wildcards or {}
    job.params = params or {}

    # Setup rule with script/notebook
    job.rule = Mock()
    job.rule.script = script
    job.rule.notebook = notebook

    # Setup resources
    def resource_get(key, default=None):
        resource_map = {
            "htcondor_transfer_input_files": htcondor_transfer_input_files,
            "htcondor_transfer_output_files": htcondor_transfer_output_files,
            "job_wrapper": job_wrapper,
            "preserve_relative_paths": True,
        }
        return resource_map.get(key, default)

    job.resources = Mock()
    job.resources.get = Mock(side_effect=resource_get)

    # Setup wildcard formatting
    def format_wildcards_func(string, **variables):
        result = string
        # Merge provided wildcards/params with any defaults
        all_vars = {}
        if "wildcards" in variables:
            all_vars.update(variables["wildcards"])
        if "params" in variables:
            all_vars.update(variables["params"])

        for key, value in all_vars.items():
            result = result.replace(f"{{wildcards.{key}}}", str(value))
            result = result.replace(f"{{params.{key}}}", str(value))
            result = result.replace(f"{{{key}}}", str(value))
        return result

    job.format_wildcards = Mock(side_effect=format_wildcards_func)

    return job


def create_mock_group_job(
    individual_jobs,
    external_outputs=None,
    htcondor_transfer_input_files=None,
    htcondor_transfer_output_files=None,
):
    """
    Create a mock grouped job containing multiple individual jobs.

    Args:
        individual_jobs: List of mock individual jobs to include in the group
        external_outputs: List of output file paths that are consumed by rules
            outside the group.  In real Snakemake, GroupJob.output only contains
            these external outputs — internal intermediates are NOT included.
            The executor must collect all outputs from job.jobs to discover
            internal intermediates.  Defaults to an empty list.
        htcondor_transfer_input_files: Additional input files from resource
        htcondor_transfer_output_files: Additional output files from resource

    Returns:
        Mock job configured as a group job.
    """
    # Use spec_set to limit what attributes the mock has
    job = Mock(spec=["is_group", "jobs", "input", "output", "resources", "name"])
    job.is_group = Mock(return_value=True)
    job.jobs = individual_jobs
    job.name = "test_group_job"

    # Aggregate inputs from all individual jobs (the executor does not use
    # job.input for group jobs, but we populate it for completeness).
    job.input = []
    for individual_job in individual_jobs:
        job.input.extend(individual_job.input)

    # In real Snakemake, GroupJob.output contains only the outputs consumed by
    # rules OUTSIDE the group.  Internal intermediates are accessible only via
    # job.jobs[*].output.  If the caller doesn't specify external_outputs, the
    # mock defaults to an empty list — exactly the worst case the executor must
    # handle correctly.
    job.output = external_outputs if external_outputs is not None else []

    # Setup resources
    def resource_get(key, default=None):
        resource_map = {
            "htcondor_transfer_input_files": htcondor_transfer_input_files,
            "htcondor_transfer_output_files": htcondor_transfer_output_files,
            "preserve_relative_paths": True,
        }
        return resource_map.get(key, default)

    job.resources = Mock()
    job.resources.get = Mock(side_effect=resource_get)

    return job


def create_mock_metadata_executor(
    tmp_path,
    mgmt_id: Optional[int] = 12345,
    total_nodes: int = 5,
    executable_nodes: int = 4,
    metadata: Optional[str] = METADATA_DEFAULT,
    schedd=None,
):
    """
    Create a mock executor configured for metadata file tests.

    This builds on create_mock_submit_executor and adds what run_job's metadata path
    depends on. It mirrors the metadata setup in Executor.__post_init__
    (resolve -> rollover -> initialize -> publish).

    Args:
        tmp_path: pytest tmp_path fixture value.
        mgmt_id: ClusterId of the local universe management job, or None when
            Snakemake is run by hand.
        total_nodes: size of workflow.dag.jobs
        executable_nodes: count of non-local needrun jobs (actually get submitted to the EP).
        metadata: value of --htcondor-metadata. None disables metadata,
            METADATA_DEFAULT writes to jobDir, otherwise a file path.
        schedd: mock returned by htcondor.Schedd(), so tests can assert on
            _publish_metadata_path's edit call. A fresh Mock is used if None.

    Returns:
        Mock executor with metadata methods bound. If metadata is enabled, any
        existing file at the resolved path has been rolled over and the initial
        metadata written.
    """
    # bind real run_job and stubs to everything else it needs
    executor = create_mock_submit_executor(tmp_path)

    os.makedirs(executor.jobDir, exist_ok=True)

    # bind all relevant methods to creating and updating metadata file
    for method_name in (
        "_initialize_metadata",
        "_write_metadata",
        "_add_job_to_metadata",
        "_update_job_status_in_metadata",
        "_flush_metadata_if_dirty",
        "_resolve_metadata_file",
        "_rollover_metadata_file",
        "_publish_metadata_path",
    ):
        method = getattr(Executor, method_name)
        setattr(executor, method_name, method.__get__(executor, Executor))

    executor._mgmt_id = mgmt_id
    executor._metadata = {}
    executor._metadata_dirty = False

    # resolve the --htcondor-metadata setting to the absolute path it is written to
    executor._metadata_file = executor._resolve_metadata_file(metadata)

    # workflow.dag setup for _initialize_metadata
    executor.workflow.dag = Mock()
    executor.workflow.dag.jobs = [Mock() for _ in range(total_nodes)]
    executor.workflow.dag.needrun_jobs = Mock(
        return_value=[Mock(is_local=False) for _ in range(executable_nodes)]
    )

    if executor._metadata_file is not None:
        executor._rollover_metadata_file(executor._metadata_file)
        executor._initialize_metadata(mgmt_id)

        schedd = schedd or Mock()
        with patch.object(plugin.htcondor, "Schedd", return_value=schedd):
            executor._publish_metadata_path()

    return executor
