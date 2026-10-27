"""配图获取"""
import os
import random
import re
import shutil
import time
import requests
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

_HEADERS = {"User-Agent": "StoryVid/0.2 (+https://github.com/questionjie-max/storyvid)"}

# 单段配图获取失败后的自动重试退避（秒）：首次失败等 2s 重试、第二次失败等 8s 再试，
# 共尝试 3 次，仍失败才标记该段跳过（成片用黑底帧）。测试可 monkeypatch 本常量加速。
IMAGE_RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0)


def _is_ready_file(path: str) -> bool:
    """断点续跑判断：文件已存在且非空，视为上次运行已完成、可直接复用。"""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _is_valid_image_file(path: str) -> bool:
    """断点续跑图片复用判据：存在、非空、且能被 PIL 完整解码。

    进程中断可能留下写到一半的 jpg——文件存在但已损坏，只查「存在且非空」
    会把坏图当成品复用，合成阶段 Image.open 直接崩溃、整次渲染白费
    （音频侧有 get_audio_duration 的 5s 兜底，图片侧此前无兜底，
    见 overnight_reports/A12_resume.md「遗留 1」，B 队复核确认的缺陷）。
    损坏文件由调用方删除后按缺失段重新获取。
    """
    if not _is_ready_file(path):
        return False
    try:
        with Image.open(path) as img:
            img.load()
        return True
    except Exception:
        return False


def _resolve_project_dir(project_dir: str = "") -> str:
    """任务显式传入的目录优先；为空时回退全局项目目录（兼容 CLI 旧用法）"""
    proj_dir = project_dir or config.get_project_dir()
    os.makedirs(proj_dir, exist_ok=True)
    return proj_dir


def _ratio_tag(aspect_ratio: str) -> str:
    """画幅比例 → 文件名安全后缀：16:9 → 16x9（冒号在 Windows 文件名非法）"""
    return (aspect_ratio or "").strip().replace(":", "x").replace("/", "x") or "default"


def crop_to_fill(src_path: str, dst_path: str, width: int, height: int) -> str | None:
    """同一底图智能裁切：等比缩放到铺满目标画幅后居中裁切（离线，不调任何 API）。

    与 cover._cover_crop 同一套数学（cover.py:58-65），独立实现避免模块耦合。
    任何一步失败返回 None（调用方回退底图），成功返回 dst_path。
    """
    try:
        with Image.open(src_path) as img:
            photo = img.convert("RGB")
        sw, sh = photo.size
        if sw <= 0 or sh <= 0:
            return None
        scale = max(width / sw, height / sh)
        nw, nh = max(width, round(sw * scale)), max(height, round(sh * scale))
        photo = photo.resize((nw, nh), Image.Resampling.LANCZOS)
        left, top = (nw - width) // 2, (nh - height) // 2
        photo = photo.crop((left, top, left + width, top + height))
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        photo.save(dst_path, quality=95)
        return dst_path
    except Exception as e:
        print(f"[Image] 智能裁切失败 ({src_path} → {dst_path}): {e}")
        return None


def derive_crops_for_aspect(
    segments: list[dict],
    aspect_ratio: str,
    project_dir: str = "",
) -> list[dict]:
    """批量渲染的配图复用：把底图（image_XXX.jpg）按目标画幅智能裁切成
    image_XXX_{16x9|9x16}.jpg，并把各段 image_path 指到对应画幅的派生图。

    批量出横竖两版时配图只获取一次（省一半配图 API 花费，与断点续跑的
    省钱哲学一致），第二版起全部走本函数离线派生。每次都重新生成派生图
    （纯 PIL 缩放裁切，秒级、不花钱），避免 PREVIEW 开关切换后复用过期分辨率。
    底图缺失（获取失败的段）保持原 image_path 不动，由合成层用黑底帧兜底。
    project_dir 为空时回退全局项目目录。
    """
    proj_dir = _resolve_project_dir(project_dir)
    width, height = config.video_size(aspect_ratio)
    for i, seg in enumerate(segments):
        base = seg.get("image_path")
        if not base or not os.path.isfile(base):
            continue  # 无底图：保持原样（黑底帧兜底）
        tag = _ratio_tag(aspect_ratio)
        dst = os.path.join(proj_dir, f"image_{i:03d}_{tag}.jpg")
        cropped = crop_to_fill(base, dst, width, height)
        if cropped:
            seg["image_path"] = cropped
        # 裁切失败：保留底图路径，由合成层拉伸兜底（degraded 但不中断）
    return segments


