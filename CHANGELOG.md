# Changelog

本项目遵循「不提交、只留工作区」的夜间开发方式，因此本文件以工作区未提交变更为准（`git status` / `git diff`）。

## [Unreleased]

### 2026-10-28 深夜（修复「两层字幕」投诉，分支 loop/quality-2)

用户反馈“还是两层字幕”。抽帧确诊：两部成片各 3 帧(含段边界)**烧录字幕始终单层**——真正的第二层是播放器(PotPlayer/QQ影音/mpv fuzzy 等)自动加载了同目录的 `subtitles.srt`,叠在烧录字幕之上。

- **外挂 SRT 默认关闭**：新增 `SUBTITLES_SRT`(默认 0)。CLI 两个调用点与 Web 两个调用点(生成/改词重渲)均改为开关控制；关闭时打印说明(字幕已烧录、需要时设 1)。字幕烧录本身完全不受影响。
- **已有产物清理**:`output/` 下 4 个项目目录的 `subtitles.srt` 已移入各自 `subtitles/` 子文件夹(保留文件、离开播放器自动加载路径)，用户立即可正常观看。
- 测试:515 passed(原 514 + 新增「默认不落盘 SRT」回归 1 例;8 处原有 SRT 断言改为显式开开关后验证)。

### 2026-10-28 晚间（成片质量优化 Loop，分支 loop/quality-2，基于 f983c66）

固定用例「故宫的未解之谜」全自动模式，5 轮循环（每轮 pytest + 端到端出片对比，只有变好且无回退才保留）：

- **R1 字幕单块时长（保留）**：`SUBTITLE_MAX_CHARS` 默认 42 → 26。基线 40 字长句一屏挂 6~7.5s 观感呆滞；切短后最长块 7.54s → 4.39s（首轮干净重测），>6s 长块 5 → 0，块数 55 → 66；时间轴仍由真实词级时间片（words_source=tts）投影保证，无非法块、无负间隙。
- **R2 配图关键词空返回重试（保留）**：OpenCode 网关间歇性返回空 content（实测同 prompt 6 次调用 5 次空），导致每段关键词退化成中文主题、Unsplash 搜不到、整片配图随机化（基线 13/13 段退化、首段配成海滩图）。`generate_image_keywords` 加空返回/行数不足重试（2 次，退避 1.5s）+ 非英文行过滤；本机 Unsplash Key 401 无效（待用户更换），修复后关键词 0/11 退化（如 "arrow on gate lintel" / "Nine Dragon Wall"），Key 恢复后即生效。
- **R3 断点续跑文本校验（保留）**：同主题同日重跑时 LLM 生成不同措辞的新文稿，旧音频被原样复用 → 字幕按新文本切、配音是旧文稿，声画错配。新增文本快照边车 `segment_XXX.mp3.sha1`（生成时写、复用前比对），文稿变了重新合成、合法续跑（中断重跑）照常复用；旧音频无快照保守重合成一次。测试：错配重生成 / 无快照重生成 / 同文本复用 + 真实管线三连跑验证。
- **R4 BGM 默认开启（保留）**：`BGM_VOLUME` 默认 0 → 0.2（低电平垫乐，合成/混音全离线）。验证：成片片头卡区间（无旁白）实测 mean -41dB 有声音 = BGM 存在，正片旁白 mean -19dB 主导（+22dB），配音始终在前。
- **R5 竖屏字幕安全区（保留）**：新增 `SUBTITLE_BOTTOM_MARGIN`（0=自动：竖屏 240 / 横屏 60，三种样式共用 `video._subtitle_bottom_margin`）。基线竖屏文字距底 80px（约 4%）会被抖音/视频号底部 UI 盖住；现竖屏文字位于底部 12.5% 安全区之上（半透明底条仍延伸至画面底边，属设计）。
- 测试基线：504 → 514 passed, 5 skipped。

### 2026-10-28（字幕双层修复 + 本地素材优先 + 节奏提速）

