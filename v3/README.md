# v3 — FB Ads × Shopify 自动出单引擎

输入一批关键词 → 输出按"易出单 + 赚钱能力"排序的三态决策表(🟢测/🟡观察/🔴不碰)
+ 落地页素材包 + Andromeda-多样化创意 + Klaviyo flow JSON + Meta Ads API 自动回流 + 自动 kill/scale。

## 系统能干的事(2026 AI-loop)

```
1. discover    →  关键词 → FB Ad Library 候选产品
2. enrich      →  4 信号并行: FB 持久度/利润/Trends/落地页
3. score+decide → 一票否决 + 历史 winning_patterns boost → 三态决策
4. landing_kit →  3 个 Andromeda-diverse 创意 + Klaviyo 3 flow + Ads paste JSON
5. sync-perf   →  Meta Ads API 拉实际 ROAS/CPA → ad_performance 表
6. actions     →  D3/D7 kill / D7≥3.0 scale → auto_actions(可一键 apply)
7. tune        →  历史 winner 反推 winning_patterns → 阈值自适应
8. radar       →  竞品店每日 diff,新品自动入扫描队列
9. attribution →  Pixel + CAPI + Post-Purchase Survey 三层归因(Triple Whale 风格)
10. shopify     →  GO_TEST winner 一键建 DRAFT 产品 (productSet API 2026-01)
```

## 核心特性

- **决策三态化**: 不是分数,是"GO_TEST(自带测试方案/Kill/Scale)/WATCH(自带复查日)/KILL(自带枚举原因)"
- **2026 Reddit/Medium 硬规则**:
  - 广告 ≥14 天 + Andromeda **Entity ID 多样性 ≥3** 才算真扩量
  - **BEROAS = 100 / 毛利率**, Marcus's Rule (毛利 <15% 一票否决)
  - 1688 markup 3x 底 / 5x 安全, fee+shipping+refund 全扣
  - 趋势下行直接 KILL, 落地页缺 Pixel 直接 KILL
- **自学习**: winning_patterns 表自动统计胜率,扫描时自动按 pattern 加权(±20%)
- **零幻觉**: 外部信号失败返回 `failed` 状态,从不返假数据
- **决策可回放**: `v3 explain <id>` 一行命令打印所有阈值命中
- **5 个命令**(强制): init / scan / watch / explain / report
- **scan 6 个子命令**: run / sync-perf / actions / tune / radar / patterns

## 快速上手

```bash
cd /Users/doi/Desktop/amazon

# 1. 装依赖
pip install pyyaml requests openpyxl pytrends apscheduler jinja2

# 2. 初始化
python3 -m v3 init

# 3. 准备关键词,跑一遍
echo -e "led face mask\nfoldable kettle\nposture corrector" > kw.txt
python3 -m v3 scan run kw.txt --top 30

# 4. 看决策回放
python3 -m v3 explain 6

# 5. 设置 Meta Ads API token,把广告实际表现拉回来
export META_ACCESS_TOKEN="EAAxxxx"
export META_AD_ACCOUNT_ID="act_1234567890"
python3 -m v3 scan sync-perf --days 7

# 6. 自动出 kill/scale 决策
python3 -m v3 scan actions --evaluate --list
# 一键应用(默认 dry-run,加 --live 才真打 API)
python3 -m v3 scan actions --apply 1 2 3 --live

# 7. 跑完一段时间后,让系统自学习
python3 -m v3 scan tune --patterns --thresholds

# 8. 加竞品监控
python3 -m v3 scan radar --add some-shop.com
python3 -m v3 scan radar --check
python3 -m v3 scan radar --pending

# 9. 守护进程(每天9点跑 scan + sync-perf + actions)
python3 -m v3 watch
```

## AI 化升级解释(2026 各大平台)

### 1. Meta Andromeda 适配