def fetch_unsplash_image(keyword: str, index: int, aspect_ratio: str = "", project_dir: str = "",
                         random_pick: bool = False) -> str | None:
    """从 Unsplash 搜索免费图片。

    默认按相关度取第一张（R1 §一#7：Unsplash 搜索接口本身按相关度排序，
    旧实现 random.choice 随机选图会让配图与关键词的匹配度抖动）；
    random_pick=True 时恢复旧行为（命中结果里随机选一张）。
    """
    ratio = aspect_ratio or config.ASPECT_RATIO
    url = "https://api.unsplash.com/search/photos"
    headers = {"Authorization": f"Client-ID {config.UNSPLASH_ACCESS_KEY}"}
    orientation = "portrait" if ratio == "9:16" else "landscape"
    params = {
        "query": keyword,
        "per_page": 10,
        "orientation": orientation,
    }
    output_path = os.path.join(_resolve_project_dir(project_dir), f"image_{index:03d}.jpg")

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        results = data.get("results", [])

        if not results:
            params["query"] = "nature landscape"
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            data = resp.json()
            results = data.get("results", [])

        if results:
            chosen = random.choice(results) if random_pick else results[0]
            img_url = chosen["urls"]["regular"]
            img_data = requests.get(img_url, timeout=15, headers=_HEADERS).content
            with open(output_path, "wb") as f:
                f.write(img_data)
            return output_path
    except Exception as e:
        print(f"[Image] Unsplash 获取失败 ({keyword}): {e}")

    return None


