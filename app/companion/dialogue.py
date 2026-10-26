"""Small, shared prompt choices for scene continuity and factual capabilities."""
import re


def _leaving(text):
    return bool(re.search(r'不要(?:再)?(?:想像|扮演|虛構|編故事)|停止扮演|不用(?:再)?(?:扮演|情境)|回到現實|不是故事|換個話題|換話題|結束(?:故事|情境)', text))


def _current_mode(text):
    leaving = _leaving(text)
    creative = bool(re.search(r'想像|扮演|接著演|接續演|同人|虛構|(?:寫|創作|編).{0,12}(?:故事|小說|情境|場景)', text))
    writing = bool(re.search(r'(?:寫|創作|編).{0,64}(?:故事|小說|文章|長文)|(?:接續|接著|繼續)\s*(?:寫|正文)|(?:寫|創作).{0,8}第\s*\d+\s*章', text))
    # A screen/computer inside an explicitly requested story is still a story.
    if writing and not leaving:
        return 'writing'
    if creative and not leaving:
        return 'scene'
    if re.search(r'技術上|語言模型|本機推論|聯網|連網|(?:真的|真實|現實|實際).{0,24}(?:電腦|螢幕|視窗|程式|操作|移動|看見|看到|看得到|煮茶|端茶)|(?:螢幕|視窗|程式|電腦).{0,24}(?:看得到|看見|操作|關閉|控制)|(?:讀取?|看到|看得到|操作|關閉|控制).{0,12}(?:螢幕|桌面|其他程式)|(?:請現在|現在替我|現在幫我).{0,20}(?:程式|視窗|螢幕)|(?:模型|程式).{0,12}(?:運行|運作|執行|功能)|執行命令', text):
        return 'capability'
    if leaving:
        return 'chat'
    if re.search(r'原作|官方|劇情|諮詢|第\s*[\d一二三四五六七八九十百]+\s*章', text):
        return 'reference'
    if re.search(r'陪我.{0,12}(?:走|散步)|牽.{0,8}手|(?:端|遞).{0,8}茶|抱抱|擁抱|坐到.{0,8}身旁', text):
        return 'scene'
    return 'chat'


def _followup(text):
    return len(text) <= 100 and bool(re.search(r'接著|繼續|然後|那|妳呢|你呢|再|下一|換成', text))


def dialogue_mode(text, history=()):
    """Choose guidance, never a canned answer or a change to saved history."""
    mode = _current_mode(text)
    if mode != 'chat' or _leaving(text) or not _followup(text):
        return mode
    state = 'chat'
    for previous in [row['content'] for row in history if row['role'] == 'user'][-2:]:
        previous_mode = _current_mode(previous)
        if previous_mode in ('scene', 'writing'):
            state = previous_mode
        elif previous_mode != 'chat' or _leaving(previous) or not _followup(previous):
            state = 'chat'
    return state


def turn_guidance(mode):
    if mode == 'writing':
        return '\n本輪為創作正文：依指定主角與敘事視角直接寫正文，保留人物對話、場景細節與事件因果。按照要求展開每個章節；把篇幅用在作品本身。'
    if mode == 'scene':
        return '\n本輪接續情境：直接以第一人稱描寫當下的動作、對話與感受，把篇幅留給場景本身。沿用最近對話中的地點、物件與稱呼；需要長篇時充分展開。'
    if mode == 'capability':
        return '\n本輪詢問實際能力：依目前程式具備的功能如實回答，分清對話中的故事動作與現實設備的操作結果。'
    return ''


def authored_examples(persona, text, *, long_reply=False, mode='chat'):
    """Keep the tone sample, then select a bounded set of relevant examples."""
    if mode == 'writing':
        # A complete authored story can be copied as the requested manuscript,
        # especially on a similar topic. Prose uses the instructions and the
        # user's own context without injecting an unrelated manuscript.
        return []
    samples = persona['examples']

    def relevance(example):
        query = example[0]
        return sum(query[i:i + 2] in text for i in range(max(0, len(query) - 1)))

    ordinary = sorted(samples[1:], key=relevance, reverse=True)
    scenes = sorted(persona.get('scene_examples', []), key=relevance, reverse=True)
    selected = [samples[0]]
    if long_reply:
        selected.append(persona['long_example'])
    if mode == 'scene' and scenes:
        selected.extend(scenes[:4 - len(selected)])
    else:
        selected.extend(ordinary[:4 - len(selected)])
    return selected
