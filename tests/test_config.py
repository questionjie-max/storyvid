"""config.py：配置校验 + 项目目录"""
import config


class TestValidateConfig:
    def _base(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_KEY", "llm-key")
        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "stepfun-key")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
        monkeypatch.setattr(config, "IMAGE_SOURCE", "minimax")
        monkeypatch.setattr(config, "UNSPLASH_ACCESS_KEY", "")

    def test_all_required_set(self, monkeypatch):
        self._base(monkeypatch)
        ok, problems, warnings = config.validate_config()
        assert ok
        assert problems == []

    def test_missing_llm_key(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        ok, problems, _ = config.validate_config()
        assert not ok
        assert any("LLM_API_KEY" in p for p in problems)

    def test_stepfun_provider_without_key(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "")
        ok, problems, _ = config.validate_config()
        assert not ok
        assert any("STEPFUN_API_KEY" in p for p in problems)

    def test_minimax_provider_needs_no_stepfun_key(self, monkeypatch):
        """回归测试：旧版一刀切要求 STEPFUN_API_KEY，minimax 用户被误拦"""
        self._base(monkeypatch)
        monkeypatch.setattr(config, "TTS_PROVIDER", "minimax")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
        ok, problems, _ = config.validate_config()
        assert ok
        assert problems == []

    def test_unknown_tts_provider(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(config, "TTS_PROVIDER", "azure")
        ok, problems, _ = config.validate_config()
        assert not ok

    def test_volc_provider_without_keys(self, monkeypatch):
        """TTS_PROVIDER=volc 但未配 AppID/Token 应拦截并提示变量名"""
        self._base(monkeypatch)
        monkeypatch.setattr(config, "TTS_PROVIDER", "volc")
        monkeypatch.setattr(config, "VOLC_TTS_APPID", "")
        monkeypatch.setattr(config, "VOLC_TTS_TOKEN", "")
        ok, problems, _ = config.validate_config()
        assert not ok
        assert any("VOLC_TTS_APPID" in p and "VOLC_TTS_TOKEN" in p for p in problems)

    def test_volc_provider_with_keys_ok(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(config, "TTS_PROVIDER", "volc")
        monkeypatch.setattr(config, "VOLC_TTS_APPID", "appid")
        monkeypatch.setattr(config, "VOLC_TTS_TOKEN", "token")
        ok, problems, _ = config.validate_config()
        assert ok
        assert problems == []

    def test_missing_image_key_is_warning_not_error(self, monkeypatch):
        self._base(monkeypatch)
        monkeypatch.setattr(config, "IMAGE_SOURCE", "minimax")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
        ok, _, warnings = config.validate_config()
        assert ok  # 配图有兜底，不应阻塞
        assert any("MINIMAX_API_KEY" in w for w in warnings)


class TestVideoSize:
    def test_landscape_default(self, monkeypatch):
        monkeypatch.setattr(config, "ASPECT_RATIO", "16:9")
        assert config.video_size() == (1920, 1080)

    def test_portrait(self, monkeypatch):
        monkeypatch.setattr(config, "ASPECT_RATIO", "16:9")
        assert config.video_size("9:16") == (1080, 1920)

    def test_unknown_ratio_falls_back_to_landscape(self):
        assert config.video_size("4:3") == (1920, 1080)


class TestSetupProject:
    def test_creates_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "PROJECT_DIR", None)
        monkeypatch.setattr(config, "TEMP_DIR", None)

        project = config.setup_project("测试主题/带特殊字符")
        assert project.startswith(str(tmp_path))
        import os
        assert os.path.isdir(project)
        assert os.path.isdir(config.TEMP_DIR)
        assert config.get_project_dir() == project