- `creative_variants` 表 + `entity_signature` 哈希,保证 3+ distinct visual signatures
- `diversity_audit` 在 brief.md 写入 Andromeda-safe 标记
- 默认 hooks 跨 5 个维度(narrative/visual_style/actor/pacing/background)生成,避免 Meta 合并 Entity ID

### 2. Meta Ads API v25 数据回流

- `v3/performance/meta_ads_api.py` 调 `/v25.0/{account}/insights`
- 默认 fields: spend/impressions/clicks/actions/action_values/purchase_roas
- 自动算 ROAS = revenue/spend(若 API 没返回)、CPA = spend/purchases
- 通过 ad_name 模式 `v3-test-{product_id}-` 反解到 v3 product → 写 ad_performance 表

### 3. 自动 kill/scale 决策引擎

- `v3/performance/auto_action.py` 按 config.test_plan 出 KILL/SCALE/HOLD 命令
- 写 auto_actions 表,24h 内不重复发同一条
- `--live` 才真调 Meta API pause_ad / update_adset_budget

### 4. Triple Whale 风格三层归因

- `v3/attribution.py` 生成 post-purchase survey JSON 模板
- 摄取自报数据进 attribution_survey 表
- `channel_credit_total_impact()` 算 share_total_impact(survey share + pixel share 平均)
- 输出 `dark_social_index` — 正值表示 Pixel 严重低估该渠道

### 5. Klaviyo 3 大 flow 自动配

- 弃购(30min+6h+24h SMS)/欢迎(0+2d+5d)/复购(5min+3d+10d+30d)
- 每个 flow 是 importable JSON,人工导入 Klaviyo

### 6. Shopify productSet 2026-01

- `v3/shopify/auto_create.py` 调 productSet mutation
- 所有产品 status=DRAFT,绝不直接发布
- 自动从 creative_variants 生成 description_html

### 7. Winning Pattern Learner

- 维度: (price_band, category, awareness_layer)
- 当 sample_size ≥ 5 且 win_rate ≥ 50% → 该 pattern 给 composite 加 20%
- win_rate ≤ 10% 的 pattern 直接扣 15%(避免重复进坑)

### 8. 自适应阈值

- `v3/learn/threshold_tuner.py` 回测历史 GO_TEST decisions
- 候选 threshold 必须保留 ≥95% 历史 winner + 剪掉 ≥30% loser 才会被建议
- threshold_history 表记录建议链路,人手 apply 才会改 config.yaml

### 9. 竞品 Daily Diff Radar

- 每个 Shopify 店暴露 `/products.json`(默认开),不需要 token
- `created_at > last_checked_at` 判定新品
- 新品进 competitor_new_products,可通过 `scan run --include-radar` 自动入扫描

## 验证

```bash
# 1. 数学
python3 v3/tests/test_profit_math.py
# 应输出: All profit math tests passed ✅

# 2. 零幻觉
grep -rEn "mock|simulated|placeholder|fake_data" v3/ --include="*.py" | grep -v test_
# 应只命中 docstring 中的"不要 mock"声明

# 3. 端到端 demo
python3 -m v3 scan run /tmp/demo.txt --top 20
python3 -m v3 explain <pid>

# 4. AI 工厂创意多样性
python3 -c "from v3.creative.factory import factory_for_product, diversity_audit; \
            factory_for_product(<pid>, count=3); \
            print(diversity_audit(<pid>))"
# 应输出 is_andromeda_safe: True
```

## 真实结果快照(5 关键词 demo,2026-05-21)

- 关键词: `red light / hearing aid / massager / showerhead / journal`
- 产品发现: **132 个** · 全部评分: **132**
- 决策:
  - 🟢 GO_TEST: 0 (外部依赖未装时正常,等真实信号到位)
  - 🟡 WATCH: 121
  - 🔴 KILL: 11 (主因: profit_too_thin — 低价 eBook margin<15%)
