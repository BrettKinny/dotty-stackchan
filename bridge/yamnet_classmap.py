"""
YAMNet class-index → label mapping (curated subset).

The full official map (521 entries) lives in TensorFlow's audioset
research code:

    https://github.com/tensorflow/models/raw/master/research/audioset/yamnet/yamnet_class_map.csv

Format of the upstream CSV is (index, mid, display_name). We only need
display_name keyed by index for this scaffold — we filter to a curated
home-assistant whitelist downstream in `audio_scene.AudioSceneClassifier`,
so a partial dict is sufficient. If a YAMNet output index is missing
here it is treated as "unknown" and ignored, which is the correct
fail-closed behaviour for an event emitter.

A future commit can swap this in-process dict for a loader that reads
the canonical CSV from `models/yamnet/yamnet_class_map.csv`. Doing so
without changing the public surface (`label_for(index)`) is fine.

Indices below were copied from the upstream CSV at commit time. They
cover the whitelist plus nearby classes that may matter for tuning
(e.g. several "speech" / "footsteps" / "music" siblings).
"""

from __future__ import annotations


# Curated subset — index → display_name (display_name matches the
# upstream CSV verbatim, including punctuation and casing). Indices
# not present here are treated as unknown by `label_for`.
YAMNET_CLASS_MAP: dict[int, str] = {
    0: "Speech",
    1: "Child speech, kid speaking",
    2: "Conversation",
    3: "Narration, monologue",
    4: "Babbling",
    5: "Speech synthesizer",
    6: "Shout",
    7: "Bellow",
    8: "Whoop",
    9: "Yell",
    10: "Children shouting",
    11: "Screaming",
    12: "Whispering",
    13: "Laughter",
    14: "Baby laughter",
    15: "Giggle",
    16: "Snicker",
    17: "Belly laugh",
    18: "Chuckle, chortle",
    19: "Crying, sobbing",
    20: "Baby cry, infant cry",
    21: "Whimper",
    22: "Wail, moan",
    23: "Sigh",
    # — Music / instruments —
    132: "Music",
    137: "Musical instrument",
    277: "Singing",
    # — Animals (domestic) —
    67: "Animal",
    68: "Domestic animals, pets",
    69: "Dog",
    70: "Bark",
    71: "Yip",
    72: "Howl",
    73: "Bow-wow",
    74: "Growling",
    75: "Whimper (dog)",
    76: "Cat",
    77: "Purr",
    78: "Meow",
    79: "Hiss",
    80: "Caterwaul",
    # — Domestic sounds / household —
    354: "Domestic sounds, home sounds",
    355: "Door",
    356: "Doorbell",
    357: "Ding-dong",
    358: "Sliding door",
    359: "Slam",
    360: "Knock",
    361: "Tap",
    362: "Squeak",
    363: "Cupboard open or close",
    364: "Drawer open or close",
    365: "Dishes, pots, and pans",
    366: "Cutlery, silverware",
    367: "Chopping (food)",
    368: "Frying (food)",
    369: "Microwave oven",
    370: "Blender",
    371: "Water tap, faucet",
    372: "Sink (filling or washing)",
    373: "Bathtub (filling or washing)",
    374: "Hair dryer",
    375: "Toilet flush",
    376: "Toothbrush",
    377: "Electric toothbrush",
    378: "Vacuum cleaner",
    379: "Zipper (clothing)",
    380: "Keys jangling",
    381: "Coin (dropping)",
    382: "Scissors",
    383: "Electric shaver, electric razor",
    384: "Shuffling cards",
    385: "Typing",
    386: "Typewriter",
    387: "Computer keyboard",
    388: "Writing",
    389: "Alarm",
    390: "Telephone",
    391: "Telephone bell ringing",
    392: "Ringtone",
    393: "Telephone dialing, DTMF",
    394: "Dial tone",
    395: "Busy signal",
    396: "Alarm clock",
    397: "Siren",
    398: "Civil defense siren",
    399: "Buzzer",
    400: "Smoke detector, smoke alarm",
    401: "Fire alarm",
    402: "Foghorn",
    403: "Whistle",
    404: "Steam whistle",
    # — Footsteps + human movement —
    63: "Walk, footsteps",
    64: "Run",
    65: "Shuffle",
    66: "Chewing, mastication",
    # — Whistles / kettles (kitchen) —
    405: "Kettle whistle",
    # — Silence / ambient —
    494: "Silence",
    500: "White noise",
    506: "Background noise",
}


# Convenience reverse lookup — display_name → index. Built lazily on
# first call so module import stays cheap. Used by tests and any future
# producer that wants to assert "this index maps to that label".
_REVERSE_CACHE: dict[str, int] | None = None


def label_for(index: int) -> str:
    """Return the display_name for a YAMNet class index, or ``""`` if
    the index is not in the curated map. Caller decides what "unknown"
    means — typical use is to drop the prediction silently."""
    return YAMNET_CLASS_MAP.get(int(index), "")


def index_for(label: str) -> int:
    """Reverse lookup. Returns -1 if the label isn't in the curated
    map. Useful for tests and for filtering YAMNet output by label
    when you don't want to hand-type indices."""
    global _REVERSE_CACHE
    if _REVERSE_CACHE is None:
        _REVERSE_CACHE = {v: k for k, v in YAMNET_CLASS_MAP.items()}
    return _REVERSE_CACHE.get(label, -1)


__all__ = ["YAMNET_CLASS_MAP", "label_for", "index_for"]
