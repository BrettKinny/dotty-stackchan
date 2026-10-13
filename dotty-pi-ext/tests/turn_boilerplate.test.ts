// Prompt scaffolding must never be stored in, or read back out of, memory.
// AI-assisted: Claude. Observed 2026-10-05: 253 of 402 conversation rows in a
// live brain.db carried the per-turn "HARD CONSTRAINTS" suffix, and a recall
// request returned those rows verbatim for TTS.

import { stripTurnBoilerplate, cleanStoredTurn } from "../src/lib/turn_text.ts";
import { extractTurnText } from "../src/lib/turn_logger.ts";
import { formatLookupResult, cleanLookupRows } from "../src/tools/memory_lookup.ts";

let failures = 0;
function assertEq(label: string, actual: unknown, expected: unknown): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    process.stdout.write(`  PASS  ${label}\n`);
    return;
  }
  process.stderr.write(`  FAIL  ${label}\n        expected: ${e}\n        actual:   ${a}\n`);
  failures++;
}

const ROUTING = "\n\nVOICE TOOL ROUTING: Decide whether to use a registered tool before composing.";
const RULES = "\n\n---\nHARD CONSTRAINTS for THIS reply (overrides everything else):\n1. Reply in ENGLISH ONLY.\nBegin your reply now.";
const MEMORY = "\n\nMEMORY SEARCH RESULTS (from earlier conversations):\nuser: old | assistant: older";

assertEq("plain text is untouched", stripTurnBoilerplate("What is your name?"), "What is your name?");
assertEq("routing + rules suffix removed", stripTurnBoilerplate("What is your name?" + ROUTING + RULES), "What is your name?");
assertEq("rules-only suffix removed", stripTurnBoilerplate("Tell me a joke." + RULES), "Tell me a joke.");
assertEq("injected memory block removed", stripTurnBoilerplate("Do you remember my colour?" + MEMORY + ROUTING + RULES), "Do you remember my colour?");
assertEq("legacy ASR JSON wrapper unwrapped",
  stripTurnBoilerplate('{"content": "Dotty, what is my favourite colour?"}\n---\nHARD CONSTRAINTS for THIS reply'),
  "Dotty, what is my favourite colour?");
assertEq("ordinary braces are not treated as a wrapper", stripTurnBoilerplate("{not json} hello"), "{not json} hello");
assertEq("a user merely saying the words keeps their sentence",
  stripTurnBoilerplate("what are hard constraints for this reply"), "what are hard constraints for this reply");

const logged = extractTurnText([
  { role: "user", content: "What is 12 plus 7?" + ROUTING + RULES, timestamp: 1 },
  { role: "assistant", content: [{ type: "text", text: "😊 19!" }] },
]);
assertEq("turn logger stores only what the person said", logged, { user: "What is 12 plus 7?", assistant: "😊 19!" });

assertEq("stored turn with truncated rules is cleaned, reply kept",
  cleanStoredTurn('user: {"content": "My favourite colour is purple."}\n---\nHARD CONSTRAINTS for THIS reply (overr | assistant: 😊 Purple is lovely!'),
  "user: My favourite colour is purple. | assistant: 😊 Purple is lovely!");
assertEq("stored turn without a reply is cleaned",
  cleanStoredTurn("user: Hello there" + RULES), "user: Hello there");
assertEq("facts and other rows pass through", cleanStoredTurn("Brett's favourite colour is purple."), "Brett's favourite colour is purple.");

const rows: any[] = [
  { key: "voice_conversation_1", category: "conversation", namespace: "voice", created_at: "x",
    content: "user: My favourite colour is purple." + RULES + " | assistant: 😊 Noted!" },
  { key: "core_1", category: "core", namespace: "default", created_at: "x", content: "Favourite colour: purple." },
];
assertEq("lookup output carries no prompt scaffolding",
  formatLookupResult(cleanLookupRows(rows)),
  "user: My favourite colour is purple. | assistant: 😊 Noted! | Favourite colour: purple.");

if (failures) {
  process.stderr.write(`\n${failures} failure(s)\n`);
  process.exit(1);
}
process.stdout.write("\nOK — 0 failure(s)\n");
