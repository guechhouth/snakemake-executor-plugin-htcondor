# Job Wrapper

A simple example demonstrating how Job Wrappers are used with HTCondor executor.

## Why Job Wrappers Are Needed

HTCondor executes jobs in sandboxed environments on remote compute nodes. These nodes don't have access to the user's home directory, which breaks Snakemake's default behavior of writing cache files to `$HOME`.

The job wrapper solves this by:

1. Setting up the proper environment (`HOME=$(pwd)`)
1. Forwarding Snakemake arguments from the access point to the execution point
1. Ensuring all job artifacts stay within the HTCondor scratch directory

### Some common scenarios where job wrapper script is needed:

- The execution point (EP) does not have the right environment to execute and needs modules to be loaded. For example: miniconda (like this example)
- You need to activate a conda environment before anything else runs
- `$HOME` is not set or is pointed to somewhere broken

### Some common scenarios where job wrapper script is not needed:

- Your workflow uses containers
- You use a shared file-system where the execution point (EP) already have the same environment as the access point (AP)
- EPs already have everything pre-installed

### Illustration

```text
[HTCondor Worker Node]
        │
        ▼
┌─────────────────────────┐
│   job_wrapper.sh        │  ← YOU write this (when needed)
│   - module load conda   │    Sets up the environment
│   - source activate env │
│   - export HOME=$(pwd)  │
│         │               │
│         ▼               │
│  [snakemake_job.sh]     │  ← Snakemake always generates this
│  - run rule `foo`       │    Runs the actual rule
│  - with input X         │
│  - producing output Y   │
└─────────────────────────┘
```

**Notes:**
Snakemake automatically generates a job script for each rule execution. The job wrapper is not a replacement for this. Instead, it is to ensure that the worker node environment is correctly configured before Snakemake's auto-generated script runs.

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

```bash
conda install -c conda-forge conda-pack
conda create -n snakemake-workflow-env -c conda-forge python
conda activate snakemake-workflow-env
pip install snakemake snakemake-executor-plugin-htcondor
cd /path/to/this/example/job-wrapper
conda pack -n snakemake-workflow-env
```

**Step 2: Run the workflow**

```bash
snakemake --profile htcondor_profile
```

**Step 3: That's it!** The workflow will:

- Submit jobs to HTCondor
- Transfer the tar file to each execution point
- Extract and activate the environment on each EP
- Run Snakemake with the activated environment
- Return results to the access point

### Understanding the Portable Conda Environment (Optional Detailed Steps)

This example uses a **portable conda environment** instead of containers.
This approach is useful when EPs don't have the same environment as the AP, and you want to avoid container overhead.

The Quick Start section above covers the full process. Below are detailed explanations for each step if you want to understand what's happening or customize the environment:

#### Prerequisites

1. Install `conda-pack` on your AP:

```bash
conda install -c conda-forge conda-pack
```

2. Create a Clean `conda` Environment

```bash
# environment name: snakemake-workflow-env
conda create -n snakemake-workflow-env -c conda-forge python
# activate the environment
conda activate snakemake-workflow-env
# install executor and Snakemake
pip install snakemake snakemake-executor-plugin-htcondor
```

3. Pack the Environment

```bash
# step 1: cd into the directory where you will be running your workflow
# step 2: pack the environment there. This creates snakemake-workflow-env.tar.gz (~191MB compressed)
conda pack -n snakemake-workflow-env
```

4. Verify the File

```bash
ls -sh snakemake-workflow-env.tar.gz
chmod 644 snakemake-workflow-env.tar.gz
```

5. Update HTCondor Profile
   In `htcondor_profile/config.yaml`, add these under `default-resources`:

```yaml
job_wrapper: "wrapper.sh"
# relative path to submit directory
htcondor_transfer_input_files: "snakemake-workflow-env.tar.gz"
request_disk: "4GB"
request_memory: "1GB"
```

6. Update the wrapper script to include the environment. Refer to `wrapper.sh` for detailed explanations.

**Note:** The `--dest-prefix` parameter is optional. Without it, the environment unpacks with `bin/`, `lib/`, etc. at the top level, which is simpler for job wrapper scripts.

Refer to the [official CHTC website for more details on creating portable Python installations with Miniconda](https://chtc.cs.wisc.edu/uw-research-computing/conda-installation#4-check-size-of-conda-environment-tar-archive)

#### How Portable Conda Environment Works

1. **Transfer**: HTCondor transfers the tar file to each EP (via `htcondor_transfer_input_files` in `htcondor_profile/config.yaml`)
1. **Extract**: The wrapper script extracts it: `tar -xzf snakemake-workflow-env.tar.gz`
1. **Activate**: The wrapper activates the environment: `export PATH=$(pwd)/bin:$PATH`
1. **Run**: Snakemake executes with the activated environment

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
