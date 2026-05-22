// dotty-pi-ext — pi extension that exposes Dotty's voice tools.
// Loaded by the dotty-pi container's pi runtime via the extensions/
// bind-mount (see ../dotty-pi/README.md).
//
// This entry point is intentionally thin: it just registers tools. All
// behaviour lives in tools/* (testable in isolation) and lib/* (the
// underlying clients — sqlite for brain.db, fetch for xiaozhi admin,
// etc).

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { logTurnEnd } from "./lib/turn_logger.ts";
import { memoryLookupTool } from "./tools/memory_lookup.ts";
import { playSongTool } from "./tools/play_song.ts";
import { recallPersonTool } from "./tools/recall_person.ts";
import { rememberTool } from "./tools/remember.ts";
import { rememberPersonTool } from "./tools/remember_person.ts";
import { takePhotoTool } from "./tools/take_photo.ts";
import { thinkHardTool } from "./tools/think_hard.ts";

export default function (pi: ExtensionAPI) {
  pi.registerTool(memoryLookupTool);
  pi.registerTool(recallPersonTool);
  pi.registerTool(rememberTool);
  pi.registerTool(rememberPersonTool);
  pi.registerTool(thinkHardTool);
  pi.registerTool(playSongTool);
  pi.registerTool(takePhotoTool);
  // Per-turn conversation auto-log — mirrors bridge.py /api/voice/memory_log,
  // fired by Tier1Slim today. Lives here so the PiVoiceLLM cutover can
  // retire that bridge endpoint.
  pi.on("agent_end", logTurnEnd);
  // set_led is intentionally absent: the LED ring is reserved for
  // mode/state indication, not voice-driven; see README.md "Not a tool".
}