用户看片反馈三项：字幕两层叠画、网上配图不贴题且节奏偏慢、希望制作时优先用自备素材。

**修复**

- **交叉淡化重叠区两层字幕同框**：`CROSSFADE_SECONDS>0` 时后段提前 `overlap` 秒开始（video.py 淡化组合段），但每段字幕块被 `pad_chunk_durations` 补齐到整段时长——重叠窗内前段末句与后段首句在同一几何位置叠画成两层。修复与 SRT 侧（subtitles.py 重叠区窗口化）对齐：`create_video` 提前按标称段时长算淡化布局（与 `generate_srt(crossfade=...)` 同一口径），把每段尾部重叠秒数以 `tail_clearance` 传入 `_build_segment_clip`；新增 `_trim_units_to_window` 截短上屏单元（Ken Burns 路径字幕提前收起、背景/音频不动），烘焙路径补一帧无字幕底图垫住时长（配音淡出段不被裁、时间轴不漂移，垫帧 `frame_XXX_tail.jpg` 仅整段重渲时重写）。测试：tests/test_video.py `TestCrossfadeSubtitleWindow`（7 例：截短/丢弃/全清、两条路径、垫帧无字幕、端到端时长守恒）。
- **字幕帧缓存不比对样式/分辨率**：`_chunks_XXX.json` 快照原只记录切块布局与 karaoke 词表，plain/card 共用帧名——Web 换图/改词（`reuse_frames=True`）时换过 `SUBTITLE_STYLE` 会把旧样式帧原样复用，成片前后段字幕样式混排。快照现写入 `style` + `size` 并参与比对，不一致即整段重渲；旧版快照（无这两个字段）一律判失配重渲一次（宁可慢不可错）。测试：`TestChunksMetaStyleSize`（2 例）。

**新增：本地素材优先（用户自备照片/视频）**

- 核心函数 `images.assign_local_media(segments, media_dir, project_dir)`（images.py）：递归扫描素材目录（图片 jpg/jpeg/png/webp + 视频 mp4/mov/webm/mkv/avi，全局路径排序保证可复现；坏图解码剔除、>200MB 跳过告警）。图片拷贝为 `image_XXX.jpg` 主图位（沿用断点续跑文件约定——`fetch_all_images` 按「已存在且可解码」直接复用不再调云端，多画幅智能裁切 / 候选换图 / segments.json 局部重渲零改动兼容）；文件名/父目录名命中段 `image_keyword` 的优先配对，其余按目录顺序填充未覆盖段（目录顺序即用户摆放意图）；**视频段也照常配图**（视频解码失败的兜底画面 + 封面素材）。视频不拷贝（可能很大），绝对路径记 `seg["video_path"]`，多条视频按 `round(k*n/v)` 在整条片均匀分布；剩余图片按段落成 `image_XXX_alt_N.jpg` 候选（每段上限 `_LOCAL_ALT_PER_SEGMENT=3`），接入既有 Web `/api/candidates` 换图 UI。
- 视频片段渲染：新 `_video_background`（video.py）——`VideoFileClip` 截取到该段配音时长（短则 `vfx.Loop` 循环补齐）、cover 等比适配铺满居中裁切（与 `fit_image_cover` 同一数学）、**原声一律静音**（旁白为主）；视频段不做 Ken Burns（画面自带运动）；解码/适配失败自动回退该段图片路径。叠加层循环从 `_ken_burns_segment_clip` 抽出为 `_segment_overlays` 两条路径共用。`segment_state.py` 落盘字段白名单加入 `video_path`（相对路径规则同 image_path）。测试：`TestVideoSegmentMedia`（7 例，真实 ffmpeg 编解码：截取/循环/静音/坏文件回退/缺文件回退/段合成/端到端）。
- 三个询问入口：CLI `--media-dir` 参数 + 非 `--auto` 交互模式文稿确认后追问「本地素材目录（回车跳过）」（main.py `run_workflow`）；Web 表单新增「本地素材目录」输入框（`/api/generate` 的 `media_dir` 字段，目录不存在 400 拒绝——显式输入不能静默降级；经任务状态透传 `_generate_video_task`，两阶段确认/重新生成均保留）；全局 `LOCAL_MEDIA_DIR` 环境变量（与 `BGM_DIR` 同语义，`validate_config` 对不存在的目录出警告；优先级：调用显式传参 > 环境变量；demo 模式保持全离线占位语义不消费本地素材）。

