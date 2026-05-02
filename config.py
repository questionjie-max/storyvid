import os
import datetime
from dotenv import load_dotenv

load_dotenv()

STEPFUN_API_KEY = os.getenv("STEPFUN_API_KEY", "")
STEPFUN_BASE_URL = "https://api.stepfun.com/v1"

# TTS 提供商: stepfun / minimax
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "stepfun")

# StepFun TTS 配置
# model: step-tts-mini / step-tts-2 / stepaudio-2.5-tts
TTS_MODEL = os.getenv("TTS_MODEL", "step-tts-2")
TTS_VOICE = os.getenv("TTS_VOICE", "cixingnansheng")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.15"))
TTS_VOLUME = float(os.getenv("TTS_VOLUME", "1.0"))
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))

# MiniMax TTS 配置
MINIMAX_TTS_MODEL = os.getenv("MINIMAX_TTS_MODEL", "speech-01-hd")
MINIMAX_TTS_VOICE_ID = os.getenv("MINIMAX_TTS_VOICE_ID", "male-qn-jingying")
MINIMAX_TTS_SPEED = float(os.getenv("MINIMAX_TTS_SPEED", "1.15"))

# voice_label: 语言/情绪/风格（三者只能选一个，JSON 格式字符串）
# 示例: {"emotion":"高兴"} 或 {"language":"粤语"} 或 {"style":"慢速"}
TTS_VOICE_LABEL = os.getenv("TTS_VOICE_LABEL", "")

# instruction: 全局自然语言指导，仅 stepaudio-2.5-tts 支持
TTS_INSTRUCTION = os.getenv("TTS_INSTRUCTION", "")

# LLM 配置（用于生成故事稿）
# 支持任意兼容 OpenAI 格式的 LLM：DeepSeek、Kimi、OpenAI、通义千问等
LLM_API_KEY = os.getenv("LLM_API_KEY", STEPFUN_API_KEY)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 配图配置
IMAGE_SOURCE = os.getenv("IMAGE_SOURCE", "minimax")  # minimax / unsplash / local
# Unsplash 配置
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
# Minimax 配置
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com")
MINIMAX_IMAGE_MODEL = os.getenv("MINIMAX_IMAGE_MODEL", "image-01")
LOCAL_IMAGE_DIR = os.getenv("LOCAL_IMAGE_DIR", "./local_images")

# 视频配置
ASPECT_RATIO = os.getenv("ASPECT_RATIO", "16:9")  # 16:9 或 9:16
if ASPECT_RATIO == "9:16":
    VIDEO_WIDTH = 1080
    VIDEO_HEIGHT = 1920
else:
    VIDEO_WIDTH = 1920
    VIDEO_HEIGHT = 1080
VIDEO_FPS = 24

# 项目根目录
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 全局输出目录
OUTPUT_DIR = os.path.join(_PROJECT_DIR, "output")

# 全局临时目录（向后兼容）
GLOBAL_TEMP_DIR = os.path.join(_PROJECT_DIR, "temp")
TEMP_DIR = GLOBAL_TEMP_DIR

# 当前项目目录（每次生成前由 setup_project 设置）
PROJECT_DIR = None


def setup_project(topic: str) -> str:
    """为本次生成创建项目目录，格式：YYYY-MM-DD_topic_name/
    所有文件（视频、音频、图片、字幕、故事稿）都直接放在这个目录下，
    方便用户整文件夹删除，不留残留。
    """
    global PROJECT_DIR, TEMP_DIR
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d")
    safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in topic)
    project_name = f"{timestamp}_{safe_name[:40].strip()}"
    PROJECT_DIR = os.path.join(OUTPUT_DIR, project_name)
    os.makedirs(PROJECT_DIR, exist_ok=True)
    # 临时目录也指向项目内，中间帧等临时文件也归在一起
    TEMP_DIR = os.path.join(PROJECT_DIR, "_temp")
    os.makedirs(TEMP_DIR, exist_ok=True)
    return PROJECT_DIR


def get_project_dir() -> str:
    """获取当前项目目录，未设置时回退到全局临时目录"""
    if PROJECT_DIR:
        return PROJECT_DIR
    return GLOBAL_TEMP_DIR
