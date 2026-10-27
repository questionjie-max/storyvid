"""字幕生成 + 文本切分工具

字幕不再整段叠加（长段落会在画面上糊成一面字墙），
而是按句子切成小块，按字符数比例分配该段的音频时长。
"""
import math
import os
import re

# ---------- 真实时间片对齐（任务 D2，通道契约见 aligner.py / tts.py） ----------
# 护栏常量：任一不满足就整体回退字符比例——坏时间戳绝不能弄坏字幕
_ALIGN_MAX_LEN_DIFF = 0.25    # 时间片文本与段落文本的归一字符数相对差上限
_ALIGN_MAX_OVERSHOOT = 1.05   # 时间片最晚结束时间允许超出段落时长的比例
_ALIGN_MIN_COVERAGE = 0.5     # 时间片覆盖段落时长的最低比例（防只拿到开头几词）
_MIN_CHUNK_DUR = 0.05         # 单块时长下限（与旧尾块保护一致，防 0 长度 ImageClip）


def _align_norm(s: str) -> str:
    """对齐用文本归一：去掉空白与全部标点（全/半角），只留文字与数字。

    TTS 返回的时间片文本（StepFun 词 / MiniMax 句 / 火山字）可能不带原文
    标点或空格，按字符对齐前必须把两端都归一到同一形态，否则永远对不上。
    """
    return re.sub(r"[\W_]+", "", s or "", flags=re.UNICODE)


def _parse_word_pieces(words) -> "list[tuple[str, float, float]] | None":
    """把通道契约时间片解析成 [(归一文本, start, end)]（按开始时间排序）。

    结构/时间非法（非 dict、缺字段、非有限数、负值、end<start）直接放弃，
    文本为空的坏片丢弃；全部无效或 words 为空返回 None。
    供 _durations_from_words（块级切块）与 karaoke_timings_from_words
    （D5 逐词高亮）共用——两条路径对同一输入必须同时可用或同时回退，
    块边界与词时间轴才不会互相矛盾。
    """
    if not words:
        return None
    pieces: list[tuple[str, float, float]] = []
    for raw in words:
        if not isinstance(raw, dict):
            return None
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end < start:
            return None
        text = raw.get("word", raw.get("text"))
        norm = _align_norm(str(text or ""))
        if norm:
            pieces.append((norm, start, end))
    if not pieces:
        return None
    pieces.sort(key=lambda p: (p[1], p[2]))
    return pieces


def _word_timeline_guards(pieces: list[tuple[str, float, float]],
                          chunk_total: int, total_duration: float) -> bool:
    """时间片护栏②③：文本归一长度差 ≤25%、最晚结束不超段时长 5%"""
    piece_total = sum(len(p[0]) for p in pieces)
    if not chunk_total or not piece_total:
        return False
    if abs(piece_total - chunk_total) > _ALIGN_MAX_LEN_DIFF * chunk_total:
        return False
    if max(p[2] for p in pieces) > total_duration * _ALIGN_MAX_OVERSHOOT:
        return False
    return True


def _piece_time_at(pos: float, pieces: list[tuple[str, float, float]],
                   piece_total: int, chunk_total: int) -> float:
    """把归一字符进度 pos（0..chunk_total）投影到时间片时间轴上。

    「归一字符进度等比投影」+ 片内线性插值：边界落在词/句边界上时得到
    精确真实时间，落在片内部时按字符进度近似插值。
    """
    target = pos * piece_total / chunk_total
    acc = 0.0
    for i, (norm, start, end) in enumerate(pieces):
        acc_next = acc + len(norm)
        # 最后一片兜底吸收浮点误差（target 理论上 ≤ piece_total）
        if target < acc_next or i == len(pieces) - 1:
            local = min(max((target - acc) / len(norm), 0.0), 1.0)
            return start + (end - start) * local
        acc = acc_next
    return pieces[-1][2]


