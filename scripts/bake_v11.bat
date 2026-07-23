@echo off
REM v11 bake (2026-07-22, Zeke greenlit the 3 measured targets). Warm from v10.1
REM (adapter_v10_1_bak) on the dataset adding: (1) LIMITS-LOOP CLOSE — hard
REM calc -> memory_recall the limit -> defer (v10.1 failed this); (2) TOOL-
REM HALLUCINATION guard — tools are ONLY the 5, no inventing [[tool:calc]]; (3)
REM mom/pet nuance — "no mom; she'd be Zeke's wife but he has none" (right
REM reasoning, right endpoint, keep the name-refusal). SEQ=320 fits the multi-
REM turn dialogues. paged_adamw_8bit avoids the wedge.
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v10_1_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=320
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v11.log" 2>&1