def generate_minimax_image(keyword: str, index: int, aspect_ratio: str = "", project_dir: str = "",
                           n: int = 0) -> str | None:
    """用 MiniMax 生成图片，支持 n>1 多候选落盘（R1 §五 P1-1）。

    n>1 时请求一次生成 n 张：第一张成功的存主图位 image_XXX.jpg（返回值），
    其余成功的依次存 image_XXX_alt_1.jpg、image_XXX_alt_2.jpg……，
    供 Web /api/candidates 列出候选、/api/select 换图。
    n<=0 时读 config.MINIMAX_IMAGE_N（默认 1 = 旧行为，不产生候选文件）。
    单张候选下载失败只跳过该张（不影响主图与已落盘的候选）；全部失败返回 None，
    由调用方按来源降级链继续（与旧行为一致）。
    """
    if not config.MINIMAX_API_KEY:
        print(f"[Image] MiniMax API Key 未配置，跳过")
        return None

    proj_dir = _resolve_project_dir(project_dir)
    output_path = os.path.join(proj_dir, f"image_{index:03d}.jpg")
    prompt = f"{keyword}, high quality, cinematic lighting, professional photography"

    minimax_ratio = "9:16" if (aspect_ratio or config.ASPECT_RATIO) == "9:16" else "16:9"

    count = max(1, int(n)) if n and int(n) > 0 else max(1, int(config.MINIMAX_IMAGE_N))
    payload = {
        "model": config.MINIMAX_IMAGE_MODEL,
        "prompt": prompt,
        "aspect_ratio": minimax_ratio,
        "response_format": "url",
        "n": count,
        "prompt_optimizer": True,
    }

    try:
        # n>1 时服务端生成耗时随张数增长，超时按张数线性放宽（上限 300s）
        timeout = min(60 * count, 300)
        resp = requests.post(
            f"{config.MINIMAX_BASE_URL}/v1/image_generation",
            headers={
                "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        data = resp.json()

        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            print(f"[Image] MiniMax 错误: {base_resp.get('status_msg')}")
            return None

        img_urls = data.get("data", {}).get("image_urls", [])[:count]
        if not img_urls:
            print(f"[Image] MiniMax 返回空图片列表")
            return None

        saved = ""
        alt_no = 0
        for img_url in img_urls:
            try:
                img_data = requests.get(img_url, timeout=30, headers=_HEADERS).content
            except Exception as e:
                print(f"[Image] MiniMax 候选图下载失败（跳过该张）: {e}")
                continue
            if not img_data:
                continue
            if not saved:
                with open(output_path, "wb") as f:
                    f.write(img_data)
                saved = output_path
                print(f"[Image] MiniMax 生图成功: {output_path}")
            else:
                alt_no += 1
                alt_path = os.path.join(proj_dir, f"image_{index:03d}_alt_{alt_no}.jpg")
                with open(alt_path, "wb") as f:
                    f.write(img_data)
                print(f"[Image] MiniMax 候选 {alt_no}: {os.path.basename(alt_path)}")
        # 一张都没落盘（下载全败）按失败处理，返回 None 交给来源降级链
        return saved or None
    except Exception as e:
        print(f"[Image] MiniMax 生图失败 ({keyword}): {e}")
        return None


def get_local_image(index: int, project_dir: str = "") -> str | None:
    """从本地目录随机选一张图"""
    if not os.path.exists(config.LOCAL_IMAGE_DIR):
        return None
    files = [f for f in os.listdir(config.LOCAL_IMAGE_DIR)
             if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    if not files:
        return None
    selected = random.choice(files)
    src = os.path.join(config.LOCAL_IMAGE_DIR, selected)
    output_path = os.path.join(_resolve_project_dir(project_dir), f"image_{index:03d}.jpg")
    shutil.copy2(src, output_path)
    return output_path


# ---------- 本地素材优先（用户自备图片/视频：LOCAL_MEDIA_DIR / CLI --media-dir / web media_dir） ----------
# 语义：制作前先问用户「有没有自己的素材」。目录里有就优先用（成片更贴主题），
# 没覆盖到的段再走既有云端配图降级链（minimax/unsplash → picsum）。

_LOCAL_MEDIA_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_LOCAL_MEDIA_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")
_LOCAL_MEDIA_FILE_LIMIT = 200 * 1024 * 1024  # 单文件上限 200MB：过大素材跳过并告警
_LOCAL_ALT_PER_SEGMENT = 3                   # 每段最多追加多少张本地候选图（防候选爆炸）


def scan_media_dir(media_dir: str) -> tuple[list[str], list[str]]:
    """递归扫描素材目录 → (图片列表, 视频列表)，均按路径排序保证分配可复现。

    只按扩展名识别 + 过滤隐藏文件；超过 _LOCAL_MEDIA_FILE_LIMIT 的文件跳过并
    告警（真实素材库常有导出的大视频，整库拷贝既慢又双倍占盘）。图片先做
    解码校验，坏文件（半截导出等）直接剔除，不进分配池。
    """
    images: list[str] = []
    videos: list[str] = []
    bad_images = 0
    for root, _dirs, files in os.walk(media_dir):
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()
            if ext not in _LOCAL_MEDIA_IMAGE_EXTS + _LOCAL_MEDIA_VIDEO_EXTS:
                continue
            try:
                if os.path.getsize(path) > _LOCAL_MEDIA_FILE_LIMIT:
                    print(f"[Media] 素材超过 {_LOCAL_MEDIA_FILE_LIMIT // (1024 * 1024)}MB 上限，"
                          f"跳过: {path}")
                    continue
            except OSError as e:
                print(f"[Media] 素材无法读取（跳过）: {path}: {e}")
                continue
            if ext in _LOCAL_MEDIA_IMAGE_EXTS:
                if _is_valid_image_file(path):
                    images.append(path)
                else:
                    bad_images += 1
            else:
                videos.append(path)
    if bad_images:
        print(f"[Media] {bad_images} 张图片无法解码（已剔除，不影响其余素材）")
    images.sort()
    videos.sort()
    return images, videos


def _media_match_score(path: str, tokens: set[str]) -> int:
    """素材文件与段关键词的匹配分：文件名/父目录名每命中一个关键词记 1 分。

    关键词 tokens 以英文为主（LLM 生成的 image_keyword），本地素材名若是中文
    通常打 0 分——此时走按文件顺序填充，顺序本身也是用户摆放素材的意图。
    """
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    parent = os.path.basename(os.path.dirname(path)).lower()
    text = f"{stem} {parent}"
    return sum(1 for tok in tokens if len(tok) >= 2 and tok in text)


def assign_local_media(segments: list[dict], media_dir: str, project_dir: str = "") -> int:
    """把用户素材目录里的图片/视频优先分配给各段（本地素材优先），返回分配到的段数。

    media_dir 为空/不存在/无可用素材时返回 0，调用方照旧走云端配图降级链。

    分配策略：
    - 图片拷贝为 image_XXX.jpg 主图位（沿用断点续跑文件约定，fetch_all_images
      会按「已存在且可解码」直接复用、不再调配图 API；多画幅智能裁切、候选
      换图、segments.json 局部重渲全部零改动兼容）。文件名/父目录名命中段
      关键词的优先配对，其余图片按文件顺序填充没覆盖到的段（目录顺序即用户
      摆放意图）；视频段也照常配图，作为视频解码失败时的兜底画面与封面素材。
    - 视频不拷贝（可能很大），绝对路径记到 seg["video_path"]，由合成层
      （video._video_background）截取到该段配音时长、静音后当背景；多条视频
      在整条片中均匀分布，不全挤在开头。
    - 剩余图片轮流落成各段 image_XXX_alt_N.jpg 候选（每段最多
      _LOCAL_ALT_PER_SEGMENT 张），接入既有 Web /api/candidates 换图 UI——
      自己的备选素材也能一键换上主图位。
    """
    if not media_dir:
        return 0
    if not os.path.isdir(media_dir):
        print(f"[Media] 本地素材目录不存在，忽略（后续配图走云端）: {media_dir}")
        return 0
    img_pool, video_pool = scan_media_dir(media_dir)
    if not img_pool and not video_pool:
        print(f"[Media] 本地素材目录里没有可用图片/视频: {media_dir}")
        return 0
    print(f"[Media] 本地素材扫描完成: {len(img_pool)} 张图片 / {len(video_pool)} 段视频"
          f"（目录: {media_dir}）")

    proj_dir = _resolve_project_dir(project_dir)
    n = len(segments)

    # --- 视频：第 k 条放在 round(k*n/v) 段，均匀铺满整条片 ---
    if video_pool and n > 0:
        count = min(len(video_pool), n)
        used_idx: set[int] = set()
        for k in range(count):
            idx = round(k * n / count)
            while idx < n and idx in used_idx:
                idx += 1
            if idx >= n:
                break
            used_idx.add(idx)
            segments[idx]["video_path"] = os.path.abspath(video_pool[k])
            print(f"[Media] 第 {idx+1} 段使用本地视频: {os.path.basename(video_pool[k])}")

    # --- 图片：命中关键词优先配对，其余按序填充 ---
    seg_tokens: list[set[str]] = []
    for seg in segments:
        raw = " ".join([seg.get("image_keyword", ""), str(seg.get("text", ""))[:120]])
        seg_tokens.append({t for t in re.split(r"[^a-zA-Z0-9]+", raw.lower()) if len(t) >= 2})

    pairs: list[tuple[int, int, int]] = []  # (匹配分, 段下标, 图片下标)
    for si, tokens in enumerate(seg_tokens):
        for ii, path in enumerate(img_pool):
            score = _media_match_score(path, tokens)
            if score > 0:
                pairs.append((score, si, ii))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    seg_img: dict[int, str] = {}
    used_imgs: set[int] = set()
    for _score, si, ii in pairs:
        if si in seg_img or ii in used_imgs:
            continue
        seg_img[si] = img_pool[ii]
        used_imgs.add(ii)

    fill_idx = 0
    for si in range(n):
        if si in seg_img:
            continue
        while fill_idx < len(img_pool) and fill_idx in used_imgs:
            fill_idx += 1
        if fill_idx >= len(img_pool):
            break
        seg_img[si] = img_pool[fill_idx]
        used_imgs.add(fill_idx)
        fill_idx += 1

    for si in range(n):
        src = seg_img.get(si)
        if not src:
            continue
        dst = os.path.join(proj_dir, f"image_{si:03d}.jpg")
        try:
            shutil.copy2(src, dst)
            segments[si]["image_path"] = dst
            segments[si]["local_media"] = True
            print(f"[Media] 第 {si+1} 段使用本地图片: {os.path.basename(src)}")
        except OSError as e:
            print(f"[Media] 第 {si+1} 段本地图片拷贝失败，该段回退云端配图: {e}")

    # --- 剩余图片：按段落成各段候选（每段上限 _LOCAL_ALT_PER_SEGMENT） ---
    spares = [p for i, p in enumerate(img_pool) if i not in used_imgs]
    if spares:
        k = 0
        for si in range(n):
            if k >= len(spares):
                break
            alt_no = 0
            for name in list_candidates(proj_dir, si):
                m = _ALT_NAME_RE.match(name)
                if m:
                    alt_no = max(alt_no, int(m.group(2)))
            placed = 0
            while k < len(spares) and placed < _LOCAL_ALT_PER_SEGMENT:
                alt_no += 1
                dst = os.path.join(proj_dir, f"image_{si:03d}_alt_{alt_no}.jpg")
                try:
                    shutil.copy2(spares[k], dst)
                except OSError as e:
                    print(f"[Media] 候选图拷贝失败（跳过）: {e}")
                k += 1
                placed += 1
        print(f"[Media] 剩余 {len(spares)} 张本地图片已按段落成候选"
              f"（每段最多 {_LOCAL_ALT_PER_SEGMENT} 张），可在 Web 候选列表换图")

    # 视频段同时拿到视频与兜底图片时只按段计一次
    return sum(1 for seg in segments
               if seg.get("video_path") or seg.get("local_media"))


def fetch_fallback_image(index: int, width: int = 0, height: int = 0, project_dir: str = "") -> str | None:
    """备用图：Picsum 随机图，失败重试一次"""
    w = width or config.video_size()[0]
    h = height or config.video_size()[1]
    output_path = os.path.join(_resolve_project_dir(project_dir), f"image_{index:03d}.jpg")
    for attempt in range(2):
        try:
            url = f"https://picsum.photos/{w}/{h}?random={index}"
            img_data = requests.get(url, timeout=15, headers=_HEADERS).content
            if img_data:
                with open(output_path, "wb") as f:
                    f.write(img_data)
                return output_path
        except Exception as e:
            print(f"[Image] 备用图获取失败（第{attempt+1}次）: {e}")
    return None


# ---------- 多候选（P1-1）：候选文件命名约定与换位 ----------
# 主图 image_XXX.jpg；候选 image_XXX_alt_N.jpg（N 从 1 起）。
# 换图 = 把选中的候选与主图位对调：原主图挪到空余的最大 alt 槽位之后，
# 选中的候选顶入主图位。这样换图结果落在本程序自己的文件约定里，
# A12 断点续跑（fetch_all_images 复用 image_XXX.jpg）自然继承人工选择。

_ALT_NAME_RE = re.compile(r"^image_(\d{3})_alt_(\d+)\.jpg$")


def _primary_name(seg_index: int) -> str:
    return f"image_{seg_index:03d}.jpg"


def list_candidates(project_dir: str, seg_index: int) -> list[str]:
    """列出某段在项目目录里的候选图文件名：主图优先，其后按 alt_N 升序。

    只看文件名约定（image_XXX.jpg / image_XXX_alt_N.jpg）且文件真实存在，
    不做解码校验（列表接口读磁盘元数据，保持轻量；坏文件由换图/合成层兜底）。
    """
    if not project_dir or not os.path.isdir(project_dir):
        return []
    names: list[str] = []
    primary = _primary_name(seg_index)
    if os.path.isfile(os.path.join(project_dir, primary)):
        names.append(primary)
    alts: list[tuple[int, str]] = []
    try:
        entries = os.listdir(project_dir)
    except OSError:
        return names
    for name in entries:
        m = _ALT_NAME_RE.match(name)
        if m and int(m.group(1)) == int(seg_index):
            alts.append((int(m.group(2)), name))
    names.extend(name for _, name in sorted(alts))
    return names


def swap_image_candidate(project_dir: str, seg_index: int, candidate_filename: str) -> str:
    """把候选图换入主图位（image_XXX.jpg），返回换入后的主图绝对路径。

    - candidate_filename 必须是该段的 image_XXX.jpg 或 image_XXX_alt_N.jpg 且真实存在
      （调用方 web /api/select 已校验，这里再兜一道，防路径穿越/跨段引用）
    - 选中的就是主图位时原样返回（幂等）
    - 原主图挪到现有最大 alt_N 之后的空槽位（alt_{max+1}.jpg），保证它仍可被再换回来
    - 同目录 os.replace 原子改名，中断也不会同时丢两张图
    """
    primary = _primary_name(int(seg_index))
    if candidate_filename == primary:
        return os.path.join(project_dir, primary)
    if os.path.basename(candidate_filename) != candidate_filename:
        raise ValueError(f"非法候选文件名: {candidate_filename}")
    m = _ALT_NAME_RE.match(candidate_filename)
    if not m or int(m.group(1)) != int(seg_index):
        raise ValueError(
            f"候选文件名 {candidate_filename} 不属于第 {seg_index} 段"
            f"（应为 {_primary_name(seg_index)} 或 image_{seg_index:03d}_alt_N.jpg）")
    src = os.path.join(project_dir, candidate_filename)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"候选图不存在: {candidate_filename}")

    primary_path = os.path.join(project_dir, primary)
    if os.path.isfile(primary_path):
        existing = list_candidates(project_dir, seg_index)
        max_alt = 0
        for name in existing:
            am = _ALT_NAME_RE.match(name)
            if am:
                max_alt = max(max_alt, int(am.group(2)))
        os.replace(primary_path, os.path.join(project_dir, f"image_{seg_index:03d}_alt_{max_alt + 1}.jpg"))
    os.replace(src, primary_path)
    return primary_path


def fetch_image_for_segment(
    keyword: str,
    index: int,
    image_source: str = "",
    aspect_ratio: str = "",
    project_dir: str = "",
) -> str | None:
    """为段落获取配图：首选来源 → 其他已配置来源 → 备用图"""
    source = image_source or config.IMAGE_SOURCE

    if source == "minimax" and config.MINIMAX_API_KEY:
        result = generate_minimax_image(keyword, index, aspect_ratio, project_dir)
        if result:
            return result
    if source == "unsplash" and config.UNSPLASH_ACCESS_KEY:
        result = fetch_unsplash_image(keyword, index, aspect_ratio, project_dir)
        if result:
            return result
    if source == "local":
        result = get_local_image(index, project_dir)
        if result:
            return result

    # 首选来源失败时，依次尝试其他已配置的来源
    if source != "minimax" and config.MINIMAX_API_KEY:
        result = generate_minimax_image(keyword, index, aspect_ratio, project_dir)
        if result:
            return result
    if source != "unsplash" and config.UNSPLASH_ACCESS_KEY:
        result = fetch_unsplash_image(keyword, index, aspect_ratio, project_dir)
        if result:
            return result

    return fetch_fallback_image(index, *config.video_size(aspect_ratio), project_dir=project_dir)


def fetch_all_images(
    segments: list[dict],
    image_source: str = "",
    aspect_ratio: str = "",
    project_dir: str = "",
) -> list[dict]:
    """为所有段落获取配图（并行，最多 3 并发）。单段失败不中断流程。

    断点续跑：项目目录里已存在、非空且能完整解码的 image_XXX.jpg 直接复用，
    不再调任何配图来源（中断重跑不浪费已花钱的 API 调用）；复用段打上
    image_reused=True。损坏的非空坏图删除后按缺失段重新获取（半截文件复用
    会让合成阶段 Image.open 崩溃）。
    单段失败自动重试 2 次（退避 2s/8s，见 IMAGE_RETRY_DELAYS），仍失败才标记
    跳过（image_path=None，合成时用黑底帧）。
    project_dir 为空时回退全局项目目录；Web 多任务并发时应显式传入各自目录。
    """
    total = len(segments)
    proj_dir = _resolve_project_dir(project_dir)

    def _task(args):
        i, seg = args
        img_path = os.path.join(proj_dir, f"image_{i:03d}.jpg")
        if _is_valid_image_file(img_path):
            # 断点续跑：上次获取成功的配图（存在且可完整解码）直接复用
            print(f"[Image] 第 {i+1}/{total} 段配图已存在（{os.path.basename(img_path)}），"
                  f"跳过获取直接复用")
            return i, img_path, True
        if _is_ready_file(img_path):
            # 存在但损坏（写盘中断留下的半截文件）：删除后按缺失段重新获取，
            # 而不是把坏图当成品复用（否则合成阶段崩溃，见 _is_valid_image_file docstring）
            print(f"[Image] 警告: {os.path.basename(img_path)} 已损坏（无法解码），"
                  f"删除后重新获取")
            try:
                os.remove(img_path)
            except OSError as e:
                print(f"[Image] 坏图删除失败（获取成功后将覆盖它）: {e}")

        keyword = seg.get("image_keyword", "story")
        print(f"[Image] 开始获取第 {i+1}/{total} 段配图 (keyword: {keyword})...")
        for attempt in range(len(IMAGE_RETRY_DELAYS) + 1):
            try:
                result = fetch_image_for_segment(keyword, i, image_source, aspect_ratio, project_dir)
            except Exception as e:
                result = None
                print(f"[Image] 第 {i+1} 段配图获取异常: {e}")
            if result:
                return i, result, False
            if attempt < len(IMAGE_RETRY_DELAYS):
                delay = IMAGE_RETRY_DELAYS[attempt]
                print(f"[Image] 第 {i+1} 段配图获取失败，{delay:g}s 后重试"
                      f"（第 {attempt+2}/{len(IMAGE_RETRY_DELAYS)+1} 次尝试）")
                time.sleep(delay)
        return i, None, False

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_task, (i, seg)): i for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                idx, img_path, reused = future.result()
            except Exception as e:
                print(f"[Image] 第 {i+1} 段配图异常: {e}")
                idx, img_path, reused = i, None, False
            segments[idx]["image_path"] = img_path
            if reused:
                segments[idx]["image_reused"] = True
            if img_path:
                print(f"[Image] 第 {idx+1}/{total} 段配图完成: {os.path.basename(img_path)}")
            else:
                print(f"[Image] 第 {idx+1}/{total} 段配图获取失败"
                      f"（已重试 {len(IMAGE_RETRY_DELAYS)} 次），将使用黑底帧")

    return segments