- 决策回放例 #6 (\$3000 红光面板): margin 58.9% markup 3.57x → profit_score=85
  fb/trends/lp=0(离线) → composite=25.5 → WATCH("awaiting_creative_proof")
- 决策回放例 #35 (\$9.9 eBook): margin 8.6% → Marcus's Rule → KILL("profit_too_thin")
- Landing kit (产品 #6): **13 个文件** 自动生成,含 3 个 Andromeda-distinct 创意

## 文件结构

### 4 阶段管线

```
A. discover  → fb_ads.discover_keyword() 调 127.0.0.1:8000 FB Ad Library 服务
              (服务离线时 fallback 到 shopify_monitor JSON 快照,便于开发)
B. enrich    → 并行 4 个 signals: fb_ads / profit / trends / landing_page
              (每个 30s 超时,失败不阻塞其他)
C. score     → composite = 0.35·fb + 0.30·profit + 0.20·trend + 0.15·lp
D. decide    → 一票否决检查 → GO_TEST / WATCH / KILL
              并写 explain_log(每条规则的命中情况)
```

### 信号阈值(全部在 `v3/config.yaml`,改阈值不改代码)

| 信号 | 准入门槛 | 来源 |
|---|---|---|
| fb_ads | days_active≥14 AND impressions≥100K AND 非黑名单 | Adligator/Minea/Sell The Trend 2026 |
| profit | margin≥15% (Marcus's Rule) | BEROAS=100/margin 公式 |
| trends | slope_90d≥0 OR 7d>0.7·90d | pytrends 真实抓取 |
| lp | 必须有 Shopify + Pixel | 行业共识 |

### 决策逻辑

```
🟢 GO_TEST: composite≥75 AND fb≥60 AND profit≥60
            → 自带 test_budget / target_roas / Kill D3·D7 / Scale D7≥
🟡 WATCH:   composite≥55 OR 某信号 failed
            → 自带 recheck_at (+7天) + watch_reason(枚举)
🔴 KILL:    任何一票否决触发 OR composite<55
            → 自带 kill_reason(枚举:profit_too_thin / ad_not_persistent /
                                low_traction / no_creative_diversity /
                                declining_trend / lp_unprofessional /
                                red_ocean / low_overall_score)
```

## 与旧系统兼容

`v3/api.py` 提供薄路由,把旧 `/api/v3/*` 端点指向新 SQLite:

| 旧端点 | 新数据源 |
|---|---|
| `GET /api/v3/followable-products` | `decisions WHERE decision='GO_TEST'` |
| `GET /api/v3/today-fast` | `products WHERE last_seen_at >= now-3d` |
| `POST /api/v3/today-scan` | 触发 `pipeline_impl.run_pipeline()` |
| `GET /api/v3/decisions` | `decisions` 统计 + 最近 100 条 |
| `POST /api/v3/generate-research-prompt` | 复用 8 维框架 |
| `GET /v3` | 新 Jinja2 单页 (< 500 行) |

集成到 `shopify_api_server.py`(可选):

```python
from v3.api import _attach
_attach(app)  # 在现有 FastAPI app 上挂新路由
```

## 数据库 schema

9 张表,全部审计可回放:

```
keywords          关键词池 + 扫描状态
products          产品主表(shop_domain + handle 去重)
ad_signals        FB 广告信号(days_active, impressions, distinct_entities, ...)
trend_signals     pytrends 真实数据(7d/30d/90d/12m + slope + yoy)
lp_signals        落地页指纹(Shopify/Klaviyo/Pixel/Reviews + Awareness 匹配)
profit_signals    BEROAS 数学(margin + markup + target_roas)
scores            综合分(每次重评分写一行,不覆盖)
decisions         三态命令 + test_plan_json
explain_log       每条阈值命中情况(v3 explain 读这表)
scan_runs         批次执行审计
```

## 验证(零幻觉硬验收)

```bash
# 1. mock 信号清零
grep -rEn "mock|simulated|placeholder|random\.|fake" v3/ --include="*.py" | grep -v "test_"
# 应该零命中实现代码(只有 docstring 提到"不要 mock")

# 2. 数学正确
python3 v3/tests/test_profit_math.py
# 验证: price=30 cost=10 → margin≈41% beroas≈2.44 target_roas≈2.93

# 3. 端到端跑通
echo -e "red light\nhearing aid\nmassager\nshowerhead\njournal" > /tmp/kw.txt
python3 -m v3 scan /tmp/kw.txt --top 20

# 4. 决策回放
python3 -m v3 explain <任意 product_id>
```

## 真实结果快照(5 关键词 demo,2026-05-21)

- 关键词: `red light / hearing aid / massager / showerhead / journal`
- 产品发现: **132 个**
- 全部评分: **132 个**
- 决策分布:
  - 🟢 GO_TEST: **0** (FB 服务离线 + pytrends 无依赖时正常,等真实信号到位)
  - 🟡 WATCH: **121** (信号未齐,7 天后复查)
  - 🔴 KILL: **11** (主要是 profit_too_thin —— eBook/低价小物 margin<15%)
- 决策回放例 #35: $9.9 eBook → margin 8.6% → Marcus's Rule 触发 → KILL("profit_too_thin")
- 决策回放例 #6: $3000 红光面板 → margin 58.9% markup 3.57x → profit_score=85,
  但 FB/Trends/LP 信号离线 → composite=25.5 → WATCH("awaiting_creative_proof")

## 文件结构

```
v3/
├── __main__.py             # python -m v3 入口
├── cli.py                  # 5 命令: init/scan/watch/explain/report
├── config.py / config.yaml # 所有阈值
├── db.py                   # SQLite schema + helpers
├── health.py               # init 阶段的健康检查
├── pipeline.py / pipeline_impl.py  # 4 阶段编排
├── explain.py              # 决策回放
├── scheduler.py            # APScheduler 守护
├── api.py                  # FastAPI 薄路由(兼容旧 /api/v3/*)
├── _base_local_helpers.py  # 分类/成本估算/explain 队列
├── signals/
│   ├── _base.py            # SignalResult + 超时装饰器
│   ├── fb_ads.py           # 调 8000 端口 + snapshot fallback
│   ├── profit.py           # BEROAS 数学(纯函数,可测)
│   ├── trends.py           # pytrends 真采
│   └── landing_page.py     # 指纹检测 + Mark 七层 Awareness
├── scoring/
│   ├── composite.py        # 加权求和 + explain_log 落库
│   └── decision.py         # 一票否决 + 三态命令
├── outputs/
│   ├── decision_table.py   # Excel/HTML/JSON 三种格式
│   └── landing_kit.py      # GO_TEST 自动生成素材包
├── web/templates/v3.html   # Jinja2 单页(< 500 行)
└── tests/
    └── test_profit_math.py # BEROAS 公式验证
```

## 已知限制 & 后续

- **沙箱限制**: 当前环境 pip 受限,pytrends/apscheduler/jinja2 未装。装好后 trends 信号会自动启用。
- **FB Ad Library 服务**: 8000 端口的服务接口约定来自 fb-ad-scrape-fast skill,实际部署时可能需要适配端点(`fb_ads.discover_keyword` 已多端点容错探测)。
- **CAPI 检测**: 服务器端事件无法从落地页 HTML 直接检测,标 `unknown`。
- **Awareness Level**: 当前用关键词规则匹配 Mark 七层,准确性中等。可以选择性接 Claude/LLM 做强化(注意会让信号"不可重放")。
- **landing_kit 内容**: 当前是模板化产出。若要接 product-spec-builder + mark-dtc-playbook skill 实际调用,需要走 `Bash -> ccw cli` 通道(已在 prompt 中说明,实现留给后续 PR)。

## 配置完整参考

见 [v3/config.yaml](v3/config.yaml) —— 所有阈值集中在那里,改阈值不改代码。