def _durations_from_words(chunks: list[str], words, total_duration: float) -> "list[float] | None":
    """把真实时间片（通道契约 seg["words"]）对齐成各字幕块时长；失败返回 None。

    对齐算法：
    1. 时间片归一（丢坏片：缺字段/时间非法/文本为空）后按字符长度铺成一条
       时间线；词级（StepFun/火山）与句级（MiniMax）记录统一处理；
    2. 段落切块同样归一，总字符数与时间片总字符数相差超过 25% 视为文本
       不一致（如 TTS 把数字读成汉字导致长度剧变）→ 放弃对齐；
    3. 时间片最晚结束时间超过段时长 5% 视为与音频不同源（如单位是毫秒
       没换算）→ 放弃；
    4. 每个块边界按「归一字符进度等比投影」落到时间片上，片内线性插值——
       块边界落在词/句边界上时得到精确真实时间，落在片内部时近似插值；
    5. 真实语音跨度不足段时长一半（时间戳可能只覆盖开头几词）→ 放弃。
    （1-4 的解析/护栏/投影已拆为 _parse_word_pieces/_word_timeline_guards/
    _piece_time_at，与 D5 karaoke 词时间表共用。）
    """
    if not words or total_duration <= 0:
        return None
    pieces = _parse_word_pieces(words)
    if not pieces:
        return None

    chunk_norms = [_align_norm(c) for c in chunks]
    chunk_total = sum(len(n) for n in chunk_norms)
    if not _word_timeline_guards(pieces, chunk_total, total_duration):
        return None
    piece_total = sum(len(p[0]) for p in pieces)

    times: list[float] = []
    acc = 0
    for norm in chunk_norms:
        times.append(_piece_time_at(acc, pieces, piece_total, chunk_total))
        acc += len(norm)
    times.append(_piece_time_at(chunk_total, pieces, piece_total, chunk_total))

    if times[-1] - times[0] < _ALIGN_MIN_COVERAGE * total_duration:
        return None

    durations: list[float] = []
    prev = times[0]
    for t in times[1:]:
        t = max(t, prev)  # 相邻边界可能落在同一时间片内得到相等时间，钳成非降
        durations.append(max(_MIN_CHUNK_DUR, t - prev))
        prev = t
    return durations

# 句末标点：在这些符号处切块
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?…；;])")
# 长句内部再切：逗号、顿号、冒号
_INNER_SPLIT = re.compile(r"(?<=[，,、：:])")


def split_text_into_chunks(text: str, max_chars: int = 0) -> list[str]:
    """把一段文字切成适合上屏的短句块。

    规则：先按句末标点切；超过 max_chars 的句子再按逗号/顿号切；
    还超长就按 max_chars 硬切。max_chars<=0 时用 config.SUBTITLE_MAX_CHARS。
    """
    if max_chars <= 0:
        import config
        max_chars = config.SUBTITLE_MAX_CHARS

    text = text.strip()
    if not text:
        return []

    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]

    chunks: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            chunks.append(part)
            continue
        # 按逗号/顿号细切
        sub_parts = [p.strip() for p in _INNER_SPLIT.split(part) if p.strip()]
        buf = ""
        for sp in sub_parts:
            if not sp:
                continue
            if len(sp) > max_chars:
                # 仍然超长：硬切
                if buf:
                    chunks.append(buf)
                    buf = ""
                for i in range(0, len(sp), max_chars):
                    chunks.append(sp[i : i + max_chars])
                continue
            if len(buf) + len(sp) > max_chars and buf:
                chunks.append(buf)
                buf = sp
            else:
                buf += sp
        if buf:
            chunks.append(buf)

    return chunks if chunks else [text]


def build_chunk_durations(text: str, total_duration: float, max_chars: int = 0,
                          words: "list[dict] | None" = None) -> list[tuple[str, float]]:
    """把段落文字切块并分配各块时长。

    words（可选）：真实时间片列表（D2 TTS 时间戳通道 / D3 本地对齐通道产出，
    通道契约与 aligner.py 的 seg["words"] 同构）：
        [{"word": 文本, "start": 秒, "end": 秒}, ...]
    时间为段内相对秒，词级（StepFun WS / 火山）或句级（MiniMax）记录均可。
    能与文本对齐时，各块按真实发音边界取时长——这是替代字符比例估算的
    主路径；对不上（字段缺失、文本偏差过大、时间超出段时长、覆盖不足等）
    整体回退按字符比例估算（旧行为），坏时间戳永远不会弄坏字幕。

    返回 [(块文本, 时长), ...]。
    - 真实时间片路径：时长之和可能略小于 total_duration（尾部静音不上字幕，
      视频侧用 pad_chunk_durations 把画面补齐到整段）；
    - 字符比例路径：时长之和等于 total_duration；例外是段落极短（每块分摊
      不足 0.05s）时尾块按 0.05s 下限保护，避免 0 长度的 ImageClip 让视频
      合成报错，此时总和会略大于 total_duration。
    """
    chunks = split_text_into_chunks(text, max_chars)
    if not chunks:
        return []
    if len(chunks) == 1:
        return [(chunks[0], total_duration)]

    if words:
        real = _durations_from_words(chunks, words, total_duration)
        if real is not None:
            return list(zip(chunks, real))

    weights = [max(len(c), 1) for c in chunks]
    total_weight = sum(weights)

    if total_duration <= 0:
        return [(c, 0.0) for c in chunks]

    durations = [total_duration * w / total_weight for w in weights]
    # 尾块吸收浮点误差，保证总和精确等于段落时长
    durations[-1] = max(0.05, total_duration - sum(durations[:-1]))

    return list(zip(chunks, durations))


