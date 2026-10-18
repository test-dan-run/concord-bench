# Concord annotation templates

Annotations are matched to audio files by file name without the extension ("stem"), so
`audio/call_01.wav` matches `call_01.txt`, an RTTM line for `call_01`, or `"file": "call_01.wav"`.
Times are seconds from the start of the file.

| File | Task | Notes |
|---|---|---|
| asr.jsonl | ASR | One JSON object per line: `audio_filepath`, `text`. Optional `start`/`end` (or `offset`/`duration`) for segment-level transcripts; extra fields become slice metadata. |
| asr.csv | ASR | Columns `audio_filepath`, `text` (optional `start`, `end`, `speaker`). |
| <stem>.txt | ASR | One transcript file per audio file, same stem. |
| diarisation.rttm | Diarisation | Standard RTTM `SPEAKER` lines. |
| diarisation_segments.json | Diarisation | List of `{file, start, end, speaker}`. |
| speaker_transcripts.jsonl | ASR + diarisation + cpWER | `{file, start, end, speaker, text}` per line (a `.json` list works too). |
| speaker_transcripts.stm | ASR + diarisation + cpWER | `file channel speaker start end [<label>] transcript`. |
| speaker_transcripts.seglst.json | ASR + diarisation + cpWER | SegLST: `session_id, speaker, start_time, end_time, words`. |
| nemo_manifest.json | ASR | NVIDIA NeMo manifest: one JSON object per line with `audio_filepath`, `duration`, `text` (optional `offset`). `.json` or `.jsonl`. |
| nemo_diarisation_manifest.json | Diarisation | NeMo diarisation manifest. Speaker labels come from the RTTM named by `rttm_filepath`, so upload those RTTM files too (matched by file name; the folder in the path does not matter). |
| nemo_diarisation_output.json | ASR + diarisation + cpWER | NeMo diarisation output: `session_id` plus `sentences` of `{speaker, start_time, end_time, text}` (or only `words` of `{word, start_time, end_time, speaker}`). |

cpWER needs speaker-attributed transcripts (STM, SegLST, JSON/JSONL segments with text, or NeMo
diarisation output). An RTTM plus per-file text gives WER and DER, but not cpWER.
