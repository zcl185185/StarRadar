"""StarRadar 全局配置。

所有配置从环境变量读取，未设置时使用默认值。
本地开发可在项目根目录创建 .env 文件（已被 .gitignore 忽略）。
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# ===== 路径 =====
# PyInstaller 打包后，代码和内置静态资源位于只读安装目录；用户数据必须留在
# LocalAppData，升级/卸载应用时才不会覆盖 Token、画像和本地数据库。
IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()

if IS_FROZEN:
    APP_DATA_DIR = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "StarRadar"
    PROJECT_ROOT = APP_DATA_DIR
    DATA_DIR = APP_DATA_DIR / "data"
    OUTPUT_DIR = APP_DATA_DIR / "output"
    STATIC_DIR = APP_DATA_DIR / "static"
    BUNDLED_STATIC_DIR = RESOURCE_ROOT / "static"
    # 打包版的 .env 放在用户数据目录，而非安装目录。
    load_dotenv(APP_DATA_DIR / ".env")
else:
    PROJECT_ROOT = Path(__file__).parent.resolve()
    DATA_DIR = PROJECT_ROOT / "data"
    OUTPUT_DIR = PROJECT_ROOT / "output"
    STATIC_DIR = PROJECT_ROOT / "static"
    BUNDLED_STATIC_DIR = STATIC_DIR
    load_dotenv()

CACHE_DIR = DATA_DIR / "cache"
PROFILE_DIR = DATA_DIR / "profile"
PERSONAL_DIR = DATA_DIR / "personal"


def _resolve_access_code() -> str:
    """访问码三级解析：环境变量 > 本地文件 > 首启随机生成并持久化。

    默认码不再硬编码在源码里（旧默认 0526 任何看过仓库的人都能过门禁）。
    门禁定位是「防窥」而非安全边界，README 已如实说明。
    """
    env = os.getenv("STARRADAR_ACCESS_CODE")
    if env:
        return env
    code_file = PROFILE_DIR / "access_code.txt"
    try:
        if code_file.is_file():
            code = code_file.read_text(encoding="utf-8").strip()
            if code:
                return code
    except OSError:
        pass
    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        code_file.write_text(code, encoding="utf-8")
    except OSError:
        pass
    return code

# 打包版会把前端复制到 LocalAppData；此版本号变更时仅同步应用壳资源，
# 不触碰 static/data 内的用户雷达数据和本地快照。
STATIC_ASSET_VERSION = "2026.08.28.5"
_STATIC_ASSET_ENTRIES = ("index.html", "css", "js", "favicon.png", "starlogo.png", "readme")


@dataclass
class GitHubConfig:
    token: str | None = field(default_factory=lambda: os.getenv("GITHUB_TOKEN"))
    api_base: str = "https://api.github.com"
    request_timeout: int = 30


@dataclass
class LLMConfig:
    api_key: str | None = field(default_factory=lambda: os.getenv("LLM_API_KEY"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    base_url: str | None = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    max_tokens_per_summary: int = 300
    # LLM_DISABLED=1 时公版管道跳过所有 LLM 调用（解读/主题归纳/查询扩展），
    # 自动降级规则文本——零花费。个人版管道载入用户自填 Key 时会被重新启用。
    enabled: bool = field(default_factory=lambda: os.getenv("LLM_DISABLED", "0") != "1")


# 内置默认 OAuth App Client ID（公开值，非 Secret——设备流只需 client_id，克隆者零配置）。
# GitHub 的 secret scanning 只针对 secret 类凭证，client_id 可安全内置。
DEFAULT_GH_CLIENT_ID = "Ov23lidxHa5chVTqVBXX"


@dataclass
class OAuthConfig:
    """GitHub OAuth 跳转登录配置（环境变量，.env 可写；secret 勿入库）。

    - client_id：.env 覆盖，否则用内置默认值（设备流零配置）
    - client_secret：仅 .env 可配——配置后个人版升级为「跳转授权」（平常网站体验）
    - redirect_base：未来服务器部署铺垫——指定固定回调 base（默认按 Host 推导，
      如 http://127.0.0.1:8970），部署到公网时设为 https://你的域名
    """
    client_id: str = field(
        default_factory=lambda: os.getenv("GH_OAUTH_CLIENT_ID") or DEFAULT_GH_CLIENT_ID
    )
    client_secret: str | None = field(default_factory=lambda: os.getenv("GH_OAUTH_CLIENT_SECRET"))
    redirect_base: str | None = field(default_factory=lambda: os.getenv("OAUTH_REDIRECT_BASE"))


@dataclass
class SearchConfig:
    """语义搜索配置（参见 docs/algorithm-semantic-search.md）。"""
    embedding_model: str = "BAAI/bge-small-zh-v1.5"      # BGE 中文嵌入
    reranker_model: str = "BAAI/bge-reranker-base"       # Cross-Encoder 重排
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200
    hnsw_ef_search: int = 64
    rrf_k: int = 60                                       # RRF 融合参数
    personalize_weight: float = 0.15                      # 个性化权重


@dataclass
class MemoryConfig:
    """个性化记忆配置（参见 docs/algorithm-personalized-memory.md）。"""
    js_drift_threshold: float = 0.15     # JS 散度漂移阈值
    forgetting_s0: float = 7.0           # 遗忘曲线初始强度
    forgetting_alpha: float = 2.0        # 遗忘曲线衰减速率
    mmr_lambda: float = 0.7              # MMR 多样性权重


@dataclass
class RecommenderConfig:
    """推荐排序配置（参见 docs/algorithm-recommendation.md）。"""
    mmr_lambda: float = 0.7              # 与个性化记忆一致
    ema_alpha: float = 0.3               # EMA 增量更新


@dataclass
class Settings:
    github: GitHubConfig = field(default_factory=GitHubConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    recommender: RecommenderConfig = field(default_factory=RecommenderConfig)
    # CORS 白名单（逗号分隔，如 CORS_ORIGINS=https://2bingling.github.io,http://127.0.0.1:8970）。
    # 默认收紧为 Pages 域名 + 本机回环地址：任何第三方网站的页面将无法静默调用本机
    # API（含行为上报接口）。有其他前端要跨域调用时用 CORS_ORIGINS 环境变量追加。
    cors_origins: list[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv(
                "CORS_ORIGINS",
                "https://2bingling.github.io,http://127.0.0.1:8970,http://localhost:8970",
            ).split(",") if s.strip()
        ]
    )
    debug: bool = field(default_factory=lambda: os.getenv("DEBUG", "0") == "1")
    # 本地个人版入口码：环境变量 > 本地文件 > 首启随机生成（见 _resolve_access_code）；
    # 该门禁用于保护本机服务（防窥），不替代公网多用户认证。
    access_code: str = field(default_factory=lambda: _resolve_access_code())
    # 本地个人版默认启用访问码门禁；需要免门禁预览时可设置
    # STARRADAR_ACCESS_REQUIRED=0。
    access_required: bool = field(default_factory=lambda: os.getenv("STARRADAR_ACCESS_REQUIRED", "1") != "0")


settings = Settings()


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (DATA_DIR, CACHE_DIR, PROFILE_DIR, PERSONAL_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not IS_FROZEN:
        return
    if not STATIC_DIR.exists():
        # 首次启动时完整复制，连同静态初始数据一起带入用户目录。
        shutil.copytree(BUNDLED_STATIC_DIR, STATIC_DIR)
    version_file = STATIC_DIR / ".asset-version"
    try:
        installed_version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        installed_version = ""
    if installed_version == STATIC_ASSET_VERSION:
        return
    # 后续升级只同步页面壳资源。static/data 是用户的本地雷达数据，不能覆盖。
    for entry in _STATIC_ASSET_ENTRIES:
        source = BUNDLED_STATIC_DIR / entry
        target = STATIC_DIR / entry
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    version_file.write_text(STATIC_ASSET_VERSION, encoding="utf-8")
