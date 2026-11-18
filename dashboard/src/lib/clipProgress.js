// How far a running job is, read from its own log: the pipeline says how many
// clips it found, then names each one as it finishes.
const FOUND = /Found (\d+) (?:viral )?clips/;
const READY = /Clip (\d+) ready/;


// {total, ready}; total is 0 until the job has picked its clips.
export function clipProgress(logs) {
  let total = 0;
  const ready = new Set();
  for (const line of logs || []) {
    const text = String(line);
    const found = FOUND.exec(text);
    if (found) {
      total = Number(found[1]);
      continue;
    }
    const done = READY.exec(text);
    if (done) ready.add(done[1]);
  }
  return { total, ready: total ? Math.min(ready.size, total) : 0 };
}

// The stage a running job is in, read from the last stage line it printed.
// Each pattern is a real print of main.py; null until one shows up.
const STAGES = [
  [/Downloading/, 'downloading the video'],
  [/Transcribing video/, 'transcribing'],
  [/Analyzing with the claude CLI|analyzing by vision/, 'claude is picking moments'],
  [/Found \d+ clips|Processing Clip \d+/, 'rendering clips'],
];

export function jobStage(logs) {
  let stage = null;
  for (const line of logs || []) {
    const text = String(line);
    for (const [re, label] of STAGES) if (re.test(text)) stage = label;
  }
  return stage;
}
