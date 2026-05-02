# StoryVid — AI Story Video

输入一个主题，自动完成：**搜索资料 → 内容审核 → AI 写稿 → 语音合成 → AI 配图 → 字幕生成 → 视频合成**，输出可直接发布的短视频。

支持两种工作模式：
- **交互模式（默认）**：每步确认后再执行——写稿后人工审核，确认才继续生成视频
- **全自动模式**：输入主题直接出片，无需中途确认

## 效果预览

```
主题: 房产周期的奥秘
故事稿 → 磁性男声配音 → AI生成配图 → 字幕叠加 → 竖屏MP4
输出: output/2025-05-01_房产周期的奥秘/房产周期的奥秘.mp4
```

## 快速开始

### 1. 安装依赖

```bash
# Python 3.10+
pip install openai flask requests python-dotenv moviepy pillow ddgs

# 系统依赖（音频处理）
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg
```

### 2. 配置 API Keys

```bash
cp .env.example .env
# 编辑 .env 填入你的 API Keys
```

**必填（三选二）**：

| 服务 | 用途 | 获取地址 |
|------|------|----------|
| DeepSeek / OpenAI 兼容 API | 写故事稿 | [platform.deepseek.com](https://platform.deepseek.com) |
| StepFun 或 MiniMax | TTS 语音合成 | [platform.stepfun.com](https://platform.stepfun.com) / [platform.minimaxi.com](https://platform.minimaxi.com) |
| MiniMax 或 Unsplash | AI 配图 | 同上 / [unsplash.com/developers](https://unsplash.com/developers) |

详细配置项见 `.env.example`，里面包含了所有可配参数和说明。

### 3. 启动

**Web 界面（推荐）**：
```bash
python web/app.py
# 打开 http://localhost:5000
# 勾选「全自动模式」= 一步出片，不勾选 = 先审核文稿再生成
```

**命令行**：
```bash
# 交互模式：写稿后手动确认
python main.py "故宫的未解之谜"

# 全自动模式：跳过确认
python main.py "故宫的未解之谜" --auto

# 竖屏 + 额外要求
python main.py "5G如何改变生活" --extra "科普风格，多点数据" --auto
```

## 工作流程

```
[搜索] → [安全审核] → [AI生成故事稿]
                           ↓
              交互模式：人工确认后继续
              全自动模式：直接继续
                           ↓
[分段] → [TTS语音合成] → [AI配图] → [字幕生成] → [MoviePy合成视频]
```

每个任务在 `output/` 下生成独立目录，方便管理或删除：
```
output/2025-05-01_房产周期的奥秘/
├── story.txt          # 故事文稿
├── seg_0001.wav       # 每段语音
├── image_0001.jpg     # 每段配图
├── subtitles.srt      # 字幕文件
└── 房产周期的奥秘.mp4  # 最终视频
```

## 画面比例

- **9:16 竖屏**（1080×1920）— 抖音 / 视频号 / 小红书
- **16:9 横屏**（1920×1080）— B站 / YouTube

## 项目结构

```
video-workflow/
├── main.py              # CLI 入口
├── web/app.py           # Web 入口
├── story.py             # 故事生成 + 自我校验 + 多种风格
├── research.py          # DuckDuckGo 搜索 + 内容安全审核
├── tts.py               # 语音合成（StepFun / MiniMax 双引擎）
├── images.py            # 配图获取（MiniMax AI / Unsplash）
├── subtitles.py         # 字幕生成
├── video.py             # 视频合成（MoviePy + Pillow）
├── config.py            # 全局配置
├── .env.example         # 环境变量模板
├── .gitignore
├── LICENSE
└── output/              # 输出目录
```

## 技术栈

- **LLM**: DeepSeek / 任意 OpenAI 兼容 API
- **TTS**: StepFun (step-tts-2) / MiniMax (speech-01-hd)
- **配图**: MiniMax Image API / Unsplash / 本地图片
- **视频合成**: MoviePy + Pillow
- **搜索**: DuckDuckGo
- **Web**: Flask + 原生 JS

## License

MIT
