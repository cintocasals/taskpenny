# Licences of the benchmark data

The Taskpenny code is MIT. The benchmark's task texts come from public datasets and keep their own licences. This
file covers `tasks.jsonl`, `tasks-holdout.jsonl` and every file in `results/`, which contain the task prompts (and
the answers written for them). The PyPI package does not include any of it.

| Tasks | Source | Licence | What we changed |
|---|---|---|---|
| `mtbench` (`mt*` ids) | MT-Bench questions and GPT-4 reference answers, [lm-sys/FastChat](https://github.com/lm-sys/FastChat) at commit `587d5cf`, `fastchat/llm_judge/data/mt_bench/` | Apache License 2.0 ([text](LICENSES/Apache-2.0.txt)) | First turn of each question only; reference answers given to the judge for maths, reasoning and coding |
| `hard` (`ah*`, `h2*` ids) | Arena-Hard-Auto v0.1 prompts, [lmarena/arena-hard-auto](https://github.com/lmarena/arena-hard-auto) at commit `196f6b8`, `data/arena-hard-v0.1/question.jsonl` | Apache License 2.0 ([text](LICENSES/Apache-2.0.txt)) | A seeded random sample of the prompts, unchanged |
| `multi` (`mp*`, `m2*` ids) | [databricks-dolly-15k](https://huggingface.co/datasets/databricks/databricks-dolly-15k), © Databricks, Inc. | [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) | Four Dolly prompts combined into one message per task, with a lead-in line and numbering. These combined tasks are shared under CC BY-SA 3.0 too |
| `classify` (`cl*` ids) | [Banking77](https://github.com/PolyAI-LDN/task-specific-datasets) test set, PolyAI (Casanueva et al., 2020, "Efficient Intent Detection with Dual Sentence Encoders") | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Ten test messages per task, with six candidate labels and an instruction; the true labels are kept for scoring |
| `ca`, `es` | Our own development cases (`bench/dev_cases.jsonl`) | MIT | None |

Every task records its source rows in its `source` field. `build.py` downloads each source from the fixed commit
above and checks its SHA-256 before use.

The Apache License 2.0 text above applies to the MT-Bench and Arena-Hard parts. The Creative Commons licences are
linked rather than copied, as they allow; their full legal code is at those addresses.
