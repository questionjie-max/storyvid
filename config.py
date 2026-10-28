import os
import datetime
from dotenv import load_dotenv

# .env 加载顺序（load_dotenv 默认不覆盖已存在的变量，先到先得）：
# 1) cwd/.env —— pipx 等非 editable 安装后，代码在 site-packages 里，
#    load_dotenv() 只会从代码所在目录向上找，永远找不到用户工作目录的 .env；
#    先补一次 cwd，让装好的 CLI 在任意目录运行都能读到当前目录的 .env
# 2) load_dotenv() —— 源码运行旧行为：从 config.py 所在目录向上找 .env，
#    保证「在仓库子目录或任意 cwd 下 python main.py」仍能读到仓库根的 .env
#    （cwd 与仓库根相同时两次指向同一文件，行为不变）
load_dotenv(os.path.join(os.getcwd(), ".env"))
load_dotenv()

# ---------- 数值环境变量守卫（须先于首个使用处定义） ----------
# B 队 mustFix 先例（A2 Ken Burns / A10 字幕样式 / B6 SUBTITLE_MAX_CHARS）：
# 数值参数配错不得在 import 阶段抛 ValueError 崩掉 main.py/web（web/app.py
# 顶部即 import config）。所有数值型 env 一律走这两个守卫，非法值打警告回退默认。
def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；缺失/非法值都回退默认，不让 main.py/web 在 import 阶段崩溃"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"[Config] 警告: {name}={raw!r} 不是合法整数，回退默认值 {default}")
        return default


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量；缺失/非法值都回退默认，不让 main.py/web 在 import 阶段崩溃"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        print(f"[Config] 警告: {name}={raw!r} 不是合法数字，回退默认值 {default}")
        return default

# ---------- LLM（写故事稿） ----------
# 支持任意 OpenAI 兼容 API：DeepSeek / Kimi / OpenAI / 通义千问 等
# LLM_API_KEY 留空时回退到 STEPFUN_API_KEY（沿用旧版习惯）
STEPFUN_API_KEY = os.getenv("STEPFUN_API_KEY", "")
STEPFUN_BASE_URL = os.getenv("STEPFUN_BASE_URL", "https://api.stepfun.com/v1")

LLM_API_KEY = os.getenv("LLM_API_KEY", "") or STEPFUN_API_KEY
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# ---------- TTS 语音合成 ----------
# 提供商: stepfun / minimax / volc
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "stepfun")

# StepFun TTS 配置
# model 可选: step-tts-mini / step-tts-2 / stepaudio-2.5-tts（以官方文档为准）
TTS_MODEL = os.getenv("TTS_MODEL", "step-tts-2")
TTS_VOICE = os.getenv("TTS_VOICE", "cixingnansheng")
TTS_SPEED = _env_float("TTS_SPEED", 1.15)
TTS_VOLUME = _env_float("TTS_VOLUME", 1.0)
TTS_SAMPLE_RATE = _env_int("TTS_SAMPLE_RATE", 24000)

# MiniMax TTS 配置
# speech-01-hd 之外也可以试 speech-02-hd / speech-2.5-hd-preview（以官方文档为准）
MINIMAX_TTS_MODEL = os.getenv("MINIMAX_TTS_MODEL", "speech-01-hd")
MINIMAX_TTS_VOICE_ID = os.getenv("MINIMAX_TTS_VOICE_ID", "male-qn-jingying")
MINIMAX_TTS_SPEED = _env_float("MINIMAX_TTS_SPEED", 1.15)

