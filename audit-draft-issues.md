# Draft GitHub issues — dotty-stackchan audit

Ready-to-file issue drafts for confirmed bugs and all critical/high-severity findings (excluding the issue/PR triage entries, which are tracked in `AUDIT-REPORT.md`). Each block is copy-pasteable into `gh issue create`.

---

## [CRITICAL] Container chain: unauthenticated admin routes + docker.sock + host networking = LAN→host-root

```
Summary
An attacker who reaches the LAN (or Tailnet) has a confirmed write primitive into the
container that holds the docker socket, and that container is one `docker run --privileged`
call from full host root. The individually-reported symptoms (unauth admin routes,
arbitrary-path play-asset, all-root containers, host networking) are each one link of a
single exploit chain.

Location
docker-compose.yml.template:43-60,59-60,596
custom-providers/xiaozhi-patches/http_server.py:596,620-672
dotty-pi/docker-compose.yml:17 / dotty-behaviour/docker-compose.yml:23 / bridge/docker-compose.yml:31

Details
- xiaozhi-server binds 0.0.0.0:8003 and registers all 11 /xiaozhi/admin/* routes with NO auth.
- The SAME container mounts /var/run/docker.sock + /usr/bin/docker (the compose comment admits
  this gives "effective root on the docker host").
- The other three services run network_mode: host with no segmentation.
- None of the four Dockerfiles set USER — every container runs as root.

Proposed fix
Break the chain at more than one link:
1. Replace the raw docker.sock bind with a least-privilege docker-socket-proxy
   (tecnativa/docker-socket-proxy) exposing ONLY the exec API PiVoiceLLM needs.
2. Add a shared-secret bearer-token aiohttp middleware over the /xiaozhi/admin/* subapp
   (constant-time compare, 401 otherwise); have callers present it.
3. Bind admin HTTP to 127.0.0.1 (set server.ip) and reach it only from sibling containers
   on loopback instead of publishing 0.0.0.0:8003.
4. Add non-root USER directives to all four Dockerfiles. Pair with no-new-privileges:true.
Do at least the socket-proxy + route auth.

Suggested labels: security, area:xiaozhi, area:bridge, priority:critical
```

---

## [CRITICAL] compose.all-in-one.yml omits the docker.sock + docker CLI mounts the default PiVoiceLLM provider requires

```
Summary
A fresh deploy following compose.all-in-one.yml's own Quick start with the default config
gets a container that cannot exec into dotty-pi — every voice turn fails.

Location
compose.all-in-one.yml:64-79

Details
The shipped .config.yaml.template defaults to selected_module.LLM: PiVoiceLLM, which reaches
the brain via `docker exec -i dotty-pi pi --mode rpc ...` (pi_client.py:360-368). That needs
the host docker socket + docker CLI bind-mounted (docker-compose.yml.template:59-60).
compose.all-in-one.yml mounts the pi_voice provider dir but NOT the socket or the docker
binary — even though its own header says PiVoiceLLM "requires the host docker socket".

Proposed fix
Add the two PiVoiceLLM mounts (matching the template):
  /var/run/docker.sock:/var/run/docker.sock
  /usr/bin/docker:/usr/bin/docker:ro
Or, if all-in-one is meant to be OpenAICompat-only, change the shipped selected_module
default and say so explicitly in the header.

Suggested labels: bug, area:deploy, priority:high
```

---

## [HIGH] compose.all-in-one.yml is missing the xiaozhi-patches mounts — admin routes + perception relay dead

```
Summary
Deploying with compose.all-in-one.yml yields upstream xiaozhi-server with no admin portal and
no perception relay, so the entire dotty-behaviour layer is inert.

Location
compose.all-in-one.yml:64-79

Details
The canonical template mounts portal_bridge.py, websocket_server.py, http_server.py, and
textMessageHandlerRegistry.py (admin routes, active_connections registry, EventTextMessageHandler).
all-in-one mounts none of them, so consumers fire admin HTTP calls into 404s and no perception
events ever reach the bus. Also missing vs the template: openai_compat, personas,
receiveAudioHandle.py, dances.py, textUtils.py, songs, and the kid/smart state mount.

Proposed fix
Bring compose.all-in-one.yml's volume list to parity with docker-compose.yml.template, or
generate the all-in-one from the template so it can't drift. At minimum add the four
xiaozhi-patches mounts plus openai_compat, personas, receiveAudioHandle, dances, textUtils,
songs, and VISION_BRIDGE_URL.

Suggested labels: bug, area:deploy, priority:high
```

---

## [HIGH] All /xiaozhi/admin/* routes are completely unauthenticated

```
Summary
Every admin endpoint is registered with no auth check, so anyone reaching port 8003 can drive
the robot and read arbitrary files.

Location
custom-providers/xiaozhi-patches/http_server.py:620-672 (handlers 34-589)

Details
inject-text, abort, set-head-angles, set-state, set-toggle, set-face-identified, take-photo,
play-asset, songs, say, devices — all added with no auth. inject-text runs the full LLM+MCP
pipeline (remote prompt-injection into the pi agent); say is verbatim TTS; play-asset reads any
absolute path (only os.path.exists). The OTA and WS paths guard themselves; these do not.

Proposed fix
Gate the routes behind a shared secret: an aiohttp middleware requiring a Bearer / X-Admin-Token
header on every /xiaozhi/admin/* request, 401 otherwise. At minimum confine play-asset to an
allow-listed base dir via os.path.realpath + startswith.

Suggested labels: security, area:xiaozhi, priority:high
```

---

## [HIGH] play-asset accepts arbitrary absolute filesystem path with no allowlist

```
Summary
/xiaozhi/admin/play-asset feeds any readable file to ffmpeg inside the docker.sock-holding
container, with the only validation being os.path.exists.

Location
custom-providers/xiaozhi-patches/http_server.py:376-411

Details
asset is a raw absolute path; no base-dir confinement, no extension allowlist before
AudioSegment.from_file (ffmpeg/libav). The sibling songs handler hard-codes a base dir + a
{opus,ogg,wav,mp3} allowlist; play-asset has neither. An unauthenticated LAN caller can
probe arbitrary host/container paths (404-vs-200) and exercise libav demuxer CVEs against
attacker-staged files — and a decoder RCE lands on the privileged process.

Proposed fix
Resolve os.path.realpath(asset) and require it to start with the songs base
(/opt/xiaozhi-esp32-server/config/assets/songs + os.sep) before decode; enforce the
{opus,ogg,wav,mp3} allowlist; prefer accepting only a basename joined onto the fixed base.

Suggested labels: security, area:xiaozhi, priority:high
```

---

## [HIGH] All four containers run as root (no USER directive)

```
Summary
None of the four service images declare USER; for xiaozhi-server the root process is the one
mounting docker.sock, so any code-exec bug there is already host root.

Location
Dockerfile, dotty-pi/Dockerfile, dotty-behaviour/Dockerfile, bridge/Dockerfile

Details
xiaozhi-server (upstream base), dotty-pi (node:25.9-alpine), dotty-behaviour + bridge
(python:3.12-slim) all run as root. The FastAPI/uvicorn and pi workloads do not need root.
dotty-behaviour and bridge run root on host networking, widening blast radius.

Proposed fix
Add a non-root USER to each Dockerfile (create app user, chown app/state dirs, drop to it).
For xiaozhi-server, run the socket-proxy so root-in-container is no longer root-on-host.
Pair with no-new-privileges:true and read-only rootfs where feasible.

Suggested labels: security, area:deploy, priority:high
```

---

## [HIGH] room_view roster recognition silently fails when person id != display_name

```
Summary
A correct VLM identification is dropped to person_id=None whenever a person's canonical id
differs from display_name.lower() — the common case — so named greetings never fire.

Location
dotty-behaviour/vision/room_view.py:115-124,159-167
dotty-behaviour/routes/vision.py:192,256-269

Details
name_choices is built from p.display_name, but parse_room_view_response validates against
roster_ids, which household.roster_ids_with_appearance() sources from p.id. For id `hudson_jr`
with display_name `Hudson`, the lowercased returned name doesn't match roster_ids and falls to
None; the synthetic face_recognized broadcast never fires. Masked because the test fake
reimplements the helper as display_name.lower() with id==display_name fixtures.

Proposed fix
Make the prompt vocabulary and the parser's validation set use the SAME key: build name_choices
from p.id, or resolve the returned display name back to the canonical id before the membership
check. Fix the test fake to return p.id and add an id != display_name fixture.

Suggested labels: bug, area:behaviour, area:vision, priority:high
```

---

## [HIGH] Double greeting on every face_recognized — FaceGreeter and ProactiveGreeter both fire

```
Summary
A single face recognition produces two back-to-back utterances because two independent
consumers both speak and don't coordinate.

Location
dotty-behaviour/consumers/face_greeter.py:133-186
dotty-behaviour/greeter/greeter.py:162 (wired in main.py:309-323)

Details
FaceGreeter._handle_face_recognized says "Oh, it's {name}!" and ProactiveGreeter._on_face_recognized
independently generates an LLM greeting and also says(). They keep separate cooldown state. The
bare-greet path was deliberately suppressed when the roster is identifiable; the named path was not.

Proposed fix
Pick one owner for the named greeting: disable FaceGreeter's named path when the proactive
greeter is enabled (empty FACE_NAME_GREET_TEMPLATE), or share a single per-identity cooldown
timestamp between the two so the second consumer backs off.

Suggested labels: bug, area:behaviour, priority:high
```

---

## [HIGH] room_view name parser cannot match multi-word display names

