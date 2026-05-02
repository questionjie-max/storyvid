"""配图获取"""
import os
import random
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    IMAGE_SOURCE, UNSPLASH_ACCESS_KEY, LOCAL_IMAGE_DIR, TEMP_DIR,
    MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_IMAGE_MODEL,
    ASPECT_RATIO, VIDEO_WIDTH, VIDEO_HEIGHT,
    get_project_dir,
)


def fetch_unsplash_image(keyword: str, index: int) -> str | None:
    """从 Unsplash 搜索免费图片"""
    url = "https://api.unsplash.com/search/photos"
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    orientation = "portrait" if ASPECT_RATIO == "9:16" else "landscape"
    params = {
        "query": keyword,
        "per_page": 10,
        "orientation": orientation,
    }
    output_path = os.path.join(get_project_dir(), f"image_{index:03d}.jpg")

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
            img_url = random.choice(results)["urls"]["regular"]
            img_data = requests.get(img_url, timeout=15).content
            with open(output_path, "wb") as f:
                f.write(img_data)
            return output_path
    except Exception as e:
        print(f"[Image] Unsplash 获取失败 ({keyword}): {e}")

    return None


def generate_minimax_image(keyword: str, index: int) -> str | None:
    """用 MiniMax 生成图片"""
    if not MINIMAX_API_KEY:
        print(f"[Image] MiniMax API Key 未配置，跳过")
        return None

    os.makedirs(get_project_dir(), exist_ok=True)
    output_path = os.path.join(get_project_dir(), f"image_{index:03d}.jpg")
    prompt = f"{keyword}, high quality, cinematic lighting, professional photography"

    # MiniMax 支持的宽高比
    aspect_map = {
        "16:9": "16:9",
        "9:16": "9:16",
    }
    minimax_ratio = aspect_map.get(ASPECT_RATIO, "16:9")

    payload = {
        "model": MINIMAX_IMAGE_MODEL,
        "prompt": prompt,
        "aspect_ratio": minimax_ratio,
        "response_format": "url",
        "n": 1,
        "prompt_optimizer": True,
    }

    try:
        resp = requests.post(
            f"{MINIMAX_BASE_URL}/v1/image_generation",
            headers={
                "Authorization": f"Bearer {MINIMAX_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        data = resp.json()

        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            print(f"[Image] MiniMax 错误: {base_resp.get('status_msg')}")
            return None

        img_urls = data.get("data", {}).get("image_urls", [])
        if not img_urls:
            print(f"[Image] MiniMax 返回空图片列表")
            return None

        img_data = requests.get(img_urls[0], timeout=30).content
        with open(output_path, "wb") as f:
            f.write(img_data)
        print(f"[Image] MiniMax 生图成功: {output_path}")
        return output_path
    except Exception as e:
        print(f"[Image] MiniMax 生图失败 ({keyword}): {e}")
        return None


def get_local_image(index: int) -> str | None:
    """从本地目录随机选一张图"""
    if not os.path.exists(LOCAL_IMAGE_DIR):
        return None
    files = [f for f in os.listdir(LOCAL_IMAGE_DIR)
             if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    if not files:
        return None
    selected = random.choice(files)
    src = os.path.join(LOCAL_IMAGE_DIR, selected)
    output_path = os.path.join(get_project_dir(), f"image_{index:03d}.jpg")
    import shutil
    shutil.copy2(src, output_path)
    return output_path


def fetch_fallback_image(index: int) -> str | None:
    """备用图：Picsum 随机图"""
    output_path = os.path.join(get_project_dir(), f"image_{index:03d}.jpg")
    try:
        w, h = (VIDEO_WIDTH, VIDEO_HEIGHT)
        url = f"https://picsum.photos/{w}/{h}?random={index}"
        img_data = requests.get(url, timeout=15).content
        with open(output_path, "wb") as f:
            f.write(img_data)
        return output_path
    except Exception as e:
        print(f"[Image] 备用图获取失败: {e}")
    return None


def fetch_image_for_segment(keyword: str, index: int) -> str | None:
    """为段落获取配图"""
    os.makedirs(get_project_dir(), exist_ok=True)

    if IMAGE_SOURCE == "minimax" and MINIMAX_API_KEY:
        result = generate_minimax_image(keyword, index)
        if result:
            return result
    elif IMAGE_SOURCE == "unsplash" and UNSPLASH_ACCESS_KEY:
        result = fetch_unsplash_image(keyword, index)
        if result:
            return result
    elif IMAGE_SOURCE == "local":
        result = get_local_image(index)
        if result:
            return result

    return fetch_fallback_image(index)


def fetch_all_images(segments: list[dict]) -> list[dict]:
    """为所有段落获取配图（并行，最多 3 并发）"""
    total = len(segments)

    def _task(args):
        i, seg = args
        keyword = seg.get("image_keyword", "story")
        print(f"[Image] 开始获取第 {i+1}/{total} 段配图 (keyword: {keyword})...")
        img_path = fetch_image_for_segment(keyword, i)
        return i, img_path

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_task, (i, seg)): i for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            i, img_path = future.result()
            segments[i]["image_path"] = img_path
            if img_path:
                print(f"[Image] 第 {i+1}/{total} 段配图完成: {os.path.basename(img_path)}")
            else:
                print(f"[Image] 第 {i+1}/{total} 段配图获取失败")

    return segments