**节奏提速（默认值调整，均可配置回退）**

- `CROSSFADE_SECONDS` 默认 0 → **0.6**（相邻段 0.6s 平滑过渡、成片更紧凑；依赖上述字幕窗口化修复）；片头卡 2.5s → **1.8s**、片尾卡 2.0s → **1.5s**（短视频前 3 秒完播率关键）。配音语速本轮不动：edge-tts 已接线 `TTS_SPEED`（tts.py rate=+15%），嫌快/慢调 `.env` 的 `TTS_SPEED` 即可。
- 文档同步：README、.env.example（`LOCAL_MEDIA_DIR` / `CROSSFADE_SECONDS` / 片头尾卡说明）。
- 测试基线：504 passed, 5 skipped（原 476 → 新增 28）。

### 新增（10-27 收尾）

- **Edge TTS 第四 provider（`TTS_PROVIDER=edge`）**：微软神经语音，免费无需任何 Key，
  音频流自带词级时间戳（WordBoundary）直接进 D2 对轴通道（卡拉OK字幕可零 Key 使用）；
  默认音色 zh-CN-YunxiNeural，语速沿用 TTS_SPEED；7 项新测试
- **LLM OpenCode 路由兼容**：base_url 含 opencode.ai 时自动附带 x-opencode-session 头
 — 2026-10-26 夜间

> 范围说明：`git diff HEAD` 的未提交变更 = 上一轮 MoviePy 2.x 翻新 + 本夜（10-26）新功能两批叠加，
> 均未提交，以下合并记录。各条目的行号为撰写时点 grep 实测值；工作区仍有多任务并行改动，行号可能漂移。
> 修订记录：初版曾注「豆包 TTS 适配器（A5）尚未出现在工作区」，经 B 队检测指出该判断已过时——
> 现已落地（实测 `grep -in volc tts.py config.py .env.example` 大量命中），已补入「新增」组并清除过时注明。

### 修复