def pad_chunk_durations(chunks: list[tuple[str, float]],
                        total_duration: float) -> list[tuple[str, float]]:
    """把块时长补齐/收敛到段落总时长（视频合成侧用；SRT 侧不用）。

    - 之和 < total_duration（真实时间片路径的尾部静音）：尾块延长兜住整段，
      避免段落片段比该段音频短、成片时间轴整体漂移；
    - 之和 > total_duration（0.05s 下限累计超出）：等比压缩回总时长；
    - 字符比例路径通常两者都不触发（总和≈total_duration），原样返回。

    SRT 不调用本函数：字幕在真实发音结束点收起（尾部静音不上字幕）更准。
    """
    if not chunks or total_duration <= 0:
        return list(chunks)
    total = sum(d for _, d in chunks)
    if total <= 0:
        return list(chunks)
    if total < total_duration - 1e-9:
        last_text, last_dur = chunks[-1]
        return chunks[:-1] + [(last_text, last_dur + total_duration - total)]
    if total > total_duration + 1e-9:
        k = total_duration / total
        return [(t, d * k) for t, d in chunks]
    return list(chunks)


# ---------- 逐词卡拉OK高亮（任务 D5，SUBTITLE_STYLE=karaoke） ----------
# 视觉渲染在 video.py（Pillow 逐帧上色），这里只负责「什么时刻高亮到哪个词」：
# 把块级显示窗口（build_chunk_durations + pad_chunk_durations 的最终布局）
# 再切成词级 token 时间表。数据源与块级切块同一通道契约 seg["words"]、
# 同一套解析/护栏/投影（_parse_word_pieces/_word_timeline_guards/
# _piece_time_at）——块级用不上时间片时词级必然也回退，两边时间轴永不矛盾；
# 坏时间戳与块级一样只会回退字符比例，绝不会弄坏字幕。

# 卡拉OK token：连续英文字母/数字（含小数，如 102.5）一个 token，
# 其它每个非空白字符（中文单字/标点）各占一个 token；空白由 karaoke_tokens
# 并入相邻 token。中文按字粒度高亮（KTV 逐字惯例），词级时间片仍完整驱动
# 时间轴：词边界处精确落位，多字词内部按字符进度插值。
_KARAOKE_TOKEN_RE = re.compile(r"[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*|[^\s0-9A-Za-z]")


def karaoke_tokens(text: str) -> list[str]:
    """把块文本切成逐词高亮 token；token 拼接严格还原原文（空白不丢）。

    连续英文字母/数字（含小数）一个 token，中文单字与标点各占一个 token，
    空白并入前一个 token（开头空白并入首 token），保证逐 token 绘制时
    字距、断行与原文完全一致。
    """
    tokens: list[str] = []
    pos = 0
    for m in _KARAOKE_TOKEN_RE.finditer(text or ""):
        gap = text[pos:m.start()]
        if gap:  # token 之间的空白并入前一个 token（开头空白并入首 token）
            if tokens:
                tokens[-1] += gap
            else:
                tokens.append(gap)
        tokens.append(m.group(0))
        pos = m.end()
    tail = (text or "")[pos:]
    if tail:
        if tokens:
            tokens[-1] += tail
        else:
            tokens.append(tail)
    return tokens