# 火山引擎（豆包）TTS 配置（TTS_PROVIDER=volc 时生效）
# 对应官方「HTTP 非流式合成接口」: https://www.volcengine.com/docs/6561/79820
# AppID / Access Token 在火山引擎控制台「语音技术-应用管理」创建应用后获取；
# cluster 与音色需与开通的服务匹配（小模型 volcano_tts / 大模型 volcano_tts 大模型音色）。
# 注意：本接入尚未用真实 key 实测，字段结构按公开文档实现，需自行验证。
VOLC_TTS_APPID = os.getenv("VOLC_TTS_APPID", "")
VOLC_TTS_TOKEN = os.getenv("VOLC_TTS_TOKEN", "")
VOLC_TTS_API_URL = os.getenv("VOLC_TTS_API_URL", "https://openspeech.bytedance.com/api/v1/tts")
VOLC_TTS_CLUSTER = os.getenv("VOLC_TTS_CLUSTER", "volcano_tts")
VOLC_TTS_VOICE_TYPE = os.getenv("VOLC_TTS_VOICE_TYPE", "BV001_streaming")
VOLC_TTS_ENCODING = os.getenv("VOLC_TTS_ENCODING", "mp3")
VOLC_TTS_SPEED_RATIO = _env_float("VOLC_TTS_SPEED_RATIO", 1.0)
VOLC_TTS_VOLUME_RATIO = _env_float("VOLC_TTS_VOLUME_RATIO", 1.0)

# voice_label: 语言/情绪/风格（三者只能选一个，JSON 格式字符串）
# 示例: {"emotion":"高兴"} 或 {"language":"粤语"} 或 {"style":"慢速"}
TTS_VOICE_LABEL = os.getenv("TTS_VOICE_LABEL", "")

# instruction: 全局自然语言指导，仅 stepaudio-2.5-tts 支持
TTS_INSTRUCTION = os.getenv("TTS_INSTRUCTION", "")

# ---------- TTS 时间戳对轴（P0-2 / 任务 D2）----------
# 生成配音时顺带获取真实时间片（StepFun 流式 WS 词级 / MiniMax 字幕句级 /
# 火山 with_timestamp 字级），字幕按真实发音边界切块
# （subtitles.build_chunk_durations(words=...)），替代字符比例估时；
# 任何失败自动回退字符比例，不影响出片。默认开启；设 0/false/off/no 关闭。
# 注意：时间戳走独立请求（音频仍走原接口），开启后每段会多一次对应
# provider 的 TTS 调用（设计取舍见 overnight_reports/D2_timestamps.md）。
TTS_TIMESTAMPS = os.getenv("TTS_TIMESTAMPS", "1").strip().lower() not in ("0", "false", "off", "no")

# Edge TTS（微软神经语音）配置（TTS_PROVIDER=edge 时生效）：免费无需任何 Key，
# 联网即用，且音频流自带词级时间戳（WordBoundary），直接喂 D2 对轴通道，
# 无需额外请求。中文推荐 zh-CN-YunxiNeural（男声）/ zh-CN-XiaoxiaoNeural（女声）；
# 语速沿用 TTS_SPEED（1.0 → +0%，1.15 → +15%）
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "zh-CN-YunxiNeural")

# StepFun 流式语音合成 WebSocket 入口（词级时间戳走这里；model 以查询参数附加）。
# 官方文档: https://platform.stepfun.com/docs/zh/api-reference/audio/ws-audio
STEPFUN_WS_URL = os.getenv("STEPFUN_WS_URL", "wss://api.stepfun.com/v1/realtime/audio")

# ---------- 配音响度归一 ----------
# 解决不同段 TTS 配音忽大忽小的问题：每段 mp3 生成后用 ffmpeg loudnorm
# （两遍法，线性模式）归一到目标响度。默认开启；设 0/false/off/no 关闭。
# TARGET_LUFS 为目标整体响度（LUFS）；归一失败自动回退原文件，不阻塞生成。
LOUDNESS_NORM = os.getenv("LOUDNESS_NORM", "1").strip().lower() not in ("0", "false", "off", "no")
try:
    TARGET_LUFS = float(os.getenv("TARGET_LUFS", "-16"))
except ValueError:
    TARGET_LUFS = -16.0  # 非法值回退默认，不中断生成

# ---------- 配图 ----------
# 来源: minimax / unsplash / local
IMAGE_SOURCE = os.getenv("IMAGE_SOURCE", "minimax")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com")
MINIMAX_IMAGE_MODEL = os.getenv("MINIMAX_IMAGE_MODEL", "image-01")
# MiniMax 生图候选数（钳制 ≥1）：n>1 时除主图 image_XXX.jpg 外，其余候选依次
# 落盘 image_XXX_alt_1.jpg、image_XXX_alt_2.jpg……，供 Web /api/candidates +
# /api/select 列出候选并人工换图（换图只重渲该段字幕帧后重新合成，D4）。
MINIMAX_IMAGE_N = max(1, _env_int("MINIMAX_IMAGE_N", 1))
LOCAL_IMAGE_DIR = os.getenv("LOCAL_IMAGE_DIR", "./local_images")

