# Job Wrapper

A simple example demonstrating how Job Wrappers are used with HTCondor executor.

## Files and Directories

| File/Directory | Purpose |
| --- | --- |
| **README.md** | Documentation explaining job wrappers, why they're needed, and how this example uses them |
| **Snakefile** | The workflow definition specifying the rules, inputs, outputs, and shell commands for the pipeline |
| **wrapper.sh** | Job wrapper script executed by HTCondor before the actual job task; sets up the mamba/conda environment and working directory for job execution |
| **htcondor_profile/** | Directory containing HTCondor executor configuration |
| **htcondor_profile/config.yaml** | Profile configuration file with HTCondor executor settings and resource defaults |
| **inputs/** | Directory containing sample input files for the workflow |
| **inputs/sample1.txt** | Sample input data file 1 |
| **inputs/sample2.txt** | Sample input data file 2 |

## Why Job Wrappers Are Needed

HTCondor executes jobs in sandboxed environments on remote compute nodes.
These nodes don't have access to the user's home directory, which breaks Snakemake's default behavior of writing cache files to `$HOME`.

The job wrapper solves this by:

1. Setting up the proper environment (`HOME=$(pwd)`)
1. Forwarding Snakemake arguments from the access point to the execution point
1. Ensuring all job artifacts stay within the HTCondor scratch directory

### Some common scenarios where job wrapper script is needed:

- The execution point (EP) does not have the right environment to execute and needs modules to be loaded. For example: miniconda (like this example)
- You need to activate a mamba/conda environment before anything else runs
- `$HOME` is not set or is pointed to somewhere broken

### Some common scenarios where job wrapper script is not needed:

- You use a shared file-system where the execution point (EP) already have the same environment as the access point (AP)
- EPs already have everything pre-installed

**Note on wrappers and containers**: This example demonstrates a non-container approach using a portable mamba/conda environment. If your workflow uses containers (as the `basic-workflow` or `grouped-jobs` examples do), you do not need a complex wrapper for environment setup as the container handles that automatically.

### Illustration

```text
[HTCondor Access Point]
        │
        ▼
HTCondor submits job with arguments
        │
        ▼
[HTCondor Worker Node]
        │
        ▼
┌──────────────────────────────────┐
│   wrapper.sh                     │  ← Receives HTCondor arguments
│   - module load conda            │    Sets up the environment
│   - source activate env          │
│   - export HOME=$(pwd)           │
│   - snakemake "$@"               │  ← Passes arguments to Snakemake
└──────────────────────────────────┘
        │
        ▼
   Snakemake executes the rule
   with those arguments
```

## How This Example Works

This example runs a simple two-step pipeline that processes two samples (`sample1.txt`, `sample2.txt`) through a series of rules:

1. **`make_intermediary`** — Processes each input file, appending `"foo"`
   to produce `results/intermediary_{sample}.txt`
1. **`make_output`** — Processes each intermediary file, appending `"bar"`
   to produce the final `results/output_{sample}.txt`

The pipeline exists purely to demonstrate the job wrapper mechanic — the rules themselves do minimal work so the focus stays on how `wrapper.sh` sets up the environment before each rule executes on the worker node.

Refer to `wrapper.sh` to see how a simple wrapper script is set up.

### Quick Start

This example requires a portable conda environment. Create it with these 3 steps:

**Step 1: Create and pack the environment**

You can use `conda` or `mamba`, we recommend `mamba` as it is much faster than `conda`.

```bash
# Install `conda-pack` on your AP
mamba install -c conda-forge conda-pack
# Create environment name: snakemake-workflow-env
mamba create -n snakemake-workflow-env -c conda-forge python
# Activate the environment
mamba activate snakemake-workflow-env
# Install executor and Snakemake
pip install snakemake snakemake-executor-plugin-htcondor
# cd into the directory where you will be running your workflow
cd /path/to/this/example/job-wrapper
mamba deactivate
# Pack the environment there. This creates snakemake-workflow-env.tar.gz (~191MB compressed)
conda-pack -n snakemake-workflow-env
# Verify the files
ls -sh snakemake-workflow-env.tar.gz
# Make environment executable
chmod 644 snakemake-workflow-env.tar.gz
```

Refer to the [official CHTC website for more details on creating portable Python installations with Miniconda](https://chtc.cs.wisc.edu/uw-research-computing/conda-installation#4-check-size-of-conda-environment-tar-archive)

**Step 2: Update HTCondor Profile and Wrapper Script**

- Update HTCondor Profile
  In `htcondor_profile/config.yaml`, add these under `default-resources`:

```yaml
job_wrapper: "wrapper.sh"
# relative path to submit directory
htcondor_transfer_input_files: "snakemake-workflow-env.tar.gz"
request_disk: "1GB"
request_memory: "512MB"
```

- Update the wrapper script to include the environment. Refer to `wrapper.sh` for detailed explanations.

**Step 3: Run the workflow**

```bash
snakemake --profile htcondor_profile
```

**Step 4: That's it!** The workflow will:

- Submit jobs to HTCondor
- Transfer the tar file to each execution point (via `htcondor_transfer_input_files` in `htcondor_profile/config.yaml`)
- Extract and activate the environment on each EP (`tar -xzf snakemake-workflow-env.tar.gz`)
- Run Snakemake with the activated environment (wrapper script does: `export PATH=$(pwd)/bin:$PATH`)
- Return results to the access point

### How to Run

See the "Quick Start" section above! Once you have created `snakemake-workflow-env.tar.gz`, simply run:

```bash
snakemake --profile htcondor_profile
```

The workflow will automatically transfer and activate the environment on each execution point.

### Expected Outputs

```
results/intermediary_sample1.txt
results/intermediary_sample2.txt
results/output_sample1.txt
results/output_sample2.txt
```
