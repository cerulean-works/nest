#!/bin/bash
#SBATCH --job-name=nest-test
#SBATCH --partition=dev
#SBATCH --cpus-per-task=1
#SBATCH --mem=256M
#SBATCH --time=00:02:00
#SBATCH --output=slurm-%j.out

echo "Hello from Slurm!"
echo "Hostname: $(hostname)"
echo "Python: $(python3 --version)"
echo "Working directory: $(pwd)"

if [ -r hello.sbatch ]; then
    echo "Shared filesystem: accessible"
else
    echo "Shared filesystem: not accessible"
fi

echo "Apptainer: $(apptainer --version)"
