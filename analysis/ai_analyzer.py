"""
AI大模型分析模块
利用AI分析市场新闻、判断市场环境、提供建议
"""

import json
import requests
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class AIAnalysisResult:
    """AI分析结果"""
    market_sentiment: str       # 市场情绪判断
    key_events: List[str]       # 关键事件
    impact_analysis: str        # 影响分析
    strategy_suggestion: str    # 策略建议
    risk_warning: str           # 风险提示
    confidence: float           # 置信度
    raw_response: str           # 原始回复


class AIAnalyzer:
    """
    AI大模型分析器

    支持：
    1. OpenAI API (GPT-4)
    2. 通义千问 API
    3. 文心一言 API
    4. 本地大模型 API
    """

    def __init__(self, provider: str = 'openai', api_key: str = '', base_url: str = ''):
        """
        初始化AI分析器

        Args:
            provider: AI提供商 ('openai', 'tongyi', 'wenxin', 'local')
            api_key: API密钥
            base_url: API地址（本地模型用）
        """
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url

        # 默认配置
        self.configs = {
            'openai': {
                'url': 'https://api.openai.com/v1/chat/completions',
                'model': 'gpt-4o-mini',
                'headers': {
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                }
            },
            'tongyi': {
                'url': 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
                'model': 'qwen-turbo',
                'headers': {
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                }
            },
            'wenxin': {
                'url': 'https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/ernie-speed-128k',
                'model': 'ernie-speed-128k',
                'headers': {
                    'Content-Type': 'application/json'
                }
            },
            'local': {
                'url': base_url or 'http://localhost:11434/api/generate',
                'model': 'qwen2.5',
                'headers': {
                    'Content-Type': 'application/json'
                }
            },
            'deepseek': {
                'url': 'https://api.deepseek.com/v1/chat/completions',
                'model': 'deepseek-chat',
                'headers': {
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                }
            }
        }

    def analyze_market_news(self, news_list: List[str], market_data: Dict = None) -> AIAnalysisResult:
        """
        分析市场新闻

        Args:
            news_list: 新闻列表
            market_data: 市场数据（可选）

        Returns:
            AIAnalysisResult: 分析结果
        """
        # 构建提示词
        prompt = self._build_news_analysis_prompt(news_list, market_data)

        # 调用AI
        response = self._call_ai(prompt)

        # 解析结果
        return self._parse_analysis_response(response)

    def analyze_event_impact(self, event: str, related_stocks: List[str] = None) -> AIAnalysisResult:
        """
        分析特定事件影响

        Args:
            event: 事件描述
            related_stocks: 相关股票

        Returns:
            AIAnalysisResult: 分析结果
        """
        prompt = self._build_event_analysis_prompt(event, related_stocks)
        response = self._call_ai(prompt)
        return self._parse_analysis_response(response)

    def get_market_regime_suggestion(self, regime_analysis: Dict) -> AIAnalysisResult:
        """
        基于市场环境分析获取建议

        Args:
            regime_analysis: 市场环境分析结果

        Returns:
            AIAnalysisResult: 分析结果
        """
        prompt = self._build_regime_suggestion_prompt(regime_analysis)
        response = self._call_ai(prompt)
        return self._parse_analysis_response(response)

    def _build_news_analysis_prompt(self, news_list: List[str], market_data: Dict = None) -> str:
        """构建新闻分析提示词"""
        news_text = "\n".join([f"- {news}" for news in news_list[:20]])  # 最多20条

        market_info = ""
        if market_data:
            # 提取关键市场数据
            market_info = f"""
当前市场数据：
- 上证指数: {market_data.get('sh_index', '未知')}
- 深证成指: {market_data.get('sz_index', '未知')}
- 创业板指: {market_data.get('cy_index', '未知')}
- 两市成交额: {market_data.get('volume', '未知')}
"""

        prompt = f"""你是一个专业的A股市场分析师。请分析以下新闻和市场信息，给出投资建议。

今日新闻：
{news_text}
{market_info}

请从以下维度分析，并用JSON格式回复：

1. market_sentiment: 市场情绪判断（极度悲观/悲观/中性/乐观/极度乐观）
2. key_events: 关键事件列表（最多5条）
3. impact_analysis: 影响分析（简述对市场的影响）
4. strategy_suggestion: 策略建议（具体的操作建议）
5. risk_warning: 风险提示
6. confidence: 分析置信度（0-1）

请直接返回JSON，不要有其他文字。"""

        return prompt

    def _build_event_analysis_prompt(self, event: str, related_stocks: List[str] = None) -> str:
        """构建事件分析提示词"""
        stocks_text = ""
        if related_stocks:
            stocks_text = f"\n相关股票：{', '.join(related_stocks[:10])}"

        prompt = f"""你是一个专业的A股市场分析师。请分析以下事件对A股市场的影响。

事件：{event}
{stocks_text}

请从以下维度分析，并用JSON格式回复：

1. market_sentiment: 对市场情绪的影响（极度悲观/悲观/中性/乐观/极度乐观）
2. key_events: 事件的关键要点（最多5条）
3. impact_analysis: 详细影响分析（对不同板块、不同类型股票的影响）
4. strategy_suggestion: 策略建议（短期和中期的操作建议）
5. risk_warning: 风险提示
6. confidence: 分析置信度（0-1）

请直接返回JSON，不要有其他文字。"""

        return prompt

    def _build_regime_suggestion_prompt(self, regime_analysis: Dict) -> str:
        """构建市场环境建议提示词"""
        prompt = f"""你是一个专业的A股市场分析师和量化策略专家。

当前市场环境分析：
- 市场类型：{regime_analysis.get('regime', '未知')}
- 趋势得分：{regime_analysis.get('trend_score', 0):.2f}
- 波动率得分：{regime_analysis.get('volatility_score', 0):.2f}
- 动量得分：{regime_analysis.get('momentum_score', 0):.2f}
- 市场广度：{regime_analysis.get('breadth_score', 0):.2f}
- 情绪得分：{regime_analysis.get('sentiment_score', 0):.2f}

基于以上分析，请给出：

1. 对当前市场环境的判断和解释
2. 最适合的2-3个策略及原因
3. 具体的仓位管理建议
4. 需要特别注意的风险点
5. 如果发生转向的信号是什么

请用JSON格式回复：
{{
    "market_sentiment": "市场情绪判断",
    "key_events": ["关键点1", "关键点2"],
    "impact_analysis": "详细分析",
    "strategy_suggestion": "策略建议",
    "risk_warning": "风险提示",
    "confidence": 0.8
}}"""

        return prompt

    def _call_ai(self, prompt: str) -> str:
        """调用AI API"""
        config = self.configs.get(self.provider)
        if not config:
            return self._fallback_analysis(prompt)

        try:
            if self.provider in ['openai', 'deepseek']:
                return self._call_openai_compatible(prompt, config)
            elif self.provider == 'tongyi':
                return self._call_tongyi(prompt, config)
            elif self.provider == 'wenxin':
                return self._call_wenxin(prompt, config)
            elif self.provider == 'local':
                return self._call_local(prompt, config)
            else:
                return self._fallback_analysis(prompt)
        except Exception as e:
            print(f"AI调用失败: {e}")
            return self._fallback_analysis(prompt)

    def _call_openai_compatible(self, prompt: str, config: Dict) -> str:
        """调用OpenAI兼容接口"""
        payload = {
            "model": config['model'],
            "messages": [
                {"role": "system", "content": "你是一个专业的A股市场分析师。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 1000
        }

        response = requests.post(
            config['url'],
            headers=config['headers'],
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            raise Exception(f"API错误: {response.status_code} {response.text}")

    def _call_tongyi(self, prompt: str, config: Dict) -> str:
        """调用通义千问"""
        payload = {
            "model": config['model'],
            "input": {
                "messages": [
                    {"role": "system", "content": "你是一个专业的A股市场分析师。"},
                    {"role": "user", "content": prompt}
                ]
            },
            "parameters": {
                "temperature": 0.3,
                "max_tokens": 1000
            }
        }

        response = requests.post(
            config['url'],
            headers=config['headers'],
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            return response.json()['output']['choices'][0]['message']['content']
        else:
            raise Exception(f"API错误: {response.status_code}")

    def _call_wenxin(self, prompt: str, config: Dict) -> str:
        """调用文心一言"""
        # 需要先获取access_token
        payload = {
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_output_tokens": 1000
        }

        url = f"{config['url']}?access_token={self.api_key}"
        response = requests.post(url, json=payload, timeout=30)

        if response.status_code == 200:
            return response.json()['result']
        else:
            raise Exception(f"API错误: {response.status_code}")

    def _call_local(self, prompt: str, config: Dict) -> str:
        """调用本地模型（如Ollama）"""
        payload = {
            "model": config['model'],
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.3
            }
        }

        response = requests.post(
            config['url'],
            json=payload,
            timeout=60
        )

        if response.status_code == 200:
            return response.json()['response']
        else:
            raise Exception(f"本地模型错误: {response.status_code}")

    def _fallback_analysis(self, prompt: str) -> str:
        """降级分析（无AI时使用）"""
        # 简单的关键词分析
        negative_keywords = ['暴跌', '大跌', '利空', '风险', '下跌', '危机', '恐慌', '衰退', '加息', '缩表']
        positive_keywords = ['利好', '上涨', '突破', '新高', '增长', '复苏', '政策支持', '降息', '刺激']

        prompt_lower = prompt.lower()

        neg_count = sum(1 for kw in negative_keywords if kw in prompt)
        pos_count = sum(1 for kw in positive_keywords if kw in prompt)

        if neg_count > pos_count + 2:
            sentiment = "悲观"
        elif pos_count > neg_count + 2:
            sentiment = "乐观"
        else:
            sentiment = "中性"

        result = {
            "market_sentiment": sentiment,
            "key_events": ["基于关键词分析"],
            "impact_analysis": f"检测到{neg_count}个负面信号和{pos_count}个正面信号",
            "strategy_suggestion": "建议观望，等待明确信号",
            "risk_warning": "AI分析不可用，使用降级分析，请谨慎参考",
            "confidence": 0.4
        }

        return json.dumps(result, ensure_ascii=False)

    def _parse_analysis_response(self, response: str) -> AIAnalysisResult:
        """解析AI回复"""
        try:
            # 尝试提取JSON
            json_str = response

            # 如果回复包含其他文字，尝试提取JSON部分
            if '{' in response and '}' in response:
                start = response.index('{')
                end = response.rindex('}') + 1
                json_str = response[start:end]

            data = json.loads(json_str)

            return AIAnalysisResult(
                market_sentiment=data.get('market_sentiment', '中性'),
                key_events=data.get('key_events', []),
                impact_analysis=data.get('impact_analysis', ''),
                strategy_suggestion=data.get('strategy_suggestion', ''),
                risk_warning=data.get('risk_warning', ''),
                confidence=float(data.get('confidence', 0.5)),
                raw_response=response
            )

        except (json.JSONDecodeError, ValueError) as e:
            # JSON解析失败，返回默认值
            return AIAnalysisResult(
                market_sentiment='中性',
                key_events=['AI回复解析失败'],
                impact_analysis=response[:500] if response else '无',
                strategy_suggestion='建议观望',
                risk_warning='AI分析结果可能不准确',
                confidence=0.3,
                raw_response=response
            )


# ============================================================
# 预设的市场事件分析模板
# ============================================================

MARKET_EVENT_TEMPLATES = {
    'fed_rate_hike': {
        'event': '美联储加息',
        'impact': {
            'A股': '利空，资金外流压力',
            '科技股': '高估值承压',
            '银行股': '息差可能扩大，中性偏多',
            '黄金': '利空',
            '美元': '利多',
        },
        'strategy': '降低仓位，关注防御板块'
    },
    'fed_rate_cut': {
        'event': '美联储降息',
        'impact': {
            'A股': '利多，资金流入',
            '科技股': '估值提升',
            '银行股': '息差收窄，偏空',
            '黄金': '利多',
            '美元': '利空',
        },
        'strategy': '可适当加仓，关注成长股'
    },
    'china_gdp_up': {
        'event': '中国GDP超预期',
        'impact': {
            'A股': '利多',
            '周期股': '需求预期改善',
            '消费股': '收入预期提升',
        },
        'strategy': '可加仓周期和消费'
    },
    'trade_war': {
        'event': '贸易摩擦升级',
        'impact': {
            'A股': '利空',
            '出口企业': '直接利空',
            '国产替代': '可能受益',
        },
        'strategy': '降低仓位，关注内需和国产替代'
    },
}


def analyze_fed_event(event_type: str = 'non_farm') -> AIAnalysisResult:
    """
    分析美联储相关事件

    Args:
        event_type: 事件类型 ('non_farm', 'rate_decision', 'cpi', 'fomc')
    """
    event_descriptions = {
        'non_farm': '美国非农就业数据超预期，市场预期美联储可能继续加息',
        'rate_decision': '美联储议息会议，市场关注利率决议和点阵图',
        'cpi': '美国CPI数据公布，通胀预期变化',
        'fomc': '美联储FOMC会议纪要公布，政策信号解读',
    }

    event = event_descriptions.get(event_type, '美联储相关事件')

    # 使用预设分析（无需AI）
    analysis = MARKET_EVENT_TEMPLATES.get('fed_rate_hike', {})

    result = AIAnalysisResult(
        market_sentiment="悲观",
        key_events=[
            event,
            "市场预期美联储加息",
            "全球资产价格承压",
            "资金可能流出新兴市场"
        ],
        impact_analysis=f"""
{event}对A股的影响：

1. **直接影响**：
   - 美元走强，人民币贬值压力
   - 外资可能流出A股
   - 高估值成长股承压

2. **板块影响**：
   - 利空：科技、新能源、消费（高估值）
   - 中性：周期股（看国内基本面）
   - 相对抗跌：银行（息差）、公用事业（低估值）

3. **传导路径**：
   美联储加息 → 美元走强 → 资金回流美国 → 新兴市场承压 → A股下跌
""",
        strategy_suggestion="""
短期策略（1-2周）：
1. 降低仓位至40-50%
2. 回避高估值成长股
3. 关注银行、公用事业等低估值防御板块
4. 等待市场消化利空

中期策略（1-3月）：
1. 关注美联储后续政策走向
2. 如果加息预期被充分定价，可逢低布局
3. 关注国内政策对冲（降准、降息）
""",
        risk_warning="""
⚠️ 风险提示：
1. 如果非农数据持续超预期，加息可能超预期
2. 人民币贬值可能加速外资流出
3. 全球风险资产可能同步下跌
4. A股可能跟跌不跟涨
""",
        confidence=0.8,
        raw_response="基于预设模板分析"
    )

    return result


def print_ai_analysis(result: AIAnalysisResult):
    """打印AI分析结果"""
    print("=" * 60)
    print("AI市场分析")
    print("=" * 60)

    sentiment_emoji = {
        "极度悲观": "😱",
        "悲观": "😟",
        "中性": "😐",
        "乐观": "😊",
        "极度乐观": "🤩",
    }

    print(f"\n市场情绪：{sentiment_emoji.get(result.market_sentiment, '❓')} {result.market_sentiment}")
    print(f"置信度：{result.confidence:.0%}")

    print(f"\n关键事件：")
    for event in result.key_events:
        print(f"  • {event}")

    print(f"\n影响分析：")
    print(result.impact_analysis)

    print(f"\n策略建议：")
    print(result.strategy_suggestion)

    print(f"\n风险提示：")
    print(result.risk_warning)

    print("=" * 60)


# ============================================================
# 使用示例
# ============================================================

if __name__ == '__main__':
    # 示例1：分析美联储非农数据事件
    print("\n=== 美联储非农数据超预期分析 ===")
    result = analyze_fed_event('non_farm')
    print_ai_analysis(result)

    # 示例2：使用AI分析（需要配置API）
    # analyzer = AIAnalyzer(provider='deepseek', api_key='your_api_key')
    # result = analyzer.analyze_market_news([
    #     "美联储非农数据超预期，市场预期加息",
    #     "A股三大指数全线下跌",
    #     "北向资金净流出50亿"
    # ])
    # print_ai_analysis(result)