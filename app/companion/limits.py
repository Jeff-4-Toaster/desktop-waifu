"""Shared conversation limits; tokens are not character counts."""
INPUT_CHARACTERS = 12000
CONTEXT_SIZES = (8192, 16384, 32768)
REPLY_TOKENS = {'brief': 512, 'balanced': 2048, 'detailed': 4096, 'long': 8192, 'extended': 16384}
THINKING_BUDGETS = {'fast': 0, 'standard': 2048, 'deep': 4096}
THINKING_FORCE_RESERVE = 128
MODEL_CONTEXTS = {'4B': (8192, 16384), '8B': (8192, 16384, 32768), '14B': (8192,)}
REPLY_GUIDANCE = {
    'brief': '偏好簡短：通常幾句說完；複雜問題保留重點。',
    'balanced': '偏好自然完整：日常寒暄可以簡短，解釋、建議或故事請充分展開，依需要分段。',
    'detailed': '偏好詳細：需要解釋、規畫或故事時，提供充分細節、例子和完整段落；簡單招呼仍自然回答。',
    'long': '偏好長篇：複雜說明或創作請充分展開，安排清楚段落與具體內容；簡單問題仍自然回答，不強迫寫滿上限。',
    'extended': '偏好超長篇：長篇分析或故事請保持完整結構、人物與細節一致，按需求充分展開；不要重複灌水，不強迫每次寫滿上限。',
}


def generation_budget(max_tokens, thinking_mode='fast', reasoning_budget=None):
    if type(max_tokens) is not int or not 1 <= max_tokens <= 16384:
        raise ValueError('無效的回覆預算')
    if not isinstance(thinking_mode, str) or thinking_mode not in THINKING_BUDGETS:
        raise ValueError('無效的思考模式')
    reasoning = THINKING_BUDGETS[thinking_mode] if reasoning_budget is None else reasoning_budget
    if type(reasoning) is not int or not 0 <= reasoning <= THINKING_BUDGETS[thinking_mode]:
        raise ValueError('無效的思考預算')
    force = THINKING_FORCE_RESERVE if thinking_mode != 'fast' else 0
    return {'answer_tokens': max_tokens, 'reasoning_tokens': reasoning,
            'force_reserve_tokens': force, 'combined_tokens': max_tokens + reasoning + force,
            'budget_scope': 'combined'}


def validate_settings(values):
    profile, context = values['profile'], values['context_tokens']
    if not isinstance(profile, str) or profile not in MODEL_CONTEXTS:
        raise ValueError('無效的模型配置')
    if type(context) is not int or context not in MODEL_CONTEXTS[profile]:
        raise ValueError(profile + ' 請選擇目前支援的對話容量')
    reply, mode = values['response_length'], values['thinking_mode']
    if not isinstance(reply, str) or reply not in REPLY_TOKENS:
        raise ValueError('無效的回覆長度')
    if profile == '14B' and reply in ('long', 'extended'):
        raise ValueError('14B 的本機試用範圍目前不含長篇／超長篇預算')
    budget = generation_budget(REPLY_TOKENS[reply], mode)
    if budget['combined_tokens'] + 256 >= context:
        raise ValueError('思考與回覆預算超過容量，請選較短回覆、快速模式或較大容量')
    return budget
