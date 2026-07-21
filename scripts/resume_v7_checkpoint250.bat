@echo off
REM Resume the half-baked v7 7B run from checkpoint-250 (2026-07-21, Zeke: "go
REM for the one that's half baked already, save every 25"). Two overnight power
REM events killed fresh runs; checkpoint-250_preblip_bak banks 250/453 steps, so
REM resuming finishes v7 in ~200 more steps instead of 453 from scratch.
REM Resume unblock: torch.load weights_only shim in the finetune script (commit
REM 90d5430) — NO torch upgrade needed. GPU power-capped to 135W separately
REM (nvidia-smi -pl 135) for the brownout concern. Pilot benched for VRAM.
REM Keep the ORIGINAL seq (256) — changing seq mid-run corrupts trainer state.
REM MUST match the original run's optimizer or the loaded state has no 'exp_avg'
REM (the checkpoint was made with PAGED_ADAMW_8BIT, bnb keys step/quant_state;
REM  default adamw_torch -> KeyError 'exp_avg'). Verified from training_args.bin.
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
REM clean checkpoint-NNN name inside output_dir so HF's ^checkpoint-(\d+)$ parses
REM the step (the _preblip_bak suffix failed the regex).
set IRIS_LB_RESUME=D:\Wren-Companion\state\little_brain\checkpoints\checkpoint-250
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v7_resume.log" 2>&1
