#!/usr/bin/env python3
"""
盈利能力分析引擎 - 补全关键财务指标
"""
import json
import re
from typing import Dict, List, Optional

class ProfitAnalyzer:
    """精准盈利分析"""
    
    # 品类成本估算表 (1688/阿里巴巴均价)
    CATEGORY_COST_MAP = {
        'LED/红光美容仪': {'cost_range': (15, 35), 'shipping': 8, 'margin_target': 0.70},
        '背部/腰部按摩工具': {'cost_range': (12, 28), 'shipping': 7, 'margin_target': 0.68},
        '颈部按摩仪': {'cost_range': (10, 25), 'shipping': 6, 'margin_target': 0.70},
        '便携按摩枪': {'cost_range': (18, 40), 'shipping': 9, 'margin_target': 0.65},
        '智能手表/手环': {'cost_range': (8, 20), 'shipping': 5, 'margin_target': 0.75},
        '蓝牙耳机': {'cost_range': (5, 15), 'shipping': 4, 'margin_target': 0.78},
        '美容仪器': {'cost_range': (12, 30), 'shipping': 7, 'margin_target': 0.68},
        '健身器材': {'cost_range': (15, 45), 'shipping': 12, 'margin_target': 0.65},
        '厨房小家电': {'cost_range': (10, 25), 'shipping': 8, 'margin_target': 0.70},
        '宠物用品': {'cost_range': (5, 18), 'shipping': 6, 'margin_target': 0.72},
        '汽车配件': {'cost_range': (8, 22), 'shipping': 7, 'margin_target': 0.68},
    }
    
    # FB广告CPM基准 (2026 Q2)
    FB_CPM = 15  # $15/千次
    FB_CTR = 0.012  # 1.2% 点击率
    FB_CVR_BENCHMARK = 0.025  # 2.5% 转化率基准
    
    # 运营成本
    SHOPIFY_MONTHLY = 79  # Shopify计划
    PAYMENT_FEE = 0.029 + 0.30  # Stripe费率
    REFUND_RATE = 0.08  # 8% 退货率
    
    def __init__(self):
        pass
    
    def estimate_cost(self, product: Dict) -> Dict:
        """估算产品成本"""
        category = product.get('specific_category', '')
        price = product.get('price', 0)
        
        cost_data = self.CATEGORY_COST_MAP.get(category)
        if not cost_data:
            # 默认按70%毛利倒推
            cost_data = {
                'cost_range': (price * 0.2, price * 0.35),
                'shipping': 7,
                'margin_target': 0.65
            }
        
        cost_low, cost_high = cost_data['cost_range']
        cost_avg = (cost_low + cost_high) / 2
        shipping = cost_data['shipping']
        
        return {
            'product_cost_low': cost_low,
            'product_cost_high': cost_high,
            'product_cost_avg': cost_avg,
            'shipping_cost': shipping,
            'total_cogs': cost_avg + shipping,
            'cost_source': 'category_benchmark' if category in self.CATEGORY_COST_MAP else 'estimated'
        }
    
    def calculate_unit_economics(self, product: Dict, cost_data: Dict) -> Dict:
        """单位经济模型"""
        price = product.get('price', 0)
        cogs = cost_data['total_cogs']
        
        # 收入
        revenue = price
        
        # 直接成本
        payment_fee = revenue * 0.029 + 0.30  # Stripe: 2.9% + $0.30
        
        # 毛利
        gross_profit = revenue - cogs - payment_fee
        gross_margin = gross_profit / revenue if revenue > 0 else 0
        
        # 贡献利润 (扣除退货)
        contribution_profit = gross_profit * (1 - self.REFUND_RATE)
        contribution_margin = contribution_profit / revenue if revenue > 0 else 0
        
        return {
            'revenue': round(revenue, 2),
            'cogs': round(cogs, 2),
            'payment_fee': round(payment_fee, 2),
            'gross_profit': round(gross_profit, 2),
            'gross_margin': round(gross_margin * 100, 1),
            'contribution_profit': round(contribution_profit, 2),
            'contribution_margin': round(contribution_margin * 100, 1),
        }
    
    def calculate_cac_breakeven(self, unit_econ: Dict, product: Dict) -> Dict:
        """计算可承受CAC和盈亏平衡点"""
        contribution_profit = unit_econ['contribution_profit']
        
        # FB广告基准
        ad_appearances = product.get('ad_appearances', 0)
        
        # CAC上限 = 贡献利润 (保守)
        max_cac_breakeven = contribution_profit
        
        # 目标CAC (留30%利润空间)
        target_cac = contribution_profit * 0.70
        
        # 估算CPA (基于FB CPM和行业基准)
        cpc = self.FB_CPM / 1000 / self.FB_CTR
        estimated_cpa = cpc / self.FB_CVR_BENCHMARK
        
        # 盈利性判断
        is_profitable = estimated_cpa < max_cac_breakeven
        profit_margin_after_cac = contribution_profit - estimated_cpa
        
        return {
            'max_cac_breakeven': round(max_cac_breakeven, 2),
            'target_cac_70pct': round(target_cac, 2),
            'estimated_cpa': round(estimated_cpa, 2),
            'is_profitable': is_profitable,
            'profit_after_cac': round(profit_margin_after_cac, 2),
            'safety_margin': round((max_cac_breakeven - estimated_cpa) / max_cac_breakeven * 100, 1) if max_cac_breakeven > 0 else 0,
        }
    
    def analyze_product(self, product: Dict) -> Dict:
        """完整盈利分析"""
        # 1. 成本估算
        cost_data = self.estimate_cost(product)
        
        # 2. 单位经济
        unit_econ = self.calculate_unit_economics(product, cost_data)
        
        # 3. CAC盈亏平衡
        cac_analysis = self.calculate_cac_breakeven(unit_econ, product)
        
        # 4. 综合判断
        verdict = self._generate_verdict(product, cost_data, unit_econ, cac_analysis)
        
        return {
            'cost_structure': cost_data,
            'unit_economics': unit_econ,
            'cac_analysis': cac_analysis,
            'verdict': verdict,
            'profit_score': self._calculate_profit_score(unit_econ, cac_analysis)
        }
    
    def _calculate_profit_score(self, unit_econ: Dict, cac: Dict) -> int:
        """盈利能力评分 0-100"""
        score = 0
        
        # 毛利率 (0-40分)
        gm = unit_econ['gross_margin']
        if gm >= 70:
            score += 40
        elif gm >= 60:
            score += 30
        elif gm >= 50:
            score += 20
        else:
            score += 10
        
        # 安全边际 (0-40分)
        safety = cac['safety_margin']
        if safety >= 50:
            score += 40
        elif safety >= 30:
            score += 30
        elif safety >= 10:
            score += 20
        else:
            score += 10
        
        # 可盈利性 (0-20分)
        if cac['is_profitable']:
            score += 20
            if cac['profit_after_cac'] > 20:
                score += 10  # 额外奖励
        
        return min(score, 100)
    
    def _generate_verdict(self, product: Dict, cost: Dict, unit_econ: Dict, cac: Dict) -> Dict:
        """生成盈利判断"""
        is_profitable = cac['is_profitable']
        gm = unit_econ['gross_margin']
        safety = cac['safety_margin']
        
        if is_profitable and gm >= 65 and safety >= 30:
            level = '🟢 高利润'
            reason = f"毛利率{gm}%，CAC安全边际{safety}%，单笔净利${cac['profit_after_cac']}"
            action = '立即测款'
        elif is_profitable and gm >= 55:
            level = '🟡 可测试'
            reason = f"毛利率{gm}%，盈利但安全边际偏低{safety}%"
            action = '小预算测试'
        elif gm < 50:
            level = '🔴 不赚钱'
            reason = f"毛利率仅{gm}%，成本过高"
            action = '放弃或重新选品'
        else:
            level = '🟠 边缘'
            reason = f"预估CPA ${cac['estimated_cpa']} 接近盈亏平衡 ${cac['max_cac_breakeven']}"
            action = '需要优化素材降低CPA'
        
        return {
            'level': level,
            'reason': reason,
            'action': action,
            'key_risks': self._identify_risks(product, cost, unit_econ, cac)
        }
    
    def _identify_risks(self, product: Dict, cost: Dict, unit_econ: Dict, cac: Dict) -> List[str]:
        """识别关键风险"""
        risks = []
        
        if unit_econ['gross_margin'] < 60:
            risks.append(f"毛利率偏低 {unit_econ['gross_margin']}%")
        
        if cac['safety_margin'] < 20:
            risks.append(f"CAC安全边际不足 {cac['safety_margin']}%")
        
        if cost['cost_source'] == 'estimated':
            risks.append("成本为估算值，需验证1688实际报价")
        
        if product.get('ad_appearances', 0) < 5:
            risks.append(f"FB广告数仅{product.get('ad_appearances')}，市场验证不足")
        
        if product.get('trend_score', 0) < 50:
            risks.append(f"趋势分数偏低 {product.get('trend_score')}")
        
        return risks