# 本地素材优先目录（用户自备照片/视频，CLI --media-dir 与 web「本地素材目录」同效）：
# 递归扫描其中的图片（jpg/jpeg/png/webp）与视频（mp4/mov/webm/mkv/avi）。
# 配置且含可用素材时优先使用（图片拷贝进项目目录走断点续跑约定，视频按段
# 截取到配音时长并静音），未覆盖到的段照旧走云端配图降级链（minimax/unsplash
# → picsum）；web 界面单次生成的 media_dir 输入优先于本全局配置。
LOCAL_MEDIA_DIR = os.getenv("LOCAL_MEDIA_DIR", "").strip()

# ---------- 视频与字幕 ----------
ASPECT_RATIO = os.getenv("ASPECT_RATIO", "16:9")  # 16:9 或 9:16
VIDEO_FPS = _env_int("VIDEO_FPS", 24)

# 预览模式：PREVIEW=1（或 true/yes/on）时按半分辨率快速渲染，
# 横屏 960×540 / 竖屏 540×960，快速迭代构图与时长，不跑全分辨率。
# 正式出片请保持关闭（默认 0）。CLI 的 --preview 参数也会打开此开关（见 main.py）。
PREVIEW = os.getenv("PREVIEW", "0").strip().lower() in ("1", "true", "yes", "on")

# 演示模式：DEMO=1（或 true/yes/on）与 CLI --demo 同效——零 Key 全离线出片：
# 内置示例文稿 + 每段固定 3 秒静音配音 + PIL 渐变大字占位图（实现见 demo.py），
# 不调搜索/审核/LLM/TTS/配图任何 API，让没配 Key 的新用户半分钟看到成品效果。
DEMO = os.getenv("DEMO", "0").strip().lower() in ("1", "true", "yes", "on")

# 指定中文字体文件路径；留空则自动探测系统字体
FONT_PATH = os.getenv("FONT_PATH", "")

# ---------- 字幕样式（参数集中在这里，video.py 只读不定义） ----------
# 字幕单块最大字符数（超过会按标点/逗号二次切分）。
# 26 = 竖屏两行内的短句节奏：40+ 字的整句一屏挂 6-7.5s 观感呆滞（基线实测），
# 切短后单屏 2-5s 更接近短视频字幕节奏；时间轴仍由真实词级时间片投影保证。
# 曾是裸 int(os.getenv(...))，配错（如 SUBTITLE_MAX_CHARS=abc）会在 import 阶段
# 抛 ValueError 崩掉 main.py/web——与 B 队在 A10 检出的 7 个样式参数同类隐患，
# 统一走 _env_int 守卫（见 overnight_reports/A10_subtitle_style.md、B6_regress.md）。
SUBTITLE_MAX_CHARS = _env_int("SUBTITLE_MAX_CHARS", 26)

# 字幕样式: plain（默认：底部整宽半透明黑条 + 白字黑描边，保持旧行为）
#          card（底部圆角半透明卡片条 + 主题色描边，数字与引号内关键词用主题色高亮）
#          karaoke（逐词卡拉OK：plain 同款黑条，已唱/当前词主题色高亮，
#                   词级时间戳 seg["words"]（D2 TTS / D3 本地对齐通道）驱动，
#                   缺失时块内按字符比例，见 subtitles.karaoke_chunk_timings）
SUBTITLE_STYLE = os.getenv("SUBTITLE_STYLE", "plain").strip().lower()
if SUBTITLE_STYLE not in ("plain", "card", "karaoke"):
    SUBTITLE_STYLE = "plain"  # 非法值回退默认，不中断生成

# card 模式主题色：文字描边与高亮颜色，HEX 格式 #RRGGBB
SUBTITLE_THEME_COLOR = os.getenv("SUBTITLE_THEME_COLOR", "#FFCD5A")