```
Summary
Any roster member whose display name contains a space is a 100% silent identification failure,
even on a perfect VLM response.

Location
dotty-behaviour/vision/room_view.py:72-81,115-124

Details
name_choices injects display_name and the prompt asks for `NAME: <display_name>`, but the NAME
capture group is `[A-Za-z_][A-Za-z0-9_-]*` — a single whitespace-free token. A reply
`NAME: Mary Anne` fails the anchored regex, so parse_room_view_response returns (cleaned, None, None).

Proposed fix
Make the NAME group accept internal spaces (e.g. `[A-Za-z_][\w '\-]*?`) and normalise before
lookup, OR request a single-token id and map ids->display names. Add a two-word display_name test.

Suggested labels: bug, area:behaviour, area:vision, priority:high
```

---

## [HIGH] Greeter calendar lookup never matches an identified person (case mismatch)

```
Summary
A greeted person's personal calendar items are silently dropped from their greeting unless
their calendar prefix is all-lowercase.

Location
dotty-behaviour/greeter/greeter.py:257-259 -> calendar_/cache.py:69-73
dotty-behaviour/calendar_/fetch.py:99

Details
summarize_for_prompt is called with person=identity (lowercased id), but the calendar `person`
field is set from the title prefix with no case folding (`[Hudson] -> "Hudson"`). The filter
`if ev["person"] != person` then drops "Hudson" against "hudson". Household-bucket events mask it.

Proposed fix
Normalise both sides: lower-case the parsed calendar person at fetch time (prefer mapping the
prefix through HouseholdRegistry.get_by_calendar_prefix to the canonical id), or compare
case-insensitively in summarize_for_prompt.

Suggested labels: bug, area:behaviour, area:calendar, priority:high
```

---

## [HIGH] memory_lookup FTS search bypasses the #53 kid-safety namespace gate

```
Summary
A fact about a minor routed to the person_pending review queue can be returned to a live turn
by memory_lookup whenever the query phrase-matches it.

Location
dotty-pi-ext/src/lib/brain_db.ts:82-90 (searchMemories SQL); tools/memory_lookup.ts

Details
fetchPersonMemories is scoped to person:<id> and deliberately never returns person_pending:<id>
(unreviewed minor facts). But searchMemories runs a bare `WHERE memories_fts MATCH ? ORDER BY rank`
over ALL rows in `memories` (external-content FTS over every row), including person_pending:<id>.
The oracle (bridge.py _voice_memory_search_blocking) is equally unfiltered, so the gap exists in both.

Proposed fix
Add a namespace guard:
  ... WHERE memories_fts MATCH ? AND m.namespace NOT LIKE 'person_pending:%' ORDER BY rank LIMIT ?
Mirror the exclusion into oracle.py/bridge.py. Add a test seeding a person_pending row and
asserting memory_lookup never returns it.

Suggested labels: bug, safety, area:dotty-pi, priority:high
```

---

## [HIGH] Mid-utterance synth failure emits SentenceType.LAST, truncating the rest of the reply

```
Summary
On any synthesis error in a non-last segment, the provider tells xiaozhi/firmware the whole TTS
response is finished, so the device stops talking mid-sentence.

Location
custom-providers/edge_stream/edge_stream.py:139-143
custom-providers/piper_local/piper_local.py:187-191 (identical bug)

Details
text_to_speak's except handler unconditionally pushes (SentenceType.LAST, [], None) regardless
of is_last. A reply is split into multiple segments; if an early/middle segment's synth throws
(edge_tts blip, Piper decode error), the LAST marker drops every remaining segment.

Proposed fix
Only emit LAST from the failure path when is_last is True; for non-last segments, log and
continue (or emit a segment-level failure sentinel) so subsequent segments still play.

Suggested labels: bug, area:tts, priority:high
```

---

## [HIGH] dotty_doctor model checks look under data/models instead of repo-root models — false FAIL

```
Summary
In the standard documented layout (config at data/.config.yaml, models at models/), both
model checks report FAIL and dotty_doctor exits non-zero even though the models are present.

Location
scripts/dotty_doctor.py:128-155

Details
_find_config prefers data/.config.yaml; check_models_* then compute root = config_path.parent
(= data/) and check data/models/SenseVoiceSmall and data/models/piper. But the models live at
repo-root models/ (docker-compose mounts ./models/...; Makefile SENSEVOICE_DIR := models/...).

Proposed fix
Don't anchor to config_path.parent. If config_path is .config.yaml under a data/ dir, use
config_path.parent.parent; or search both root/models and root.parent/models.

Suggested labels: bug, area:tooling, priority:high
```

---

## [HIGH] provision.py: voice "General" collides with text "general" and gets moved on every run

```
Summary
Syncing the VOICE "General" channel destructively moves the existing TEXT #general into the
VOICE category, recurring on every idempotent re-run; the real voice channel is never created.

Location
community/discord/provision.py:93-96,197-202

Details
find_channel searches the whole guild case-insensitively and ignores channel type, so
find_channel(guild, "General") matches text "general"; the existing-channel branch then calls
existing.edit(category=VOICE), moving #general. Same collision class hits any text/voice name
pair differing only by case.

Proposed fix
Scope find_channel by the expected channel type (pass kind and filter on isinstance /
ChannelType), or compare (name, type) tuples. Don't move a channel whose type doesn't match.

Suggested labels: bug, area:community, priority:high
```

---

## [MEDIUM] Dashboard action endpoints always block 8s and report "no reply" — the event producer is dead

```
Summary
Every dashboard Say/Dance/Mood/Story action blocks for the full 8s then renders
"Sent — no reply in 8s." because nothing ever enqueues onto the bridge event bus.

Location
bridge.py:613-626 (producer stub); bridge/dashboard.py:658-704,2091-2133

Details
_inject_or_error subscribes to _dashboard_event_listeners and waits 8s for Dotty's reply, but
the voice-turn producer moved to dotty-pi in #36 and was never re-wired to the bridge SSE bus.
The injection fires (device speaks) but the dashboard can never show the reply.

Proposed fix
Either wire a real producer (dotty-pi/xiaozhi POSTs completed turns to a bridge endpoint that
calls put_nowait on each subscriber queue), or drop the subscribe/wait and return "Sent"
immediately. Document /ui/events as heartbeat-only until a producer exists.

Suggested labels: bug, area:dashboard
```

---

## [MEDIUM] Blocking synchronous HTTP inside async dashboard routes stalls the event loop

```
Summary
Uncached dotty-behaviour getters and the robot-photo proxy do synchronous requests.get with no
to_thread, blocking the entire asyncio loop for up to 1.5-2.0s and freezing all dashboard clients.

Location
bridge.py:471-504 (_dotty_behaviour_get)
bridge/dashboard.py:193-218 (_fetch_robot_photo), 1366, 2085, 801, 1925

Details
The perception/vision/audio getters funnel through _dotty_behaviour_get (requests.get timeout=1.5)
and are called from async routes with no await. The HTMX 10s poll fans each render into 3-6 such
calls. The codebase already uses asyncio.to_thread for device-count/songs, so these are clear
omissions. _build_perception_card_ctx's "no I/O" docstring is now false.

Proposed fix
Wrap the blocking HTTP in asyncio.to_thread at the call sites, or use httpx.AsyncClient. Fix the
stale docstring.

Suggested labels: bug, area:dashboard
```

---

## [MEDIUM] CSRF middleware blocks the localhost /admin/* operator endpoints

```
Summary
The documented localhost CLI back-channel (/admin/*) is 403'd by the app-wide CSRF middleware
before it ever reaches its own localhost auth, so the entire admin surface is unusable via its
intended caller.

Location
bridge/csrf.py:33,69-70,83; bridge.py:800-924

Details
CSRFMiddleware exempts only ('/api/', '/metrics', '/health'). The /admin router (kid-mode,
smart-mode, state, persona, safety) is not exempt and a curl/script call carries no CSRF cookie,
so it is rejected before _admin_require_localhost runs.

Proposed fix
Add '/admin/' to _EXEMPT_PREFIXES in bridge/csrf.py (the /admin router already has its own
localhost-only auth, the correct machine-to-machine model).

Suggested labels: bug, area:dashboard
```

---

## [MEDIUM] SSE /events served under BaseHTTPMiddleware (CSRF) — Starlette buffering hazard

```
Summary
The text/event-stream response is wrapped by BaseHTTPMiddleware (CSRF), a documented Starlette
SSE footgun that buffers chunks and breaks disconnect detection.

Location
bridge/dashboard.py:2091-2133; bridge/csrf.py:73-98

Details
CSRFMiddleware subclasses starlette.middleware.base.BaseHTTPMiddleware, which consumes the
StreamingResponse through an anyio memory stream and can delay/not-flush chunks, defeating the
X-Accel-Buffering: no header. Today only heartbeats flow, but the channel is structurally
degraded once a producer is wired back.

Proposed fix
Exempt the SSE path from BaseHTTPMiddleware-style wrapping, or migrate CSRFMiddleware to a raw
ASGI middleware so streaming responses pass through untouched.

Suggested labels: bug, area:dashboard
```

---

## [MEDIUM] Kid/smart-mode toggles report ok:False while having already persisted+applied the flip

```
Summary
When the firmware doesn't ack a toggle, the endpoint returns ok:False even though the bridge has
already flipped and persisted the bit, so the operator sees "failed", re-toggles, and the state
ping-pongs out of sync.

Location
bridge.py:630-643 (_dashboard_set_kid_mode); 695-707 (_dashboard_set_smart_mode)

Details
_dashboard_set_kid_mode persists + applies first, then dispatches the firmware set_toggle; if the
device is asleep / WiFi drops, _dispatch_set_toggle returns False and the function returns
{ok:False} even though the bridge state is already flipped. The UI can't distinguish "fully
applied" from "not applied".

Proposed fix
Return ok:True with a separate device_pushed:false / warning field when the bridge write
succeeded but only the firmware LED push failed (mirror the /admin/kid-mode shape). Render the
persisted new_state and surface the firmware-ack failure as a non-fatal note.

Suggested labels: bug, area:dashboard
```

---

## [MEDIUM] network_mode: host binds dashboard + /metrics to 0.0.0.0 with auth unset by default

```
Summary
The shipped bridge compose runs host networking with no dashboard auth and no CSRF secret set, so
the full mutation surface and /metrics are reachable unauthenticated from the whole LAN.

Location
bridge/docker-compose.yml:31,41-60; bridge.py:936; bridge/dashboard.py:259-267

Details
DOTTY_DASHBOARD_USER/PASS, DOTTY_CSRF_SECRET, and DOTTY_BRIDGE_HOST are all unset; auth is opt-in
(_verify_dashboard_auth returns immediately when either env var is empty); DOTTY_BRIDGE_HOST
defaults to 0.0.0.0. CSRF alone does not authenticate. An unauthenticated kid-mode-off toggle is
reachable from the LAN.

Proposed fix
Ship the compose with DOTTY_DASHBOARD_USER/PASS + a persistent DOTTY_CSRF_SECRET from .env, and/or
bind 127.0.0.1 by default with a documented reverse proxy. At minimum document the required auth env.

Suggested labels: security, area:dashboard
```

---

## [MEDIUM] Async dashboard routes read + JSON-parse the entire daily convo log synchronously

```
Summary
status_strip / host_detail / alerts_count / alerts_detail read the whole convo-*.ndjson and
json.loads every line on the loop thread, blocking all concurrent requests on each ~10s poll.

Location
bridge/dashboard.py:377-398,472-514,517-555,1631-1648

Details
_stackchan_last_seen does path.read_bytes() + per-line json.loads with no to_thread; alerts_count
and alerts_detail do read_bytes().splitlines() + per-line json.loads inline. The log grows all day
and is polled per open tab.

Proposed fix
Wrap the read+parse bodies in await asyncio.to_thread(...). Optionally tail the file rather than
parsing the whole day.

Suggested labels: bug, area:dashboard
```

---

## [MEDIUM] Dashboard /actions/say and /actions/start-story bypass the kid-mode content filter

```
Summary
The dashboard say/start-story ingresses hand text straight to inject-text with no content
filtering, a second unfiltered speech path that isn't acknowledged in the kid-safety surface.

Location
bridge/dashboard.py:706-729,732-773

Details
say() and start_story() sanitise control chars and length, then call _inject_or_error without
content_filter() (imported via bridge.text). When DOTTY_DASHBOARD_USER/PASS are unset (default),
any LAN client can make Dotty speak arbitrary unfiltered text; these turns also never populate
the _cf_recent ring powering /ui/safety/recent.

Proposed fix
Run the sanitised text through content_filter() when kid-mode is on (use kid_mode_getter),
returning the safe replacement / a rejection and recording the hit.

Suggested labels: bug, safety, area:dashboard
```

---

## [MEDIUM] face_detected during talk state silently drops the bridge perception relay

```
Summary
face_detected/face_lost transitions received while current_state == 'talk' are never forwarded to
dotty-behaviour, desyncing consumers (greeter state, face_lost_aborter) for the whole conversation.

Location
custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:134-138

Details
The face_detected branch does `if cstate == 'talk': ... return`, which exits handle() entirely so
the relay POST at lines 167-193 never runs. The talk-gate's intent is only to suppress
re-triggering room_view capture, not forwarding the event.

Proposed fix
Replace the return with control flow that skips only the capture kick-off but still falls through
to the relay (wrap the capture-start in `if cstate != 'talk':`).

Suggested labels: bug, area:xiaozhi, area:behaviour
```

---

## [MEDIUM] face_lost does not reset _room_description_in_flight, can wedge room_view capture

```
Summary
A leaked _room_description_in_flight flag makes every subsequent face_detected refuse to re-capture
for the life of the connection.

Location
custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:92-102,139-141

Details
face_detected gates capture on `not _room_description_in_flight`. face_lost clears the other
room-view caches but never clears _room_description_in_flight, so a capture that crashed without
resetting the flag wedges all future captures.

Proposed fix
In the face_lost branch also set conn._room_description_in_flight = False so a lost-then-reacquired
face always re-arms a capture.

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] WebSocket query-param device-id can bypass token auth via the allowlist short-circuit

```
Summary
A client can put ?device-id=<an-allowed-device> in the URL and skip token verification, because
query-param identity is injected into request.headers and trusted by the allowlist bypass.

Location
custom-providers/xiaozhi-patches/websocket_server.py:84-108,216

Details
_handle_connection injects device-id/client-id/authorization from query params into
request.headers; _handle_auth reads them back and the allowed_devices whitelist short-circuit
treats a query-param device-id the same as a header one. Headers mutation semantics are also
version-fragile (__setitem__ may append).

Proposed fix
Treat query-param identity as untrusted: do not let a query-string device-id satisfy the
allowlist bypass; require the token path for query-param connections. Use a separate dict for
derived identity rather than mutating request.headers.

Suggested labels: security, area:xiaozhi
```

---

## [MEDIUM] OTA _is_higher_version treats pre-release/build-suffix versions as numerically higher

```
Summary
A release-candidate or build-tagged version is seen as HIGHER than its GA, so a device on GA is
offered a stale rc and a device on the rc never sees the GA as newer.

Location
custom-providers/xiaozhi-patches/ota_handler.py:24-43,315 (also 314-332,94)

Details
_parse_version does re.findall(r'\d+', ver), so '1.2.3-rc1' -> (1,2,3,1) compares greater than
'1.2.3' -> (1,2,3); '1.2' equals '1.2.0'. Build suffixes ('1.3.2 (build 47)') invert ordering.
Verified: _is_higher_version('1.2.3-rc1','1.2.3') returns True. Drives both the candidate sort
and the update decision.

Proposed fix
Use a real semver-ish comparator (strip leading v, parse the numeric core, rank pre-release
suffixes BELOW the release, consider only the first 3 segments and ignore build metadata).
Add unit tests for rc/build-suffix cases; add the module to coverage + a ci.yml test dir.

Suggested labels: bug, area:xiaozhi, area:firmware
```

---

## [MEDIUM] play-asset clobbers conn.client_abort / client_is_speaking, corrupting a concurrent voice turn

```
Summary
A timer-driven play-asset landing mid-turn cancels a user's barge-in and marks the device idle
while chat TTS is still streaming, desyncing the speaking-state machine.

Location
custom-providers/xiaozhi-patches/http_server.py:452-453,474

Details
_dispatch() unconditionally sets conn.client_abort=False then conn.client_is_speaking=True, and in
finally sets client_is_speaking=False. play-asset is fire-and-forget on a ~20s security timer with
non-deterministic device selection, with no guard that the conn is idle.

Proposed fix
Bail (or queue) if the conn is mid-turn — check client_is_speaking / an in-progress sentence_id
and refuse with 409 if busy. Save/restore prior flags rather than hard-setting, or route admin
audio through the TTS-priority queue.

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] Admin handlers call conn.websocket.send() concurrently with the active chat-path writer

```
Summary
Fire-and-forget admin sends run concurrently with xiaozhi's own send on the same WS connection;
the websockets library forbids interleaved sends, so the command silently fails or a chat frame
gets corrupted.

Location
custom-providers/xiaozhi-patches/http_server.py:146,194,242,288,338,456-468

Details
Every admin handler and play-asset write directly to conn.websocket.send() via _spawn(). A send
from an admin task while the chat task is mid-send can raise ConcurrencyError (swallowed by the
task done-callback) or interleave frames. play-asset (hundreds of opus frames) maximizes overlap.

Proposed fix
Serialize all device-bound writes through a single per-conn asyncio.Lock that admin handlers
acquire. At minimum log the ConcurrencyError rather than dropping it, and gate audio pushes behind
an idle check.

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] _dotty_say mutates conn.sentence_id from a worker thread, racing the consumer and pre-empting chat TTS

```
Summary
A /say landing mid-turn overwrites conn.sentence_id, causing the consumer to drop the chat turn's
remaining sentences — truncating Dotty mid-sentence — and is a data race against the consumer thread.

Location
custom-providers/xiaozhi-patches/http_server.py:547-574,582

Details
_enqueue runs on a thread (asyncio.to_thread) and sets conn.sentence_id with no busy-check or lock
before enqueueing FIRST/MIDDLE/LAST. The greeter fires on perception events that can coincide with
a live conversation. There is also no tts_text_queue guard (AttributeError swallowed → silent no-op).

Proposed fix
Refuse /say with 409 when the conn is mid-turn; or push the greeting through the queue without
overwriting sentence_id. Guard for tts_text_queue and 503 if absent. If sentence_id must be set, do
it on the loop thread.

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] play-asset omits the tts 'start' lifecycle frame and uses an unvalidated Opus rate

```
Summary
Playback never sends the {type:tts,state:start} frame (firmware-fragile) and feeds conn.sample_rate
straight into the Opus encoder without checking it is a legal rate; a bad rate silently returns 200.

Location
custom-providers/xiaozhi-patches/http_server.py:414,421-423,456-461

Details
The playback only emits sentence_start then opus frames then stop. tgt = conn.sample_rate is passed
to OpusEncoderUtils; Opus only supports 8/12/16/24/48 kHz, so a non-legal rate raises inside _decode,
is caught by the broad except (logs "decode failed"), and the endpoint still returns 200 ok.

Proposed fix
Emit the full lifecycle (start -> sentence_start -> frames -> stop). Validate tgt against
{8000,12000,16000,24000,48000} (reject or resample). Propagate decode failure as 500 instead of 200.

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] EventTextMessageHandler relays perception events to a stale BRIDGE_URL

```
Summary
Perception events are POSTed to {BRIDGE_URL or VISION_BRIDGE_URL}/api/perception/event, but the bus
moved to dotty-behaviour:8090 in #115; if the deploy sets the wrong var, every event 404s and is
dropped, silently disabling face_greeter/sound_turner/state gating.

Location
custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:67-78,178

Details
The env var names and warning text still say BRIDGE_URL/VISION_BRIDGE_URL. If BRIDGE_URL points at
the dashboard container (which no longer serves /api/perception/event), the relay 404s.

Proposed fix
Rename/select the env var to point at dotty-behaviour explicitly (e.g. BEHAVIOUR_URL) and update the
warning string; or document that BRIDGE_URL must resolve to dotty-behaviour:8090. Verify the deploy
compose sets the var the handler reads.

Suggested labels: bug, area:xiaozhi, area:behaviour
```

---

## [MEDIUM] state_changed/face_detected ordering can corrupt the room_view gate

```
Summary
If the firmware emits face_detected before the IDLE->TALK state_changed it acted on, the room_view
gate sees 'idle' and fires a capture for a mid-talk flicker — the stacked "Hi NAME" failure the
comment claims to prevent.

Location
custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:109-115,133-138

Details
conn.current_state is set from state_changed and read on face_detected. The gate relies on
state_changed always preceding the talk-phase face_detected, which the firmware does not guarantee.

Proposed fix
Gate on a positive "room_view already captured this session" flag (only capture if
conn._room_description is None and no capture since last face_lost) rather than current_state. Or
have firmware stamp face_detected with its state.

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] Turn timeout is a fixed wall-clock deadline that ignores streaming progress

```
Summary
A healthy but slow reply (think_hard escalation, 6-sentence story) is killed mid-stream because the
turn deadline is set once and never extended while text streams.

Location
custom-providers/pi_voice/pi_client.py:220-269

Details
iter_turn_text sets deadline = time.time() + self._turn_timeout_sec (default 120s) before the loop
and never resets it. The user hears a partial reply then the "(brain offline)" fallback. The timeout
should bound silence/stall, not total length.

Proposed fix
Reset deadline = time.time() + self._turn_timeout_sec each time a text_delta (or any progress frame)
is yielded, so the cap only fires when pi goes quiet.

Suggested labels: bug, area:voice
```

---

## [MEDIUM] new_session failure proceeds with un-reset pi session, leaking prior-turn context

```
Summary
When new_session fails, the next voice turn runs against pi's previous, non-reset session, carrying
prior content (or a partially-absorbed jailbreak) into the next, possibly child, interaction.

Location
custom-providers/pi_voice/pi_voice.py:136-141

Details
response() calls self._client.new_session() but on PiClientError only logs and continues, and sets
self._first_turn = False unconditionally — so a failed/timed-out reset is treated as a successful
fresh turn.

Proposed fix
On new_session failure force a hard reset before proceeding (close + respawn the pi process), or
surface the failure to the caller. Do not treat a failed reset as a fresh turn.

Suggested labels: bug, safety, area:voice
```

---

## [MEDIUM] _closed flag never reset after close() + reuse, suppressing reader-thread crash logging

```
Summary
After a close() + reuse, a genuine reader-thread crash is silently swallowed, so turns just time out
with no diagnostic trail.

Location
custom-providers/pi_voice/pi_client.py:171-182,311-313,352-354

Details
close() sets self._closed = True; _ensure_started() respawns the process and starts fresh reader
threads but never resets _closed to False. The stdout/stderr exception handlers guard
logger.exception with `if not self._closed:`, so with _closed stuck True real crashes are hidden.

Proposed fix
Reset self._closed = False inside _ensure_started() on (re)spawn; better, scope the "expected
shutdown" suppression to the specific process generation rather than a sticky boolean.

Suggested labels: bug, area:voice
```

---

## [MEDIUM] Abandoned iter_turn_text leaves pi streaming; a stale agent_end can terminate the next turn

```
Summary
After a barge-in/abort, the following voice turn can come back empty or with stale text because an
abandoned turn's trailing agent_end (not id-matched) terminates the next turn.

Location
custom-providers/pi_voice/pi_client.py:211-269 (also 188-209)

Details
iter_turn_text sends a prompt then yields deltas until agent_end. On early stop (face_lost_aborter
abort, barge-in) the generator is GC'd without consuming agent_end; pi keeps generating and queues
frames. new_session may race the in-flight turn, and a leftover agent_end satisfies the NEXT
iter_turn_text's terminator (agent_end is matched on type, not req_id).

Proposed fix
Send an explicit cancel/abort to pi when a turn generator is abandoned (try/finally inside
iter_turn_text). Drain-and-discard the prior turn's frames keyed by the abandoned req_id before the
next prompt. Tag agent_end matching with req_id. Have new_session() block until the prior turn's
agent_end (or a hard reset).

Suggested labels: bug, area:voice
```

---

## [MEDIUM] OpenAICompat can emit two emojis when a valid emoji is not at the start

```
Summary
When the model's reply contains a valid emoji but not as the first character, the code prepends the
fallback emoji AND yields the original content with its embedded emoji — two emojis, violating the
single-emoji HARD CONSTRAINT.

Location
custom-providers/openai_compat/openai_compat.py:218-229

Details
Enforcement only checks so_far.startswith(emoji). For "Well, 😊 hi there", so_far starts with 'W',
so it prepends 😐 and then yields content still containing 😊. Firmware keys the face on the first
emoji; the second garbles TTS. The check latches after the first content, never stripping embedded emojis.

Proposed fix
After deciding the leading content lacks a valid emoji prefix, strip embedded emojis from the
streamed body before yielding (reuse textUtils.check_emoji/is_emoji). A robust fix buffers until the
first non-whitespace char, decides the prefix, then filters subsequent emojis.

Suggested labels: bug, area:voice
```

---

## [MEDIUM] OpenAICompat speaks chain-of-thought when pointed at a reasoning model

```
Summary
OpenAICompat has no thinking filter, so an inline-<think> reasoning model has Dotty narrate its
reasoning aloud, and the leading < defeats the emoji-prefix check.

Location
custom-providers/openai_compat/openai_compat.py:189-229

Details
PiClient filters thinking_delta; OpenAICompat reads only choices[0].delta.content and yields it
verbatim. The local backend serves qwen3.6 reasoning models that emit <think>...</think> inline
(spoken to TTS) or in delta.reasoning_content (dropped, but the visible answer arrives after a long
silent gap, tripping the turn timeout).

Proposed fix
Strip <think>...</think> spans from content before the emoji check/yield (stateful across chunks),
and ignore delta.reasoning_content. Or send a param to disable thinking. Guard against a leading <
defeating the emoji-prefix enforcement.

Suggested labels: bug, area:voice
```

---

## [MEDIUM] OpenAICompat appends the safety/format suffix to the wrong message when no user turn exists

```
Summary
If the final dialogue turn is a system/assistant/tool message, the HARD CONSTRAINTS suffix
(emoji rule, English-only, length, kid-mode filter) is never injected and the model runs unconstrained.

Location
custom-providers/openai_compat/openai_compat.py:101-114

Details
_build_messages computes last_user_idx and appends _TURN_SUFFIX only there; if last_user_idx is None
(no user role — proactive greeter injections, tool/function turns), the suffix is never added.
pi_voice avoids this by only sending the last user text; OpenAICompat forwards the whole dialogue.

Proposed fix
If last_user_idx is None, append _TURN_SUFFIX to a synthesized trailing user message (or to the last
message regardless of role) so the safety/format constraints are always present.

Suggested labels: bug, safety, area:voice
```

---

## [MEDIUM] OpenAICompat yields whitespace-only content chunks to TTS before emoji enforcement

```
Summary
A whitespace-only first chunk is yielded to TTS before the emoji-prefix enforcement engages, so the
first thing TTS/firmware sees is whitespace rather than the emoji the contract promises.

Location
custom-providers/openai_compat/openai_compat.py:216-229

Details
Every non-empty content delta is yielded at :229 unconditionally; enforcement only fires once
so_far = ''.join(full_text).lstrip() is non-empty. Leading whitespace deltas (common with reasoning
models / templated outputs) go out before any emoji; an all-whitespace stream fires the fallback
after whitespace already went to TTS.

Proposed fix
Buffer content until so_far (lstripped) is non-empty before yielding anything; do not yield raw
whitespace-only deltas. Yield the (optionally emoji-prefixed) accumulated buffer once real content
exists.

Suggested labels: bug, area:voice
```

---

## [MEDIUM] processed_chars over-count in edge_stream and piper_local

```
Summary
After synthesizing the remaining tail, processed_chars is advanced by len(full_text) on top of an
already-nonzero absolute index, overshooting; a re-entered LAST silently drops the final sentence's audio.

Location
custom-providers/edge_stream/edge_stream.py:67-74
custom-providers/piper_local/piper_local.py:114-121

Details
remaining_text = full_text[self.processed_chars:] (absolute index), then
self.processed_chars += len(full_text). Post-condition should be processed_chars == len(full_text).
Masked by the per-turn FIRST reset, but a LAST without an intervening FIRST drops the tail.

Proposed fix
self.processed_chars = len(full_text) (or += len(remaining_text)) in both files. Consider hoisting
the duplicated method into a shared mixin. Add the packages to coverage + a unit test.

Suggested labels: bug, area:tts
```

---

## [MEDIUM] fun_local crashes in __init__ when output_dir is unset

```
Summary
FunASR fails hard on a config that WhisperLocal tolerates, because os.makedirs(None) raises TypeError.

Location
custom-providers/asr/fun_local.py:51-58

Details
self.output_dir = config.get("output_dir") returns None when absent, then
os.makedirs(self.output_dir, exist_ok=True) is called unconditionally. whisper_local.py:48 guards
with `if self.output_dir:`; fun_local does not.

Proposed fix
Guard the call: `if self.output_dir: os.makedirs(self.output_dir, exist_ok=True)`.

Suggested labels: bug, area:asr
```

---

## [MEDIUM] ASR model invoked from worker threads with no lock — not thread-safe under concurrent transcription

```
Summary
faster-whisper / FunASR are not safe for concurrent calls on the same model object; two overlapping
sessions can corrupt output or segfault the container.

Location
custom-providers/asr/whisper_local.py:107-109,161-173
custom-providers/asr/fun_local.py:81-88 (mirror)

Details
speech_to_text offloads inference via asyncio.to_thread on a singleton model shared across
connections; to_thread schedules concurrent calls on separate threads racing the underlying
CTranslate2 / FunASR C++ state.

Proposed fix
Serialize inference with a per-instance threading.Lock held inside _transcribe_blocking / around
model.generate. Negligible latency for the single-device case, prevents the concurrent crash.

Suggested labels: bug, area:asr
```

---

## [MEDIUM] before_stop play files dropped when the final segment's synthesis fails

```
Summary
A LAST-segment synth failure abandons any queued before_stop_play_files (e.g. a play_song trailing
asset) — they are neither played nor cleared, and can leak into the next utterance.

Location
custom-providers/edge_stream/edge_stream.py:139-143
custom-providers/piper_local/piper_local.py:187-191 (mirror)

Details
The normal path calls _process_before_stop_play_files() when is_last; the except handler never does.

Proposed fix
In the except handler, when is_last is True, still call self._process_before_stop_play_files() (and
clear the list) so trailing assets are honored or cleaned up.

Suggested labels: bug, area:tts
```

---

## [MEDIUM] SentenceType.FIRST audio marker is emitted once per segment instead of once per reply

```
Summary
A multi-segment reply pushes multiple FIRST markers, which downstream treats as start-of-response
and can re-trigger the talk animation / state mid-sentence.

Location
custom-providers/edge_stream/edge_stream.py:101
custom-providers/piper_local/piper_local.py:146 (mirror)

Details
text_to_speak unconditionally puts (SentenceType.FIRST, [], text) at the top of every call; each
segment is a separate text_to_speak call.

Proposed fix
Gate FIRST so it is emitted only for the first segment of a reply (track a per-reply
first_segment_sent flag, otherwise emit a continuation/MIDDLE marker).

Suggested labels: bug, area:tts
```

---

## [MEDIUM] Face recognizer reads the camera frame buffer after releasing the arbiter (latent UAF)

```
Summary
processFrame() releases the detection arbiter, then still uses the raw V4L2 frame pointer; once the
ESP-DL embedding crop is wired in, this becomes a live use-after-free / torn-frame read across the
two camera tasks.

Location
firmware/firmware/main/stackchan/face/face_detector.cpp:236-292

Details
arbiter.releaseForDetection() at :236, but frame_data (captured at :183) is stored into face_img.data
(:277) and passed to recognize() (:292). After release, acquireForCapture()/StreamCaptures() can
dequeue/requeue the V4L2 buffer and overwrite the memory frame_data points into.

Proposed fix
Hold the detection lock across recognize() (move releaseForDetection() after the recognition block),
OR copy the bounded face crop into a heap buffer while still holding the arbiter and point
face_img.data at the copy. Never pass a driver-owned frame pointer to a consumer that runs after release.

Suggested labels: bug, area:firmware, safety
```

---

## [MEDIUM] SoundLocalizer high-pass filter state goes stale during cooldown, firing spurious direction changes

```
Summary
The HPF state freezes during the 750ms cooldown, so the first post-cooldown frame injects a large
artificial transient that can push energy past threshold and fire a spurious sound_event direction change.

Location
firmware/firmware/main/stackchan/sound_localizer.cpp:19-23,44-51

Details
OnStereoFrame() returns early on the cooldown gate BEFORE the per-sample loop that advances
_hp_*_prev_x/_hp_*_prev_y. The first frame after cooldown evaluates x[n] - x[n-1] across the 750ms
discontinuity.

Proposed fix
Advance the HPF state on every frame regardless of the gates (move the filter loop above the cooldown
return, keeping only the emit gated), or seed _hp_*_prev_x to the new frame's first sample so the next
post-cooldown frame doesn't see a step.

Suggested labels: bug, area:firmware
```

---

## [MEDIUM] ImuEventModifier steals and releases the shared modify-lock it may not own

```
Summary
A shake during a photo releases Capture()'s modify-lock while the photo is in progress, letting the
head move during capture and corrupting the still.

Location
firmware/firmware/main/stackchan/modifiers/imu.h:58-61,117,79,105

Details
IMU sets the motion lock only if not held (lines 58-61), but restore_state() ALWAYS clears it (117).
If Capture() already holds the lock when a shake arrives, IMU skips re-locking but its restore
releases Capture's lock. The avatar lock has the same shape.

Proposed fix
Give the modify-lock real ownership (refcount/owner-token), or have IMU remember whether it actually
acquired the lock (bool _took_motion_lock) and only release in restore_state() if it took it.

Suggested labels: bug, area:firmware
```

---

## [MEDIUM] FaceDetector::stop() frees buffer and deletes the stop-semaphore without checking the take succeeded

```
Summary
If the detector task overruns the 2s stop timeout, stop() deletes the semaphore and frees the buffer
the still-running task owns — a use-after-free of both.

Location
firmware/firmware/main/stackchan/face/face_detector.cpp:100-117

Details
stop() does xSemaphoreTake(_stop_sem, 2000ms) WITHOUT checking the return, then unconditionally nulls
_task_handle, vSemaphoreDelete(_stop_sem), and heap_caps_free(_rgb_buffer). The live task then gives a
deleted semaphore and uses freed state. Currently unwired (only start()/setEnabled() are used).

Proposed fix
Capture the take result; only delete the semaphore / free the buffer / null the handle when it
returned pdTRUE. On timeout, log and leak (or loop with a longer bound) rather than tearing down
resources the live task still owns.

Suggested labels: bug, area:firmware
```

---

## [MEDIUM] startToChat: missing "language" key throws KeyError and feeds raw JSON to the pipeline

```
Summary
For a voiceprint payload lacking a language field, the speaker-extraction silently fails and the raw
JSON envelope is run through ASR corrections / intent detection / the LLM.

Location
receiveAudioHandle.py:1057-1063

Details
The speaker block does _language_tag = data["language"] (subscript) inside a try with
`except (json.JSONDecodeError, KeyError): pass`. When speaker+content are present but language is
absent, line 1059 raises KeyError before actual_text is assigned, so actual_text stays the raw JSON
string and current_speaker is never set.

Proposed fix
Use _language_tag = data.get("language") so a missing language key is tolerated; only speaker/content
should be required (already gated by the `in` checks).

Suggested labels: bug, area:xiaozhi
```

---

## [MEDIUM] provision.py: read-only permission overwrites never re-applied to existing channels

```
Summary
The script isn't idempotent for permissions: a READ_ONLY channel that already exists keeps
@everyone send_messages, with the script reporting "= #... (exists)".

Location
community/discord/provision.py:197-202

Details
The existing-channel branch only fixes the category, then continues — it never calls overwrites_for()
or applies overwrites. So missing/incorrect permission overwrites are never reconciled, a
security-relevant gap for a public community server.

Proposed fix
In the existing branch, compute ov = overwrites_for(...) and, when non-empty, await
existing.edit(overwrites=ov) so permissions converge on every run.

Suggested labels: bug, area:community
```

---

## [MEDIUM] dotty_doctor check_http treats 404 (and all <500) as pass for the OTA endpoint

```
Summary
A 404 on /xiaozhi/ota/ — exactly the misconfiguration the doctor exists to catch — is reported as PASS.

Location
scripts/dotty_doctor.py:166-168

Details
check_http returns 'pass' for any HTTP status < 500. The OTA path returning 404 (route wrong/missing,
or server up but not serving OTA) passes silently.

Proposed fix
Treat 2xx (and arguably 3xx) as pass; flag 4xx as warn/fail with the status code in the detail.

Suggested labels: bug, area:tooling
```

---

## [MEDIUM] MCP JSON-RPC request ids collide for calls in the same millisecond

```
Summary
Two MCP calls dispatched within the same millisecond get identical JSON-RPC ids; any relay that
correlates responses by id will mismatch or drop the colliding pair.

Location
receiveAudioHandle.py:281,319,349,373,399,456,559

Details
Every outbound MCP call computes id = int(time.time()*1000) % 0x7FFFFFFF. This happens routinely:
_sync_toggles_once fires set_toggle(kid_mode) then set_toggle(smart_mode) back-to-back, and
execute_choreography sends a HEAD and a LED at the same t_ms timeline mark.

Proposed fix
Use a monotonic per-connection counter (itertools.count / conn._mcp_id) via a shared
_next_mcp_id(conn) helper, fixing all seven sites at once.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] dotty_doctor passes Piper without the required .onnx.json config pair

```
Summary
A missing/stub .onnx.json (the exact corrupt-download failure the SenseVoice check was hardened
against) still reports a green PASS while the TTS provider crashes at runtime.

Location
scripts/dotty_doctor.py:146-155

Details
check_models_piper only globs *.onnx; Piper requires a companion <model>.onnx.json. The glob *.onnx
does not match *.onnx.json, so the config is never inspected.

Proposed fix
For each *.onnx found, assert the sibling *.onnx.json exists above a sane byte floor (~200 B),
mirroring the SenseVoice size-floor approach.

Suggested labels: bug, area:tooling
```

---

## [LOW] render_singing_sinsy crashes with ZeroDivisionError on empty/all-comment lyrics

```
Summary
A lyrics file that is empty or comments-only crashes build_singing_score with an opaque
ZeroDivisionError instead of a clean error.

Location
scripts/render_singing_sinsy.py:89 (reachable via load_lyrics 48-61)

Details
load_lyrics skips blank/comment lines and can return []. build_singing_score guards the note list but
not the syllable list; line 89 does syllables[i % len(syllables)] with len==0.

Proposed fix
After load_lyrics in main() (or at the top of build_singing_score), add a guard:
if not syllables: print error; return 1. Mirror the existing 'has no notes' guard.

Suggested labels: bug, area:tooling
```

---

## [LOW] _send_led_color swallows all exceptions silently

```
Summary
A broken WS during a dance silently no-ops every LED update with no log, making "dance ran but no
lights" undebuggable.

Location
receiveAudioHandle.py:285-286

Details
_send_led_color wraps the websocket.send in a bare `except Exception: pass` with no logging, unlike
_send_led_multi which warns-once.

Proposed fix
Log the exception (warn-once, matching _send_led_multi's pattern) rather than a silent pass.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] _encode_midi_to_opus forces ALL set_tempo events to one value

```
Summary
Songs whose MIDI encodes legitimate mid-piece tempo changes are flattened to a single constant
tempo, breaking duration/beat alignment for any such file.

Location
receiveAudioHandle.py:850-857

Details
When target_tempo_bpm is set, the loop rewrites every set_tempo in every track to new_tempo. Benign
for the current .mid registry, latent for any added song with tempo automation.

Proposed fix
Rewrite only the first/global tempo, or scale all tempos by the same ratio relative to the original
first tempo.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] _handle_dance: singing stream task untracked, so a new utterance cancels choreography but not the audio

```
Summary
On barge-in the choreography is cancelled but the singing audio keeps streaming Opus to the device,
relying entirely on the abort flag being set synchronously.

Location
receiveAudioHandle.py:789-802,1092-1096

Details
Choreography runs as conn._dance_task (tracked, cancelled on the next turn); the singing audio runs as
a separate fire-and-forget asyncio.create_task whose handle is never stored on conn.

Proposed fix
Store conn._singing_task and cancel it alongside _dance_task on the new-utterance/abort path.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] Idle end-prompt re-enters the full intent pipeline via startToChat

```
Summary
A customised idle end-prompt containing words like "dance"/"sleep"/a vision phrase fires that
side-effect at the moment the connection is closing.

Location
receiveAudioHandle.py:1208-1211

Details
no_voice_close_connect feeds end_prompt.prompt through startToChat, which runs _detect_state_phrase /
_is_vision_request / _is_dance_request. close_after_chat is already set, so the behaviour competes with
shutdown.

Proposed fix
Submit the end-prompt directly to the LLM (_submit_chat / conn.chat) bypassing the intent pipeline, or
add a flag to startToChat to skip state/dance/vision detection for system-originated prompts.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] provision.py create_forum drops the computed permission overwrites

```
Summary
The FORUM branch omits overwrites=ov, silently no-opping a future read-only forum lockdown.

Location
community/discord/provision.py:204-213

Details
overwrites_for is computed for every new channel and the text branch passes overwrites=ov, but
guild.create_forum(...) omits it. Benign today (no forum in READ_ONLY).

Proposed fix
Pass overwrites=ov to guild.create_forum(...), matching the text-channel branch.

Suggested labels: bug, area:community
```

---

## [LOW] /api/perception/state emits invalid JSON (Infinity) for devices with no last_event_t

```
Summary
A device with no last_event_t serializes sensor_age_s as the bare token Infinity, which is not valid
JSON and breaks strict parsers / the dashboard perception card.

Location
dotty-behaviour/perception/state.py:328-336 (reachable via routes/perception.py:73-85, routes/vision.py:188)

Details
_annotate sets age = float('inf') when last_event_t is absent; Starlette's JSONResponse serializes
with allow_nan=True. Reachable for a known device that got a room_view capture but never sent a
perception event, and unconditionally for any unknown device_id.

Proposed fix
Replace age = float('inf') with a JSON-safe value (None / -1) and rely on sensor_stale=True; add a
regression test that the route body parses as standard JSON.

Suggested labels: bug, area:behaviour
```

---

## [LOW] Failed/empty weather fetch arms the full 30-min TTL

```
Summary
A transient weather failure marks the cache fresh for the full TTL, so no retry happens and prompts
keep using stale (or empty) weather text.

Location
dotty-behaviour/calendar_/cache.py:124-127 (set_weather); driven by poll.py:46-48

Details
fetch_weather returns "" on any failure; set_weather only overwrites weather_text on truthy text but
UNCONDITIONALLY bumps weather_fetched_perf, so ttl_expired stays false for the next full TTL window.

Proposed fix
Advance weather_fetched_perf only on a successful (non-empty) fetch (guard the bump behind `if text:`),
or use a short retry interval on empty. Add a test asserting an empty fetch leaves the timestamp
unchanged.

Suggested labels: bug, area:behaviour
```

---

## [LOW] No upload size limit on /api/vision/explain and /api/audio/explain (LAN OOM)

```
Summary
Both endpoints read the entire multipart upload into memory (+ ~1.33x for base64) with no size cap,
so a single large/malicious POST can exhaust container memory and take down the daemon.

Location
dotty-behaviour/routes/vision.py:145 (and audio.py:87)

Details
await file.read() reads the whole upload; no Content-Length cap, no streaming. Both endpoints bind
0.0.0.0:8090. The JPEG is also retained per-device in vision_cache.

Proposed fix
Check Content-Length up front and reject >N (e.g. 5 MB JPEG, 10 MB audio) with 413, or read in
bounded chunks. Apply to both endpoints.

Suggested labels: bug, security, area:behaviour
```

---

## [LOW] Consumer task crashes are silently swallowed (no log, no restart)

```
Summary
A consumer's run() raising at runtime dies silently and the daemon keeps reporting "ready" while the
consumer is dead.

Location
dotty-behaviour/main.py:325-341

Details
Consumers are launched with asyncio.create_task and only awaited at shutdown via
gather(..., return_exceptions=True), which captures and discards exceptions. No done-callback surfaces
them at failure time; no restart.

Proposed fix
Attach a done-callback that logs non-cancelled exceptions at failure time; optionally wrap each
consumer in a supervising restart loop.

Suggested labels: bug, area:behaviour
```

---

## [LOW] vision_latest lost-wakeup race + unconditional fresh-capture eviction

```
Summary
A concurrent explain that signals between the cache pop and waiter registration is missed (spurious
404), and the unconditional pop discards a fresh capture another producer just wrote.

Location
dotty-behaviour/routes/vision.py:331-352,332

Details
vision_latest pops the cache, registers a waiter, then awaits the Event; signal_vision_waiters only
set()s already-registered Events, so a signal in the pop→register window is lost and the waiter 404s
at the 15s timeout. The pop has no freshness check, so it deletes a fresh idle-photographer/room_view
entry (and its jpeg_bytes that /api/vision/photo serves).

Proposed fix
Make the wakeup level-triggered (re-check the cache after registering before awaiting, or use a
generation counter). Pop only entries actually older than the TTL.

Suggested labels: bug, area:behaviour, area:vision
```

---

## [LOW] Failed room_view VLM call still arms the 120s cooldown

```
Summary
A transient VLM blip arms the full 120s room_view cooldown despite never producing a usable
description, disabling named greetings for minutes.

Location
dotty-behaviour/routes/vision.py:188-204

Details
last_room_view_capture_t is set BEFORE vlm.describe_image, which never raises (returns
VLM_NETWORK_ERROR_SENTINEL / VLM_OFFLINE_SENTINEL on failure). So an unreachable VLM still records the
timestamp and gates further captures for 120s.

Proposed fix
Set last_room_view_capture_t only after a successful, non-sentinel VLM response, or use a much shorter
failure-cooldown.

Suggested labels: bug, area:behaviour, area:vision
```

---

## [LOW] VLM error/offline SENTINEL strings cached and leaked into the perception snapshot

```
Summary
A single VLM outage poisons Dotty's voice prompt for up to 60s on subsequent turns by caching the
loud-error sentinel as a real description.

Location
dotty-behaviour/routes/vision.py:195-242 (sentinels in dispatch/vlm.py:22-29)

Details
vision_explain stores VLM_OFFLINE_SENTINEL / VLM_NETWORK_ERROR_SENTINEL verbatim as
vision_cache[device_id]["description"] with a fresh wall_ts. perception/snapshot.py:142-147 then
surfaces it as `You see: ERROR: the vision service didn't respond...`, and the idle photographer can
persist it to NDJSON.

Proposed fix
Detect the sentinel set after describe_image and skip the cache write + signal_vision_waiters on
sentinel returns. Same guard for the audio fallback string.

Suggested labels: bug, area:behaviour, area:vision
```

---

## [LOW] perception/state limit query params unvalidated (negative/zero slice quirks)

```
Summary
A client-supplied limit flows unbounded into list slices; limit=-3 drops the 3 newest events and
balances[-0:] returns the FULL series.

Location
dotty-behaviour/routes/perception.py:88-99,124

Details
perception_recent forwards limit into items[:limit] (state.py:152) with no >=0 bound; sound_balance_series
ends with balances[-limit:] where limit=0 returns the whole list.

Proposed fix
Clamp limit = max(0, min(limit, MAX)); special-case limit <= 0 (return []) before the balance slice.

Suggested labels: bug, area:behaviour
```

---

## [LOW] Client-supplied event ts can be set in the future, defeating staleness gates

```
Summary
A future ts makes age negative → clamped to 0, so get_fresh_face_id treats a stale identity as
permanently fresh and staleness never trips. ESP32 RTCs are frequently unsynced.

Location
dotty-behaviour/routes/perception.py:61-68

Details
perception_event uses payload.ts verbatim. Downstream freshness logic computes age = now - last_t with
max(0.0, ...) clamps or direct > ttl comparisons.

Proposed fix
Reject or clamp implausible future timestamps on ingest (substitute time.time() if >few seconds
ahead). Guard get_fresh_face_id against negative age (treat as stale).

Suggested labels: bug, area:behaviour
```

---

## [LOW] perception/state stale dance_active never cleared on a missed terminal event

```
Summary
A dropped dance_ended/state_changed latches dance_active True indefinitely, permanently suppressing
room_view captures and greetings for that device.

Location
dotty-behaviour/perception/state.py:185-199

Details
dance_active is set on dance_started / state_changed->dance and cleared only on a terminal event; the
bus drops events on a full subscriber queue, so a lost terminal frame strands the flag. No time-based
expiry.

Proposed fix
Make is_dance_active freshness-bounded against last_dance_started_t with a max-dance TTL.

Suggested labels: bug, area:behaviour
```

---

## [LOW] Greeter day-GC discards other days' slots; midnight-roll cooldown corruption

```
Summary
The greeter persists never more than one day of slots, and a perception event spanning midnight can
slip a near-instant second greeting through.

Location
dotty-behaviour/greeter/greeter.py:224-228

Details
_take_slot derives today from wall-now but uses event_ts for cooldown; the GC collapses _state to
{today: ...} (then _save_state persists the truncated dict). A pre-midnight event handled just after
midnight is filed under the new day with old-day cooldown math.

Proposed fix
Key the day on event_ts; GC by dropping only days older than a retention window (keep today + yesterday).

Suggested labels: bug, area:behaviour
```

---

## [LOW] SecurityCycle keeps capturing on a missed/early transition

```
Summary
A dropped non-security state_changed leaves the per-device capture loop firing the camera forever, and
a security session already live when the consumer subscribes never starts the timer.

Location
dotty-behaviour/consumers/security_cycle.py:238-257

Details
Capture is started/stopped purely off live state_changed events with no reconciliation against
current_device_state.

Proposed fix
On subscribe, seed timers from current per-device current_state. Reconcile each running timer against
current_device_state per cycle and self-cancel when no longer 'security'.

Suggested labels: bug, area:behaviour, safety
```

---

## [LOW] calendar fetch time window excludes the final minute of the day

```
Summary
Late-evening events near midnight can be silently missing because timeMax is exclusive and set to 23:59:59.

Location
dotty-behaviour/calendar_/fetch.py:60-66

Details
time_max = now.replace(hour=23,minute=59,second=59,microsecond=0). The Calendar API timeMax is
exclusive; the conventional correct bound is the start of the next local day.

Proposed fix
time_max = (start_of_today + timedelta(days=1)).isoformat().

Suggested labels: bug, area:behaviour, area:calendar
```

---

## [LOW] CalendarCache.flush_for_new_day produces a long empty-context window on day-roll fetch failure

```
Summary
A failing day-roll refresh leaves a guaranteed-empty calendar and, with backoff, can show no events
for up to 10 minutes in the morning even though yesterday's data was serviceable a moment earlier.

Location
dotty-behaviour/calendar_/cache.py:116-122 + poll.py:53-75

Details
refresh_if_stale eagerly flushes events then attempts the fetch; on failure the except only bumps
calendar_failures, and _sleep_seconds escalates to up to 600s.

Proposed fix
Flush only on a successful fetch, or reset calendar_failures / use the base interval for the first
retry after a day-roll flush.

Suggested labels: bug, area:behaviour, area:calendar
```

---

## [LOW] room_view 'no one in view' sentinel uses substring match

```
Summary
A valid identification whose DESC merely mentions "no one in view" is discarded as an empty frame,
throwing away both the description and the matched identity.

Location
dotty-behaviour/vision/room_view.py:152-153

Details
parse_room_view_response does `if ROOM_VIEW_NO_PERSON in cleaned.lower(): return None, None, None` — a
substring test against the whole reply.

Proposed fix
Match the sentinel only when it is the entire reply (modulo whitespace/punctuation); try the DESC/NAME
regex first and only fall back to the sentinel check on a format miss.

Suggested labels: bug, area:behaviour, area:vision
```

---

## [LOW] FaceIdentifiedRefresher keeps the green ID LED lit when identity came from room_view

```
Summary
A room_view identity keeps the green identified pip lit for the full TTL even after the person walked
off, because face_present is never set.

Location
dotty-behaviour/consumers/face_identified_refresher.py:65-71

Details
The skip gate `if not face_present and last_lost:` only suppresses when the face was both absent AND
previously lost. A room_view identification sets last_face_id without face_present and without a
face_lost, so the gate is never entered and the LED re-fires every interval.

Proposed fix
Treat "identity present but face_present False and never positively detected" as a stop condition
(skip refresh when not face_present, or require a positive face_present before keeping the pip lit).

Suggested labels: bug, area:behaviour
```

---

## [LOW] FaceGreeter consumes the greet cooldown even when suppressed by an active dance

```
Summary
A face event during a dance silently burns the per-identity/per-device cooldown without greeting, so a
person who arrives during a dance is denied their greeting both during the dance and for the full
cooldown after.

Location
dotty-behaviour/consumers/face_greeter.py:148-168,105-126

Details
The cooldown slot is written BEFORE the is_dance_active gate in both _handle_face_recognized and
_handle_face_detected. last_face_greet_t also arms face_lost_aborter.

Proposed fix
Move the is_dance_active (and empty-text / mic-only) checks ABOVE the cooldown-slot write in both
handlers.

Suggested labels: bug, area:behaviour
```

---

## [LOW] last_chat_t is never written on real conversations — QUIET_AFTER_CHAT gates are dead

```
Summary
The named-greet and sound-turner "don't fire while the user is mid-conversation" gates never fire,
because nothing records actual conversation activity.

Location
dotty-behaviour/consumers/face_greeter.py:149-156 + sound_turner.py:77-79 + perception/state.py:200-203

Details
Both gates read dev_state['last_chat_t'], but the only writer is purr_player.py (which pokes it into
the future). update_state handles chat_status by setting 'listening', not last_chat_t. Regression from
bridge.py's /api/message ingress.

Proposed fix
Write last_chat_t on conversation activity (set it on a chat_status 'listening' event and/or the
relayed user-utterance path), or drop the gates as inert.

Suggested labels: bug, area:behaviour
```

---

## [LOW] FaceLostAborter leaks completed abort tasks in _pending forever

```
Summary
A completed abort task with no subsequent face event stays in _pending, so the dict never shrinks for
departed devices.

Location
dotty-behaviour/consumers/face_lost_aborter.py:101-103,47-55

Details
_pending entries are only removed by a subsequent face_detected or a newer face_lost; a simple
fire-and-complete leaves a done task in the dict.

Proposed fix
Add a done-callback that discards the entry (matching the _tasks.discard pattern other consumers use).

Suggested labels: bug, area:behaviour
```

---

## [LOW] Vision room_view block-reason early return skips stale-cache eviction

```
Summary
On frequently-gated deployments, stale vision_cache entries for other devices are never reclaimed
because the block-reason early return bypasses the eviction loop.

Location
dotty-behaviour/routes/vision.py:176-186

Details
A gated room_view request writes a fresh entry and returns at :186, before the stale-eviction loop at
:273-279 that runs only on the normal path.

Proposed fix
Factor the stale-eviction loop into a helper and call it on every exit path, including the block-reason
early return.

Suggested labels: bug, area:behaviour
```

---

## [LOW] Blocking file I/O on the event loop in greeter state persistence

```
Summary
Every successful greet stalls the single event loop on synchronous disk I/O.

Location
dotty-behaviour/greeter/greeter.py:246,420-435

Details
_take_slot calls _save_state() synchronously (mkdir, tmp.write_text, os.replace) from the bus event
handler on the asyncio loop.

Proposed fix
await asyncio.to_thread(self._save_state) (the handler is already async), or batch/debounce persistence.

Suggested labels: bug, area:behaviour
```

---

## [LOW] SleepDreamer / DanceReflector dereference event.data without None-guard, crashing the consumer loop

```
Summary
A state_changed/dance_ended frame with data=None raises AttributeError caught only by the OUTER
try/except, which logs "crashed" and EXITS the consumer permanently.

Location
dotty-behaviour/consumers/sleep_dreamer.py:166-168; dance_reflector.py:89-92

Details
Both do `.get` directly on event.data; sibling consumers use `(event.data or {})`. The except is
outside the while loop, so one bad frame kills the consumer for the process lifetime.

Proposed fix
Use (event.data or {}).get(...); wrap per-event handling so a single bad frame doesn't terminate the
loop. Consider giving PerceptionEvent.data a default_factory=dict.

Suggested labels: bug, area:behaviour
```

---

## [LOW] HouseholdRegistry.get_by_calendar_prefix only matches with bracketed YAML

```
Summary
A household member whose calendar_prefix is configured without brackets is unreachable by
get_by_calendar_prefix.

Location
dotty-behaviour/household/registry.py:190-200 vs 293-294

Details
_reload indexes by_prefix[person.calendar_prefix.strip().lower()] verbatim, but get_by_calendar_prefix
bracket-normalizes the query (`if not key.startswith('['): key = f'[{key}]'`). The two agree only when
the YAML value already has brackets. (No non-test callers today, so latent.)

Proposed fix
Normalize both sides identically (bracket-normalize calendar_prefix when building by_prefix, or strip
brackets on both). Add a test for both YAML forms.

Suggested labels: bug, area:behaviour
```

---

## [LOW] Person.days_until_birthday shifts Feb-29 birthdays to Feb-28, firing the greeting a day early

```
Summary
In non-leap years a Feb-29 birthday is treated as Feb-28, so the greeter says "It is X's birthday
today" a day early.

Location
dotty-behaviour/household/registry.py:82-92

Details
self.birthdate.replace(year=ref.year) raises for Feb-29 in a common year and the except falls back to
date(ref.year, month, 28); days_until_birthday()==0 then fires on Feb-28.

Proposed fix
Decide and document a deliberate Feb-29 policy (roll to Mar-1 in common years, or keep Feb-28 but make
it intentional and consistent across both branches).

Suggested labels: bug, area:behaviour
```

---

## [LOW] Admin/behaviour HTTP clients disarm the timeout before reading the response body

```
Summary
A server that sends headers then stalls the body hangs the voice tool forever — no timeout fires, no
exception is thrown, so the try/catch fallbacks never run and the whole voice turn wedges.

Location
dotty-pi-ext/src/lib/xiaozhi_admin.ts:33-46; dotty-pi-ext/src/lib/dotty_behaviour.ts:20-37

Details
adminFetch/behaviourFetch arm an AbortController timer, await fetch() (resolves on headers), then clear
the timer in finally — before any caller reads the body (fetchSongCatalog .json(), playAsset .text(),
fetchTakePhoto/fetchPersonReviewStatus .json()). The sibling llama_swap.ts:42-71 does this correctly.

Proposed fix
Keep the timer armed across the body read: move the body read inside adminFetch/behaviourFetch (return
parsed JSON/text) so the existing try/finally covers it, mirroring llama_swap.ts.

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] play_song caches an empty catalogue on transient fetch failure for 60s

```
Summary
A single transient xiaozhi hiccup pins an empty catalogue for the full 60s TTL, so play_song returns
"(song catalogue is empty)" for up to a minute after recovery.

Location
dotty-pi-ext/src/tools/play_song.ts:83-90

Details
getCatalog unconditionally caches fetchSongCatalog's result, which returns [] on ANY failure — it
cannot distinguish "no songs" from "fetch failed".

Proposed fix
Only cache non-empty results: if fresh.length > 0, cache; else return fresh without caching so the next
turn retries.

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] play_song matchSong: empty stem from a dotfile matches any query, diverging from the oracle

```
Summary
A leading-dot catalogue entry matches every query and can win the best-candidate race, diverging from
the Python oracle and violating the byte-equal contract.

Location
dotty-pi-ext/src/tools/play_song.ts:39-74

Details
stemLower computes the stem via lastIndexOf('.'); for '.mp3', dot===0 so the stem is '', and
qStem.includes('') is always true. Python's os.path.splitext('.mp3') keeps '.mp3'.

Proposed fix
Match splitext semantics: only strip an extension when the dot index > 0 (`dot > 0 ? lower.slice(0,dot) : lower`).
Guard the substring branch against an empty stem. Add a leading-dot fixture to play_song.test.ts.

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] think_hard truncates by UTF-16 code units, diverging from the oracle and sibling tools

```
Summary
think_hard caps the reply with .slice (UTF-16 code units) while the oracle and the other tools use
codepoint slicing, so an emoji-prefixed near-cap reply mis-cuts.

Location
dotty-pi-ext/src/tools/think_hard.ts:72-77

Details
content.trim().slice(0, MAX_OUTPUT_CHARS). bridge.py oracle uses Python codepoint slicing [:500].
memory_lookup/recall_person/remember/remember_person all use Array.from codepoint slicing; think_hard
is the lone inconsistent path.

Proposed fix
Use codepoint-aware truncation like the sibling tools:
const cp = Array.from(content.trim()); cp.length > MAX ? cp.slice(0, MAX).join('') : content.trim();

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] play_song host-presence guard ignores the _XIAOZHI_HOST fallback

```
Summary
play_song refuses early with "(can't reach xiaozhi-server)" in an environment where only _XIAOZHI_HOST
is set, even though the admin client would reach the server.

Location
dotty-pi-ext/src/tools/play_song.ts:104

Details
The guard checks `!opts.host && !process.env.XIAOZHI_HOST`, but DEFAULT_HOST resolves
XIAOZHI_HOST ?? _XIAOZHI_HOST ?? 'localhost'. (No setter for _XIAOZHI_HOST today, so latent.)

Proposed fix
Use the same precedence as the admin client, or drop the guard and let fetchSongCatalog's empty-list
fallback drive the result.

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] extractTurnText concatenates assistant text blocks with no separator

```
Summary
Multi-message/multi-block assistant turns are joined with the empty string, fusing fragments
("Let me check…The answer is 4") in the logged turn.

Location
dotty-pi-ext/src/lib/turn_logger.ts:56-62 (join("")), 79-91

Details
extractTurnText joins assistantParts and TextContent items with "". No oracle exists for this
extraction (pure pi-ext logic).

Proposed fix
Join multi-message assistant text and TextContent items with a separator (a space, or "\n").

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] think_hard default model resolved at module load, diverging from the per-call oracle

```
Summary
DEFAULT_MODEL reads VOICE_THINKER_MODEL once at import; the oracle reads it per call, so a later env
change diverges the request body.

Location
dotty-pi-ext/src/tools/think_hard.ts:25

Details
DEFAULT_MODEL = process.env.VOICE_THINKER_MODEL ?? 'qwen3.6:27b-think' is evaluated at import. The
Python oracle resolves os.environ.get('VOICE_THINKER_MODEL', ...) on every call.

Proposed fix
Resolve the env lazily inside buildThinkRequest/runThinkHard at call time.

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] play_song getCatalog TOCTOU lets concurrent turns both refetch

```
Summary
Two interleaved play_song calls both see the stale timestamp and both issue a fetch; combined with the
empty-cache bug, one can poison the other.

Location
dotty-pi-ext/src/tools/play_song.ts:83-90

Details
getCatalog reads _catalogFetchedAt, awaits fetchSongCatalog(), then writes the cache; the pi agent can
run tool calls concurrently across the await.

Proposed fix
Store an in-flight Promise so concurrent callers await the same fetch, and cache only on a non-empty
successful result (also resolves the empty-cache bug).

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] turn_logger.formatTurnLog omits the final strip the oracle applies (user-only turn divergence)

```
Summary
Every user-only turn stores a row that is not byte-identical to the tuned-against oracle (trailing
space after "assistant:").

Location
dotty-pi-ext/src/lib/turn_logger.ts:98-102

Details
formatTurnLog returns `user: ${u} | assistant: ${a}` directly; the oracle stores content.strip(). When
the assistant text is empty (a logged user-only turn), TS produces "...assistant: " vs the oracle's
"...assistant:". The integration test that would catch it (assistant_empty_user_only) only runs when
DOTTY_BRAIN_DB_SNAPSHOT is set, which npm test never sets.

Proposed fix
return (`user: ${u} | assistant: ${a}`).trim(); wire the Layer-2 integration pass into npm test.

Suggested labels: bug, area:dotty-pi
```

---

## [LOW] Seqlock reader can return a torn read on weak memory (missing acquire barrier)

```
Summary
A torn read can pass the seqlock check because the data reads can sink below the second seq acquire-load.

Location
firmware/firmware/main/stackchan/face/face_detection_result.h:33-47

Details
read() loads s1 (acquire), copies the plain data fields, loads s2 (acquire), accepts when s1==s2. An
acquire load forbids later ops moving before it, not earlier plain reads from sinking below it. The
writer side is correct; the reader is missing the symmetric barrier. (Low real-world hit-rate on
Xtensa LX7.)

Proposed fix
Insert an acquire fence between the data copies and the second seq load:
read the fields, then std::atomic_thread_fence(std::memory_order_acquire); then s2 = seq.load(relaxed).

Suggested labels: bug, area:firmware
```

---

## [LOW] Idle-channel proactive reconnect churns close+reopen every ~30 ticks

```
Summary
A quiet-but-healthy idle channel close+reopens every ~30s indefinitely (a ~30s reconnect loop), each
iteration paying a 200ms delay + full TLS/WS handshake on the main task.

Location
firmware/firmware/patches/xiaozhi-esp32.patch:23-32

Details
The reconnect fires when clock_ticks_ % 30 == 0 && state==Idle && protocol_->IsTimeout(). IsTimeout()
is time-since-last-INCOMING; a reopened channel the server never sends on keeps IsTimeout() true. No
backoff, no recently-reconnected suppression.

Proposed fix
Gate on an actual dead-channel signal (IsAudioChannelOpened() false) or a last-reconnect-timestamp
backoff, or reset the incoming-frame clock on OpenAudioChannel() so a successful reopen clears IsTimeout().

Suggested labels: bug, area:firmware
```

---

## [LOW] CloseAudioChannel() blocks the calling task 200ms on every close

```
Summary
An added vTaskDelay(200ms) stalls whatever task calls close (incl. the main Run loop) on every channel
close, compounding the reconnect churn.

Location
firmware/firmware/patches/xiaozhi-esp32.patch:582

Details
The patch adds vTaskDelay(pdMS_TO_TICKS(200)) to WebsocketProtocol::CloseAudioChannel() after
websocket_.reset(). The intent (drain the FIN) is reasonable but should not be a hard sleep on a
shared task.

Proposed fix
Move the drain delay to only the reopen path that needs it (or poll socket state), or make close async,
rather than unconditionally sleeping 200ms on the caller's task.

Suggested labels: bug, area:firmware
```

---

## [LOW] PrivacyLeds::update() non-atomic RMW races the guard, latching the listening LED on

```
Summary
The green mic privacy LED can keep showing "listening" after the mic ADC closed, lying about live
capture state until the next repaint.

Location
firmware/firmware/main/stackchan/privacy/privacy_leds.cpp:49-60

Details
update() (tick task) load→derive→store of _mic_state is not atomic as a unit; MicPeripheralGuard's
dtor (codec-close task) setMicState(Off) between the load and store is overwritten. Fail-safe in
direction (over-indicates) but a privacy-indicator correctness bug.

Proposed fix
Make the reconciliation a CAS that only updates while non-Off: load s; if s==Off return; compute
desired; compare_exchange_strong(s, desired).

Suggested labels: bug, area:firmware
```

---

## [LOW] CameraPeripheralGuard ctor-fallback/dtor-normal mismatch underflows the refcount

```
Summary
A ctor that took the null-mutex fallback (no increment) followed by a normal dtor (fetch_sub) underflows
g_refcount to UINT32_MAX, so the stream never tears down and the camera LED stays Active indefinitely.

Location
firmware/firmware/main/stackchan/privacy/camera_peripheral_guard.cpp:40-66,68-86

Details
The null-mutex ctor fallback sets CameraState::Active and returns WITHOUT incrementing g_refcount; the
dtor unconditionally fetch_sub(1) on the normal path.

Proposed fix
Track per-guard whether the ctor incremented (bool _incremented) and only fetch_sub in the dtor when
true. Clamp/guard against fetch_sub when g_refcount==0.

Suggested labels: bug, area:firmware
```

---

## [LOW] MCP take_photo arms a fixed 10s camera-glow timer decoupled from capture duration

```
Summary
The cosmetic camera-activity glow is hardcoded to 10s with no off-call, so it lingers after a fast
capture or goes dark before a slow one.

Location
firmware/firmware/patches/xiaozhi-esp32.patch:523

Details
The take_photo patch adds GetHAL().setCameraLedActive(true, 10000) before camera->Capture(). The proper
privacy LED is driven by CameraPeripheralGuard; this glow no longer tracks the real lifecycle.

Proposed fix
Tie the glow to the capture scope: set it active without a timeout and clear it after Capture() returns,
or drive it from the CameraPeripheralGuard refcount transitions like the privacy pixel.

Suggested labels: bug, area:firmware
```

---

## [LOW] Unescaped name / session_id in Protocol::SendEvent JSON construction

```
Summary
SendEvent builds the perception frame by raw string concatenation; neither the event name nor the
server-supplied session_id is escaped, so any future runtime name or a quoted session id emits
malformed JSON the relay drops.

Location
firmware/firmware/patches/xiaozhi-esp32.patch:537-542

Details
Not currently exploitable (literal names, pre-escaped data, server-controlled session id), but SendEvent
is the documented thread-safe perception-emit boundary with no escaping/validation.

Proposed fix
Build the frame with cJSON (escapes both name and session_id; attach pre-validated data_json as a parsed
sub-object), or assert+escape and validate data_json parses.

Suggested labels: bug, area:firmware
```

---

## [LOW] Debug log leftovers fire at WARN/ERROR on every WS and LLM frame

```
Summary
An ESP_LOGW on every LLM frame and an ESP_LOGE on every inbound WS frame bypass log-level filtering,
spam serial, and bury real errors.

Location
firmware/firmware/patches/xiaozhi-esp32.patch:65,590

Details
Patch line 65 logs WARN on every LLM frame; line 590 logs ERROR on every inbound WS frame (hello, tts,
stt, llm, mcp, event acks).

Proposed fix
Drop these two lines from the patch, or demote to ESP_LOGD.

Suggested labels: bug, area:firmware
```

---

## [LOW] HeadPet/Imu gesture flags are plain volatile bool, not atomic; fixed order can invert fast sequences

```
Summary
A gesture can be lost (non-atomic read-then-clear across cores), and a fast release-then-press processed
in fixed press→release order ends with _is_touched=false, silently never arming the hold-to-listen window.

Location
firmware/firmware/main/stackchan/modifiers/head_pet.h:50-52 (and head_pet.cpp:48-83, imu.h:124)

Details
_event_press/_event_swipe/_event_release/_event_shake are volatile bools written from the HAL signal
callback (possibly a different core) and read+cleared on the tick task in a hardcoded order.

Proposed fix
Use std::atomic<bool> with exchange(false) to consume the flags, or post gestures through a FreeRTOS
queue so they are consumed in arrival order.

Suggested labels: bug, area:firmware
```

---

## [LOW] ParentalGate unlock at millis()==0 treated as "never unlocked"

```
Summary
A successful unlock in the first millisecond after boot stores 0, which isUnlocked() treats as the
"never" sentinel, so the unlock is silently dropped.

Location
firmware/firmware/main/stackchan/face/parental_gate.cpp:43,71-72

Details
_unlocked_at_ms uses 0 as the sentinel; tryUnlockByPIN/tryUnlockByLongPress store now_ms() which can be
0 right after boot. (Dormant scaffold.)

Proposed fix
Use a separate atomic<bool> _is_unlocked alongside the timestamp, or store max(now,1) and treat 0
strictly as "never".

Suggested labels: bug, area:firmware
```

---

## [LOW] CameraArbiter::acquireForCapture clears the shared _capture_pending on timeout

```
Summary
_capture_pending is a single shared atomic, not per-call; a future second capture path plus a timeout
could drop both callers' intent, wedging the other's still-pending capture.

Location
firmware/firmware/main/stackchan/face/camera_arbiter.cpp (header camera_arbiter.h:38-44)

Details
acquireForCapture() sets _capture_pending = true on entry and clears it to false on mutex-take timeout.
Latent today (one Capture() caller) but contradicts the header's "two concurrent consumers compose
cleanly".

Proposed fix
Make _capture_pending a refcount (fetch_add on entry, fetch_sub on timeout/release), or document+assert
single-caller.

Suggested labels: bug, area:firmware
```

---

## [LOW] Whisper odd-length PCM buffer raises ValueError instead of degrading

```
Summary
A truncated/short final VAD frame (odd byte count) discards the whole utterance as an empty transcript
with no retry.

Location
custom-providers/asr/whisper_local.py:104

Details
np.frombuffer(artifacts.pcm_bytes, dtype=np.int16) raises ValueError on an odd byte count, caught by the
broad except → ('', None). The failure is deterministic so the retry loop can't help.

Proposed fix
Trim to an even length before frombuffer:
buf = artifacts.pcm_bytes; buf = buf[: len(buf) - (len(buf) % 2)].

Suggested labels: bug, area:asr
```

---

## [LOW] get_emotion matches the first mapped emoji anywhere, not the leading emoji; default 🙂 outside the set

```
Summary
get_emotion can select a wrong/secondary face when a non-prefix emoji leaks into the reply, and its
default 🙂 is outside the firmware-recognized set.

Location
custom-providers/textUtils.py:146-152

Details
The protocol requires the FIRST char to be the emotion emoji, but get_emotion breaks on the first
EMOJI_MAP char wherever it appears; the default emoji='🙂'/emotion='happy' masks a missing prefix.

Proposed fix
Resolve emotion from the leading grapheme only (inspect text[0]), falling back to FALLBACK_EMOJI/😐
rather than 🙂/happy.

Suggested labels: bug, area:tts
```

---

## [LOW] piper effective_rate==24000 shortcut skips resampling when native rate isn't 24000

```
Summary
A non-24000 Hz voice whose pitch_scale lands effective_rate on 24000 emits raw native-rate PCM into a
24000 Hz encoder, playing back at the wrong speed/pitch.

Location
custom-providers/piper_local/piper_local.py:67-74

Details
The no-resample shortcut (self._up = self._down = 1) fires whenever effective_rate == 24000, conflating
"no pitch shift" with "no rate conversion". The common default voice en_GB-cori-medium is 22050 Hz.

Proposed fix
Gate the shortcut on the SOURCE rate: only _up=_down=1 when src_rate==24000 AND pitch_scale==1.0.
Otherwise always go through the gcd/resample branch (compute g=gcd(effective_rate,24000), up=24000//g,
down=effective_rate//g unconditionally).

Suggested labels: bug, area:tts
```

---

## [LOW] piper length_scale not validated for <= 0 (unlike pitch_scale)

```
Summary
A configured length_scale <= 0 yields empty/garbage audio with no early error, while pitch_scale raises
a clear ValueError.

Location
custom-providers/piper_local/piper_local.py:32-42

Details
pitch_scale is validated positive (33-36); length_scale is taken straight from config into
SynthesisConfig(length_scale=float(length_scale)) with no bounds check.

Proposed fix
Mirror the pitch_scale guard: if length_scale is not None and float(length_scale) <= 0, raise
ValueError. Construct self.syn_config only after the check.

Suggested labels: bug, area:tts
```

---

## [LOW] whisper metrics: lang_prob can be None with forced language, dropping the ASR-METRICS line

```
Summary
With a forced language, the #105 ASR-METRICS line is silently dropped on every utterance where
language_probability is None.

Location
custom-providers/asr/whisper_local.py:132-137

Details
getattr(_info, "language_probability", 0.0) only fires when the attribute is ABSENT, not present-but-None;
{lang_prob:.3f} then raises TypeError, swallowed by the surrounding try/except.

Proposed fix
lang_prob = getattr(_info, "language_probability", 0.0) or 0.0 (guards both absent and present-but-None).

Suggested labels: bug, area:asr
```

---

## [LOW] FIRST audio-start marker emitted even when synth yields zero audio

```
Summary
A mid-reply empty-synth segment emits a FIRST marker with no frames and no LAST, wedging per-sentence
marker accounting.

Location
custom-providers/edge_stream/edge_stream.py:101-115; piper_local.py:146-159 (mirror)

Details
text_to_speak puts (SentenceType.FIRST, [], text) before synthesis; on empty audio for a non-last
segment it returns without enqueueing any frames or a terminator.

Proposed fix
Emit FIRST only after confirming there is audio, or on the empty-synth early return also enqueue a
terminating marker so every FIRST is balanced. Apply to both files.

Suggested labels: bug, area:tts
```

---

## [LOW] security_watch defaults use retired RPi/zeroclaw paths and wrong port

```
Summary
security_watch's CONVO_LOG_DIR default disagrees with dashboard.py, and _BRIDGE_INTERNAL_URL points at
the wrong port (8080 vs the bridge's 8081).

Location
bridge/security_watch.py:85-101

Details
SECURITY_LOG_DIR defaults CONVO_LOG_DIR to /root/zeroclaw-bridge/logs (the retired RPi path) while
dashboard.py defaults it to /var/lib/dotty-bridge/logs; _BRIDGE_INTERNAL_URL defaults to
http://127.0.0.1:8080 while the bridge listens on 8081.

Proposed fix
Align the CONVO_LOG_DIR default with dashboard.py and fix _BRIDGE_INTERNAL_URL to :8081. Better: since
the whole consumer is unreachable, delete the module or gate it behind a clear 'not wired' guard.

Suggested labels: bug, area:dashboard
```

---

## [LOW] Dashboard live perception feed (EventSource '/api/perception/feed') 404s — endpoint ripped in #111

```
Summary
The dashboard's live perception EventSource 404s on the bridge (the endpoint was deleted in #111, the
consumer left behind), so the feed never streams, every dotty-refresh nudge is dead, and the browser
hammers a 404 reconnect every few seconds per open tab.

Location
bridge/templates/dashboard.html:564

Details
The page (served from :8081) opens EventSource('/api/perception/feed'), but the bridge serves no /api/*
routes. Commit 30d9113 added the endpoint; c6df5c5 (#111) deleted it and left the EventSource. The real
SSE lives on dotty-behaviour (routes/perception.py:127) at :8090 — a different origin.

Proposed fix
Add a bridge SSE passthrough route proxying dotty-behaviour's /api/perception/feed (under the /api/
CSRF-exempt prefix) so the relative URL resolves, OR point the EventSource at the dotty-behaviour origin
explicitly with CORS configured.

Suggested labels: bug, area:dashboard
```

---

## [LOW] play-asset / vision-photo proxy interpolate unencoded device_id into an internal URL

```
Summary
_fetch_robot_photo builds the dotty-behaviour URL with the raw device_id path-param (unvalidated,
unencoded), allowing query/fragment injection into the internal request.

Location
bridge/dashboard.py:201,638,1924

Details
f"{_DOTTY_BEHAVIOUR_URL}/api/vision/photo/{device_id}" with device_id from the URL; FastAPI path
matching blocks literal /, but query/fragment chars still alter the request target.

Proposed fix
urllib.parse.quote(device_id, safe='') before interpolation, and validate device_id against an expected
id charset (e.g. re.fullmatch(r'[A-Za-z0-9:_-]+', device_id)), returning 400 on mismatch.

Suggested labels: bug, area:dashboard
```

---

## [LOW] _dotty_behaviour_get caches the failure fallback for the full TTL

```
Summary
A single transient dotty-behaviour failure pins an empty {}/[] result for the cache TTL, so every
dashboard tile blanks for ~2s after each blip even after recovery.

Location
bridge.py:492-504

Details
On any timeout/connection/HTTP/JSON error the helper sets value = fallback and unconditionally writes it
into _dotty_behaviour_cache with the full TTL.

Proposed fix
Only populate the cache on success (move the write inside the try after r.json()), returning fallback on
the exception path without caching it.

Suggested labels: bug, area:dashboard
```

---

## [LOW] bridge writes to brain.db (RW) primarily owned by dotty-pi — approve/redact can lock or race

```
Summary
The bridge's memory approve/redact open brain.db RW across containers; in non-WAL delete mode a writer
blocks readers, and failures are swallowed and surface as a generic "not found".

Location
bridge.py:316,349

Details
_voice_memory_approve_blocking / _voice_memory_delete_blocking sqlite3.connect(str(_VOICE_MEMORY_DB))
RW (timeout=5) against brain.db, which dotty-pi-ext actively writes. The list path correctly uses
?mode=ro.

Proposed fix
Open with WAL-aware busy handling (PRAGMA busy_timeout); surface lock failures distinctly from
'not found'. Longer term, route approve/redact through dotty-pi (the canonical writer).

Suggested labels: bug, area:dashboard, area:dotty-pi
```

---

## [LOW] host_detail 'server' modal hardcodes 'Recent errors today: —' despite the count being computed elsewhere

```
Summary
The server host-detail modal shows a permanent dash for today's error count, even though the bridge
already tallies it for the alerts chip.

Location
bridge/dashboard.py:1832

Details
The modal renders ('Recent errors today', '—') with a TODO, but alerts_count() (485-500) already counts
rec.get('error') from the convo log.

Proposed fix
Factor the per-day error tally out of alerts_count() into a helper and reuse it. If the intent is
xiaozhi-container errors specifically, note that in the label rather than a bare '—'.

Suggested labels: bug, area:dashboard
```

---

## [LOW] PiVoiceLLM fallback strings violate the mandatory emoji-prefix protocol

```
Summary
PiVoiceLLM's server-generated fallback strings begin with '(', so the firmware emotion parser sees '('
and produces no/garbage face state.

Location
custom-providers/pi_voice/pi_voice.py:130,150

Details
yield "(empty turn)" and yield "(brain offline — try again in a moment)" bypass the persona and lack an
emoji prefix. OpenAICompat correctly prefixes all its fallbacks with FALLBACK_EMOJI.

Proposed fix
Prepend an allowed emoji to both fallbacks (e.g. "😐 (empty turn)", "😐 (brain offline — try again ...)"),
or import ensure_emoji_prefix/FALLBACK_EMOJI from the shared textUtils.

Suggested labels: bug, area:voice
```

---

## [LOW] new_session response match does not verify request id

```
Summary
new_session accepts a stale response from a previously-timed-out call because the drain loop never
checks the request id.

Location
custom-providers/pi_voice/pi_client.py:192-209

Details
new_session sends req_id but the drain loop matches only type=='response' and command=='new_session',
unlike iter_turn_text which matches the accept-ack by id.

Proposed fix
Add `and frame.get('id') == req_id` to the match condition.

Suggested labels: bug, area:voice
```

---

## [LOW] PiVoiceLLM _first_turn desyncs from the pi process after a respawn

```
Summary
If pi dies after turn 1, the next turn runs new_session() against a freshly respawned process that has
no session, producing a spurious 10s timeout and dead air.

Location
custom-providers/pi_voice/pi_voice.py:136-141

Details
_first_turn is tracked in PiVoiceLLM but the spawn decision lives in PiClient._ensure_started(); after a
respawn _first_turn is False so new_session runs and waits up to 10s for an ack a clean process may
never send.

Proposed fix
Have PiClient signal whether _ensure_started actually spawned a fresh process and skip new_session() in
that case (or move the freshly-spawned suppression into PiClient.new_session()).

Suggested labels: bug, area:voice
```

---

## [LOW] iter_turn_text process-exit detection only runs on queue-empty

```
Summary
A pi crash with frames still queued is reported as a generic 120s turn timeout instead of "pi process
exited", delaying the user-facing fallback by up to 2 minutes.

Location
custom-providers/pi_voice/pi_client.py:222-269

Details
The "pi process exited mid-turn" check is only reached inside the `except Empty` branch; if pi emits
some frames then dies, the loop drains them without hitting Empty and spins to the turn timeout.

Proposed fix
Check self._proc.poll() is not None on every loop iteration and raise "pi process exited mid-turn"
immediately when the process is gone and no agent_end has been seen.

Suggested labels: bug, area:voice
```

---

## [LOW] _next_id increments shared counter without the lock; reader-thread frame routing races a respawn

```
Summary
A respawn swaps _event_queue while an old, never-joined reader thread can still put stale frames into
the new queue, injecting a previous process's frames into the new turn.

Location
custom-providers/pi_voice/pi_client.py:283-285,149-169

Details
_next_id mutates _next_req_id outside _lock; _ensure_started() replaces _event_queue on every respawn;
the stdout reader routes frames by reading self._event_queue live without the lock.

Proposed fix
Join (or signal-stop) the prior reader/stderr threads before swapping _event_queue, bind each reader to
its specific Queue/proc instance (pass as args), and take the lock around _next_req_id mutation.

Suggested labels: bug, area:voice
```

---

## [LOW] _completions_url mishandles a base ending in /chat/completions

```
Summary
A base_url whose path contains but doesn't end in /chat/completions (e.g. a gateway route
/chat/completions/stream) gets a second /chat/completions appended.

Location
custom-providers/openai_compat/openai_compat.py:125-132

Details
The endsWith heuristic on an already-rstripped base appends /chat/completions when the base path
contains the literal but isn't exactly it.

Proposed fix
Make the endpoint construction explicit via a config flag (url is base vs full endpoint), or document
that url must be the base and drop the endswith special-case.

Suggested labels: bug, area:voice
```

---

## [LOW] OpenAICompat yields leading whitespace before emoji-prefix engages

```
Summary
A whitespace-only first chunk is yielded to TTS before enforcement, so the first TTS chunk isn't the
emoji the contract promises.

Location
custom-providers/openai_compat/openai_compat.py:218-229

Details
Enforcement fires only once so_far = ''.join(full_text).lstrip() is non-empty; a leading-whitespace
content chunk is still emitted via yield content while emoji_checked is False.

Proposed fix
Buffer (do not yield) content until so_far is non-empty and the emoji check has run; strip leading
whitespace from the first emitted chunk.

Suggested labels: bug, area:voice
```

---

## [LOW] OTAHandler.handle_post finally-block can swallow a secondary exception

```
Summary
handle_post's finally calls _add_cors_headers with no try/except and a bare return that masks a
propagating exception, so an edge case 500s the device with non-JSON.

Location
custom-providers/xiaozhi-patches/ota_handler.py:359-361

Details
handle_download wraps _add_cors_headers in try/except; handle_post does not, and the bare `return` in
finally masks any exception from the except branch.

Proposed fix
Mirror handle_download: wrap _add_cors_headers in try/except; initialize response = None and
short-circuit if still None.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] _dotty_inject_text reads conn.headers without the 'or {}' guard used by sibling handlers

```
Summary
inject-text raises AttributeError (returning a misleading 500 after already dispatching chat) if
conn.headers is present-but-None.

Location
custom-providers/xiaozhi-patches/http_server.py:66

Details
Line 66 does getattr(conn,'headers',{}).get('device-id',''); every other admin handler uses
(getattr(conn,'headers',{}) or {}).get(...).

Proposed fix
Apply the same (getattr(conn,'headers',{}) or {}) guard on line 66.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] OTA mqtt branch ships an empty-password block with no websocket fallback

```
Summary
A misconfigured MQTT signature key yields an mqtt config with a blank password and no websocket
fallback — the device gets a connection config it cannot authenticate with.

Location
custom-providers/xiaozhi-patches/ota_handler.py:260-279

Details
When mqtt_gateway is configured but mqtt_signature_key is missing (or generate_password_signature
fails), the handler sends mqtt with password:"" and, being exclusive with the websocket else, no
alternate.

Proposed fix
If signature is required but unavailable, fall back to the websocket block (or return an error) rather
than shipping an mqtt config with an empty password.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] OTA model-name matching is brittle — updates silently never offered

```
Summary
device_model must exactly match the operator's bin-filename prefix; when they differ (the common case)
the device is silently told it is up-to-date.

Location
custom-providers/xiaozhi-patches/ota_handler.py:84-90,200-201,304

Details
files_by_model.get(device_model, []) requires an exact string match; the StackChan board reports
board.type, which rarely equals the human-chosen filename prefix. The same log fires for "model unknown"
and "already latest".

Proposed fix
Log the parsed device_model + known model keys when candidates is empty; allow a configured fallback
bucket or case-insensitive match; emit distinct log lines for the two cases.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] OTA timezone_offset / firmware_cache_ttl assume numeric YAML

```
Summary
A quoted YAML timezone_offset ("8") produces a 60-copy string in the OTA payload; a non-int
firmware_cache_ttl makes int(...) raise.

Location
custom-providers/xiaozhi-patches/ota_handler.py:62,226

Details
server_config.get("timezone_offset", 8) * 60 and int(self._bin_cache.get("ttl", 30)) trust the YAML
type.

Proposed fix
Coerce with int(server_config.get("timezone_offset", 8)) * 60 and validate firmware_cache_ttl at
construction (int() with try/except defaulting to 30).

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] Firmware bin cache re-scans disk on every OTA POST until a valid bin exists

```
Summary
With no conforming bins yet, the TTL short-circuit never considers the cache warm, so the glob+regex
scan + makedirs runs on every OTA POST.

Location
custom-providers/xiaozhi-patches/ota_handler.py:66-72

Details
The short-circuit requires a non-empty files_by_model; {} is falsy, so updated_at is bumped meaninglessly
each pass and the scan never caches.

Proposed fix
Track freshness with a separate _scanned flag/updated_at sentinel independent of emptiness, so an
empty-but-recent scan is honored for the TTL window.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] Admin MCP handlers fire-and-forget send() with no guard for a torn-down socket

```
Summary
A single late send to an already-closed WS fails invisibly while the HTTP caller already got {"ok":true}.

Location
custom-providers/xiaozhi-patches/http_server.py:146,194,242,288,338

Details
Each direct-MCP route does _spawn(conn.websocket.send(msg), ...) after the registry lookup; between
lookup and send executing, the WS can close (the registry pop is in the WS handler's finally), so
send() raises in the spawned task.

Proposed fix
Wrap the send in a coroutine that checks conn.websocket state and catches+logs the exception, and/or
await the send before returning so the HTTP response reflects whether the frame was queued.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] set-toggle/set-state have no response correlation; set-head-angles unclamped

```
Summary
A device-side MCP rejection still returns {"ok":true} (no response correlation), and set-head-angles
forwards arbitrary ints with no clamp.

Location
custom-providers/xiaozhi-patches/http_server.py:143,191,239,285,335

Details
MCP calls use a truncated wall-clock id and the handler returns before any response frame; set_head_angles
validates only type, so a 100000 speed or 999 yaw is sent verbatim.

Proposed fix
Clamp set-head-angles yaw/pitch/speed to firmware-legal ranges before sending. Optionally correlate by
request id with a timeout, or document the endpoints as best-effort.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] play-asset format inference treats bare .opus as Ogg

```
Summary
A .opus asset shown in the songs picker can fail to play (forced format=ogg fails on raw Opus) with only
a swallowed warning behind an already-sent 200.

Location
custom-providers/xiaozhi-patches/http_server.py:406-411

Details
fmt is derived from the extension; {"opus":"ogg",...}.get(ext) forces .opus to ogg, and unmapped
extensions yield fmt=None (autodetect) with no distinct log.

Proposed fix
Let ffmpeg autodetect for .opus (pass no explicit format); on decode failure return a distinguishable
error code rather than a swallowed warning behind a 200.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] play-asset resample can feed huge factors to resample_poly; no sample_rate guard

```
Summary
Coprime rates give large up/down factors (slow/memory-heavy filter), and a 0/None conn.sample_rate blows
up inside _decode masked by the broad except.

Location
custom-providers/xiaozhi-patches/http_server.py:415-420

Details
g = gcd(src_rate, tgt); resample_poly(pcm, tgt//g, src_rate//g). No guard that conn.sample_rate is a
positive int; failures are logged as 'decode failed', hiding the cause.

Proposed fix
Validate conn.sample_rate > 0 (return 400/503 if not); log the actual src/tgt rates on decode failure;
consider capping or special-casing common rates.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] Admin routes resolve target via next(iter(...)) — nondeterministic device with multiple StackChans

```
Summary
With more than one device connected and no device_id, admin handlers act on an arbitrary dict entry, so
abort/set-state can hit the wrong robot.

Location
custom-providers/xiaozhi-patches/http_server.py:55,87,123,171,218,266,316,388,523

Details
next(iter(_dotty_active_connections.values()), None) picks the first dict entry; the caller has no way
to know which device was targeted.

Proposed fix
When multiple devices are connected and no device_id is given, return 409/400 requiring an explicit
device_id (or target all). Single-device deployments are unaffected.

Suggested labels: bug, area:xiaozhi
```

---

## [LOW] EventTextMessageHandler attributes events to "unknown" device on header miss

```
Summary
If conn.headers is None, the relayed perception event is attributed to device "unknown", which never
matches the real device id used by the admin routes, so per-device consumers silently never fire.

Location
custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:60-65

Details
handle() does device_id = conn.headers.get("device-id","unknown") inside a bare try/except that swallows
all exceptions, leaving device_id="unknown".

Proposed fix
Resolve device_id like the WS registration ((getattr(conn,"headers",None) or {}).get("device-id")); if
it resolves falsy/"unknown", drop the event with a warning rather than relay under a fabricated id.

Suggested labels: bug, area:xiaozhi, area:behaviour
```
