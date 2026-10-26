"""Product choices for the pinned Qwen3 backend; no model or profile probing."""
from limits import MODEL_CONTEXTS, REPLY_TOKENS, THINKING_BUDGETS


def choices(engine):
    labels={'4B':'輕量 · 4B','8B':'日常 · 8B','14B':'品質試用 · 14B'}
    descriptions={
        '4B':'較省資源；依實際問題選擇回覆長度。',
        '8B':'目前日常基準。32K 與長篇需要較多顯存及等待時間。',
        '14B':'選配品質試用；本機限 8K。12GB 顯存餘裕較小，請先卸載遊戲等高用量程式。',
    }
    profiles={}
    for key,contexts in MODEL_CONTEXTS.items():
        installed=engine.model_path(key).is_file()
        profiles[key]={'label':labels[key],'contexts':list(contexts),
            'thinking_modes':list(THINKING_BUDGETS),
            'response_lengths':[name for name in REPLY_TOKENS if key!='14B' or name not in ('long','extended')],
            'installed':installed,'experimental':key=='14B','installable':key!='14B',
            'status':'trial' if installed else 'not_installed',
            'description':descriptions[key]+(' 請先備妥固定版本的離線選配權重。' if key=='14B' and not installed else '')}
    return {'profiles':profiles,'thinking_modes':{
        key:{'label':label,'budget_tokens':THINKING_BUDGETS[key]}
        for key,label in [('fast','快速'),('standard','標準思考 · 試用'),('deep','深入思考 · 試用')]},
        'reply_tokens':REPLY_TOKENS,'budget_scope':'combined','force_reserve_tokens':128,
        'context_safety_tokens':256}
