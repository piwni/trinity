#!/usr/bin/env python3
"""Render text to MP3 via OpenAI's speech API (requires OPENAI_API_KEY in .env)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from trinity.config import get_config

_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer")
_MAX_CHARS = 4096


def synthesize(
    text: str,
    *,
    api_key: str,
    voice: str = "nova",
    model: str = "tts-1-hd",
    speed: float = 1.0,
) -> bytes:
    if len(text) > _MAX_CHARS:
        raise ValueError(f"text is {len(text)} chars; OpenAI TTS cap is {_MAX_CHARS}")
    resp = httpx.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": text, "voice": voice, "speed": speed},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.content


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI text-to-speech")
    parser.add_argument("input", type=Path, help="Plain-text file to read aloud")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output .mp3 path")
    parser.add_argument("--voice", default="nova", choices=_VOICES)
    parser.add_argument("--model", default="tts-1-hd", help="tts-1 (faster) or tts-1-hd")
    parser.add_argument("--speed", type=float, default=0.95, help="0.25–4.0; slightly slower for coaching tone")
    args = parser.parse_args()

    get_config()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY in .env (see .env.example).", file=sys.stderr)
        raise SystemExit(1)

    text = args.input.read_text(encoding="utf-8").strip()
    if not text:
        print(f"Empty input: {args.input}", file=sys.stderr)
        raise SystemExit(1)

    audio = synthesize(text, api_key=api_key, voice=args.voice, model=args.model, speed=args.speed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(audio)
    print(f"Wrote {args.output} ({len(audio) / 1024:.0f} KB, voice={args.voice})")


if __name__ == "__main__":
    main()