def karaoke_timings_proportional(chunks: list[tuple[str, float]]
                                 ) -> list[list[tuple[str, float, float]]]:
    """字符比例兜底词时间表：每块内 token 按归一字符长度比例分配块时长。

    与块级字符比例估算（build_chunk_durations 的比例路径）同一哲学：
    权重 = max(归一字符数, 1)（纯标点 token 也分得 1 份，避免除零）；
    尾 token 吸收浮点误差钉住块尾，块内时间无缝铺满 [0, 块时长]。
    """
    out: list[list[tuple[str, float, float]]] = []
    for chunk_text, chunk_dur in chunks:
        tokens = karaoke_tokens(chunk_text) or [chunk_text]
        weights = [max(len(_align_norm(t)), 1) for t in tokens]
        total_weight = sum(weights)
        if chunk_dur <= 0 or total_weight <= 0:
            out.append([(t, 0.0, 0.0) for t in tokens])
            continue
        durs = [chunk_dur * w / total_weight for w in weights]
        durs[-1] = max(0.0, chunk_dur - sum(durs[:-1]))
        timings: list[tuple[str, float, float]] = []
        start = 0.0
        for tok, d in zip(tokens, durs):
            timings.append((tok, start, start + d))
            start += d
        out.append(timings)
    return out


def karaoke_timings_from_words(chunks: list[tuple[str, float]], words
                               ) -> "list[list[tuple[str, float, float]]] | None":
    """词级时间表：seg["words"]（通道契约，段内相对秒）投影到各块的最终
    显示窗口，返回与 chunks 等长的 [[(token, 块内相对开始, 相对结束), ...]]。

    token 边界按「归一字符进度等比投影」落到时间片时间轴（与块级切块
    _durations_from_words 同一投影函数）：词/句边界处精确，片内部线性插值。
    护栏与块级完全同口径（同一解析器 + 护栏函数 + 覆盖率判据），
    任一不满足返回 None（调用方回退字符比例）。

    块内相对时间以「该块显示窗口起点」为锚（= 前序块 pad 后时长之和，
    与成片时间轴一致），越界钳回 [0, 块时长]。首尾不强制钉死块边界：
    开唱前/末词后的静音由 video 侧的「高亮集合变化点」切分兜住
    （开唱前全白、末词后停在末词），块内时间无缝由变化点切分保证。
    """
    if not chunks:
        return None
    total = sum(d for _, d in chunks)
    if total <= 0:
        return None
    pieces = _parse_word_pieces(words)
    if not pieces:
        return None
    chunk_norms = [_align_norm(text) for text, _ in chunks]
    chunk_total = sum(len(n) for n in chunk_norms)
    if not chunk_total or not _word_timeline_guards(pieces, chunk_total, total):
        return None
    piece_total = sum(len(p[0]) for p in pieces)
    # 护栏④覆盖率（与 _durations_from_words 同口径）：
    # 真实语音跨度不足段时长一半 → 时间戳可能只覆盖开头几词，放弃
    if (_piece_time_at(chunk_total, pieces, piece_total, chunk_total)
            - _piece_time_at(0, pieces, piece_total, chunk_total)
            < _ALIGN_MIN_COVERAGE * total):
        return None

    timings: list[list[tuple[str, float, float]]] = []
    anchor = 0.0
    global_pos = 0
    for (chunk_text, chunk_dur), cnorm in zip(chunks, chunk_norms):
        tokens = karaoke_tokens(chunk_text)
        if not tokens:
            tokens = [chunk_text]
        bounds = [0]
        acc = 0
        for tok in tokens:
            acc += len(_align_norm(tok))
            bounds.append(acc)
        if acc != len(cnorm):
            # 理论不可达（token 拼接还原原文、归一逐字对应），防御性兜底：
            # 宁可整段回退比例，也不上错位的高亮
            return None
        raw = [_piece_time_at(global_pos + b, pieces, piece_total, chunk_total)
               for b in bounds]
        rel = [min(max(t - anchor, 0.0), max(chunk_dur, 0.0)) for t in raw]
        for i in range(1, len(rel)):  # 时间片天然非降，钳制后保持非降（防浮点毛刺）
            rel[i] = max(rel[i], rel[i - 1])
        timings.append([(tok, rel[i], rel[i + 1]) for i, tok in enumerate(tokens)])
        anchor += chunk_dur
        global_pos += len(cnorm)
    return timings


