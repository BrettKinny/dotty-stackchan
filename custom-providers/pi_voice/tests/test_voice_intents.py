"""Explicit voice-tool requests are recognised; everything else is left to
the model (#165).

AI-assisted (Claude). The live failure: under the production prompt the 4B
answered "please remember that" with a spoken acknowledgement and no
`remember` call, and answered "think hard" questions itself.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_intents import VoiceIntent, match_voice_intent, split_room_view  # noqa: E402


ROUTED = [
    # remember: command first
    ("Remember that my calibration color is ultraviolet",
     VoiceIntent("remember", "my calibration color is ultraviolet")),
    ("Dotty, remember that my favourite colour is purple.",
     VoiceIntent("remember", "my favourite colour is purple")),
    ("Hey Dotty, please remember my favourite colour is purple",
     VoiceIntent("remember", "my favourite colour is purple")),
    ("Remember, my name is Brett.", VoiceIntent("remember", "my name is Brett")),
    ("Can you remember that my birthday is in May?",
     VoiceIntent("remember", "my birthday is in May")),
    ("I want you to remember that Hudson likes trains",
     VoiceIntent("remember", "Hudson likes trains")),
    ("Don't forget that grandma visits on Sunday",
     VoiceIntent("remember", "grandma visits on Sunday")),
    # remember: fact first (the 2026-07-11 UAT phrasing)
    ("My favourite colour is purple, please remember that.",
     VoiceIntent("remember", "My favourite colour is purple")),
    ("My dog is called Rex. Can you remember that?",
     VoiceIntent("remember", "My dog is called Rex")),
    ("Dotty's favourite colour is blue, remember that",
     VoiceIntent("remember", "Dotty's favourite colour is blue")),
    # recall
    ("Do you remember my favourite colour?",
     VoiceIntent("memory_lookup", "my favourite colour")),
    ("Dotty, do you remember what my favourite colour is?",
     VoiceIntent("memory_lookup", "my favourite colour")),
    ("What did I tell you about my calibration color?",
     VoiceIntent("memory_lookup", "my calibration color")),
    ("Can you remember what my favourite colour is?",
     VoiceIntent("memory_lookup", "my favourite colour")),
    # think_hard
    ("Think hard about: what is six times seven?",
     VoiceIntent("think_hard", "what is six times seven")),
    ("Dotty, think hard: what is 47 times 89?",
     VoiceIntent("think_hard", "what is 47 times 89")),
    ("What is 47 times 89? Think hard.", VoiceIntent("think_hard", "What is 47 times 89")),
    ("Please think really hard about why the sky is blue",
     VoiceIntent("think_hard", "why the sky is blue")),
    ("Can you think hard about how many legs a spider has?",
     VoiceIntent("think_hard", "how many legs a spider has")),
    # recall_person: a named friend (2026-10-23 A/V run, T03: the 4B said
    # "I don't have any information" without looking)
    ("What do you know about my friend, Sam Baker?",
     VoiceIntent("recall_person", "Sam Baker")),
    ("What do you know about my friend Sam Baker?",
     VoiceIntent("recall_person", "Sam Baker")),
    ("Dotty, what do you know about my best friend Mia?",
     VoiceIntent("recall_person", "Mia")),
    ("What can you tell me about my neighbour Mrs Lee?",
     VoiceIntent("recall_person", "Mrs Lee")),
    # take_photo (fallback behind xiaozhi's vision shortcut)
    ("Take a photo", VoiceIntent("take_photo", "Take a photo")),
    ("What do you see?", VoiceIntent("take_photo", "What do you see?")),
    ("Dotty, can you look at this?", VoiceIntent("take_photo", "can you look at this?")),
    ("What's in front of you?", VoiceIntent("take_photo", "What's in front of you?")),
    ("Please use your camera and tell me about the room in one short sentence.",
     VoiceIntent("take_photo",
                 "Please use your camera and tell me about the room in one short sentence.")),
    ("Can you describe the room?", VoiceIntent("take_photo", "Can you describe the room?")),
    ("Tell me what's in the room", VoiceIntent("take_photo", "Tell me what's in the room")),
]

LEFT_TO_THE_MODEL = [
    "Can you remember my birthday?",          # a question, not a fact
    "I can't remember that.",
    "I don't remember that",
    "I remember that day",
    "Remember when we went to the park?",
    "Remember to brush your teeth",
    "remember me?",
    "Remember?",
    "What do you know about dinosaurs?",       # general knowledge, not memory
    "What do you know about my friend?",      # no name to look up
    "What do you know about friendship?",
    "What do you know about my friend's dog Rex and his cat and his house?",
    "What's my favourite colour?",
    "Think hard about it",                     # nothing to think about
    "Don't think hard about it",
    "I think hard work pays off",
    "Tell me a joke",
    "Tell me about the room you were built in",   # not a request to look
    "My camera is broken",
    "Hello Dotty",
    "Dotty",
    "",
    # Prompts xiaozhi-server writes itself are never routed.
    "[CAMERA_DISABLED] Camera access is disabled in Kid Mode. "
    "This request did not take a photo or access a camera view.",
    '[You just used your camera and took a photo. The photo shows: a cat]\n'
    'The child said: "what do you see"',
    "[STATE_CHANGE:story_time] You just entered story_time state.",
]


class RoutedPhrases(unittest.TestCase):
    def test_explicit_requests_name_their_tool(self):
        for text, expected in ROUTED:
            with self.subTest(text=text):
                self.assertEqual(match_voice_intent(text), expected)

    def test_everything_else_falls_through(self):
        for text in LEFT_TO_THE_MODEL:
            with self.subTest(text=text):
                self.assertIsNone(match_voice_intent(text))

    def test_curly_apostrophes_from_asr_are_handled(self):
        self.assertEqual(
            match_voice_intent("Don’t forget that the bins go out on Tuesday"),
            VoiceIntent("remember", "the bins go out on Tuesday"),
        )


class RoomViewPrefix(unittest.TestCase):
    """receiveAudioHandle._with_room_view_marker's v2 shape."""

    def test_marker_with_person_id_is_split_off(self):
        self.assertEqual(
            split_room_view("[ROOM_VIEW]\na child in a red hat\nhudson\nremember that I like cats"),
            ("a child in a red hat", "remember that I like cats"),
        )

    def test_marker_with_blank_person_id_is_split_off(self):
        self.assertEqual(
            split_room_view("[ROOM_VIEW]\nan adult waving\n\nDo you remember my colour?"),
            ("an adult waving", "Do you remember my colour?"),
        )

    def test_text_without_marker_is_unchanged(self):
        self.assertEqual(split_room_view("hello"), ("", "hello"))
        self.assertEqual(split_room_view("[ROOM_VIEW] said inline"), ("", "[ROOM_VIEW] said inline"))


if __name__ == "__main__":
    unittest.main()
