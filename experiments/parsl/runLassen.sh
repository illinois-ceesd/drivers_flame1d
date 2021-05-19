#!/bin/bash

module load gcc/7.3.1
module load spectrum-mpi
conda deactivate
conda activate mirgeDriver.flame1d
#export PYOPENCL_CTX="port:tesla"
export PYOPENCL_CTX="0:1"
jsrun_cmd="jsrun -g 1 -a 1 -n 1"
export XDG_CACHE_HOME="/tmp/$USER/xdg-scratch"
$jsrun_cmd js_task_info
$jsrun_cmd python -u -m mpi4py ./flame.py