# Models per task and token usage (`claude_cli.py`)

Every Claude call goes through `claude_cli.ask(contents, schema, task=...)`
(text calls through `llm_backend.generate_json`, frame calls through
`llm_backend.ask_frames`, both of which pass the task on). The task decides the
model and is the key usage is counted under.

## Tasks

| Task | Caller | Default |
|---|---|---|
| `clips` | `main.get_viral_clips` (score + detail passes) | `sonnet` |
| `visual_clips` | `main.get_visual_clips` (silent video, frames) | `sonnet` |
| `layout` | `layout_picker`, `screencast_layout` | `haiku` |
| `hooks` | `hook_grounding` | `haiku` |
| `subtitles` | `burned_subtitles` | `haiku` |
| `edits` | `editor.VideoEditor` (auto edit) | `haiku` |
| `studio` | `thumbnail.py` (titles, thumbnail text, descriptions) | `sonnet` |
| `pro_mode` | `pro_mode.write_titles` | `sonnet` |

`claude_cli.model_for(task)`: the pick in `output/.models.json`, else
`LLM_MODEL` for the text tasks (`clips`, `visual_clips`, `studio`,
`pro_mode`), else the default above. The file is read on every call, so a
change in Settings applies to jobs already running. A model name is checked
against `[a-z0-9][a-z0-9.\-\[\]]{0,63}` wherever it comes from (it becomes a
`--model` argument). A new kind of call adds a row to `claude_cli.TASKS`; a
call with no task is counted as `other` on the default model.

`GET /api/settings/models` lists the tasks, their default, pick and effective
model, and the choices Settings offers; `PUT` takes `{"models": {task: model |
null}}` (null restores the default). Self-host only: 404 when
`BILLING_ENABLED`.

## Usage records

Each call, failed or not, appends one JSON line
`{at, task, model, input_tokens, output_tokens, seconds, ok}`:

- to `output/.usage.jsonl`, the server's log. Past 5 MB it rotates to
  `.usage.jsonl.1` (one old file kept);
- inside a job, also to `output/<job>/.usage.jsonl`. `app.run_job` sets
  `USAGE_LOG` in the job process's env; the file goes away with the job.

Input tokens include cache creation and cache reads, as the subscription's
usage limit counts them. Each call also prints
`🤖 [claude] <task> · <model> · <in> in / <out> out tokens · <s>s[ · failed]`,
which the dashboard's log panel shows as one row.

Reading back goes through `claude_cli.summarize`: totals plus per task
(`calls`, tokens, `seconds`, `failed`, `models`). Lines that do not parse,
numbers that are not numbers and unknown task or model names are dropped or
counted as `other` / `unknown`: the file is data from disk.

- `GET /api/usage?days=N` (N ≤ 90): the server log summed, plus per day.
- `GET /api/jobs` and `GET /api/status/{id}`: the job's own `usage`.

Calls made by the API process for a job (auto edit, Studio) go to the server
log only, not to the job's.

`tests/conftest.py` points `SETTINGS_FILE` and `USAGE_FILE` at the test's
temporary directory, so the suite never writes to the real `output/`.