- **配图非同画幅直接拉伸变形（P0-1，R1_competitive.md §五）**：合成层底图原为 `img.resize((width, height))` 直接拉伸（原 video.py:316 prepare_base_image、:522 Ken Burns 背景），横图进竖屏（或反向）整段画面变形。新增 `video.fit_image_cover`（video.py:309）：按目标宽高比取源图最大居中裁剪框、再 LANCZOS 一次缩放到目标画幅（「先裁后缩」，ImageOps.fit 思路；cover 语义与 `images.crop_to_fill` / `cover._cover_crop` 一致），两条底图路径（烘焙路径 `prepare_base_image`、Ken Burns 路径 `_ken_burns_background`）统一接入；同画幅输入裁剪框=整图，行为与旧纯 resize 逐像素一致。内存上界与源画幅比例无关——唯一一次 resize 的目标恒为输出画幅，不会像「先缩后裁」那样对极端画幅源（1×5000 → 480×270）先放大出 ≈3.46GB 中间图（B 队检测后由初版实现修正，实测 13.19s→0.9ms）。测试：tests/test_video.py 新增 `TestImageCoverFit`（圆形素材不变形 / 三色带竖图中心裁切 / 同画幅逐像素回归 / 极端画幅不崩 / resize 目标监听防内存放大 / Ken Burns zoom+pan 两动效），`TestPrepareBaseImage` 同画幅回归测试随之更名。
- **Web 多任务并发时项目目录互相串写**：管线各步原来回读全局 `config.PROJECT_DIR`，并发任务 B 一 `setup` 就会改写全局，A 的音频/图片/视频写进 B 的目录。现给 `generate_all_speech`（tts.py）、`fetch_all_images` 全链路（images.py）、`create_video`（video.py）加 `project_dir=""` 参数（空值回退全局，兼容 CLI），Web 端（web/app.py）与 CLI（main.py）显式传本任务目录；回归测试 `tests/test_web.py::TestTwoPhaseFlow::test_pipeline_writes_into_task_project_dir`。
- **SRT 时间轴毫秒不进位**：`subtitles._format_time` 旧实现先取小数毫秒再封顶 999，`1.9999s` 会写成 `00:00:01,999`，长视频累计漂移。改为总毫秒 `divmod` 进位（subtitles.py），`59.9995 -> 00:01:00,000` 已实测。
- **段间交叉淡化开启时 SRT 产出非法区间**：`generate_srt` 原来只截尾块 end 不 clamp start，重叠较大时出现 end<start、零时长块。已修复并补 2 个回归测试（tests/test_video.py TestCrossfade）。
- **未配置 API Key 时 `import` 即崩**：StepFun 客户端原为模块级 `OpenAI(...)`，未配 key 时 `import tts` 直接抛错。抽出 `llm.py`（懒加载客户端，首次调用才创建并给出中文指引）与 `tts._get_stepfun_client()`（tts.py），`--help`、测试、web 启动均不再被配置卡死。
- **配置写错值让服务起不来**：新增 `config._env_int` / `config._env_float` 守卫，`SUBTITLE_CARD_*`、`KEN_BURNS_STRENGTH`、`TARGET_LUFS`、`BGM_VOLUME` 等数值型环境变量缺失/空串/非数值一律回退默认并打印警告（config.py），import 阶段不再抛 `ValueError`。
- **内容审核服务不可用导致流程中断**：`research.py` 审核调用失败时改为打印告警并默认放行（标注「审核服务不可用，默认通过」），不再阻塞出片。
- **响度归一失败不阻塞出片**：`tts.normalize_loudness` 两遍法 loudnorm 任何一步失败（ffmpeg 缺失/静音段/解析失败）都保留原文件继续（tts.py）；纯静音段直接跳过归一。
- **本地图片目录不存在时崩溃 / 备用图目录未创建**：`images.get_local_image` 改为动态读 `config.LOCAL_IMAGE_DIR` 并妥善回退；fallback 图写盘前先建目录（images.py）。
- **Web 画幅比例/音色设置不生效**：改为按任务显式传参（`aspect_ratio`/`voice` 进 `_generate_video_task`），不再互相覆盖（web/app.py；README 同步说明）。

### 新增