def enrich_profit_data(input_json: str, output_json: str):
    """批量补全盈利数据"""
    analyzer = ProfitAnalyzer()
    
    with open(input_json) as f:
        data = json.load(f)
    
    enriched = []
    for product in data.get('scored_products', []):
        # 只分析高价值产品
        if product['tier'] not in ['🚀 立即测款', '⚡ 轻跟观察', '🔬 素材拆解']:
            enriched.append(product)
            continue
        
        # 补全盈利分析
        profit_analysis = analyzer.analyze_product(product)
        product['profit_analysis'] = profit_analysis
        
        enriched.append(product)
    
    data['scored_products'] = enriched
    
    with open(output_json, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 已补全 {len(enriched)} 个产品的盈利数据")
    
    # 统计
    go_products = [p for p in enriched if p['tier'] == '🚀 立即测款']
    print(f"\n🚀 立即测款产品盈利概览:")
    for p in go_products[:10]:
        pa = p.get('profit_analysis', {})
        v = pa.get('verdict', {})
        print(f"\n{p['product_title'][:40]}")
        print(f"  {v.get('level')} - {v.get('action')}")
        print(f"  毛利率: {pa.get('unit_economics', {}).get('gross_margin')}%")
        print(f"  CAC上限: ${pa.get('cac_analysis', {}).get('max_cac_breakeven')}")
        print(f"  预估CPA: ${pa.get('cac_analysis', {}).get('estimated_cpa')}")
        print(f"  盈利评分: {pa.get('profit_score')}/100")


if __name__ == '__main__':
    import sys
    input_file = sys.argv[1] if len(sys.argv) > 1 else '/tmp/today-fast-8011.json'
    output_file = sys.argv[2] if len(sys.argv) > 2 else '/tmp/today-fast-8011-enriched.json'
    
    enrich_profit_data(input_file, output_file)
