// The short contract page (HANDOFF §3.5): what Concord sends and expects, the detection rules, and
// links to the platform docs, which are the source of truth.

import { Link } from "react-router-dom";
import { PageHeader, Panel } from "../../components/ui";

const RULES = [
  ["openai / audio.transcriptions", "verbose_json + timestamp_granularities[]=word", "words[] → ASR with word times · segments[] only → segment times · text only → text only · empty or far from the clip's transcript (WER > 50 %) → warning"],
  ["openai / chat.completions", "user message with a text instruction and an input_audio part (base64 WAV)", "plausible transcript → ASR, text only · 4xx on the audio part, or an answer unrelated to the clip → fail: “audio part ignored — model can't take audio”"],
  ["oip-v2 / infer with AUDIO + SAMPLE_RATE", "infer on the clip", "SEGMENTS only → VAD · SEGMENTS + SPEAKERS → diarisation (2 speakers → pass, otherwise warning) · segments outside the clip → fail"],
  ["anything else", "none", "not usable as a model; text.transform services are offered as WER normalisers instead"],
];

export function ContractPage() {
  return (
    <>
      <PageHeader crumbs="Bring your own" title="Model contract" subtitle="What Concord sends to a model and what it expects back. The platform defines the protocols; Concord only reads them." />
      <Panel title="Source of truth">
        <p className="m-0 text-[13px] text-muted leading-relaxed">
          Model services, their protocols and how they are started are defined by the AI platform: <span className="num text-fg">dev/README.md</span> and{" "}
          <span className="num text-fg">platform/contracts/README.md</span>. Concord reads the platform's endpoint map (via <span className="num">aicore</span>) for every
          model's contract, URL, image tag and tasks, and starts services with the platform's own <span className="num">tools/stack.py</span>. If anything here disagrees
          with the platform docs, the platform docs win.
        </p>
      </Panel>
      <div className="grid gap-5 lg:grid-cols-3">
        <Panel title="ASR · openai / audio.transcriptions">
          <pre className="m-0 text-[11px] num bg-canvas border border-track rounded-md p-2.5 whitespace-pre-wrap">{`POST /v1/audio/transcriptions  (multipart)
file=<16 kHz mono WAV: the file, or a segment cut>
model=<service name>
response_format=verbose_json | json
timestamp_granularities[]=word   (word-timed models)
language=<dataset language, e.g. en>`}</pre>
          <span className="text-xs text-muted">Expects <span className="num">text</span>, and <span className="num">words[]</span> / <span className="num">segments[]</span> when the model has them. Each file (or segment) goes to the model in one request, with no client-side chunking, so very long files work only if the model accepts them.</span>
        </Panel>
        <Panel title="Audio LLM · openai / chat.completions">
          <pre className="m-0 text-[11px] num bg-canvas border border-track rounded-md p-2.5 whitespace-pre-wrap">{`POST /v1/chat/completions
{"model": "<service>", "temperature": 0,
 "messages": [{"role": "user", "content": [
   {"type": "text", "text": "Transcribe … transcript only."},
   {"type": "input_audio",
    "input_audio": {"data": "<base64 WAV>", "format": "wav"}}]}]}`}</pre>
          <span className="text-xs text-muted">Scored on text only. In pipelines the VAD / diarisation stage provides timing.</span>
        </Panel>
        <Panel title="VAD / diarisation · oip-v2 / infer">
          <pre className="m-0 text-[11px] num bg-canvas border border-track rounded-md p-2.5 whitespace-pre-wrap">{`POST /v2/models/<service>/infer
inputs:  AUDIO        FP32  [-1]   16 kHz mono
         SAMPLE_RATE  INT32 [1]    16000
outputs: SEGMENTS     FP32  [-1,2] start, end (s)
         SPEAKERS     INT32 [-1]   diarisers only`}</pre>
          <span className="text-xs text-muted">A model that returns <span className="num">SPEAKERS</span> is a diariser (DER, cpWER); without it, a VAD (missed / false-alarm speech, WER).</span>
        </Panel>
      </div>
      <Panel title="How capabilities and timing are detected" subtitle="The compatibility check runs on a 10 s synthesised two-speaker clip with a known transcript and turns.">
        <div className="overflow-x-auto scroll-thin">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="text-[11px] text-muted uppercase tracking-wider text-left">
                <th scope="col" className="font-medium px-3 py-2 border-b border-line">Registry entry</th>
                <th scope="col" className="font-medium px-3 py-2 border-b border-line">Probe</th>
                <th scope="col" className="font-medium px-3 py-2 border-b border-line">Result</th>
              </tr>
            </thead>
            <tbody>
              {RULES.map(([e, p, r]) => (
                <tr key={e} className="border-b border-track align-top">
                  <td className="px-3 py-2 num text-xs">{e}</td>
                  <td className="px-3 py-2 text-xs text-muted">{p}</td>
                  <td className="px-3 py-2 text-xs">{r}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <span className="text-xs text-muted">Every check line is pass / warn / fail with a plain-language reason. “Add” is blocked only when nothing usable was detected. <Link to="/models/add">Add a model</Link>.</span>
      </Panel>
    </>
  );
}