- **用户自备曲库 BGM_DIR（D7，R1 §五 P0-4 补充）**：合成垫乐之外支持用户自备本地曲库——`.env` 设 `BGM_DIR` 指向曲目目录（递归扫描 `.mp3`/`.wav`，支持子文件夹，video.py `bgm_library_tracks`），混音音源优先级变为「web 上传 BGM > 曲库选曲 > 合成垫乐」（video.py `_mix_bgm`）：有可用曲库时随机选曲（`pick_bgm_from_library`），同一项目**当日固定一首不重复**（多画幅批量渲染 / web 换图·改词局部重渲 / CLI 断点续跑时配乐保持一致，不会同一支视频换歌），跨天重选避开历史已用直到整库轮完一圈；选曲记忆原子落盘 `项目目录/_bgm_used.json`（损坏视为空记忆不阻塞）。曲库音量语义与上传 BGM 相同：跟随 `BGM_VOLUME`，`BGM_VOLUME=0`（默认）时按 `_USER_BGM_DEFAULT_VOLUME`（0.25）自动开启——配置曲库即代表想用；曲库曲目与合成垫乐走同一段「截断/循环铺满 + MultiplyVolume + 首尾淡入淡出」管线（淡入淡出共存），单个坏文件自动回退合成垫乐；`config.validate_config` 对指向不存在目录的 `BGM_DIR` 出警告（config.py）。文档同步 README「背景音乐」与 `.env.example`。测试：tests/test_bgm.py 新增 `TestBgmLibrary`（扫描递归/选曲记忆/当日固定/跨天避开历史/整库轮尽重置/记忆损坏容忍，曲库两首测试曲由 ffmpeg 现场合成：一首 mp3 + 一首 wav）+ `TestBgmLibraryMixing`（曲库优先于合成垫曲、BGM_VOLUME=0 自动开启、空曲库与坏文件回退合成、上传仍最高优先、端到端真实出片验证曲库曲目混入 + 淡入淡出 + 短曲循环铺满）。
- **分段改词局部重跑（D6，R1 §五 P1-3 / §一#8）**：改任何一段的词不再整条重跑。新模块 `segment_state.py`——段级状态（文本/时长/配音/配图路径/关键词/词级时间戳）原子落盘 `项目目录/segments.json`（路径存相对项目目录形式，载入时还原绝对；CLI `_produce_video` 单/批画幅与 Web 任务完成、D4 换图后均同步落盘）；Web 新增 `GET /api/segments/<task_id>`（列段状态）与 `POST /api/segment/edit`（JSON `{task_id,index,text}`）：后台线程更新该段文本→删除旧 `segment_XXX.mp3` 并作废旧词级时间戳→`generate_all_speech` 按 A12 断点续跑语义只真正重新 TTS 被改段（其余段 mp3 已存在直接复用）→重建字幕→各画幅 `reuse_frames=True + rerender_segments={index}` 局部重渲重新合成→封面重出→segments.json 回写（web/app.py `_segment_edit_task`，并发闸与 D4 `/api/select` 同构）。前端 index.html 成片后展示分段列表 + 「改词重跑」入口（视频 src 带时间戳参数强制刷新）。测试：新文件 tests/test_segment_state.py（6 例读写往返）+ tests/test_web.py `TestSegmentEditAPI`（8 例：只重跑被改段/产物更新其他段不动/连改两段以磁盘状态为事实源/多画幅 frame_tag）+ tests/test_resume.py `TestMainSegmentsJson`（CLI 落盘），全量 pytest 434 passed 5 skipped。
- **配图多候选 + 相关度选图 + 一键换图（P1-1，D4）**：Unsplash 改为按相关度取第一张（R1 §一#7，原 `random.choice` 随机选图让配图匹配度抖动），`fetch_unsplash_image(random_pick=True)` 可恢复旧行为（images.py:113）；MiniMax 生图支持 `n>1` 多候选落盘——主图 `image_XXX.jpg` + 候选 `image_XXX_alt_N.jpg`，候选数由 `MINIMAX_IMAGE_N` 配置或 `generate_minimax_image(n=...)` 指定，单张候选下载失败只跳过不整单作废（images.py:156）；新增候选管理 `images.list_candidates` / `images.swap_image_candidate`（换图 = 候选与主图位原子对调，原主图挪到空余 alt 槽位仍可换回，选择结果落在本程序文件约定里，A12 断点续跑复用 `image_XXX.jpg` 时自然继承人工选择，images.py:278-343）。Web 新增 `GET /api/candidates/<task_id>/<seg_index>`（列候选）与 `POST /api/select`（选定后后台线程仅重渲该段字幕帧并按画幅重新合成，web/app.py:774-881）：`video.create_video` 新增 `reuse_frames` / `rerender_segments` / `frame_tag` 参数——未选中段复用上一轮已渲染的字幕帧/叠加层（切块布局快照 `_chunks_XXX.json` 一致 + 文件可解码才复用，布局漂移或坏帧自动回退重渲；多画幅缓存帧按画幅后缀分开命名防互相覆盖），换一张配图不再重画全片字幕（video.py:553, 670-717, 977）；任务状态新增段级清单（`segments`/`video_stem`/`bgm_used` 等，web/app.py:350-364）。测试：新文件 tests/test_candidates.py（16 例）+ tests/test_video.py `TestReuseFramesRerender`（5 例，真实离线合成）+ tests/test_web.py `TestImageCandidatesAPI`（8 例 flask test client），全量 pytest 417 passed。
- **词级时间戳引擎无关兜底对齐（P0-2 Tier3，D3）**：新模块 `aligner.py`——TTS 通道未产出词级时间戳的段（含断点续跑复用段）用本地 Whisper 系引擎对配音 mp3 做词级对齐，补 `seg["words"] = [{"word","start","end"}, ...]` + `seg["words_source"]="aligner"`（与 TTS 时间戳通道共用同一字段，`generate_all_speech` 末尾接线、只补缺不覆盖）；引擎二选一 `faster-whisper`（优先，ctranslate2 无 torch）/ `whisper-timestamped`，检测用 `importlib.util.find_spec` 只查安装不 import 大依赖，`ALIGNER=auto（默认）/off/引擎名` 控制，未安装时打印安装指引并优雅跳过、**绝不自动安装**（pip 不在代码路径里）；对齐结果缓存 `segment_XXX.words.json`（text/engine/model 校验失效，断点续跑不重复推理）；模型进程内缓存、惰性加载。测试 tests/test_aligner.py（29 例：env 开关/引擎检测/双引擎 mock 时间戳结构适配/缓存与失效/通道接线，注入 sys.modules mock 引擎模块走真实适配代码路径；真实模型验证用例 importorskip 标注待办）。
- **断点续跑（A12）**：`generate_all_speech` / `fetch_all_images` 遇到项目目录里已存在且非空的 `segment_XXX.mp3` / `image_XXX.jpg` 直接复用（打 `audio_reused` / `image_reused` 标记，不再调 API、复用段不再做响度归一），中断重跑不浪费已花钱的调用；单段失败自动重试 2 次（退避 2s/8s，常量 `TTS_RETRY_DELAYS` / `IMAGE_RETRY_DELAYS`），仍失败才标记跳过（无配音/黑底帧）；CLI（main.py）在语音/配图步骤后输出「检测到 N 个已完成段，已复用」（web/app.py 无需改动，复用逻辑对它透明）；测试 tests/test_tts.py（TestGenerateAllSpeechResume / TestGenerateAllSpeechResumeRealAudio / TestGenerateAllSpeechRetry）+ 新文件 tests/test_resume.py（配图复用/重试 + main 提示），全量 pytest 224 passed。
- **Ken Burns 静图运镜**：静态图片段缓推/横移动效，逐句字幕改为透明叠加层保证缩放连续；`KEN_BURNS`（默认开）、`KEN_BURNS_STRENGTH`（默认 0.06，钳制 [0, 0.5]）（video.py `_ken_burns_*`、config.py）。
- **段间交叉淡化**：`CROSSFADE_SECONDS`（默认 0=硬切），画面 `CrossFadeIn` + 音频 `AudioFadeIn/Out` 混音，成片时长 = 各段之和 − 重叠，SRT 自动同步前移（video.py `_crossfade_layout`、subtitles.py `generate_srt(crossfade=...)`）。
- **片头片尾卡**：片头 2.5s 渐变底+主题大字、片尾 2s 引导关注；`INTRO_OUTRO`（默认开）、`INTRO_TEXT`、`OUTRO_TEXT` 可配（video.py `_build_intro_card` 等）。
- **背景音乐（离线合成）**：`BGM_VOLUME`（默认 0=关）+ `BGM_TEMP`（warm/cool/deep 三档风格），用 imageio-ffmpeg 自带 ffmpeg lavfi 合成低通噪声+正弦和弦垫乐，`CompositeAudioClip` 混音、首尾自适应淡入淡出，无需任何 API Key（video.py `generate_bgm`/`_mix_bgm`）。
- **封面自动生成**：新模块 `cover.py`——渐变底色（按主题 MD5 五选一）+ 半透明遮罩 + 可混入首图 + 大标题自动换行 + 底部日期时长副标题，输出 `项目目录/cover.jpg`；`COVER_TITLE` 可覆盖标题；CLI（[7/7] 步骤）与 Web（任务状态新增 `cover_rel_path`）均已接入，失败仅告警。
- **文稿直出**：CLI `python main.py [主题] --script-file 故事稿.txt`（main.py `run_workflow_from_script`，管线抽公共函数 `_produce_video`）；Web `/api/generate` 新增 `script` 字段（与 `topic` 二选一，缺主题自动取文稿首行），跳过搜索/审核/LLM 写稿，且对 script 模式禁用「重新生成故事稿」；`config.validate_config(require_llm=False)` 豁免 LLM key 校验（main.py、web/app.py、config.py、story.py）。
- **字幕样式 plain/card**：`SUBTITLE_STYLE`（默认 plain）；card 为底部圆角半透明卡片条 + 主题色描边 + 数字/引号内关键词高亮，参数集中在 config.py（`SUBTITLE_THEME_COLOR`、`SUBTITLE_CARD_*`、`SUBTITLE_HIGHLIGHT_QUOTE_MAX`）（video.py `_draw_card_subtitle` 等）。
- **TTS 配音响度归一**：每段 mp3 用 ffmpeg loudnorm 两遍法归一到 `TARGET_LUFS`（默认 -16），`LOUDNESS_NORM`（默认开）可关；消除不同段忽大忽小（tts.py `normalize_loudness`）。
- **快速预览模式**：`PREVIEW=1`（env）或 CLI `--preview`，以 960×540 / 540×960 渲染快速迭代，Web 任务状态带 `preview` 标记与提示（config.py `video_size(preview=...)`、main.py、web/app.py）。
- **字幕逐句切分**：整段字幕墙改为按句切块、按字符数比例分配时长，超长再按 `SUBTITLE_MAX_CHARS`（默认 42）在逗号处二次切分；SRT 同粒度（subtitles.py `split_text_into_chunks`/`build_chunk_durations`）。
- **火山引擎（豆包）TTS 适配器（A5）**：`TTS_PROVIDER=volc` 时走火山引擎「HTTP 非流式合成接口」（tts.py:212 `generate_speech_volc`，文档 https://www.volcengine.com/docs/6561/79820 ；分发 tts.py:289-290）；配置 `VOLC_TTS_APPID` / `VOLC_TTS_TOKEN` / `VOLC_TTS_CLUSTER`（默认 volcano_tts）/ `VOLC_TTS_VOICE_TYPE`，另有 API_URL / ENCODING / SPEED_RATIO / VOLUME_RATIO 可选项（config.py:35-47、.env.example:36-50）；README 新增「火山引擎（豆包）TTS」专门小节（README.md:89-107）；离线测试覆盖提供商分发与 volc 路由（tests/test_tts.py `test_volc_provider_routes_to_volc` 等）。
- **StepFun 音色/语调控制**：`TTS_VOICE_LABEL`（语言/情绪/风格 JSON）与 `TTS_INSTRUCTION`（全局自然语言指导，仅 stepaudio-2.5-tts）透传（tts.py、config.py）。
- **图片来源扩展**：`IMAGE_SOURCE=minimax/unsplash/local` 三源 + Picsum 备用随机图兜底（images.py）；配图整体降为可选，不配 key 也能出片。
- **离线测试套件**：新目录 `tests/`（config/llm/story/research/tts/subtitles/video/bgm/cover/web/script_direct，A12 后增至 12 个文件含 test_resume.py），全部离线无需 API Key；配套 `pyproject.toml`（pytest 配置）与 `requirements-dev.txt`（pytest>=8.0.0）；用例数见「测试基线」（224）。
- **新机器安装引导**：README「新机器安装」一节，覆盖 python3-venv 缺失时的 `--without-pip` + `get-pip.py` 实测路径（见 README）。
- **演示模式（B3）**：CLI `--demo`（env `DEMO=1` 同效）零 Key 全离线出片——内置示例文稿（新模块 `demo.py`，可 `--demo --script-file` 换用户文稿），跳过搜索/审核/LLM；TTS 换每段固定 3 秒静音 mp3（`demo.fill_demo_speech`，imageio-ffmpeg 自带 ffmpeg `anullsrc` 离线合成）；配图换 PIL 渐变大字占位图（`demo.fetch_demo_images`，复用 `video._build_card_frame`，逐段轮换 6 组配色）。`validate_config` 在演示模式整体跳过（一个 Key 都不需要）；默认联动预览分辨率快速渲染（显式设 `PREVIEW=0` 可全分辨率）；分段/字幕/合成/封面/批量画幅/断点续跑全部沿用正式管线。测试 tests/test_demo.py 17 例（含 `--demo` 从 argv 到 mp4 落地的端到端 + 全 API 出口哨兵验证零网络）。

