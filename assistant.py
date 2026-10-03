"""
QueryCraft — multilingual local voice assistant (EN / JA / KO)
Stack: faster-whisper -> llama.cpp server -> Kokoro TTS -> sounddevice
Built-in tools: weather + web search. Additional tools arrive through MCP.
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import requests as req
import sounddevice as sd
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from kokoro import KPipeline

from mcp_host import MCPHostSync

PROJECT_ROOT = Path(__file__).resolve().parent
_env_override = os.getenv("QUERYCRAFT_ENV")
ENV_PATH = Path(_env_override).expanduser().resolve() if _env_override else PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=False)

# =====================================================================
# CONFIG
# =====================================================================
SAMPLE_RATE = 16_000
CHANNELS = 1
BLOCK_SIZE = 1024

SILENCE_THRESHOLD = float(os.getenv("SILENCE_THRESHOLD", "0.015"))
SILENCE_SECONDS = float(os.getenv("SILENCE_SECONDS", "1.5"))
MAX_RECORD_SECS = int(os.getenv("MAX_RECORD_SECS", "30"))
VAD_CALIBRATION_SECS = float(os.getenv("VAD_CALIBRATION_SECS", "1.0"))
VAD_SPEECH_MULTIPLIER = float(os.getenv("VAD_SPEECH_MULTIPLIER", "3.0"))
VAD_MIN_SPEECH_SECS = float(os.getenv("VAD_MIN_SPEECH_SECS", "0.4"))

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))
WHISPER_INITIAL_PROMPT = os.getenv("WHISPER_INITIAL_PROMPT", "QueryCraft. RSS. RSS feed. FreshRSS. MCP. web search. weather.")

LLAMA_CPP_URL = os.getenv(
    "LLAMA_CPP_URL",
    "http://127.0.0.1:8080/v1/chat/completions",
)
LLAMA_CPP_MODEL = os.getenv("LLAMA_CPP_MODEL", "querycraft")
LLAMA_CPP_TIMEOUT = float(os.getenv("LLAMA_CPP_TIMEOUT", "60"))
LLAMA_CPP_MAX_TOKENS = int(os.getenv("LLAMA_CPP_MAX_TOKENS", "256"))
LLAMA_CPP_TEMPERATURE = float(os.getenv("LLAMA_CPP_TEMPERATURE", "0.7"))
LLAMA_CPP_TOP_P = float(os.getenv("LLAMA_CPP_TOP_P", "0.8"))
LLAMA_CPP_TOP_K = int(os.getenv("LLAMA_CPP_TOP_K", "20"))

KOKORO_LANG_MAP = {
    "en": ("a", os.getenv("KOKORO_VOICE_EN", "af_heart")),
    "ja": ("j", os.getenv("KOKORO_VOICE_JA", "jf_alpha")),
}
KOKORO_LANG_DEFAULT = ("a", os.getenv("KOKORO_VOICE_EN", "af_heart"))
KOREAN_TTS_BACKEND = os.getenv("KOREAN_TTS_BACKEND", "auto").strip().lower()
MELO_TTS_DEVICE = os.getenv("MELO_TTS_DEVICE", "auto")
MELO_TTS_SPEED = float(os.getenv("MELO_TTS_SPEED", "1.0"))
MELO_TTS_SPEAKER = os.getenv("MELO_TTS_SPEAKER", "KR")

WEATHER_API = "https://api.open-meteo.com/v1/forecast"
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_LAT = float(os.getenv("DEFAULT_LAT", "51.4778"))
DEFAULT_LON = float(os.getenv("DEFAULT_LON", "0.0015"))
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "GREENWICH")

HTTP = req.Session()

# =====================================================================
# WEATHER
# =====================================================================
def sanitize_city(city: str) -> str:
    city = re.sub(r"[^\w\s\-\.]", "", city, flags=re.UNICODE).strip()
    return city[:100]


def geocode(city: str) -> tuple[float, float, str]:
    city = sanitize_city(city)
    if not city:
        return DEFAULT_LAT, DEFAULT_LON, DEFAULT_CITY
    try:
        response = HTTP.get(
            GEO_API,
            params={"name": city, "count": 1, "language": "en"},
            timeout=5,
            verify=True,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if results:
            result = results[0]
            return result["latitude"], result["longitude"], result.get("name", city)
    except Exception as exc:
        print(f"[geo] {exc}")
    return DEFAULT_LAT, DEFAULT_LON, DEFAULT_CITY


def get_weather(city: str | None = None) -> dict:
    if city:
        lat, lon, resolved = geocode(city)
    else:
        lat, lon, resolved = DEFAULT_LAT, DEFAULT_LON, DEFAULT_CITY

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": [
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "wind_speed_10m",
            "weather_code",
        ],
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "weather_code",
        ],
        "forecast_days": 3,
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }

    try:
        response = HTTP.get(WEATHER_API, params=params, timeout=8, verify=True)
        response.raise_for_status()
        if int(response.headers.get("Content-Length", 0)) > 1_000_000:
            return {"error": "Weather API response too large"}

        data = response.json()
        current = data["current"]
        daily = data["daily"]
        descriptions = {
            0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
            45: "foggy", 48: "icy fog",
            51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
            61: "light rain", 63: "rain", 65: "heavy rain",
            71: "light snow", 73: "snow", 75: "heavy snow",
            80: "rain showers", 81: "rain showers", 82: "violent showers",
            95: "thunderstorm", 96: "thunderstorm with hail",
        }

        def describe(code):
            return descriptions.get(int(code), f"code {code}")

        return {
            "city": resolved,
            "current": {
                "temp_c": current["temperature_2m"],
                "feels_like_c": current["apparent_temperature"],
                "humidity_pct": current["relative_humidity_2m"],
                "wind_ms": current["wind_speed_10m"],
                "condition": describe(current["weather_code"]),
            },
            "forecast": [
                {
                    "date": daily["time"][index],
                    "max_c": daily["temperature_2m_max"][index],
                    "min_c": daily["temperature_2m_min"][index],
                    "precip_mm": daily["precipitation_sum"][index],
                    "condition": describe(daily["weather_code"][index]),
                }
                for index in range(3)
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}


def weather_tool_call(tool_input: dict) -> str:
    return json.dumps(get_weather(tool_input.get("city")), ensure_ascii=False)


TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather and a 3-day forecast for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name. Omit it to use the configured default location.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current, recent, or uncertain information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Plain search query in the language most useful for the search.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]

_LATIN_WEATHER_KEYWORDS = re.compile(
    r"\b(weather|forecast|temperature|temp|rain|snow|wind|storm|cloud|sunny|humid|"
    r"umbrella|jacket|cold|hot|warm|freezing|foggy|typhoon|hurricane)\b",
    re.IGNORECASE,
)
_CJK_WEATHER_KEYWORDS = (
    "날씨", "기온", "비", "눈", "바람", "흐림", "맑음", "우산", "기상",
    "天気", "気温", "雨", "雪", "風", "晴れ", "曇り", "傘", "予報",
)


def is_weather_query(text: str) -> bool:
    return bool(_LATIN_WEATHER_KEYWORDS.search(text)) or any(
        keyword in text for keyword in _CJK_WEATHER_KEYWORDS
    )

# =====================================================================
# WEB SEARCH
# =====================================================================
def sanitize_search_query(query: str) -> str:
    query = re.sub(r"[\x00-\x1f\x7f]", "", query).strip()
    return query[:200]


def normalize_query(query: str) -> str:
    # DDGS supports multilingual queries. Preserve the user's terms instead of
    # turning the query into a meta instruction such as "Translate and search".
    return sanitize_search_query(query)


def web_search(query: str, max_results: int = 4) -> dict:
    from ddgs import DDGS

    query = normalize_query(query)
    if not query:
        return {"error": "empty query"}

    results = []
    try:
        with DDGS() as ddgs:
            for result in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": result.get("title", "")[:200],
                    "snippet": result.get("body", "")[:400],
                    "url": result.get("href", ""),
                })
    except Exception as exc:
        print(f"[search] DDGS error: {exc}")
        return {"error": str(exc)}

    if not results:
        return {"error": f"No results found for: {query}"}
    return {"query": query, "results": results}


def web_search_tool_call(tool_input: dict) -> str:
    raw = tool_input.get("query", "")
    if isinstance(raw, dict):
        raw = raw.get("description") or raw.get("value") or ""
    if not isinstance(raw, str):
        raw = str(raw)
    return json.dumps(web_search(raw), ensure_ascii=False)

# =====================================================================
# STT
# =====================================================================
class SpeechListener:
    def __init__(self, whisper_model: str = WHISPER_MODEL):
        print(f"[stt] Loading Whisper model '{whisper_model}' …")
        self.model = WhisperModel(
            whisper_model,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
        )
        self.speech_threshold = SILENCE_THRESHOLD
        print("[stt] Ready.")

    def calibrate(self) -> None:
        print("[stt] Calibrating ambient noise … please stay quiet.")
        rms_samples: list[float] = []

        def callback(indata, frames, time_info, status):
            del frames, time_info, status
            rms_samples.append(float(np.sqrt(np.mean(indata[:, 0] ** 2))))

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=callback,
        ):
            time.sleep(VAD_CALIBRATION_SECS + 0.1)

        if rms_samples:
            ambient = float(np.mean(rms_samples))
            self.speech_threshold = max(
                ambient * VAD_SPEECH_MULTIPLIER,
                SILENCE_THRESHOLD,
            )
            print(
                f"[stt] Ambient RMS={ambient:.4f} -> "
                f"speech threshold={self.speech_threshold:.4f}"
            )
        else:
            self.speech_threshold = SILENCE_THRESHOLD
            print(f"[stt] Calibration failed, using {self.speech_threshold:.4f}")

    def record_until_silence(self) -> np.ndarray:
        audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        recording: list[np.ndarray] = []
        pre_buffer: list[np.ndarray] = []
        silence_seconds = 0.0
        speech_seconds = 0.0
        speech_started = False
        threshold = self.speech_threshold

        def callback(indata, frames, time_info, status):
            del frames, time_info, status
            try:
                audio_queue.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=callback,
        ):
            print("[stt] Listening … (speak now)")
            start = time.time()
            while True:
                try:
                    chunk = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - start > MAX_RECORD_SECS:
                        break
                    continue

                rms = float(np.sqrt(np.mean(chunk ** 2)))
                block_seconds = BLOCK_SIZE / SAMPLE_RATE

                if not speech_started:
                    if rms > threshold:
                        pre_buffer.append(chunk)
                        speech_seconds += block_seconds
                        if speech_seconds >= VAD_MIN_SPEECH_SECS:
                            speech_started = True
                            recording.extend(pre_buffer)
                            pre_buffer.clear()
                            print("[stt] Recording …")
                    else:
                        speech_seconds = 0.0
                        pre_buffer.clear()
                    continue

                recording.append(chunk)
                if rms < threshold:
                    silence_seconds += block_seconds
                else:
                    silence_seconds = 0.0

                if silence_seconds >= SILENCE_SECONDS:
                    break
                if time.time() - start > MAX_RECORD_SECS:
                    print("[stt] Max recording length reached.")
                    break

        if recording:
            return np.concatenate(recording)
        return np.zeros(SAMPLE_RATE, dtype="float32")

    def transcribe(self, audio: np.ndarray) -> tuple[str, str]:
        segments, info = self.model.transcribe(
            audio,
            beam_size=WHISPER_BEAM_SIZE,
            language=None,
            task="transcribe",
            initial_prompt=WHISPER_INITIAL_PROMPT,
        )
        text = " ".join(segment.text for segment in segments).strip()
        text = normalize_transcript(text)
        return text, info.language

def normalize_transcript(text: str) -> str:
    """Fix a few domain-specific ASR homophones before tool routing."""
    text = re.sub(
        r"\b(?:rss|r\s*s\s*s|ores(?:['’]s|es)?)\s+(?:feet|feat)\b",
        "RSS feed",
        text,
        flags=re.IGNORECASE,
    )
    return text


def is_rss_query(text: str) -> bool:
    lowered = text.lower()
    latin = re.search(
        r"\b(rss|feed|feeds|unread|headline|headlines|news|articles?)\b",
        lowered,
    )
    cjk = any(token in text for token in ("ニュース", "フィード", "未読", "뉴스", "피드", "읽지 않은"))
    return bool(latin or cjk)


def explicitly_requests_web(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"\b(web|internet|online|website|site|google|search the web)\b", lowered))


def requested_tts_language(user_text: str, fallback: str = "en") -> str:
    lowered = user_text.lower()
    if re.search(r"\b(japanese|nihongo)\b", lowered) or "日本語" in user_text:
        return "ja"
    if re.search(r"\b(korean|hangul)\b", lowered) or any(
        token in user_text for token in ("한국어", "韓国語")
    ):
        return "ko"
    if re.search(r"\benglish\b", lowered) or "英語" in user_text or "영어" in user_text:
        return "en"
    return fallback if fallback in {"en", "ja", "ko"} else "en"


def detect_tts_language(text: str, fallback: str = "en") -> str:
    """Choose TTS language from generated reply, not microphone language."""
    if re.search(r"[\uac00-\ud7a3]", text):
        return "ko"
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    return fallback if fallback in {"en", "ja", "ko"} else "en"


# =====================================================================
# INPUT GUARD
# =====================================================================
_INJECTION_PATTERNS = re.compile(
    r"(ignore (previous|all|prior) instructions?|system prompt|you are now|"
    r"act as (an? )?(different|new|other)|"
    r"disregard (your |all )?(previous |prior )?instructions?|jailbreak|"
    r"do anything now|forget (everything|all)|new persona)",
    re.IGNORECASE,
)


def _check_prompt_injection(text: str) -> None:
    if _INJECTION_PATTERNS.search(text):
        print(f"[security] Possible prompt injection detected: {text!r}")

# =====================================================================
# LLM — llama.cpp OpenAI-compatible API
# =====================================================================
SYSTEM_PROMPT = """You are QueryCraft, a multilingual voice assistant.