# 字幕条距画面底边（像素）；0 = 自动：竖屏 240（约 12.5%，避开抖音/视频号
# 底部 UI 安全区——旧值 80 会被平台文案栏和进度条盖住）/ 横屏 60。
# 质量循环 R5 新增：plain / karaoke 与 card 的自动回退共用这一套取值
# （card 另有 SUBTITLE_CARD_BOTTOM_MARGIN 显式配置，优先）。
SUBTITLE_BOTTOM_MARGIN = _env_int("SUBTITLE_BOTTOM_MARGIN", 0)

# 是否在项目目录落盘外挂 SRT（subtitles.srt）。字幕已烧录进画面，而同目录的
# SRT 会被播放器（PotPlayer / QQ影音 / mpv fuzzy 等）自动加载，与烧录字幕
# 叠成两层——用户两次看片反馈的「两层字幕」根因（视频本身抽帧验证始终单层）。
# 默认关闭；需要外挂字幕（平台字幕上传 / 归档 / 剪辑软件导入）时设 1 开启。
SUBTITLES_SRT = os.getenv("SUBTITLES_SRT", "0").strip().lower() in ("1", "true", "yes", "on")

# card 模式卡片条参数（数值范围钳制在 video.py 绘制处做，这里只防解析崩溃）
SUBTITLE_CARD_OPACITY = _env_int("SUBTITLE_CARD_OPACITY", 170)   # 卡片不透明度 0-255
SUBTITLE_CARD_RADIUS = _env_int("SUBTITLE_CARD_RADIUS", 28)      # 圆角半径（像素）
SUBTITLE_CARD_MARGIN = _env_int("SUBTITLE_CARD_MARGIN", 60)      # 卡片距画面左右边距（像素）
SUBTITLE_CARD_PADDING = _env_int("SUBTITLE_CARD_PADDING", 24)    # 文字与卡片边缘内边距（像素）
# 卡片距画面底边（像素）；0 = 自动（竖屏 80 / 横屏 40，与 plain 一致）
SUBTITLE_CARD_BOTTOM_MARGIN = _env_int("SUBTITLE_CARD_BOTTOM_MARGIN", 0)
SUBTITLE_CARD_STROKE_WIDTH = _env_int("SUBTITLE_CARD_STROKE_WIDTH", 2)  # 主题色描边宽度（像素）

# 引号内关键词高亮的最大长度：超过视为整句引用，不做高亮
SUBTITLE_HIGHLIGHT_QUOTE_MAX = _env_int("SUBTITLE_HIGHLIGHT_QUOTE_MAX", 24)

# ---------- Ken Burns 动效 ----------
# 静态图片段缓推/横移动效；默认开启；设 0/false/off/no 关闭
KEN_BURNS = os.getenv("KEN_BURNS", "1").strip().lower() not in ("0", "false", "off", "no")

# 动效幅度：画面放大量/横移余量（0.06 即约 6%）；上限 0.5、负值按 0（静止），
# 具体钳制在 video._ken_burns_strength() 中做；非法值回退默认不让 import 崩溃
KEN_BURNS_STRENGTH = _env_float("KEN_BURNS_STRENGTH", 0.06)

# 主题色解析失败时的兜底色（与片头片尾卡装饰线同色系）
_DEFAULT_THEME_COLOR = (255, 205, 90)


def subtitle_theme_color() -> tuple[int, int, int]:
    """把 SUBTITLE_THEME_COLOR（#RRGGBB）解析成 RGB 元组；非法值回退默认主题色"""
    raw = (SUBTITLE_THEME_COLOR or "").strip().lstrip("#")
    if len(raw) == 6:
        try:
            return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return _DEFAULT_THEME_COLOR

# ---------- 片头片尾卡 ----------
# 是否在成片开头/结尾插入片头卡（2.5s 渐变+主题大字）和片尾卡（2s 引导关注）
# 默认开启；设 0/false/off/no 关闭
INTRO_OUTRO = os.getenv("INTRO_OUTRO", "1").strip().lower() not in ("0", "false", "off", "no")

# 片头卡文案；留空时用视频主题作大字
INTRO_TEXT = os.getenv("INTRO_TEXT", "")
# 片尾卡文案（引导关注）；留空用内置默认文案
OUTRO_TEXT = os.getenv("OUTRO_TEXT", "")