### 变更

- **MoviePy 1.0.3 → 2.x 全量适配**：video.py 为本次 diff 最大改动（约 850 行），`concatenate_videoclips`/特效/音频 API 全面迁移至 MoviePy 2.x 写法；`requirements.txt` 同步升级 `moviepy>=2.0.0`、`openai>=1.40.0`、`Pillow>=11.0.0`、`ddgs>=9.0.0`（Python 3.10+，3.14 实测通过）。
- **输出文件名变化**：项目目录产物由 `seg_0001.wav`/`image_0001.jpg` 改为 `segment_000.mp3`/`image_000.jpg`，新增逐句字幕帧 `frame_000_00.jpg` 与 `cover.jpg`（README 项目结构已同步）。
- **CLI 管线重构**：`run_workflow` 与文稿直出共用 `_produce_video`，步骤编号 [x/6] → [x/7]（新增封面步），新增 `--preview`、`--script-file/-s`，`topic` 变为可选位置参数（main.py）。
- **Web 端健壮化**：任务结构显式记录 `aspect_ratio`/`voice`，新增 `_spawn_worker`（线程启动统一出口）、`_prune_tasks`（任务表清理）、预览与封面字段（web/app.py）。
- **默认监听地址收紧**：Web 默认 `127.0.0.1`（原 README 写 0.0.0.0 语境），新增 `WEB_HOST`/`WEB_PORT` 环境变量可改（config.py、web/app.py、.env.example）。

