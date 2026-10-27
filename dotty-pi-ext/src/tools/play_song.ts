// play_song voice tool — pi-extension port of bridge.py's
// _voice_tool_play_song / _voice_tool_play_song_catalog /
// _voice_tool_play_song_match (lines ~4056-4147).
//
// Three-step flow:
//   1. Fetch the catalogue from xiaozhi-server (60 s TTL cache).
//   2. Resolve the free-form name against the catalogue —
//      case-insensitive exact-stem match first, then substring containment.
//   3. POST /xiaozhi/admin/play-asset with the absolute asset path and
//      after_turn, so the song starts once this turn's reply has been spoken
//      instead of being cut off by it.
//
// Failure-mode strings match bridge.py exactly so the LLM's prompt
// behaviour holds.

import { Type } from "typebox";
import {
  fetchSongCatalog,
  playAsset,
  type AdminOptions,
} from "../lib/xiaozhi_admin.ts";

const ASSET_BASE = (
  process.env.VOICE_PLAY_SONG_ASSET_BASE ??
    "/opt/xiaozhi-esp32-server/config/assets/songs"
).replace(/\/+$/, "");

const CATALOG_TTL_SEC = 60;

// Module-level cache — bridge.py kept the same shape so two back-to-back
// "play X" turns don't refetch.
let _catalog: string[] = [];
let _catalogFetchedAt = 0;

/** Test-only helper. */
export function _resetCacheForTests(): void {
  _catalog = [];
  _catalogFetchedAt = 0;
}

function stemLower(name: string): string {
  const lower = name.toLowerCase();
  const dot = lower.lastIndexOf(".");
  return dot >= 0 ? lower.slice(0, dot) : lower;
}

/**
 * Pure matcher — exposed so the oracle can diff every match decision
 * against bridge.py's algorithm.
 *
 * Algorithm (verbatim from bridge.py:_voice_tool_play_song_match):
 *   1. Normalise query: lowercase, strip whitespace, drop extension.
 *   2. For each file: lowercase stem.
 *      a. If file stem == query stem → return immediately.
 *      b. If query stem ∈ file stem OR file stem ∈ query stem → track
 *         the candidate with the smallest |len(file_stem) - len(query_stem)|.
 *   3. Return the best substring candidate, or null.
 */
export function matchSong(query: string, files: string[]): string | null {
  if (!query || files.length === 0) return null;
  const q = query.trim().toLowerCase();
  const qStem = stemLower(q).trim();
  if (!qStem) return null;
  let best: { score: number; file: string } | null = null;
  for (const f of files) {
    const stem = stemLower(f);
    if (stem === qStem) return f;
    if (qStem.length > 0 && (stem.includes(qStem) || qStem.includes(stem))) {
      const score = Math.abs(stem.length - qStem.length);
      if (best === null || score < best.score) {
        best = { score, file: f };
      }
    }
  }
  return best?.file ?? null;
}

export interface PlaySongOptions extends AdminOptions {
  /** Override the cached catalogue (test only). */
  catalogOverride?: string[];
  /** Override the asset base path (test only). */
  assetBaseOverride?: string;
}

async function getCatalog(opts: PlaySongOptions): Promise<string[]> {
  if (opts.catalogOverride) return opts.catalogOverride;
  const now = Date.now() / 1000;
  if (now - _catalogFetchedAt < CATALOG_TTL_SEC) return _catalog;
  _catalog = await fetchSongCatalog(opts);
  _catalogFetchedAt = now;
  return _catalog;
}

export async function runPlaySong(
  name: string,
  opts: PlaySongOptions = {},
): Promise<string> {
  const trimmed = (name ?? "").trim();
  if (!trimmed) return "(no song name given)";

  // XIAOZHI_HOST presence check — bridge.py short-circuits here with
  // "(can't reach xiaozhi-server)". The TS equivalent: when neither
  // explicit opt.host nor env XIAOZHI_HOST is set, refuse early. We
  // can't introspect "is this localhost by default" cleanly, so use
  // the same env contract bridge.py does.
  if (!opts.host && !process.env.XIAOZHI_HOST) {
    process.stderr.write(`[play_song] XIAOZHI_HOST not set\n`);
    return "(can't reach xiaozhi-server)";
  }

  const files = await getCatalog(opts);
  if (files.length === 0) return "(song catalogue is empty)";

  const match = matchSong(trimmed, files);
  if (match === null) {
    const sample = files
      .slice(0, 5)
      .map((f) => f.replace(/\.[^.]+$/, ""))
      .join(", ");
    return `(no match for '${trimmed}'; have: ${sample})`;
  }

  const base = (opts.assetBaseOverride ?? ASSET_BASE).replace(/\/+$/, "");
  const assetPath = `${base}/${match}`;
  // Called mid-turn: the song waits for this turn's reply to finish.
  const result = await playAsset(assetPath, { ...opts, afterTurn: true });
  if (result.ok) {
    return `playing ${match.replace(/\.[^.]+$/, "")}`;
  }
  return `(couldn't play ${match}: ${result.error ?? "unknown"})`;
}

export const playSongTool = {
  name: "play_song",
  label: "Play Song",
  description:
    "Play a song from Dotty's local catalogue through the robot's " +
    "speaker. Pass the song name (or a fragment of it); the matcher is " +
    "tolerant of extensions and substrings.",
  promptSnippet:
    "Play a song from Dotty's local catalogue by name or fragment.",
  promptGuidelines: [
    "Use play_song when the user asks Dotty to play, sing, or put on a " +
      "specific song. Pass the user's free-form name in `name`.",
  ],
  parameters: Type.Object({
    name: Type.String({
      description: "Free-form song name or fragment.",
    }),
  }),
  async execute(
    _toolCallId: string,
    params: { name: string },
    _signal: AbortSignal | undefined,
    _onUpdate: unknown,
    _ctx: unknown,
  ): Promise<{ content: Array<{ type: "text"; text: string }> }> {
    const text = await runPlaySong(params.name);
    return { content: [{ type: "text", text }] };
  },
};