# ---------- 段间交叉淡化 ----------
# 相邻故事片段之间的交叉淡化秒数；默认 0.6 = 相邻段重叠 0.6 秒平滑过渡
# （转场更自然、成片更紧凑；重叠区字幕已做窗口化，不会出现两层字幕）。
# 设 0 = 关闭（硬切，旧行为）。开启后成片时长 = 各段时长之和 - 相邻段重叠
# （见 video._crossfade_layout），SRT 时间轴需用 generate_srt(crossfade=...) 同步前移。
try:
    CROSSFADE_SECONDS = float(os.getenv("CROSSFADE_SECONDS", "0.6"))
except ValueError:
    CROSSFADE_SECONDS = 0.6  # 非法值回退默认，不中断生成
CROSSFADE_SECONDS = max(CROSSFADE_SECONDS, 0.0)  # 负值视为关闭

# ---------- 背景音乐（BGM） ----------
# 柔和氛围垫乐总音量（0-1）：默认 0.2 = 开启（质量循环第 4 轮：基线成片
# 完全无 BGM,短视频平台观感单薄；0.2 为推荐区间的低档，垫乐按低电平合成
# （见 video._BGM_STYLES），配音始终在前不被盖过）。设 0 = 关闭。
# 非法值由 _env_float 回退默认 0.2；负值与 >1 在 video._bgm_volume() 钳制。
BGM_VOLUME = _env_float("BGM_VOLUME", 0.2)

# 垫乐风格（温度感）: warm（默认·暖夜） / cool（清凉·稍亮） / deep（低沉·氛围暗）
# 非法值回退 warm，不中断生成（与 SUBTITLE_STYLE 的兜底策略一致）
BGM_TEMP = os.getenv("BGM_TEMP", "warm").strip().lower()
if BGM_TEMP not in ("warm", "cool", "deep"):
    BGM_TEMP = "warm"

# 用户自备曲库目录（任务 D7，R1 §五 P0-4 补充）：BGM_DIR 指向本地曲目目录，
# 递归扫描其中的 .mp3/.wav。配置且含可用曲目时优先从曲库选曲（同一项目
# 当日固定一首、跨天重选避开历史已用，记忆文件 项目目录/_bgm_used.json，
# 见 video.pick_bgm_from_library），无可用曲库才回退合成垫乐；音量跟随
# BGM_VOLUME，BGM_VOLUME=0（默认）时曲库按 _USER_BGM_DEFAULT_VOLUME（0.25）
# 混入——明确配置曲库即代表想用，与 web 上传 BGM 同语义；web 上传的单曲
# BGM 仍是最高优先。
BGM_DIR = os.getenv("BGM_DIR", "").strip()


# 预览分辨率：横屏 / 竖屏（与全分辨率同画幅比例，约减半，合成速度大幅提升）
_PREVIEW_LANDSCAPE = (960, 540)
_PREVIEW_PORTRAIT = (540, 960)


def video_size(aspect_ratio: str = "", preview: bool | None = None) -> tuple[int, int]:
    """根据画面比例返回 (宽, 高)。未传参时用全局 ASPECT_RATIO。

    preview=True 时返回预览分辨率（横 960×540 / 竖 540×960）；
    preview=None 时跟随全局 PREVIEW 开关（环境变量 PREVIEW=1 或 CLI --preview）。
    """
    ratio = aspect_ratio or ASPECT_RATIO
    use_preview = PREVIEW if preview is None else bool(preview)
    if use_preview:
        return _PREVIEW_PORTRAIT if ratio == "9:16" else _PREVIEW_LANDSCAPE
    if ratio == "9:16":
        return 1080, 1920
    return 1920, 1080


# ---------- Web 服务 ----------
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = _env_int("WEB_PORT", 5000)

# 项目根目录
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 输出/临时目录根：默认跟随 config.py 所在目录（源码运行 = 仓库根，旧行为不变）。
# pipx 等非 editable 安装后 config.py 位于虚拟环境的 site-packages 里，若不加干预
# 视频会写进 pipx 环境内部，且 pipx upgrade/重装会把历史视频一并清掉——
# 设 STORYVID_HOME 可把 输出(output/) 与 临时(temp/) 目录指到稳定位置
# （pipx 用户建议指向自己的工作/数据目录，见 README「pipx 安装」一节）
_STORYVID_HOME = os.getenv("STORYVID_HOME", "")
if _STORYVID_HOME:
    OUTPUT_DIR = os.path.join(_STORYVID_HOME, "output")
    GLOBAL_TEMP_DIR = os.path.join(_STORYVID_HOME, "temp")
