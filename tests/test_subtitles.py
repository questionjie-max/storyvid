"""subtitles.py：文本切分 + 时间轴 + SRT 生成"""
import pytest

import subtitles


class TestSplitTextIntoChunks:
    def test_split_by_sentence_endings(self):
        chunks = subtitles.split_text_into_chunks("你好世界。这是第二句！第三句来了？")
        assert chunks == ["你好世界。", "这是第二句！", "第三句来了？"]

    def test_empty_text(self):
        assert subtitles.split_text_into_chunks("") == []

    def test_long_sentence_split_on_commas(self):
        text = "这一句话特别长，里面有很多逗号，还有很多顿号、分号；需要被切开"  # 30+ chars
        chunks = subtitles.split_text_into_chunks(text, max_chars=10)
        assert len(chunks) > 1
        assert "".join(chunks) == text  # 不丢字

    def test_hard_wrap_beyond_max_chars(self):
        text = "这是一个完全没有任何标点符号的超长句子用来测试硬切逻辑"  # 无逗号
        chunks = subtitles.split_text_into_chunks(text, max_chars=8)
        assert all(len(c) <= 8 for c in chunks)
        assert "".join(chunks) == text

    def test_no_punctuation_short_text(self):
        assert subtitles.split_text_into_chunks("短文本") == ["短文本"]

    def test_default_max_chars_from_config(self):
        import config
        assert subtitles.split_text_into_chunks("字" * (config.SUBTITLE_MAX_CHARS + 5)) != [
            "字" * (config.SUBTITLE_MAX_CHARS + 5)
        ]


class TestBuildChunkDurations:
    def test_durations_sum_to_total(self):
        text = "第一句话。第二句话稍微长一点。第三句！"
        result = subtitles.build_chunk_durations(text, 10.0)
        assert sum(d for _, d in result) == pytest.approx(10.0)
        assert len(result) == 3

    def test_single_chunk_gets_all_time(self):
        result = subtitles.build_chunk_durations("一句话。", 7.5)
        assert result == [("一句话。", 7.5)]

    def test_zero_duration(self):
        result = subtitles.build_chunk_durations("第一句。第二句。", 0)
        assert all(d == 0.0 for _, d in result)

    def test_longer_text_gets_more_time(self):
        result = subtitles.build_chunk_durations("短。这是一个明显长了很多很多的句子。", 10.0)
        durations = [d for _, d in result]
        assert durations[1] > durations[0]


class TestSrt:
    def test_format_time(self):
        assert subtitles._format_time(0) == "00:00:00,000"
        assert subtitles._format_time(3661.5) == "01:01:01,500"
        assert subtitles._format_time(59.999) == "00:00:59,999"

    def test_format_time_negative_clamped(self):
        assert subtitles._format_time(-3) == "00:00:00,000"

    def test_format_time_millisecond_carry(self):
        """回归测试：0.9995s 以上必须向秒位进位，而不是截成 ,999"""
        assert subtitles._format_time(1.9999) == "00:00:02,000"
        assert subtitles._format_time(2.99999) == "00:00:03,000"
        assert subtitles._format_time(59.9995) == "00:01:00,000"

    def test_generate_srt_chunk_level(self, tmp_path):
        segments = [
            {"text": "第一句很短。第二句稍微长一些，还带个逗号。", "duration": 10.0},
            {"text": "第二段开头。", "duration": 4.0},
        ]
        out = tmp_path / "sub.srt"
        subtitles.generate_srt(segments, str(out))

        content = out.read_text(encoding="utf-8")
        blocks = [b for b in content.strip().split("\n\n") if b]
        # 第一段 2 块（句号切分后第二句带逗号但在 42 字内不细切 → 其实按句号是 2 块）
        assert len(blocks) == 3
        assert "第一句很短。" in blocks[0]
        assert "-->" in blocks[0]
        # 时间单调递增
        assert "00:00:00,000 -->" in blocks[0]

    def test_generate_srt_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "deep" / "dir" / "sub.srt"
        subtitles.generate_srt([{"text": "一句话。", "duration": 2.0}], str(out))
        assert out.exists()


class TestDefaultMaxChars26:
    """质量循环 R1：默认 SUBTITLE_MAX_CHARS 42→26，长句一屏不再挂 6-7.5s"""

    BASELINE_LONG = "清宫档案里，它和一场惊天刺杀有关：嘉庆十八年，天理教起义者攻入紫禁城，直逼内廷。"

    def test_default_splits_baseline_long_sentence(self):
        """基线实测 40 字长句(原一屏 7.5s)在默认 26 下必须被标点切短"""
        import config
        assert config.SUBTITLE_MAX_CHARS == 26
        chunks = subtitles.split_text_into_chunks(self.BASELINE_LONG)
        assert len(chunks) > 1, "40 字长句应被切成多块"
        assert all(len(c) <= 26 for c in chunks)
        assert "".join(chunks) == self.BASELINE_LONG  # 不丢字

    def test_words_alignment_survives_split(self):
        """切短后真实词级时间片仍能投影对齐(总时长守恒、无负时长)"""
        words = [{"word": "字", "start": i * 0.2, "end": (i + 1) * 0.2} for i in range(30)]
        chunks = subtitles.build_chunk_durations(
            "第一句很长有逗号，后面还有内容，继续说到句号。", 6.0, words=words)
        assert all(d > 0 for _t, d in chunks)
        assert sum(d for _t, d in chunks) == pytest.approx(6.0, abs=0.2)