You can speak and understand English, Japanese, and Korean. Never claim otherwise.
Reply in the user's language unless the user explicitly asks for another language.
Keep the final reply natural and brief: 1-3 spoken sentences in plain text.
Do not use markdown, bullet lists, or decorative formatting in the final reply.

Tool policy:
- Use get_weather only for weather, temperature, precipitation, wind, or forecast questions.
- Prefer FreshRSS tools for latest news, headlines, unread items, RSS feeds, or requests about the user's feeds.
- Use web_search when the user explicitly asks to search the web, or when FreshRSS cannot answer a current-information request.
- Use available MCP tools when the request concerns their data or actions.
- Do not invent tool results. After a tool call, answer from the returned data.
- Supply only the arguments required by the selected tool.
- When asked about your abilities, describe capabilities from the tools available in this turn.

The current local date and time is supplied below. Treat it as authoritative.
If no tool is needed, answer directly."""


class LLMAgent:
    MAX_HISTORY_TURNS = int(os.getenv("LLM_HISTORY_TURNS", "12"))
    MAX_TOOL_ROUNDS = int(os.getenv("LLM_MAX_TOOL_ROUNDS", "5"))
    AGENT_DEADLINE_SECONDS = float(os.getenv("LLM_AGENT_DEADLINE_SECONDS", "30"))

    def __init__(
        self,
        model: str = LLAMA_CPP_MODEL,
        mcp_host: "MCPHostSync | None" = None,
    ):
        self.model = model
        self.history: list[dict] = []
        self._mcp = mcp_host
        self._session = req.Session()

    def _tools_for(self, user_text: str) -> list[dict]:
        tools: list[dict] = []
        if is_weather_query(user_text):
            tools.append(TOOLS_SPEC[0])

        mcp_tools = self._mcp.get_tools() if self._mcp else []
        rss_names = {"get_unread_articles", "get_articles_from_feed", "mark_article_read", "check_freshrss_connection"}
        rss_tools = [
            spec for spec in mcp_tools
            if spec.get("function", {}).get("name") in rss_names
        ]
        other_mcp_tools = [
            spec for spec in mcp_tools
            if spec.get("function", {}).get("name") not in rss_names
        ]

        rss_intent = is_rss_query(user_text) and bool(rss_tools)
        if rss_intent:
            tools.extend(rss_tools)
            if explicitly_requests_web(user_text):
                tools.append(TOOLS_SPEC[1])
        else:
            tools.append(TOOLS_SPEC[1])
            tools.extend(rss_tools)

        tools.extend(other_mcp_tools)
        return tools

    def _request(self, payload: dict) -> dict:
        response = self._session.post(
            LLAMA_CPP_URL,
            json=payload,
            timeout=LLAMA_CPP_TIMEOUT,
            verify=True,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"llama.cpp response had no choices: {data}")
        return choices[0].get("message") or {}

    @staticmethod
    def _parse_arguments(raw_arguments) -> dict:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _call_tool(self, name: str, arguments: dict) -> str:
        print(f"[agent] Calling tool: {name}({arguments})")
        if name == "get_weather":
            return weather_tool_call(arguments)
        if name == "web_search":
            return web_search_tool_call(arguments)
        if self._mcp and name in self._mcp.list_tools():
            return self._mcp.call_tool(name, arguments)
        return json.dumps({"error": f"unknown tool {name}"})

    def chat(self, user_text: str) -> str:
        _check_prompt_injection(user_text)
        self.history.append({"role": "user", "content": user_text})

        max_messages = self.MAX_HISTORY_TURNS * 2
        if len(self.history) > max_messages:
            self.history = self.history[-max_messages:]

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tools = self._tools_for(user_text)
        tool_names = [spec.get("function", {}).get("name", "") for spec in tools]
        available_tools = ", ".join(name for name in tool_names if name) or "none"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{SYSTEM_PROMPT}\n\n"
                        f"Current local date and time: {now}\n"
                        f"Available tools this turn: {available_tools}"
                    ),
                },
                *self.history,
            ],
            "stream": False,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": LLAMA_CPP_MAX_TOKENS,
            "temperature": LLAMA_CPP_TEMPERATURE,
            "top_p": LLAMA_CPP_TOP_P,
            "top_k": LLAMA_CPP_TOP_K,
        }

        deadline = time.time() + self.AGENT_DEADLINE_SECONDS
        for round_index in range(self.MAX_TOOL_ROUNDS):
            if time.time() > deadline:
                print("[llm] Agent loop deadline exceeded.")
                return "Sorry, that took too long. Please try again."

            try:
                message = self._request(payload)
            except Exception as exc:
                print(f"[llm error] {exc}")
                return "Sorry, I couldn't reach the language model. Please try again."

            tool_calls = message.get("tool_calls") or []
            content = message.get("content") or ""

            if tool_calls:
                # llama.cpp follows OpenAI's tool-call message shape. Keep the
                # assistant tool_call IDs and echo each ID on its tool result.
                for index, tool_call in enumerate(tool_calls):
                    if not tool_call.get("id"):
                        tool_call["id"] = f"call_{round_index}_{index}"
                payload["messages"].append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                })

                for tool_call in tool_calls:
                    function = tool_call.get("function") or {}
                    name = function.get("name", "")
                    arguments = self._parse_arguments(function.get("arguments", {}))
                    result = self._call_tool(name, arguments)
                    payload["messages"].append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result,
                    })
                continue

            # Compatibility fallback for a model that prints a fake web_search
            # call instead of emitting a structured tool call.
            fake_call = re.search(
                r"web_search\s*\{\s*query\s*:\s*(.+?)\s*\}",
                content,
                re.IGNORECASE,
            )
            if fake_call:
                query = re.sub(r'[<>|\"]', "", fake_call.group(1)).strip()
                print(f"[agent] Recovered fake tool call: web_search({query})")
                result = web_search_tool_call({"query": query})
                payload["messages"].append(message)
                payload["messages"].append({
                    "role": "user",
                    "content": (
                        "The web_search tool returned this JSON. Answer the original "
                        f"question using it:\n{result}"
                    ),
                })
                continue

            reply = content.strip()
            if not reply:
                reply = "Sorry, I couldn't complete that request."
            self.history.append({"role": "assistant", "content": reply})
            return reply

        return "Sorry, I couldn't complete that request."

# =====================================================================
# TTS
# =====================================================================
class Speaker:
    def __init__(self, no_tts: bool = False):
        self.no_tts = no_tts
        self._pipelines: dict[str, KPipeline] = {}
        self._melo_ko = None
        if not no_tts:
            print("[tts] Kokoro ready for English/Japanese; Korean uses local fallback.")

    def _get_pipeline(self, lang_code: str) -> KPipeline:
        if lang_code not in self._pipelines:
            print(f"[tts] Loading Kokoro pipeline for lang_code='{lang_code}' …")
            self._pipelines[lang_code] = KPipeline(lang_code=lang_code)
            print(f"[tts] Pipeline '{lang_code}' ready.")
        return self._pipelines[lang_code]

    def _speak_korean_melo(self, text: str) -> bool:
        try:
            from melo.api import TTS
            import soundfile as sf
        except ImportError:
            return False

        if self._melo_ko is None:
            print("[tts] Loading MeloTTS Korean model …")
            self._melo_ko = TTS(language="KR", device=MELO_TTS_DEVICE)
            print("[tts] MeloTTS Korean model ready.")

        speaker_ids = self._melo_ko.hps.data.spk2id
        speaker_id = speaker_ids.get(MELO_TTS_SPEAKER)
        if speaker_id is None:
            speaker_id = speaker_ids.get("KR")
        if speaker_id is None:
            speaker_id = next(iter(speaker_ids.values()))

        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            self._melo_ko.tts_to_file(
                text,
                speaker_id,
                path,
                speed=MELO_TTS_SPEED,
                quiet=True,
            )
            audio, sample_rate = sf.read(path, dtype="float32")
            if getattr(audio, "ndim", 1) > 1:
                audio = np.mean(audio, axis=1)
            sd.play(audio, samplerate=int(sample_rate))
            sd.wait()
            return True
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _speak_korean_windows(text: str) -> bool:
        if sys.platform != "win32":
            return False

        script = r'''$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$culture = New-Object System.Globalization.CultureInfo('ko-KR')
$voices = $s.GetInstalledVoices($culture)
if ($voices.Count -eq 0) { throw 'No Korean Windows speech voice is installed.' }
$s.SelectVoice($voices[0].VoiceInfo.Name)
$s.Speak($args[0])'''
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script, text],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            return True
        except Exception as exc:
            print(f"[tts] Windows Korean voice unavailable: {exc}")
            return False

    def _speak_korean(self, text: str) -> None:
        backend = KOREAN_TTS_BACKEND
        if backend not in {"auto", "melo", "windows"}:
            raise ValueError("KOREAN_TTS_BACKEND must be auto, melo, or windows")

        if backend in {"auto", "melo"}:
            try:
                if self._speak_korean_melo(text):
                    return
            except Exception as exc:
                print(f"[tts] MeloTTS Korean error: {exc}")
                if backend == "melo":
                    raise

        if backend in {"auto", "windows"} and self._speak_korean_windows(text):
            return

        raise RuntimeError(
            "No Korean TTS backend is available. Install MeloTTS, or install a Korean "
            "Windows speech voice and use KOREAN_TTS_BACKEND=windows."
        )

    def speak(self, text: str, lang: str = "en") -> None:
        if self.no_tts or not text:
            print(f"[tts] (skipped) {text}")
            return

        max_chars = 1000
        if len(text) > max_chars:
            print(f"[tts] Truncating {len(text)} -> {max_chars} chars.")
            text = text[:max_chars]

        try:
            if lang == "ko":
                self._speak_korean(text)
                return

            lang_code, voice = KOKORO_LANG_MAP.get(lang, KOKORO_LANG_DEFAULT)
            pipeline = self._get_pipeline(lang_code)
            played_any = False
            for _, _, audio in pipeline(text, voice=voice):
                if hasattr(audio, "numpy"):
                    chunk = audio.numpy().astype(np.float32)
                else:
                    chunk = np.asarray(audio, dtype=np.float32)
                if chunk.size == 0:
                    continue
                sd.play(chunk, samplerate=24_000)
                sd.wait()
                played_any = True
            if not played_any:
                print("[tts] No audio generated.")
        except Exception as exc:
            print(f"[tts] Error: {exc}")
            print(f"[tts] Response: {text}")

# =====================================================================
# MAIN
# =====================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="QueryCraft multilingual voice assistant")
    parser.add_argument("--no-tts", action="store_true", help="Disable TTS output")
    parser.add_argument("--model", default=LLAMA_CPP_MODEL, help="llama.cpp model alias")
    parser.add_argument(
        "--whisper",
        default=WHISPER_MODEL,
        choices=["tiny", "base", "small", "medium", "large-v3"],
        help="Whisper model size",
    )
    args = parser.parse_args()

    mcp_host = MCPHostSync()
    try:
        print(f"[config] .env: {ENV_PATH} ({'loaded' if ENV_PATH.exists() else 'not found'})")
        mcp_host.start()
        loaded_tools = mcp_host.list_tools()
        print(f"[mcp] Tools: {', '.join(loaded_tools) if loaded_tools else '(none)'}")
        listener = SpeechListener(whisper_model=args.whisper)
        agent = LLMAgent(model=args.model, mcp_host=mcp_host)
        speaker = Speaker(no_tts=args.no_tts)

        listener.calibrate()
        print("\n======================================")
        print("  QueryCraft Voice Assistant (EN/JA/KO)")
        print("  llama.cpp backend")
        print("  Press Ctrl+C to quit")
        print("======================================\n")

        while True:
            try:
                audio = listener.record_until_silence()
                text, lang = listener.transcribe(audio)
                if not text:
                    print("[stt] Nothing heard, retrying …")
                    continue

                print(f"[you({lang})] {text}")
                print("[llm] Thinking …")
                reply = agent.chat(text)
                print(f"[assistant] {reply}")
                requested_lang = requested_tts_language(text, fallback=lang)
                reply_lang = detect_tts_language(reply, fallback=requested_lang)
                print(f"[tts] Reply language: {reply_lang}")
                speaker.speak(reply, lang=reply_lang)
            except KeyboardInterrupt:
                print("\n[assistant] Goodbye!")
                break
            except Exception as exc:
                print(f"[error] {exc}")
                time.sleep(1)
    finally:
        mcp_host.stop()
        HTTP.close()


if __name__ == "__main__":
    main()
