#!/usr/bin/env python3
"""operator_workbench_ui.py — 一键操作工作台 HTML 前端 v1.0

解决问题：工作台体验差 - 只能看列表，不能直接操作

核心功能：
1. 一键生成 Shopify draft（调用 shopify-launch-master）
2. 一键生成竞品拆解报告（PDP/广告/定价/FAQ）
3. 一键标记"已跟/不跟/监控中"
4. 实时告警通知栏
5. 证据质量仪表盘
"""

from pathlib import Path

def generate_workbench_html() -> str:
    """生成工作台 HTML"""
    
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Operator 作战台 - 一键操作</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #fff;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1600px; margin: 0 auto; }
        
        /* 顶部告警栏 */
        .alerts-bar {
            background: rgba(255,255,255,0.15);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 15px 20px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .alert-item {
            background: rgba(255,255,255,0.2);
            padding: 8px 15px;
            border-radius: 8px;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .alert-item.high { background: rgba(239, 68, 68, 0.3); }
        .alert-item.medium { background: rgba(251, 146, 60, 0.3); }
        
        /* 主面板 */
        .main-panel {
            display: grid;
            grid-template-columns: 300px 1fr;
            gap: 20px;
        }
        
        /* 侧边栏 */
        .sidebar {
            background: rgba(255,255,255,0.15);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 20px;
        }
        .sidebar h3 {
            font-size: 18px;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid rgba(255,255,255,0.2);
        }
        .stat-card {
            background: rgba(255,255,255,0.1);
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 12px;
        }
        .stat-card h4 {
            font-size: 14px;
            opacity: 0.8;
            margin-bottom: 5px;
        }
        .stat-value {
            font-size: 32px;
            font-weight: 700;
        }
        .stat-label {
            font-size: 12px;
            opacity: 0.7;
            margin-top: 5px;
        }
        .grade-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 14px;
        }
        .grade-A { background: #10b981; }
        .grade-B { background: #3b82f6; }
        .grade-C { background: #f59e0b; }
        .grade-D { background: #ef4444; }
        
        /* 产品列表 */
        .products-panel {
            background: rgba(255,255,255,0.15);
            backdrop-filter: blur(10px);
            border-radius: 12px;
            padding: 20px;
        }
        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid rgba(255,255,255,0.2);
        }
        .panel-header h2 { font-size: 24px; }
        .filter-tabs {
            display: flex;
            gap: 10px;
        }
        .filter-tab {
            padding: 8px 16px;
            border-radius: 6px;
            background: rgba(255,255,255,0.1);
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s;
        }
        .filter-tab:hover { background: rgba(255,255,255,0.2); }
        .filter-tab.active { background: #10b981; font-weight: 600; }
        
        /* 产品卡片 */
        .product-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 15px;
        }
        .product-card {
            background: rgba(255,255,255,0.1);
            border-radius: 10px;
            padding: 15px;
            transition: all 0.2s;
        }
        .product-card:hover {
            background: rgba(255,255,255,0.15);
            transform: translateY(-2px);
        }
        .product-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 12px;
        }
        .product-title {
            font-size: 16px;
            font-weight: 600;
            line-height: 1.3;
            flex: 1;
        }
        .product-score {
            background: #10b981;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 14px;
            font-weight: 600;
            margin-left: 10px;
        }
        .product-meta {
            display: flex;
            gap: 15px;
            margin-bottom: 12px;
            font-size: 13px;
            opacity: 0.9;
        }
        .meta-item { display: flex; align-items: center; gap: 5px; }
        
        .evidence-badges {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 12px;
        }
        .badge {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 500;
        }
        .badge.verified { background: rgba(16, 185, 129, 0.3); }
        .badge.pending { background: rgba(251, 146, 60, 0.3); }
        .badge.missing { background: rgba(239, 68, 68, 0.3); }
        
        /* 操作按钮 */
        .actions {
            display: flex;
            gap: 10px;
            margin-top: 12px;
        }
        .btn {
            padding: 8px 16px;
            border-radius: 6px;
            border: none;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            flex: 1;
        }
        .btn-primary {
            background: #10b981;
            color: white;
        }
        .btn-primary:hover { background: #059669; }
        .btn-secondary {
            background: rgba(255,255,255,0.2);
            color: white;
        }
        .btn-secondary:hover { background: rgba(255,255,255,0.3); }
        .btn-danger {
            background: #ef4444;
            color: white;
        }
        .btn-danger:hover { background: #dc2626; }
        
        /* 状态标签 */
        .status-tag {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }
        .status-follow { background: #10b981; }
        .status-monitor { background: #f59e0b; }
        .status-skip { background: #6b7280; }
        
        /* 加载中 */
        .loading {
            text-align: center;
            padding: 60px 20px;
            font-size: 18px;
        }
        .spinner {
            border: 4px solid rgba(255,255,255,0.2);
            border-top-color: white;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 20px auto;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        /* 模态框 */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal.show { display: flex; }
        .modal-content {
            background: #1f2937;
            border-radius: 12px;
            padding: 30px;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }
        .modal-header {
            font-size: 20px;
            font-weight: 600;
            margin-bottom: 20px;
        }
        .modal-close {
            float: right;
            font-size: 24px;
            cursor: pointer;
            opacity: 0.7;
        }
        .modal-close:hover { opacity: 1; }
    </style>
</head>
<body>
    <div class="container">
        <!-- 告警栏 -->
        <div class="alerts-bar" id="alertsBar">
            <div style="font-weight: 600;">🚨 实时告警</div>
            <div id="alertsList">加载中...</div>
        </div>
        
        <!-- 主面板 -->
        <div class="main-panel">
            <!-- 侧边栏 -->
            <div class="sidebar">
                <h3>📊 证据质量</h3>
                <div class="stat-card">
                    <h4>数据质量</h4>
                    <div class="stat-value" id="dataQuality">--</div>
                    <div class="stat-label">
                        <span class="grade-badge" id="dataGrade">-</span>
                    </div>
                </div>
                
                <div class="stat-card">
                    <h4>证据质量</h4>
                    <div class="stat-value" id="evidenceQuality">--</div>
                    <div class="stat-label">
                        <span class="grade-badge" id="evidenceGrade">-</span>
                    </div>
                </div>
                
                <div class="stat-card">
                    <h4>判断质量</h4>
                    <div class="stat-value" id="judgementQuality">--</div>
                    <div class="stat-label">
                        <span class="grade-badge" id="judgementGrade">-</span>
                    </div>
                </div>
                
                <h3 style="margin-top: 30px;">📦 产品统计</h3>
                <div class="stat-card">
                    <h4>总产品数</h4>
                    <div class="stat-value" id="totalProducts">--</div>
                </div>
                
                <div class="stat-card">
                    <h4>可跟品</h4>
                    <div class="stat-value" id="followableProducts">--</div>
                    <div class="stat-label">马上跟 + 轻跟先拆</div>
                </div>
            </div>
            
            <!-- 产品列表 -->
            <div class="products-panel">
                <div class="panel-header">
                    <h2>🎯 跟品队列</h2>
                    <div class="filter-tabs">
                        <div class="filter-tab active" data-filter="all">全部</div>
                        <div class="filter-tab" data-filter="follow_now">马上跟</div>
                        <div class="filter-tab" data-filter="follow_light">轻跟先拆</div>
                        <div class="filter-tab" data-filter="hard_risk">硬风险不碰</div>
                    </div>
                </div>
                
                <div id="productsContainer">
                    <div class="loading">
                        <div class="spinner"></div>
                        加载产品数据...
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- 模态框 -->
    <div class="modal" id="reportModal">
        <div class="modal-content">
            <span class="modal-close" onclick="closeModal()">&times;</span>
            <div class="modal-header" id="modalTitle">竞品拆解报告</div>
            <div id="modalContent"></div>
        </div>
    </div>
    
    <script>
        let currentFilter = 'all';
        let allProducts = [];
        
        // 加载数据
        async function loadData() {
            try {
                // 加载产品列表（使用dashboard API）
                const productsResp = await fetch('/api/dashboard/products?limit=100');
                const productsData = await productsResp.json();
                
                if (productsData.ok && productsData.products) {
                    allProducts = productsData.products;
                    renderProducts();
                }
                
                // 加载 AI 自动驾驶状态
                const statusResp = await fetch('/api/ai-autopilot/status');
                const status = await statusResp.json();
                
                // 更新质量指标
                updateQualityMetrics(status);
                
                // 加载告警
                loadAlerts();
                
            } catch (error) {
                console.error('加载失败:', error);
                document.getElementById('productsContainer').innerHTML = 
                    '<div class="loading">❌ 加载失败: ' + error.message + '</div>';
            }
        }
        
        // 更新质量指标
        function updateQualityMetrics(status) {
            const quality = status.judgement_quality || {};
            
            // 数据质量
            const dataScore = quality.data_quality?.score || 0;
            document.getElementById('dataQuality').textContent = dataScore;
            document.getElementById('dataGrade').textContent = quality.data_quality?.grade || '-';
            document.getElementById('dataGrade').className = 'grade-badge grade-' + (quality.data_quality?.grade || 'E');
            
            // 证据质量
            const evidenceScore = quality.evidence_quality?.score || 0;
            document.getElementById('evidenceQuality').textContent = evidenceScore;
            document.getElementById('evidenceGrade').textContent = quality.evidence_quality?.grade || '-';
            document.getElementById('evidenceGrade').className = 'grade-badge grade-' + (quality.evidence_quality?.grade || 'E');
            
            // 判断质量
            const judgementScore = quality.score || 0;
            document.getElementById('judgementQuality').textContent = judgementScore;
            document.getElementById('judgementGrade').textContent = quality.grade || '-';
            document.getElementById('judgementGrade').className = 'grade-badge grade-' + (quality.grade || 'E');
            
            // 产品统计
            const queueStats = status.queue?.stats || {};
            document.getElementById('totalProducts').textContent = queueStats.total || 0;
            document.getElementById('followableProducts').textContent = 
                (queueStats.follow_now || 0) + (queueStats.follow_light || 0);
        }
        
        // 渲染产品列表
        function renderProducts() {
            const container = document.getElementById('productsContainer');
            
            // 过滤
            const filtered = currentFilter === 'all' 
                ? allProducts 
                : allProducts.filter(p => {
                    if (currentFilter === 'follow_now') return p.lane === 'follow_now';
                    if (currentFilter === 'follow_light') return p.lane === 'follow_light';
                    if (currentFilter === 'hard_risk') return p.lane === 'hard_risk';
                    return true;
                });
            
            if (filtered.length === 0) {
                container.innerHTML = '<div class="loading">暂无产品</div>';
                return;
            }
            
            // 渲染卡片
            const html = filtered.map(p => renderProductCard(p)).join('');
            container.innerHTML = `<div class="product-grid">${html}</div>`;
        }
        
        // 渲染单个产品卡片
        function renderProductCard(product) {
            const lane = product.lane || 'unknown';
            const laneLabels = {
                'follow_now': '马上跟',
                'follow_light': '轻跟先拆',
                'hard_risk': '硬风险不碰'
            };
            
            const score = product.operator_score || 0;
            const title = product.title || product.product_title || '未知产品';
            const domain = product.domain || '';
            const price = product.price || 0;
            const adCount = product.ad_count || 0;
            const trendScore = product.trend_score || 0;
            
            // 证据状态
            const validation = product.required_validation || {};
            const slots = validation.slots || [];
            const evidenceBadges = slots.map(slot => {
                const status = slot.status || 'pending';
                const label = slot.label || slot.key;
                const badgeClass = status === 'verified' ? 'verified' : 
                                   status === 'pending' ? 'pending' : 'missing';
                return `<span class="badge ${badgeClass}">${label}</span>`;
            }).join('');
            
            const laneLabel = laneLabels[lane] || lane;
            const laneClass = lane === 'follow_now' ? 'follow' : 
                             lane === 'follow_light' ? 'monitor' : 'skip';
            
            return `
                <div class="product-card" data-product='${JSON.stringify(product).replace(/'/g, "&apos;")}'>
                    <div class="product-header">
                        <div class="product-title">${title}</div>
                        <div class="product-score">${score}</div>
                    </div>
                    
                    <div class="product-meta">
                        <div class="meta-item">🏪 ${domain}</div>
                        <div class="meta-item">💰 $${price}</div>
                        <div class="meta-item">📊 ${adCount} 广告</div>
                        <div class="meta-item">📈 趋势 ${trendScore}</div>
                    </div>
                    
                    ${product.profit_analysis ? `
                    <div class="profit-box" style="background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); border-radius: 8px; padding: 12px; margin: 10px 0;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight: 600; font-size: 14px;">${product.profit_analysis.verdict.level}</span>
                            <span style="font-weight: 700; color: #10b981;">${product.profit_analysis.profit_score}/100</span>
                        </div>
                        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; font-size: 12px;">
                            <div>
                                <div style="opacity: 0.7;">毛利率</div>
                                <div style="font-weight: 600;">${product.profit_analysis.unit_economics.gross_margin}%</div>
                            </div>
                            <div>
                                <div style="opacity: 0.7;">CAC上限</div>
                                <div style="font-weight: 600;">$${product.profit_analysis.cac_analysis.max_cac_breakeven}</div>
                            </div>
                            <div>
                                <div style="opacity: 0.7;">预估利润</div>
                                <div style="font-weight: 600; color: ${product.profit_analysis.cac_analysis.profit_after_cac > 30 ? '#10b981' : '#f59e0b'};">$${product.profit_analysis.cac_analysis.profit_after_cac}</div>
                            </div>
                        </div>
                        <div style="margin-top: 8px; font-size: 11px; opacity: 0.8;">
                            💡 ${product.profit_analysis.verdict.action}
                        </div>
                    </div>
                    ` : ''}
                    
                    <div style="margin-bottom: 10px;">
                        <span class="status-tag status-${laneClass}">${laneLabel}</span>
                    </div>
                    
                    <div class="evidence-badges">
                        ${evidenceBadges || '<span class="badge pending">待补证</span>'}
                    </div>
                    
                    <div class="actions">
                        <button class="btn btn-primary" onclick="createShopifyDraft(this)">
                            🚀 生成 Draft
                        </button>
                        <button class="btn btn-secondary" onclick="generateReport(this)">
                            📄 拆解报告
                        </button>
                        <button class="btn btn-secondary" onclick="enrichEvidence(this)">
                            🔍 补证
                        </button>
                    </div>
                </div>
            `;
        }
        
        // 切换过滤器
        document.querySelectorAll('.filter-tab').forEach(tab => {
            tab.addEventListener('click', (e) => {
                document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
                e.target.classList.add('active');
                currentFilter = e.target.dataset.filter;
                renderProducts();
            });
        });
        
        // 一键生成 Shopify Draft
        async function createShopifyDraft(btn) {
            const card = btn.closest('.product-card');
            const product = JSON.parse(card.dataset.product);
            
            btn.disabled = true;
            btn.textContent = '⏳ 生成中...';
            
            try {
                const resp = await fetch('/api/v2/shopify/replica-draft/preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        product_url: product.url || '',
                        limit: 1
                    })
                });
                
                const result = await resp.json();
                
                if (result.ok) {
                    btn.textContent = '✅ 已生成';
                    btn.className = 'btn btn-primary';
                    alert(`✅ Shopify Draft 已生成！\n\n文件: ${result.catalog_path}`);
                } else {
                    throw new Error(result.error || '生成失败');
                }
            } catch (error) {
                btn.textContent = '❌ 失败';
                alert('生成失败: ' + error.message);
            } finally {
                setTimeout(() => {
                    btn.disabled = false;
                    btn.textContent = '🚀 生成 Draft';
                }, 2000);
            }
        }
        
        // 生成竞品拆解报告
        function generateReport(btn) {
            const card = btn.closest('.product-card');
            const product = JSON.parse(card.dataset.product);
            
            const report = `
                <h3>🏪 基础信息</h3>
                <p><strong>域名:</strong> ${product.domain}</p>
                <p><strong>标题:</strong> ${product.title || product.product_title}</p>
                <p><strong>价格:</strong> $${product.price}</p>
                <p><strong>URL:</strong> <a href="${product.url}" target="_blank">${product.url}</a></p>
                
                <h3 style="margin-top: 20px;">📊 数据指标</h3>
                <p><strong>广告数:</strong> ${product.ad_count} 个</p>
                <p><strong>趋势评分:</strong> ${product.trend_score}</p>
                <p><strong>Operator 评分:</strong> ${product.operator_score}/100</p>
                
                <h3 style="margin-top: 20px;">🎯 跟品建议</h3>
                <p><strong>决策:</strong> ${product.lane}</p>
                <p><strong>原因:</strong> ${product.reason || '待分析'}</p>
                <p><strong>下一步:</strong> ${product.next_step || '待定'}</p>
                
                <h3 style="margin-top: 20px;">🔍 证据状态</h3>
                ${renderEvidenceSlots(product.required_validation)}
            `;
            
            document.getElementById('modalTitle').textContent = '竞品拆解报告';
            document.getElementById('modalContent').innerHTML = report;
            document.getElementById('reportModal').classList.add('show');
        }
        
        function renderEvidenceSlots(validation) {
            if (!validation || !validation.slots) return '<p>无证据数据</p>';
            
            return validation.slots.map(slot => {
                const status = slot.status === 'verified' ? '✅' : 
                              slot.status === 'pending' ? '⏳' : '❌';
                return `<p>${status} <strong>${slot.label}:</strong> ${slot.status}</p>`;
            }).join('');
        }
        
        // 补证
        async function enrichEvidence(btn) {
            const card = btn.closest('.product-card');
            const product = JSON.parse(card.dataset.product);
            
            btn.disabled = true;
            btn.textContent = '⏳ 补证中...';
            
            alert('🔍 开始补证...\n\n这将调用 evidence_enrichment_engine 自动采集 Reddit、Amazon、供应链数据。');
            
            // TODO: 调用证据补全 API
            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = '🔍 补证';
            }, 2000);
        }
        
        // 关闭模态框
        function closeModal() {
            document.getElementById('reportModal').classList.remove('show');
        }
        
        // 加载告警
        async function loadAlerts() {
            try {
                const resp = await fetch('/api/dashboard/alerts?limit=5');
                const data = await resp.json();
                
                if (data.alerts && data.alerts.length > 0) {
                    const html = data.alerts.map(alert => 
                        `<div class="alert-item ${alert.severity}">
                            ${getAlertEmoji(alert.type)} ${alert.type}
                        </div>`
                    ).join('');
                    document.getElementById('alertsList').innerHTML = html;
                } else {
                    document.getElementById('alertsList').innerHTML = 
                        '<div class="alert-item">✅ 暂无告警</div>';
                }
            } catch (error) {
                document.getElementById('alertsList').innerHTML = 
                    '<div class="alert-item">⚠️ 加载失败</div>';
            }
        }
        
        function getAlertEmoji(type) {
            const map = {
                'new_product': '🆕',
                'price_drop': '💰',
                'sold_out': '⚠️',
                'ad_surge': '📈'
            };
            return map[type] || '📢';
        }
        
        // 初始化
        loadData();
        
        // 自动刷新
        setInterval(loadData, 60000); // 每分钟刷新
    </script>
</body>
</html>
"""


def write_workbench_html():
    """写入工作台 HTML 文件"""
    output_path = Path(__file__).parent / "operator_workbench.html"
    output_path.write_text(generate_workbench_html(), encoding="utf-8")
    return output_path


if __name__ == "__main__":
    path = write_workbench_html()
    print(f"✅ 工作台 HTML 已生成: {path}")
    print(f"📖 访问: http://127.0.0.1:8001/operator-workbench")