### 文档

- **README.md**：补 2026-10 翻新说明、可选配图与降级策略、健壮性设计说明、背景音乐用法、火山引擎（豆包）TTS 使用小节（README.md:89-107）、测试一节、项目结构更新（llm.py/tests/cover 等）、技术栈更新（MoviePy 2.x / Python 3.14）；本次再补「新机器安装」（python3-venv 缺失引导 + Python 3.14 兼容说明）。
- **.env.example**：全量重排并补注释——LLM/TTS（含 StepFun voice_label/instruction、火山引擎豆包 VOLC_TTS_* 模板）配图分组、片头片尾卡、交叉淡化、BGM、封面标题、WEB_HOST/WEB_PORT、字体与字幕参数说明。
- **CHANGELOG.md**：本文件，首次建立。

### 测试基线

- 本夜全量基线随并行任务持续增长：A2 时点 164 passed → A4 修订时点 199 passed → A12 落地后 224 用例（B 队检测时点 `--collect-only` 收集 216，并行任务继续追加）。
- A6 修订版实测（2026-10-26）：`cd storyvid && .venv/bin/python -m pytest -q` →
  **`224 passed, 53 warnings in 19.82s`**，0 失败；`.venv/bin/python -m pytest --collect-only -q` → `224 tests collected`。
  （53 条 warning 为 numpy 弃用提示，来自 moviepy/Pillow 依赖，非测试失败；实测 `pwd` 为 storyvid，Python 3.14.4 + pytest venv。）
