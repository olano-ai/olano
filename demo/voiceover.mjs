// Generates a demo's voiceover lines with OpenAI text-to-speech into audio/<demo>/vo<n>.mp3 (committed, so building
// the videos never needs the API). mix.mjs times each line to its step in record.mjs.
// The music bed is made separately by music.py.
//   OPENAI_API_KEY=... node voiceover.mjs tour|create|chat|team|schedule|control|install|source [line numbers...]
// One line per step of the flow (record.mjs marks), the last one over the end card.
import fs from 'node:fs';
import path from 'node:path';

const KEY = process.env.OPENAI_API_KEY;
if (!KEY) { console.error('OPENAI_API_KEY is not set'); process.exit(1); }
const VOICE = process.env.VOICE || 'marin';
const STYLE = `Voice: a calm, confident woman in her thirties narrating a short product film for a software platform.
Tone: clear, friendly and matter-of-fact; quietly assured, never salesy or excited.
Delivery: natural and conversational, unhurried, with soft natural pauses at commas and full stops.
Pronunciation: "Olano" is said oh-LAH-no.
Accent: neutral international English.`;
export const LINES = {
  tour: [
    'This is Olano. A team of AI agents that works for your business, and one place to run it.',
    'Every agent has a role, its own files, and a memory that grows as it works.',
    'Ask for something in plain words. You see each step, and then the answer.',
    'Agents pass work along, the way a team does. The org chart shows who reports to whom.',
    'Give them a timetable, and the morning report is waiting when you arrive.',
    'And when an agent wants to send something out, it asks you first.',
    'Olano. Open source, and yours to run.',
  ],
  create: [
    "Need help at the front desk? Add an agent, the way you'd bring on a new hire.",
    'Give your agent a name. This one is Maya, and Maya looks after guests.',
    'Then say what the job is, in plain words.',
    'Choose the model behind it. Bring a key from your provider, or run on Olano credits.',
    'Olano sets up a workspace and a standard set of tools, and Maya joins the team.',
    'Then say hello. Maya is ready for the first guest.',
    'Olano. Build your first agent today.',
  ],
  chat: [
    "Start with a question you'd ask a colleague. How did sales go last month?",
    'Nova opens the sales file and works through it. Each step appears as it happens.',
    'The answer comes back with a chart, right there in the conversation.',
    'The full report is saved as a file. Download it, or open it in the workspace.',
    'Want a deeper look? Change the model, and the conversation carries on.',
    'Olano. Agents that show their work.',
  ],
  team: [
    "One agent can't do everything. So Olano gives you a team, with a lead and specialists.",
    'Ask Atlas for a launch plan. Atlas turns to Nova for the research.',
    'Everything the agents say to each other is kept, and you can read it.',
    'Bigger jobs become issues. Agents and people open them, pick them up and close them.',
    'You asked once. The plan comes back with the research and the copy in it.',
    'Olano. Put a team on it.',
  ],
  schedule: [
    'Some jobs come round every day. Piper can take those.',
    'Say what to do, pick the days and the time. Weekdays at eight: who is behind on rent?',
    'The calendar shows the week ahead for the whole team.',
    "You don't have to wait for morning to see it work. Run it now.",
    'Each run shows up as its own conversation, with the report in it.',
    'Olano. The morning report, ready.',
  ],
  control: [
    'Aria has drafted an email to a client. Before it goes anywhere, Aria asks.',
    'Read exactly what will be sent. Then approve it, or say no.',
    'Passwords and API keys live in an encrypted vault, not in files or chats.',
    'Insights shows what each agent is using, so the bill holds no surprises.',
    'And every change to your agents is recorded, so you can see what changed and when.',
    'Olano. Your agents, your rules.',
  ],
  install: [
    'Olano installs with one line. You need Docker, two CPUs and four gigabytes of memory.',
    'The installer downloads the stack, writes the settings and starts three containers.',
    "When it's done, it prints two things: the dashboard address, and a one-time setup code.",
    'Open the address, enter the code, and create your account. The first account is the admin.',
    'The wizard asks how to pay for models: your own provider keys, or Olano credits.',
    'Then you meet Agent Father. Describe the agent you need, and it builds it.',
    'For a public address, add your own domain. For a separate shell per agent, add sandbox.',
    'Olano. Running on your own machine.',
  ],
  source: [
    'Want to read the code, or change it? Run Olano from source.',
    'The install script sets up Python, the dependencies and the tools around them. Run it twice and nothing breaks.',
    'Setup asks a few questions: who you are, which model to use, how careful to be, and your first agent.',
    'Doctor checks the install: dependencies, workspace, vault and model access.',
    'One command starts the runtime and the dashboard, on port thirty-two hundred.',
    'Olano. Open source, start to finish.',
  ],
};
const DEMO = process.argv[2];
if (!Object.hasOwn(LINES, DEMO ?? '')) { console.error('usage: node voiceover.mjs <demo> [line numbers...]'); process.exit(1); }
const DIR = path.join(path.dirname(new URL(import.meta.url).pathname), 'audio', DEMO);
fs.mkdirSync(DIR, { recursive: true });
const only = process.argv.slice(3).map(Number);
for (const [i, input] of LINES[DEMO].entries()) {
  if (only.length && !only.includes(i)) continue;
  const r = await fetch('https://api.openai.com/v1/audio/speech', {
    method: 'POST', headers: { authorization: 'Bearer ' + KEY, 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'gpt-4o-mini-tts', voice: VOICE, input, instructions: STYLE, response_format: 'mp3' }),
  });
  if (!r.ok) throw new Error(r.status + ' ' + (await r.text()));
  fs.writeFileSync(path.join(DIR, `vo${i}.mp3`), Buffer.from(await r.arrayBuffer()));
  console.log(`vo${i}.mp3`, input);
}
