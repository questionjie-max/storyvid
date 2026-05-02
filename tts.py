"""TTS 语音生成（支持 StepFun / MiniMax）"""
import os
import binascii
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from config import (
    STEPFUN_API_KEY,
    STEPFUN_BASE_URL,
    TTS_PROVIDER,
    TTS_MODEL,
    TTS_VOICE,
    TTS_SPEED,
    TTS_VOLUME,
    TTS_SAMPLE_RATE,
    TTS_VOICE_LABEL,
    TTS_INSTRUCTION,
    MINIMAX_API_KEY,
    MINIMAX_BASE_URL,
    MINIMAX_TTS_MODEL,
    MINIMAX_TTS_VOICE_ID,
    MINIMAX_TTS_SPEED,
    TEMP_DIR,
    get_project_dir,
)

stepfun_client = OpenAI(api_key=STEPFUN_API_KEY, base_url=STEPFUN_BASE_URL)


def generate_speech_stepfun(text: str, output_path: str) -> str:
    """StepFun TTS 生成单段语音"""
    import json

    if len(text) > 1000:
        text = text[:990] + "..."

    extra_body = {
        "volume": TTS_VOLUME,
        "sample_rate": TTS_SAMPLE_RATE,
    }

    if TTS_VOICE_LABEL:
        try:
            extra_body["voice_label"] = json.loads(TTS_VOICE_LABEL)
        except json.JSONDecodeError:
            print(f"[TTS] voice_label JSON 解析失败，忽略: {TTS_VOICE_LABEL}")

    if TTS_INSTRUCTION:
        extra_body["instruction"] = TTS_INSTRUCTION

    response = stepfun_client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        speed=TTS_SPEED,
        response_format="mp3",
        extra_body=extra_body,
    )

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    response.stream_to_file(output_path)
    return output_path


def generate_speech_minimax(text: str, output_path: str) -> str:
    """MiniMax TTS 生成单段语音"""
    if not MINIMAX_API_KEY:
        raise ValueError("MiniMax API Key 未配置")

    payload = {
        "model": MINIMAX_TTS_MODEL,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": MINIMAX_TTS_VOICE_ID,
            "speed": MINIMAX_TTS_SPEED,
            "vol": 1,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }

    resp = requests.post(
        f"{MINIMAX_BASE_URL}/v1/t2a_v2",
        headers={
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    data = resp.json()
    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code") != 0:
        raise RuntimeError(f"MiniMax TTS 错误: {base_resp.get('status_msg')}")

    audio_hex = data["data"]["audio"]
    audio_bytes = binascii.unhexlify(audio_hex)
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(audio_bytes)
    return output_path


def generate_speech(text: str, output_path: str) -> str:
    """根据配置选择 TTS 提供商生成语音"""
    if TTS_PROVIDER == "minimax":
        return generate_speech_minimax(text, output_path)
    return generate_speech_stepfun(text, output_path)


def get_audio_duration(path: str) -> float:
    """获取音频文件时长（秒）"""
    # moviepy 的 AudioFileClip 最可靠
    try:
        from moviepy.editor import AudioFileClip
        clip = AudioFileClip(path)
        duration = clip.duration
        clip.close()
        if duration and duration > 0:
            return duration
    except Exception:
        pass

    return 5.0  # 默认 5 秒


def generate_all_speech(segments: list[dict]) -> list[dict]:
    """为所有段落生成语音，并记录时长（并行，最多 3 并发）"""
    proj_dir = get_project_dir()
    os.makedirs(proj_dir, exist_ok=True)
    total = len(segments)

    def _task(args):
        i, seg = args
        audio_path = os.path.join(proj_dir, f"segment_{i:03d}.mp3")
        print(f"[TTS] 开始生成第 {i+1}/{total} 段语音...")
        generate_speech(seg["text"], audio_path)
        duration = get_audio_duration(audio_path)
        return i, audio_path, duration

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_task, (i, seg)): i for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            i, audio_path, duration = future.result()
            segments[i]["audio_path"] = audio_path
            segments[i]["duration"] = duration
            print(f"[TTS] 第 {i+1}/{total} 段语音完成，时长 {duration:.1f}s")

    return segments