else:
    OUTPUT_DIR = os.path.join(_PROJECT_DIR, "output")
    GLOBAL_TEMP_DIR = os.path.join(_PROJECT_DIR, "temp")

# 全局临时目录（向后兼容）
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


def validate_config(require_llm: bool = True) -> tuple[bool, list[str], list[str]]:
    """按当前配置检查必填项。返回 (是否可用, 致命问题列表, 警告列表)。

    致命问题 = 不修就无法生成（LLM / 所选 TTS 提供商缺 key）
    警告 = 有兜底方案（配图缺 key 会退到备用图）

    require_llm=False 用于文稿直出模式（CLI --script-file 与演示模式 --demo）：
    该模式跳过搜索/审核/LLM 写稿与 LLM 配图关键词，全程不调用 llm.chat，
    因此 LLM_API_KEY 缺失不再是致命问题。注意：web 端并不调用本函数
    （grep validate_config web/ 零命中），web 的 script 模式跳过 LLM 是
    _generate_video_task 自己的 skip_llm_keywords 参数实现的，
    不要据本函数的 require_llm 推断 web 行为。
    """
    problems: list[str] = []
    warnings: list[str] = []

    if require_llm and not LLM_API_KEY:
        problems.append(
            "缺少 LLM_API_KEY（用于生成故事稿）。请在 .env 中配置，"
            "支持 DeepSeek / Kimi / OpenAI 等任意 OpenAI 兼容 API"
        )

    if TTS_PROVIDER == "stepfun":
        if not STEPFUN_API_KEY:
            problems.append("TTS_PROVIDER=stepfun 但缺少 STEPFUN_API_KEY")
    elif TTS_PROVIDER == "minimax":
        if not MINIMAX_API_KEY:
            problems.append("TTS_PROVIDER=minimax 但缺少 MINIMAX_API_KEY")
    elif TTS_PROVIDER == "volc":
        if not VOLC_TTS_APPID or not VOLC_TTS_TOKEN:
            problems.append(
                "TTS_PROVIDER=volc 但缺少 VOLC_TTS_APPID / VOLC_TTS_TOKEN"
                "（火山引擎控制台「语音技术-应用管理」获取）"
            )
    elif TTS_PROVIDER == "edge":
        pass  # Edge TTS 免费无需任何 Key（微软神经语音，联网即用）
    else:
        problems.append(
            f"TTS_PROVIDER 只支持 stepfun / minimax / volc / edge，当前是: {TTS_PROVIDER}"
        )

    if IMAGE_SOURCE == "minimax":
        if not MINIMAX_API_KEY:
            warnings.append("IMAGE_SOURCE=minimax 但缺少 MINIMAX_API_KEY，配图将退回备用随机图")
    elif IMAGE_SOURCE == "unsplash":
        if not UNSPLASH_ACCESS_KEY:
            warnings.append("IMAGE_SOURCE=unsplash 但缺少 UNSPLASH_ACCESS_KEY，配图将退回备用随机图")
    elif IMAGE_SOURCE == "local":
        if not os.path.isdir(LOCAL_IMAGE_DIR):
            warnings.append(f"IMAGE_SOURCE=local 但目录不存在: {LOCAL_IMAGE_DIR}，配图将退回备用随机图")

    if BGM_DIR and not os.path.isdir(BGM_DIR):
        warnings.append(f"BGM_DIR 曲库目录不存在: {BGM_DIR}，背景音乐将回退合成垫乐")

    if LOCAL_MEDIA_DIR and not os.path.isdir(LOCAL_MEDIA_DIR):
        warnings.append(f"LOCAL_MEDIA_DIR 本地素材目录不存在: {LOCAL_MEDIA_DIR}，"
                        f"配图将回退云端来源")

    return (len(problems) == 0), problems, warnings