def karaoke_chunk_timings(chunks: list[tuple[str, float]],
                          words: "list[dict] | None" = None
                          ) -> list[list[tuple[str, float, float]]]:
    """karaoke 词时间表主入口：词级时间片可用（护栏全过）用真实投影，
    否则块内字符比例兜底——永不为 None、永不抛错，坏时间戳不会弄坏字幕。
    """
    if words:
        real = karaoke_timings_from_words(chunks, words)
        if real is not None:
            return real
    return karaoke_timings_proportional(chunks)


def karaoke_active_index(timings: list[tuple[str, float, float]], t: float) -> int:
    """时刻 t（块内相对秒）正唱到的 token 序号：最后一个 start ≤ t 的 token。

    t 早于首 token（开唱前静音）返回 -1（渲染为全白）；零时长 token
    （标点）不被单独选中——其时刻归属后面的词，标点随其后的高亮词一起上色。
    """
    active = -1
    for i, (_tok, start, _end) in enumerate(timings):
        if start <= t:
            active = i
    return active


def generate_srt(
    segments: list[dict],
    output_path: str,
    offset: float = 0.0,
    crossfade: float = 0.0,
) -> str:
    """生成 SRT 字幕文件（块级粒度：一句话一条字幕）

    offset: 整体时间轴偏移秒数。成片带片头卡时正片整体后移，
    传 video.intro_offset()（INTRO_OUTRO 关闭时它返回 0）即可让字幕与成片对齐。

    crossfade: 段间交叉淡化秒数（传 video.crossfade_seconds()，默认 0 = 关闭）。
    开启后后一段在成片中比顺序拼接提前 overlap 秒开始（overlap 与视频侧一致，
    按 min(crossfade, 前段长, 后段长) 截断），字幕时间轴相应前移；
    同时每段（除最后一段）的所有字幕块都收进本段展示窗口 [seg_start, seg_end]
    （结束与开始时间都截断到下一段开始），落在窗口之外的块整块丢弃，
    保证不产生 end<start 的非法块、零时长块，相邻字幕区间也不交叠。
    总长 = offset + 各段之和 - 重叠总和，与成片时间轴一致。
    """
    xfade = max(float(crossfade), 0.0)
    durations = [float(seg.get("duration", 5.0)) for seg in segments]
    srt_lines = []
    current_time = float(offset)
    index = 1

    for si, seg in enumerate(segments):
        duration = durations[si]
        # 本段与下一段的重叠：本段在成片中的实际展示跨度为 duration - overlap
        if xfade > 0 and si < len(segments) - 1:
            overlap = min(xfade, duration, durations[si + 1])
        else:
            overlap = 0.0
        seg_start = current_time
        seg_end = seg_start + duration - overlap  # 下一段在成片中的开始时间

        # 有真实时间片（seg["words"]，D2 TTS 通道 / D3 本地对齐通道产出）时
        # 按真实发音边界切块；没有则回退字符比例估算（build_chunk_durations 内部处理）
        chunks = build_chunk_durations(seg["text"], duration, words=seg.get("words"))
        for ci, (text, dur) in enumerate(chunks):
            start = current_time
            end = start + dur
            if overlap > 0 and si < len(segments) - 1:
                # 所有块都收进本段展示窗口：既截尾也截头，
                # 否则 xfade 较大时尾块会出现 end<start 的非法区间
                end = min(end, seg_end)
                start = min(start, seg_end)
                if end <= start:
                    # 整块落入与下一段的重叠区（画面已被淡化覆盖、音频已淡出），不上屏
                    current_time = end
                    continue
            srt_lines.append(str(index))
            srt_lines.append(f"{_format_time(start)} --> {_format_time(end)}")
            srt_lines.append(text)
            srt_lines.append("")
            index += 1
            current_time = end

        # 段级推进吸收段间重叠，保持与视频时间轴一致
        current_time = seg_end

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    return output_path


def _format_time(seconds: float) -> str:
    """将秒数格式化为 SRT 时间格式 HH:MM:SS,mmm

    先换算成总毫秒再取整，避免旧实现中 0.9995s 以上被截成 ",999"
    而不向秒位进位的问题（例如 1.9999s 应显示 00:00:02,000）。
    """
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
