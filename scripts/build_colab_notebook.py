"""Writes training/train_interpreter_colab.ipynb (kept as code here so the notebook is reviewable).

    python scripts/build_colab_notebook.py
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "training" / "train_interpreter_colab.ipynb"


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
            "source": text.strip("\n").splitlines(keepends=True)}


CELLS = [
    md("""
# Train the Proof-Carrying Data Analyst question interpreter

This notebook fine-tunes a small open model to turn analytical questions into the analyst's structured
query format. It does not write code and never sees your data, only table and column names.

**Before you start:** `Runtime → Change runtime type → T4 GPU` (free tier is enough).

**You need two files** from the project's `training/` folder, created by `python scripts/make_training_data.py`:
`train.jsonl` and `val.jsonl`.

Run the cells top to bottom. Approximate times on a T4: install 3 min · training 35–60 min (1.5B) or 15–25 min (0.5B) ·
export 5 min. At the end a zip downloads with the model file and an Ollama `Modelfile`.
"""),
    code("""
# 1. Check that a GPU is attached (should list a Tesla T4 or better)
!nvidia-smi --query-gpu=name,memory.total --format=csv
"""),
    code("""
# 2. Install the training libraries (about 3 minutes)
%%capture
!pip install unsloth
"""),
    code("""
# 3. Upload train.jsonl and val.jsonl from the project's training/ folder
import os
from google.colab import files

if not (os.path.exists("train.jsonl") and os.path.exists("val.jsonl")):
    files.upload()
for name in ("train.jsonl", "val.jsonl"):
    assert os.path.exists(name), f"{name} is missing: upload it in this cell"
    print(name, sum(1 for _ in open(name, encoding="utf-8")), "examples")
"""),
    code("""
# 4. Settings. 1.5B is the recommended size (about 1 GB once exported, runs on any PC with 4 GB RAM).
#    Switch to 0.5B for very weak machines (about 400 MB, faster, somewhat less accurate).
MODEL_NAME = "unsloth/Qwen2.5-1.5B-Instruct"   # or "unsloth/Qwen2.5-0.5B-Instruct"
OUTPUT_NAME = "pcda-interpreter"
EPOCHS = 1                 # 1 epoch over 20,000 examples is enough; use 2 if validation accuracy is below 90%
MAX_SEQ_LENGTH = 2048      # prompts are about 400-700 tokens
"""),
    code("""
# 5. Load the base model in 4-bit and attach a small trainable LoRA adapter (QLoRA)
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=MODEL_NAME, max_seq_length=MAX_SEQ_LENGTH, load_in_4bit=True)
model = FastLanguageModel.get_peft_model(
    model, r=16, lora_alpha=16, lora_dropout=0, bias="none",
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    use_gradient_checkpointing="unsloth", random_state=3407)
"""),
    code("""
# 6. Prepare the conversations (system prompt, catalog + question, expected JSON) in the model's chat format
from datasets import load_dataset

data = load_dataset("json", data_files={"train": "train.jsonl", "val": "val.jsonl"})

def to_text(batch):
    return {"text": [tokenizer.apply_chat_template(m, tokenize=False) for m in batch["messages"]]}

data = data.map(to_text, batched=True, remove_columns=data["train"].column_names)
print(data["train"][0]["text"][:1200])
"""),
    code("""
# 7. Train. Loss is computed only on the expected JSON, not on the prompt.
from trl import SFTConfig, SFTTrainer
from unsloth.chat_templates import train_on_responses_only

trainer = SFTTrainer(
    model=model, tokenizer=tokenizer, train_dataset=data["train"], eval_dataset=data["val"],
    args=SFTConfig(
        dataset_text_field="text", max_seq_length=MAX_SEQ_LENGTH, packing=False,
        per_device_train_batch_size=8, gradient_accumulation_steps=2, num_train_epochs=EPOCHS,
        learning_rate=2e-4, warmup_steps=30, lr_scheduler_type="cosine", weight_decay=0.01,
        logging_steps=25, eval_strategy="steps", eval_steps=400, save_strategy="no",
        optim="adamw_8bit", seed=3407, report_to="none", output_dir="outputs"))
trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\\n",
                                  response_part="<|im_start|>assistant\\n")
trainer.train()
"""),
    code("""
# 8. Measure accuracy on 200 held-back validation questions (exact match and per field)
import json, random

FIELDS = ["table", "measure", "aggregation", "ratio_filter", "group_by", "time_grain", "top_n", "order",
          "currency", "date_from", "date_to", "growth_from", "growth_to", "date_column"]
DEFAULTS = {"aggregation": "sum", "order": "desc"}

def norm(d):
    if d.get("unresolved"):
        return {"unresolved": True}
    out = {f: d.get(f, DEFAULTS.get(f)) for f in FIELDS}
    if not out["top_n"]:
        out["order"] = None
    return out

FastLanguageModel.for_inference(model)
rows = [json.loads(l) for l in open("val.jsonl", encoding="utf-8")]
random.Random(0).shuffle(rows)
exact, field_ok, field_n, bad_json = 0, 0, 0, 0
for r in rows[:200]:
    prompt = tokenizer.apply_chat_template(r["messages"][:2], tokenize=False, add_generation_prompt=True)
    ids = tokenizer(prompt, return_tensors="pt").to("cuda")
    out = model.generate(**ids, max_new_tokens=256, do_sample=False)
    text = tokenizer.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
    try:
        got = norm(json.loads(text))
    except json.JSONDecodeError:
        bad_json += 1
        continue
    want = norm(r["target"])
    exact += got == want
    for k in want:
        field_n += 1
        field_ok += got.get(k) == want[k]
print(f"exact match {exact / 200:.1%} · field accuracy {field_ok / max(field_n, 1):.1%} · invalid JSON {bad_json}")
"""),
    code("""
# 9. Export to a 4-bit GGUF file for Ollama, write the Modelfile, zip and download (about 5 minutes)
import glob, shutil

model.save_pretrained_gguf(OUTPUT_NAME, tokenizer, quantization_method="q4_k_m")
gguf = sorted(glob.glob(f"{OUTPUT_NAME}/**/*.gguf", recursive=True) + glob.glob(f"{OUTPUT_NAME}*.gguf"),
              key=os.path.getsize)[-1]
os.makedirs("export", exist_ok=True)
shutil.copy(gguf, f"export/{OUTPUT_NAME}.gguf")
open("export/Modelfile", "w").write(
    f"FROM ./{OUTPUT_NAME}.gguf\\n"
    'TEMPLATE \"\"\"{{- range .Messages }}<|im_start|>{{ .Role }}\\n{{ .Content }}<|im_end|>\\n{{ end }}<|im_start|>assistant\\n\"\"\"\\n'
    'PARAMETER stop "<|im_end|>"\\nPARAMETER temperature 0\\nPARAMETER num_ctx 4096\\n')
print(f"model file: {os.path.getsize('export/' + OUTPUT_NAME + '.gguf') / 1e6:.0f} MB")
shutil.make_archive(OUTPUT_NAME, "zip", "export")
files.download(f"{OUTPUT_NAME}.zip")
"""),
    md("""
## Install it on your PC

1. Unzip the download anywhere (it contains `pcda-interpreter.gguf` and `Modelfile`).
2. Install Ollama if this PC does not have it: https://ollama.com/download
3. From the project folder run:

```
.venv\\Scripts\\python scripts\\install_local_model.py path\\to\\pcda-interpreter.gguf
```

The analyst picks the model up automatically on its next start (the System page shows "Local model").
"""),
]


def main():
    nb = {"cells": CELLS, "metadata": {"accelerator": "GPU", "colab": {"provenance": []},
                                       "kernelspec": {"display_name": "Python 3", "name": "python3"},
                                       "language_info": {"name": "python"}},
          "nbformat": 4, "nbformat_minor": 5}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(nb, indent=1), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
